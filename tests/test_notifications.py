import copy
import json
import smtplib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from backend import notifications as n
from backend.database import Store, Tick

NOW = datetime(2026, 7, 1, 14, 0, tzinfo=UTC)
HOUR, DAY = 3600, 86400


@pytest.fixture
def store(tmp_path):
    result = Store(f'sqlite:///{tmp_path}/alerts.db')
    result.initialize()
    return result


def tick(store, symbol, price, at, provider='test', ingested=None):
    with store.session() as session:
        session.add(Tick(symbol=symbol, price=price, timestamp=at, ingested_at=ingested or at, provider=provider, payload={}))
        session.commit()


def moves(store, window, **changes):
    """symbol=(before, after) observed `window` seconds apart, ending 10 s before NOW."""
    for symbol, (before, after) in changes.items():
        tick(store, symbol, before, NOW - timedelta(seconds=window + 30))
        tick(store, symbol, after, NOW - timedelta(seconds=10))


def settings(**values):
    return n.NotificationSettings(_env_file=None, **values)


def quote(price, fresh=True):
    return {'price': price, 'freshness_status': 'FRESH' if fresh else 'STALE', 'timestamp': NOW.isoformat()}


def test_window_changes_use_units_and_exclude_stale_stub_and_future_ticks(store):
    moves(store, HOUR, BTC=(100.0, 98.0), US10Y=(4.00, 4.08), DXY=(100.0, 100.5))
    tick(store, 'ETH', 10.0, NOW - timedelta(seconds=HOUR + 30), provider='massive-stub')
    tick(store, 'ETH', 11.0, NOW - timedelta(seconds=5), provider='massive-stub')
    tick(store, 'GOLD', 10.0, NOW - timedelta(seconds=HOUR + 30))
    tick(store, 'GOLD', 11.0, NOW - timedelta(seconds=600))  # observation too old
    tick(store, 'VIX', 10.0, NOW - timedelta(seconds=HOUR + 30))
    tick(store, 'VIX', 20.0, NOW - timedelta(seconds=5), ingested=NOW + timedelta(seconds=5))  # not known yet
    changes = n.window_changes(store, ['BTC', 'US10Y', 'DXY', 'ETH', 'GOLD', 'VIX'], HOUR, NOW)
    assert set(changes) == {'BTC', 'US10Y', 'DXY'}
    assert changes['US10Y']['unit'] == 'bp' and changes['US10Y']['change'] == pytest.approx(8.0)
    assert changes['DXY']['change'] == pytest.approx(0.5) and changes['BTC']['change'] == pytest.approx(-2.0)


def test_level_break_is_high_priority_only_when_macro_drivers_align(store):
    moves(store, HOUR, US10Y=(4.00, 4.08), DXY=(100.0, 100.5))
    changes = n.window_changes(store, ['US10Y', 'DXY'], HOUR, NOW)
    alert = n.level_break_alert('BTC', quote(59_500), {'support': 60_000, 'resistance': 65_000}, changes, NOW)
    assert alert['kind'] == 'LEVEL_BREAK' and alert['priority'] == 'HIGH'
    assert 'US10Y +8.0bp' in alert['message'] and 'DXY +0.50%' in alert['message']
    assert alert['metrics']['aligned'] == 2
    missing = next(d for d in alert['metrics']['drivers'] if d['symbol'] == 'NASDAQ')
    assert missing['status'] == 'missing'  # never shown as a measured zero
    plain = n.level_break_alert('BTC', quote(59_500), {'support': 60_000}, {}, NOW)
    assert plain['priority'] == 'MEDIUM' and 'without enough aligned' in plain['message']
    assert n.level_break_alert('BTC', quote(59_500, fresh=False), {'support': 60_000}, changes, NOW) is None
    assert n.level_break_alert('BTC', quote(61_000), {'support': 60_000, 'resistance': 65_000}, changes, NOW) is None
    # A bullish break is not aligned with bearish macro pressure.
    up = n.level_break_alert('BTC', quote(66_000), {'support': 60_000, 'resistance': 65_000}, changes, NOW)
    assert up['priority'] == 'MEDIUM'


def test_divergence_flags_relative_strength_against_driver_pressure(store):
    moves(store, DAY, BRENT=(80.0, 83.2), US10Y=(4.00, 4.06), DXY=(100.0, 100.4), BTC=(60_000, 60_720))
    changes = n.window_changes(store, ['BRENT', 'WTI', 'US10Y', 'DXY', 'BTC'], DAY, NOW)
    [alert] = n.divergence_alerts(changes, NOW)
    assert alert['kind'] == 'DIVERGENCE' and alert['priority'] == 'HIGH' and alert['symbol'] == 'BTC'
    assert 'relative strength' in alert['message'] and 'BRENT +4.00%' in alert['message'] and 'BTC +1.20%' in alert['message']
    falling = dict(changes, BTC=dict(changes['BTC'], change=-1.2))
    assert n.divergence_alerts(falling, NOW) == []
    assert n.divergence_alerts({k: v for k, v in changes.items() if k != 'BTC'}, NOW) == []


def test_market_alerts_combine_level_breaks_and_divergences(store):
    moves(store, HOUR, US10Y=(4.00, 4.08), DXY=(100.0, 100.5))
    assets = [{'symbol': 'BTC', 'quote': quote(59_500), 'decision': {'levels': {'support': 60_000, 'resistance': 65_000}}},
              {'symbol': 'ETH', 'quote': None, 'decision': None}]
    alerts = n.market_alerts(store, assets, NOW)
    assert [(a['kind'], a['priority']) for a in alerts] == [('LEVEL_BREAK', 'HIGH')]


def test_builders_for_setup_anomaly_event_release_and_provider():
    decision = {'id': 'd1', 'symbol': 'BTC', 'direction': 'LONG', 'score': 35, 'audit_status': 'PASS',
                'confirmation': 'Break resistance'}
    setup = n.setup_alert(decision, NOW)
    assert setup['priority'] == 'MEDIUM' and setup['decision_id'] == 'd1'
    assert n.setup_alert(dict(decision, direction='STRONG LONG'), NOW)['priority'] == 'HIGH'
    assert n.setup_alert(dict(decision, direction='WAIT'), NOW) is None
    assert n.setup_alert(dict(decision, audit_status='BLOCK'), NOW) is None
    assert n.setup_alert({k: v for k, v in decision.items() if k != 'audit_status'}, NOW) is None  # unaudited
    anomaly = {'symbol': 'BTC', 'change': 2.4, 'unit': 'pct', 'threshold': 1.0, 'window_seconds': 300, 'triggered': True}
    assert n.anomaly_alert('BTC', anomaly, NOW)['priority'] == 'HIGH'
    legacy = n.anomaly_alert('BTC', {'change_pct': 1.2, 'threshold_pct': 1.0, 'triggered': True}, NOW)
    assert legacy['priority'] == 'MEDIUM' and legacy['message'].startswith('+1.20%')
    event = {'id': 'e1', 'summary_fact': 'Fed cut rates', 'event_type': 'CENTRAL_BANK', 'verification_status': 'CONFIRMED',
             'importance': 0.9, 'fact_confidence': 98, 'assets': ['US2Y', 'DXY']}
    assert n.event_alert(event, NOW)['priority'] == 'HIGH'
    assert n.event_alert(dict(event, assets=[]), NOW) is None  # no identified asset impact: news, not an alert
    assert n.event_alert(dict(event, importance=0.2), NOW) is None
    assert n.event_alert({k: v for k, v in event.items() if k != 'importance'}, NOW) is None
    assert n.event_alert(dict(event, verification_status='UNCONFIRMED'), NOW) is None
    release = {'id': 'cpi', 'name': 'US CPI', 'country': 'US', 'impact': 5, 'actual': 3.4, 'consensus': 3.1,
               'raw_surprise': 0.3, 'z_score': 2.0}
    made = n.macro_release_alert(release, NOW)
    assert made['priority'] == 'HIGH' and 'surprise +0.3' in made['message'] and 'z +2.00' in made['message']
    assert n.macro_release_alert(dict(release, actual=None), NOW) is None
    assert n.macro_release_alert(dict(release, impact=1), NOW) is None
    online = {'name': 'massive', 'status': 'ONLINE'}
    down = n.provider_alert(online, {'name': 'massive', 'status': 'OFFLINE', 'error': 'HTTPError: HTTP transport'}, NOW)
    assert down['priority'] == 'HIGH' and down['symbol'] is None
    assert n.provider_alert(None, online, NOW) is None
    assert n.provider_alert(online, online, NOW) is None
    assert n.provider_alert({'name': 'x', 'status': 'PENDING'}, {'name': 'x', 'status': 'NOT CONFIGURED'}, NOW) is None
    assert n.provider_alert({'name': 'massive', 'status': 'OFFLINE'}, online, NOW)['title'] == 'massive: recovered'


def test_alert_ids_are_stable_inside_the_cooldown_window():
    decision = {'id': 'd1', 'symbol': 'BTC', 'direction': 'LONG', 'score': 35, 'audit_status': 'PASS'}
    first, later = n.setup_alert(decision, NOW), n.setup_alert(dict(decision, id='d2'), NOW + timedelta(minutes=10))
    assert first['id'] == later['id'] and first['dedupe_key'] == 'SETUP:BTC:LONG'
    assert set(first) >= {'id', 'kind', 'priority', 'title', 'message', 'symbol', 'created_at', 'decision_id',
                          'event_id', 'metrics'}


class Telegram:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.responses.pop(0)
        return httpx.Response(status, json=body)


def alert(priority='HIGH', minutes=0):
    return n.make_alert('LEVEL_BREAK', priority, 'BTC: HIGH PRIORITY break of support', 'BTC broke support',
                        NOW + timedelta(minutes=minutes), f'support:{priority}', symbol='BTC')


@pytest.mark.asyncio
async def test_dispatcher_stores_in_app_dedupes_and_retries_telegram(store):
    telegram = Telegram([(429, {'ok': False, 'parameters': {'retry_after': 3}}), (200, {'ok': True})])
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
    channels = n.build_channels(store, settings(telegram_bot_token='123456:SECRET-TOKEN', telegram_chat_id='42'),
                                transport=httpx.MockTransport(telegram))
    dispatcher = n.Dispatcher(store, channels, sleep=sleep, clock=lambda: NOW)
    result = await dispatcher.dispatch(alert())
    status = {d['channel']: d['status'] for d in result['deliveries']}
    assert status == {'in_app': 'SENT', 'telegram': 'SENT', 'email': 'NOT_CONFIGURED', 'webpush': 'NOT_CONFIGURED'}
    assert sleeps == [3.0] and len(telegram.requests) == 2
    body = json.loads(telegram.requests[-1].content)
    assert body['chat_id'] == '42' and body['text'].startswith('[HIGH] BTC: HIGH PRIORITY')
    assert store.get('alerts', alert()['id'])['priority'] == 'HIGH'
    recorded = store.get('notification_deliveries', f"{alert()['id']}:telegram")
    assert recorded['attempts'] == 2 and 'SECRET' not in json.dumps(recorded)
    assert (await dispatcher.dispatch(alert(minutes=5)))['status'] == 'DUPLICATE'
    assert len(telegram.requests) == 2


@pytest.mark.asyncio
async def test_unconfigured_low_priority_and_rate_limited_channels_never_send(store, monkeypatch):
    def refuse(request):
        raise AssertionError('no network in tests')
    channels = n.build_channels(store, settings(), transport=httpx.MockTransport(refuse))
    result = await n.Dispatcher(store, channels).dispatch(alert())
    assert [d['status'] for d in result['deliveries']] == ['SENT', 'NOT_CONFIGURED', 'NOT_CONFIGURED', 'NOT_CONFIGURED']
    configured = n.build_channels(store, settings(telegram_bot_token='123456:TOKEN', telegram_chat_id='1'),
                                  transport=httpx.MockTransport(refuse))
    low = await n.Dispatcher(store, configured).dispatch(alert('MEDIUM', minutes=120))
    assert {d['channel']: d['status'] for d in low['deliveries']}['telegram'] == 'SKIPPED_PRIORITY'
    limited = n.Dispatcher(store, configured)
    original = n._config

    def capped():
        config = copy.deepcopy(original())  # _config() is the shared read-only view
        config['channels']['telegram']['max_per_hour'] = 0
        return config
    monkeypatch.setattr(n, '_config', capped)
    blocked = await limited.dispatch(alert(minutes=240))
    assert {d['channel']: d['status'] for d in blocked['deliveries']}['telegram'] == 'RATE_LIMITED'


@pytest.mark.asyncio
async def test_telegram_client_errors_fail_without_retry_or_token_leak(store):
    telegram = Telegram([(400, {'ok': False, 'description': 'chat not found'})])
    channel = n.TelegramChannel('123456:SECRET-TOKEN', '42', httpx.MockTransport(telegram))
    record = await n.Dispatcher(store, [channel])._deliver(channel, alert())
    assert record['status'] == 'FAILED' and record['attempts'] == 1 and record['error'] == 'HTTP 400'


class FakeSMTP:
    sent: list = []
    calls: list = []
    fail_auth = False

    def __init__(self, host, port, timeout):
        FakeSMTP.calls.append(('connect', host, port))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, context):
        FakeSMTP.calls.append(('starttls',))

    def login(self, user, password):
        if FakeSMTP.fail_auth:
            raise smtplib.SMTPAuthenticationError(535, b'bad credentials')
        FakeSMTP.calls.append(('login', user))

    def send_message(self, message):
        FakeSMTP.sent.append(message)


@pytest.mark.asyncio
async def test_email_channel_uses_starttls_login_and_fails_closed_on_auth(store):
    FakeSMTP.sent, FakeSMTP.calls, FakeSMTP.fail_auth = [], [], False
    config = settings(smtp_host='smtp.example.test', smtp_from='hub@example.test', smtp_to='a@example.test, b@example.test',
                      smtp_username='hub', smtp_password='smtp-password')
    channel = n.EmailChannel(config, smtp_factory=FakeSMTP)
    assert channel.configured and channel.status()['status'] == 'CONFIGURED'
    record = await n.Dispatcher(store, [channel])._deliver(channel, alert())
    assert record['status'] == 'SENT'
    message = FakeSMTP.sent[0]
    assert message['Subject'] == '[HIGH] BTC: HIGH PRIORITY break of support' and message['To'] == 'a@example.test, b@example.test'
    assert [c[0] for c in FakeSMTP.calls] == ['connect', 'starttls', 'login']
    FakeSMTP.fail_auth = True
    failed = await n.Dispatcher(store, [channel])._deliver(channel, alert())
    assert failed['status'] == 'FAILED' and failed['attempts'] == 1 and failed['error'] == 'SMTP authentication failed'
    assert not n.EmailChannel(settings(smtp_host='smtp.example.test')).configured


@pytest.mark.asyncio
async def test_email_login_is_never_sent_without_tls_to_a_remote_server(store):
    FakeSMTP.sent, FakeSMTP.calls, FakeSMTP.fail_auth = [], [], False
    base = dict(smtp_from='hub@example.test', smtp_to='a@example.test', smtp_username='hub', smtp_password='smtp-password',
                smtp_starttls=False, smtp_ssl=False)
    plain = n.EmailChannel(settings(smtp_host='smtp.example.test', **base), smtp_factory=FakeSMTP)
    with pytest.raises(n.DeliveryError) as info:
        await plain.send(alert())
    assert info.value.reason == n.PLAINTEXT_LOGIN and not info.value.retryable and 'smtp-password' not in str(info.value)
    assert FakeSMTP.calls == [] and FakeSMTP.sent == []  # refused before connecting: the password never left
    record = await n.Dispatcher(store, [plain])._deliver(plain, alert())
    assert record['status'] == 'FAILED' and record['attempts'] == 1 and record['error'] == n.PLAINTEXT_LOGIN
    for host in ('localhost', '127.0.0.1', '::1', '[::1]'):  # a server on this computer may use a plain connection
        await n.EmailChannel(settings(smtp_host=host, **base), smtp_factory=FakeSMTP).send(alert())
    assert [c[0] for c in FakeSMTP.calls] == ['connect', 'login'] * 4 and len(FakeSMTP.sent) == 4
    FakeSMTP.calls = []
    anonymous = dict(base, smtp_username='', smtp_password='')  # no login: nothing secret crosses the network
    await n.EmailChannel(settings(smtp_host='smtp.example.test', **anonymous), smtp_factory=FakeSMTP).send(alert())
    await n.EmailChannel(settings(smtp_host='smtp.example.test', **dict(base, smtp_ssl=True)), smtp_factory=FakeSMTP).send(alert())
    assert [c[0] for c in FakeSMTP.calls] == ['connect', 'connect', 'login']


@pytest.mark.asyncio
async def test_webpush_delivers_deactivates_expired_and_reports_missing_library(store):
    ok = n.register_push_subscription(store, {'endpoint': 'https://fcm.googleapis.com/fcm/send/a', 'keys': {'p256dh': 'p' * 20, 'auth': 'a' * 16}})
    gone = n.register_push_subscription(store, {'endpoint': 'https://updates.push.services.mozilla.com/wpush/v2/b', 'keys': {'p256dh': 'q' * 20, 'auth': 'b' * 16}})
    calls = []

    def sender(subscription, data, config, ttl):
        calls.append((subscription['endpoint'], json.loads(data)['priority'], ttl))
        return 201 if subscription['endpoint'].endswith('/a') else 410
    keys = settings(webpush_vapid_public_key='public-key', webpush_vapid_private_key='private-key',
                    webpush_subject='mailto:ops@example.test')
    channel = n.WebPushChannel(store, keys, sender=sender)
    record = await n.Dispatcher(store, [channel])._deliver(channel, alert())
    assert record['status'] == 'SENT' and len(calls) == 2
    assert store.get('push_subscriptions', gone)['active'] is False and store.get('push_subscriptions', ok)['active']
    assert n.WebPushChannel(store, settings()).status()['status'] == 'NOT CONFIGURED'
    missing = n.WebPushChannel(store, keys)
    missing.sender = None
    assert missing.status()['status'] == 'UNAVAILABLE' and not missing.configured
    with pytest.raises(ValueError):
        n.register_push_subscription(store, {'endpoint': 'http://insecure.example.test', 'keys': {}})


def test_push_subscriptions_accept_only_browser_push_services(store):
    keys = {'p256dh': 'p' * 20, 'auth': 'a' * 16}
    for endpoint in ('https://127.0.0.1:8000/api/admin/recalculate', 'https://internal.example.test/hook',
                     'https://fcm.googleapis.com.evil.test/x'):
        with pytest.raises(ValueError, match='push service'):  # the server would POST there: no arbitrary hosts
            n.register_push_subscription(store, {'endpoint': endpoint, 'keys': keys})
    assert n.register_push_subscription(store, {'endpoint': 'https://wns2-par02p.notify.windows.com/w/?token=x', 'keys': keys})


def test_alert_settings_expose_status_not_credentials(store):
    channels = n.build_channels(store, settings(telegram_bot_token='123456:SECRET-TOKEN', telegram_chat_id='42',
                                                smtp_password='smtp-password'))
    payload = n.alert_settings(channels)
    text = json.dumps(payload)
    assert 'SECRET' not in text and 'smtp-password' not in text
    assert {c['name']: c['status'] for c in payload['channels']} == {
        'in_app': 'ONLINE', 'telegram': 'CONFIGURED', 'email': 'NOT CONFIGURED', 'webpush': 'NOT CONFIGURED'}


def test_level_break_requires_a_volatility_scaled_margin():
    # A tick a few dollars through support is noise, not a break (0.1% of price or 0.25 ATR, whichever is larger).
    assert n.level_break_alert('BTC', quote(84_370), {'support': 84_375}, {}, NOW) is None
    assert n.level_break_alert('BTC', quote(84_200), {'support': 84_375, 'atr': 900}, {}, NOW) is None
    assert n.level_break_alert('BTC', quote(84_100), {'support': 84_375, 'atr': 900}, {}, NOW)['kind'] == 'LEVEL_BREAK'


@pytest.mark.parametrize('message,shown', [
    ('HTTPStatusError: HTTP 503', 'Server error (HTTP 503)'),
    ('HTTPStatusError: HTTP 404', 'Not found (HTTP 404)'),
    ('ConnectTimeout: timed out', 'Timed out'),
    ('RemoteProtocolError: peer closed connection', 'Network error'),
    ('ValueError: could not convert string to float', 'Invalid request or response'),
    ('Status ONLINE -> OFFLINE', 'Was online, now offline'),
    ('finnhub: NOT CONFIGURED', 'Not configured'),
    ('finnhub: circuit open', 'Finnhub (US equities): circuit open'),
    ('parser found no FOMC meetings (page layout changed?)', 'parser found no FOMC meetings (page layout changed?)'),
])
def test_legacy_provider_alert_causes_read_as_plain_text(message, shown):
    row = {'kind': 'PROVIDER', 'title': 'finnhub: offline', 'message': message, 'created_at': '2026-09-28T09:00:00+00:00',
           'metrics': {'provider': 'finnhub', 'status': 'OFFLINE', 'previous_status': 'ONLINE'}}
    alert = n.readable_alert(row)
    assert alert['message'] == shown and alert['title'] == 'Finnhub (US equities): offline'
    assert row['message'] == message and 'label' not in row['metrics']  # the stored row is untouched
