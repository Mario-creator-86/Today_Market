"""Regressions from the Playwright UI audit (backend owner BE1): the analysis-cycle status (C1), event importance (C14),
event list order and paging (C4, C5), FX rate decisions (C6), daily bars and cross-asset fallbacks (C7), gzip (C8),
date-only calendar rows, coverage source health and stub social links. No network: providers are built without keys
and never called; rows are written straight to a temporary database."""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_coverage_service import add_tick, copy_daily_bars, daily_payload
from test_technicals import daily_store as daily_store

from backend import decision
from backend import service as service_module
from backend.analytics import basket_confirmation, measure_reaction
from backend.config import Settings, get_settings, rules
from backend.database import Decision, Store, utcnow
from backend.main import create_app
from backend.schemas import DocumentInput, Quote
from backend.service import ANALYSIS_STATUS_KEY, HubService, importance_fields

HEADERS = {'X-Admin-Token': 'test-admin-token'}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for name in ('ALPACA_API_KEY_ID', 'ALPACA_API_SECRET_KEY'):
        monkeypatch.setenv(name, '')
    store = Store(f'sqlite:///{tmp_path}/audit.db')
    store.initialize()
    result = HubService(store, Settings(_env_file=None, enable_ingestion=False))
    result.initialize()
    return result


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/api.db')
    monkeypatch.setenv('ENABLE_INGESTION', 'false')
    monkeypatch.setenv('ADMIN_TOKEN', 'test-admin-token')
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def btc_quote(hub):
    hub.ingest_quote(Quote(symbol='BTC', price=100, timestamp=utcnow(), provider='binance', bid=99.9, ask=100.1))


def official(hub, title, published, source='fed', domain='federalreserve.gov'):
    slug = ''.join(c for c in title.lower() if c.isalnum())[:40]
    return hub.ingest_document(DocumentInput(source_id=source, title=title, content=f'{title}. Official text.',
                                             url=f'https://www.{domain}/newsevents/{slug}.htm', published_at=published),
                               trusted_transport=True)


# --- C1: the full analysis status (DASH-06, M1, SHELL-05) -------------------------------------------------------------

@pytest.mark.asyncio
async def test_last_analysis_is_the_last_successful_cycle_never_the_morning_brief(hub):
    btc_quote(hub)
    decisions = await hub.recalculate()
    analysis = hub.dashboard()['analysis']
    stamp = decisions[0]['created_at']
    assert analysis['last_success_at'] == stamp and analysis['last_attempt_at'] == stamp
    assert analysis['last_error_type'] is None and analysis['stale'] is False
    assert analysis['cadence_seconds'] == rules()['refresh']['cadences']['interpretation']['seconds']
    assert hub.dashboard()['last_analysis'] == stamp
    hub.publish_briefs(utcnow() + timedelta(hours=5))  # the morning-brief job: no analysis ran
    assert hub.dashboard()['last_analysis'] == stamp


@pytest.mark.asyncio
async def test_a_failed_cycle_is_recorded_and_marks_the_analysis_stale(hub, monkeypatch):
    btc_quote(hub)
    first = (await hub.recalculate())[0]['created_at']

    def broken(*args, **kwargs):
        raise TypeError('argument of type int is not a container')
    monkeypatch.setattr(decision, 'CycleContext', broken)
    with pytest.raises(TypeError):
        await hub.recalculate()
    analysis = hub.dashboard()['analysis']
    assert analysis['last_success_at'] == first and analysis['last_attempt_at'] > first
    assert analysis['last_error_type'] == 'TypeError' and analysis['stale'] is True


@pytest.mark.asyncio
async def test_every_asset_failing_is_a_failed_cycle_but_one_asset_is_not(hub, monkeypatch):
    btc_quote(hub)
    original = decision.analyse

    async def eth_fails(ctx, asset):
        if asset['symbol'] == 'ETH':
            raise KeyError('price')
        return await original(ctx, asset)
    monkeypatch.setattr(decision, 'analyse', eth_fails)
    published = await hub.recalculate()
    record = hub.store.get('asset_scores', ANALYSIS_STATUS_KEY)
    assert 'ETH' not in {d['symbol'] for d in published} and record['failed_symbols'] == ['ETH']
    assert record['last_error_type'] is None and record['last_success_at'] == published[0]['created_at']

    async def all_fail(ctx, asset):
        raise ValueError('bad input')
    monkeypatch.setattr(decision, 'analyse', all_fail)
    assert await hub.recalculate() == []
    analysis = hub.dashboard()['analysis']
    assert analysis['last_error_type'] == 'ValueError' and analysis['stale'] is True
    assert analysis['last_success_at'] == record['last_success_at']


def test_analysis_is_stale_after_two_cadences_without_success(hub):
    now = utcnow()
    cadence = rules()['refresh']['cadences']['interpretation']['seconds'] * rules()['ui']['analysis_stale_cadences']
    for age, stale in ((cadence - 60, False), (cadence + 60, True)):
        stamp = (now - timedelta(seconds=age)).isoformat()
        hub.store.put('asset_scores', ANALYSIS_STATUS_KEY, {'last_success_at': stamp, 'last_attempt_at': stamp,
                                                             'last_error_type': None})
        assert hub.analysis_status(now)['stale'] is stale


def test_before_any_recorded_cycle_the_newest_decision_is_the_last_success(hub):
    created = utcnow() - timedelta(hours=5)
    payload = {'id': 'old', 'symbol': 'BTC', 'direction': 'WAIT', 'category': 'WAIT', 'score': 0,
               'created_at': created.isoformat(), 'reasons': []}
    with hub.store.session() as session:
        session.add(Decision(id='old', symbol='BTC', direction='WAIT', score=0, created_at=created, payload=payload))
        session.commit()
    hub.store.put('asset_scores', 'latest-cycle', {'generated_at': utcnow().isoformat()})  # a brief, not an analysis
    board = hub.dashboard()
    assert board['last_analysis'] == created.isoformat() and board['analysis']['stale'] is True


@pytest.mark.asyncio
async def test_a_cycle_with_a_confirmed_event_and_stored_baskets_completes(hub):
    """Regression (M10): 'expected' is a count; a legacy basket without expected_symbols aborted every cycle."""
    btc_quote(hub)
    event = official(hub, 'Federal Reserve announces policy decision', utcnow())
    assert event['verification_status'] == 'CONFIRMED'
    rows = [measure_reaction(event['id'], 'US2Y', '1h', (4.0, utcnow()), (4.1, utcnow()), 1)]
    basket = basket_confirmation(event['id'], '1h', rows, {'US2Y': 1, 'DXY': 1, 'BTC': -1})
    legacy = {k: v for k, v in basket.items() if k != 'expected_symbols'}
    hub.store.put('event_relationships', f"basket:{event['id']}:1h", legacy)
    published = await hub.recalculate()
    assert len(published) == len(rules()['assets'])
    assert hub.dashboard()['analysis']['last_error_type'] is None


def test_admin_recalculate_failure_is_a_plain_message(client, monkeypatch):
    def broken(*args, **kwargs):
        raise TypeError('boom')
    monkeypatch.setattr(decision, 'CycleContext', broken)
    response = client.post('/api/admin/recalculate', headers=HEADERS)
    assert response.status_code == 500
    assert 'previous decisions stay published' in response.json()['detail'] and 'TypeError' not in response.text
    assert client.get('/api/dashboard').json()['analysis']['last_error_type'] == 'TypeError'


# --- C14: importance = tier weight x extractor importance (DASH-04, EV-25) --------------------------------------------

def test_importance_combines_the_tier_weight_and_the_extractor():
    assert importance_fields({'importance': 1.0}) == {'importance': None, 'importance_basis': 'not assessed', 'tier_weight': 1.0}
    combined = importance_fields({'importance': 0.9, 'extraction': {'potential_importance': 30}})
    assert combined == {'importance': 0.27, 'importance_basis': 'source tier x extractor importance', 'tier_weight': 0.9}
    stored = {'tier_weight': 0.5, 'importance': 0.35, 'extraction': {'potential_importance': 70}}
    assert importance_fields(stored)['importance'] == 0.35  # recomputing a stored value is stable
    assert importance_fields({'extraction': {'potential_importance': 70}}) == {}  # no tier weight: nothing guessed


def test_events_enriched_before_the_fix_read_their_real_importance(hub):
    """The old max(tier, tier x extractor) always returned the tier weight: 'Presidential Message ...' read 100/100."""
    event = official(hub, 'Presidential Message on National Hunting and Fishing Day', utcnow())
    from backend.database import Event
    with hub.store.session() as session:
        row = session.get(Event, event['id'])
        row.payload = dict(row.payload, importance=1.0, importance_basis='source tier x extractor importance',
                           extraction={'potential_importance': 5}, ai_enriched=True)
        session.commit()
    read = hub.event(event['id'])
    assert read['importance'] == 0.05 and read['tier_weight'] == 1.0
    assert read['importance_basis'] == 'source tier x extractor importance'
    assert next(e for e in hub.events() if e['id'] == event['id'])['importance'] == 0.05


@pytest.mark.asyncio
async def test_enrichment_stores_the_product_and_its_factors(hub, monkeypatch):
    event = official(hub, 'Services Producer Price Index released', utcnow(), source='bls', domain='bls.gov')
    extraction = {'who': [], 'what': 'SPPI', 'when': 'today', 'where': 'Japan', 'direct_claims': [], 'new_information': [],
                  'background_information': [], 'mentioned_assets': [], 'mentioned_entities': [],
                  'event_type': event['event_type'], 'potential_importance': 30}

    async def run(role, inputs, **kwargs):
        return (extraction, 'run-1') if role == 'extractor' else (None, None)
    monkeypatch.setattr(hub.ai, 'run_with_id', run)
    await hub.enrich_event(event['id'])
    from backend.database import Event
    with hub.store.session() as session:
        stored = session.get(Event, event['id']).payload
    assert stored['importance'] == 0.3 and stored['tier_weight'] == 1.0
    assert stored['importance_basis'] == 'source tier x extractor importance'


# --- C4 / C5: event and alert lists ------------------------------------------------------------------------------------

def test_events_are_cut_and_ordered_by_publication_not_ingestion(hub):
    now = utcnow()
    older = official(hub, 'Board approves bank merger application', now - timedelta(hours=3))
    newer = official(hub, 'Discount rate minutes released for August', now - timedelta(minutes=10))
    backfill = official(hub, 'Sanctions list update from June archive', now - timedelta(days=100))  # ingested last
    assert [e['id'] for e in hub.events(2)] == [newer['id'], older['id']]
    assert [e['id'] for e in hub.events(10)] == [newer['id'], older['id'], backfill['id']]


def test_events_and_alerts_accept_a_limit_up_to_500(client):
    hub = client.app.state.hub
    hub.store.put_many('alerts', {f'a{i}': {'kind': 'EVENT', 'priority': 'LOW', 'title': f'Alert {i}'} for i in range(130)})
    assert len(client.get('/api/alerts').json()) == 100
    assert len(client.get('/api/alerts', params={'limit': 500}).json()) == 130
    assert client.get('/api/alerts', params={'limit': 501}).status_code == 422
    assert client.get('/api/events', params={'limit': 500}).status_code == 200
    assert client.get('/api/events', params={'limit': 0}).status_code == 422


# --- C6: FX next rate decision (FX-1, FX-2, FX-4) ---------------------------------------------------------------------

def calendar_row(key, name, at, **extra):
    return key, dict({'id': key, 'name': name, 'country': 'US', 'category': 'CENTRAL_BANK', 'scheduled_at': at.isoformat(),
                      'impact': 9, 'time_known': True}, **extra)


def test_fx_next_meeting_is_each_banks_next_rate_decision(client):
    hub = client.app.state.hub
    now = utcnow()
    rows = dict([
        calendar_row('speech', 'Discussion - Vice Chair for Supervision', now + timedelta(hours=2), kind='SPEECH',
                     is_decision=False, bank='fed'),
        calendar_row('past', 'FOMC rate decision', now - timedelta(days=1), kind='DECISION', is_decision=True, bank='fed',
                     event_class='FOMC'),
        calendar_row('fomc', 'FOMC rate decision', now + timedelta(days=30), kind='DECISION', is_decision=True, bank='fed',
                     event_class='FOMC'),
        calendar_row('cancelled', 'ECB monetary policy decision', now + timedelta(days=10), kind='DECISION',
                     is_decision=True, bank='ecb', event_class='ECB', status='CANCELLED', country='EU'),
        calendar_row('ecb', 'ECB monetary policy decision', now + timedelta(days=31), kind='DECISION', is_decision=True,
                     bank='ecb', event_class='ECB', country='EU'),
        calendar_row('boj', 'BoJ monetary policy meeting', now + timedelta(days=20), kind='DECISION', is_decision=True,
                     event_class='BOJ', country='JP')])  # no bank field: matched by its event class
    hub.store.put_many('macro_calendar', rows)
    hub.store.put('central_bank_state', 'fed', {'bank': 'fed', 'bank_name': 'Fed', 'rate': 3.875,
                                                 'policy_rate_range': [3.75, 4.0],
                                                 'basis': 'FOMC target range midpoint (FRED DFEDTARL/DFEDTARU)'})
    hub.store.put('central_bank_state', 'ecb', {'bank': 'ecb', 'rate': 2.0, 'basis': 'ECB deposit facility rate'})
    fx = {r['pair']: r for r in client.get('/api/fx').json()}
    eurusd = fx['EURUSD']
    assert eurusd['next_meeting']['name'] == 'FOMC rate decision' and eurusd['next_meeting']['bank_name'] == 'Fed'
    assert [m['name'] for m in eurusd['next_meetings']] == ['ECB monetary policy decision', 'FOMC rate decision']
    assert eurusd['fed_rate_range'] == [3.75, 4.0] and eurusd['fed_rate_basis'].startswith('FOMC target range midpoint')
    assert eurusd['other_bank'] == 'ecb' and eurusd['other_bank_name'] == 'ECB'
    assert eurusd['other_rate_basis'] == 'ECB deposit facility rate'
    assert fx['USDJPY']['next_meeting']['name'] == 'BoJ monetary policy meeting'
    assert fx['DXY']['next_meeting']['bank'] == 'fed' and len(fx['DXY']['next_meetings']) == 1
    rba = next(m for m in fx['AUDUSD']['next_meetings'] if m['bank'] == 'rba')
    assert rba['scheduled_at'] is None and rba['reason'] == 'No rate-decision calendar source for the Reserve Bank of Australia'
    assert fx['AUDUSD']['next_meeting']['name'] == 'FOMC rate decision'  # the Fed's decision is still known
    hub.store.put_many('macro_calendar', {'fomc': dict(rows['fomc'], status='POSTPONED')})
    fed = next(m for m in client.get('/api/fx').json()[0]['next_meetings'] if m['bank'] == 'fed')
    assert fed['scheduled_at'] is None and 'No Fed rate decision in the calendar' in fed['reason']


# --- C7: daily bars and cross-asset fallback (ASSET-02, ASSET-04, ASSET-17) --------------------------------------------

def test_asset_detail_serves_completed_daily_sessions(hub, daily_store, monkeypatch):
    copy_daily_bars(daily_store[0], hub)
    monkeypatch.setattr(service_module, 'utcnow', lambda: datetime(2026, 9, 28, 14, tzinfo=UTC))
    detail = hub.asset('NVDA')
    daily = detail['daily_bars']
    assert detail['bars'] == [] and daily and len(daily) <= rules()['ui']['daily_bars_sessions']
    assert daily[-1]['time'] == '2026-09-25' and daily[-1]['close'] != 999  # Monday's forming bar is not a close yet
    assert set(daily[-1]) == {'time', 'open', 'high', 'low', 'close', 'volume'}
    assert [b['time'] for b in daily] == sorted(b['time'] for b in daily)
    assert detail['daily_bars_basis'] == 'Daily bars: Alpaca IEX'


def test_close_only_series_serve_daily_closes_and_feed_the_overlays(hub, monkeypatch):
    now = datetime(2026, 9, 28, 14, tzinfo=UTC)
    monkeypatch.setattr(service_module, 'utcnow', lambda: now)
    # Thursday's and Friday's 16:00 New York closes, then Friday's close republished later that day (the latest wins).
    for stamp, price in ((datetime(2026, 9, 24, 20, tzinfo=UTC), 4.10), (datetime(2026, 9, 25, 20, tzinfo=UTC), 4.12),
                         (datetime(2026, 9, 25, 21, tzinfo=UTC), 4.16)):
        add_tick(hub, 'US10Y', stamp, price, 'treasury', daily_payload('US10Y', stamp, price, 'treasury'))
    daily = hub.asset('US10Y')['daily_bars']
    assert [b['time'] for b in daily] == ['2026-09-24', '2026-09-25']  # one close per New York date (latest wins)
    assert daily[-1] == {'time': '2026-09-25', 'open': None, 'high': None, 'low': None, 'close': 4.16, 'volume': None}
    assert hub.asset('US10Y')['daily_bars_basis'] == 'Daily closes: U.S. Treasury'
    btc = hub.asset('BTC')
    assert btc['cross_asset']['US10Y'] == [{'time': b['time'], 'close': b['close']} for b in daily]
    assert btc['cross_asset_interval']['US10Y'] == 86400
    assert btc['cross_asset']['DXY'] == [] and btc['cross_asset_interval']['DXY'] == 60


def test_minute_bars_carry_one_time_field(hub):
    now = utcnow()
    for seconds, price in ((180, 100.0), (120, 101.0), (60, 102.0)):
        add_tick(hub, 'BTC', now - timedelta(seconds=seconds), price, 'binance', {'data_class': 'LIVE'})
    bars = hub.asset('BTC')['bars']
    assert len(bars) >= 2 and all('time' in b and 'timestamp' not in b for b in bars)


# --- C8: gzip ----------------------------------------------------------------------------------------------------------

def test_large_responses_are_gzip_compressed(client):
    response = client.get('/api/dashboard', headers={'Accept-Encoding': 'gzip'})
    assert response.status_code == 200 and response.headers.get('content-encoding') == 'gzip'
    assert response.json()['assets']
    assert client.get('/health', headers={'Accept-Encoding': 'gzip'}).headers.get('content-encoding') is None


# --- calendar, coverage, social ---------------------------------------------------------------------------------------

def test_date_only_rows_stay_through_their_day(hub):
    anchor = datetime(2026, 10, 6, 4, tzinfo=UTC)  # 00:00 New York: '3-Year Note auction', no time published
    rows = [{'id': 'auction', 'scheduled_at': anchor.isoformat(), 'time_known': False},
            {'id': 'timed', 'scheduled_at': anchor.isoformat(), 'time_known': True},
            {'id': 'yesterday', 'scheduled_at': (anchor - timedelta(days=1)).isoformat(), 'time_known': False}]
    kept = [r['id'] for r in HubService.calendar_window(rows, anchor + timedelta(hours=10))]
    assert kept == ['auction']  # the timed row left 6 h after its time; yesterday's date-only row has ended


def test_coverage_separates_level_and_proxy_sources_with_their_health(hub):
    massive = hub.providers.get('massive')
    assert massive is not None
    massive.health.update(status='AUTH_ERROR', error='Authentication rejected (HTTP 403)')
    massive.configured = True  # as with a saved key
    gold = next(a for a in hub.assets(event_overlay=False) if a['symbol'] == 'GOLD')['coverage']
    assert 'massive' in gold['level_providers'] and 'massive' not in gold['proxy_providers']
    assert set(gold['proxy_providers']) <= {'binance', 'coinbase', 'alpaca'} and gold['proxy_providers']
    level = next(s for s in gold['sources'] if s['name'] == 'massive')
    assert {k: v for k, v in level.items() if k != 'label'} == {
        'name': 'massive', 'role': 'level', 'state': 'READY', 'health': 'AUTH_ERROR',
        'error': 'Authentication rejected (HTTP 403)'}
    assert {s['role'] for s in gold['sources'] if s['name'] in {'binance', 'coinbase'}} == {'proxy'}


def test_stub_posts_never_expose_a_link(client):
    hub = client.app.state.hub
    hub.store.put('social_posts', 'stub-1', {'text': 'Synthetic development post', 'author_username': 'federalreserve',
                                             'stub': True, 'url': 'https://x.com/federalreserve/status/stub-1'})
    post = client.get('/api/social').json()[0]
    assert post['stub'] is True and post['url'] is None


def test_historical_probability_names_what_n_counts(hub):
    from backend.calibration import historical_probability
    result = historical_probability(hub.store, 'BTC', 40, None, '4h', utcnow())
    assert result['status'] == 'NOT ENOUGH DATA' and result['n_basis'].startswith('past decisions for this asset')


def test_a_switched_off_provider_keeps_its_display_label_and_category():
    from backend.service import disabled_health
    row = disabled_health({'name': 'finnhub', 'label': 'Finnhub (equities)', 'category': 'Market data',
                           'status': 'RATE_LIMITED', 'error': 'Rate limited (HTTP 429)', 'error_type': 'HTTPStatusError',
                           'latency_ms': 120.0, 'last_success': '2026-09-27T08:00:00+00:00'})
    assert row == {'name': 'finnhub', 'label': 'Finnhub (equities)', 'category': 'Market data', 'status': 'DISABLED',
                   'latency_ms': None, 'last_success': '2026-09-27T08:00:00+00:00', 'error': None, 'error_type': None}
    bare = disabled_health({'name': 'x', 'status': 'ONLINE'})  # an adapter without descriptive fields: one shape
    assert bare['label'] is None and bare['category'] is None and bare['status'] == 'DISABLED'


# --- round 2 ----------------------------------------------------------------------------------------------------------

def shift_clock(monkeypatch, seconds):
    """Moves utcnow forward in every backend module that imported it (the dashboard reads the clock in several)."""
    import sys

    from backend import database
    real = database.utcnow
    moved = datetime.now(UTC) + timedelta(seconds=seconds)
    for name, module in list(sys.modules.items()):
        if name.startswith('backend') and getattr(module, 'utcnow', None) is real:
            monkeypatch.setattr(module, 'utcnow', lambda: moved)


@pytest.mark.asyncio
async def test_an_unchanged_dashboard_is_not_pushed_again(hub, monkeypatch):
    """Regression (C9 review): event_risk.generated_at and each provider poll's latency / last success were restamped on
    every build, so every 5 s build counted as changed and was broadcast."""
    from backend import streams
    now = utcnow()
    btc_quote(hub)
    official(hub, 'Federal Reserve announces policy decision', now - timedelta(minutes=5))
    hub.store.put_many('macro_calendar', dict([calendar_row('cpi', 'CPI', now + timedelta(hours=3), category='MACRO',
                                                            event_class='US_CPI', kind='RELEASE')]))
    fanout = streams.Fanout(streams.MemoryBus(), snapshot=hub.dashboard)
    queue = fanout.subscribe()
    await fanout._snapshot_round(True)
    first = queue.get_nowait()['data']
    shift_clock(monkeypatch, 7)
    provider = next(iter(hub.providers.values()))
    provider.health.update(latency_ms=321.0, last_success=service_module.utcnow().isoformat())  # a routine poll
    second = hub.dashboard()
    assert second['as_of'] != first['as_of'] and second['event_risk']['generated_at'] != first['event_risk']['generated_at']
    assert streams.fingerprint(second) == streams.fingerprint(first)
    await fanout._snapshot_round(True)
    assert queue.empty()  # nothing changed: no frame
    hub.ingest_quote(Quote(symbol='BTC', price=101, timestamp=service_module.utcnow(), provider='binance', bid=100.9,
                           ask=101.1))
    await fanout._snapshot_round(True)
    pushed = queue.get_nowait()  # a real change is pushed: a price move as the quote alone (contract C15)
    assert pushed['type'] == 'asset_quotes' and list(pushed['data']['quotes']) == ['BTC'] and queue.empty()
    assert pushed['data']['quotes']['BTC']['price'] == 101 and pushed['data']['as_of'] == fanout.latest_snapshot['as_of']
    moved = fanout.latest_snapshot
    assert pushed['data']['quotes']['BTC'] == next(a for a in moved['assets'] if a['symbol'] == 'BTC')['quote']


def weekday_closes(hub, symbol, provider, first_day, last_day):
    """Official 16:00 New York closes (20:00 UTC) on the weekdays of September 2026 between the two days."""
    for day in range(first_day, last_day + 1):
        stamp = datetime(2026, 9, day, 20, tzinfo=UTC)
        if stamp.weekday() < 5:
            add_tick(hub, symbol, stamp, 4.0 + day / 100, provider, daily_payload(symbol, stamp, 4.0 + day / 100, provider))


def test_close_only_series_serve_daily_closes_on_any_weekday(hub, monkeypatch):
    """Regression (C7 review): Tuesday to Friday a close-only series has two closes within the 48 h minute window, which
    became two fake 'minute' bars and a 2-point overlay instead of the daily closes."""
    now = datetime(2026, 9, 30, 14, tzinfo=UTC)  # a Wednesday: Monday's and Tuesday's closes are within 48 h
    monkeypatch.setattr(service_module, 'utcnow', lambda: now)
    weekday_closes(hub, 'US10Y', 'treasury', 14, 29)
    for minutes in range(1, 200):
        add_tick(hub, 'BTC', now - timedelta(minutes=minutes), 100 + minutes, 'binance', {'data_class': 'LIVE'})
    us10y = hub.asset('US10Y')
    assert us10y['bars'] == [] and len(us10y['daily_bars']) == 12  # never official closes drawn as minute bars
    assert us10y['daily_bars_basis'] == 'Daily closes: U.S. Treasury'
    btc = hub.asset('BTC')
    assert btc['cross_asset_interval']['US10Y'] == 86400
    assert btc['cross_asset']['US10Y'] == [{'time': b['time'], 'close': b['close']} for b in us10y['daily_bars']]
    assert len(btc['bars']) == 199


def test_minute_bars_and_levels_leave_out_daily_closes(hub, monkeypatch):
    now = datetime(2026, 9, 30, 14, tzinfo=UTC)
    monkeypatch.setattr(service_module, 'utcnow', lambda: now)
    stamp = datetime(2026, 9, 29, 19, 30, tzinfo=UTC)  # an EIA spot close among live prints
    add_tick(hub, 'WTI', stamp, 70.0, 'eia', daily_payload('WTI', stamp, 70.0, 'eia'))
    for minutes in range(1, 30):
        add_tick(hub, 'WTI', now - timedelta(minutes=minutes), 72 + minutes / 100, 'massive', {'data_class': 'LIVE'})
    detail = hub.asset('WTI')
    assert len(detail['bars']) == 29 and all(b['close'] != 70.0 for b in detail['bars'])
    assert len(hub.bars('WTI', live_only=True)) == 29 and len(hub.bars('WTI')) == 30
    assert detail['technical_levels']['observations'] == 29  # all live bars completed; daily close excluded
    assert detail['technical_levels']['session']['low'] > 72  # EIA's daily spot close cannot become intraday support
    assert decision.CycleContext(hub, now).levels['WTI'] == detail['technical_levels']


def hourly_bar(symbol, start, close, provider='binance'):
    from backend.database import Bar
    return Bar(symbol=symbol, interval_seconds=3600, start=start, open=close, high=close + 1, low=close - 1, close=close,
               volume=1.0, ticks=10, provider=provider)


def test_crypto_daily_closes_come_from_stored_hourly_bars(hub, monkeypatch):
    """Regression (C7 review): BTC and ETH had no daily series, so a daily overlay had nothing to be compared with."""
    now = datetime(2026, 9, 30, 14, tzinfo=UTC)
    monkeypatch.setattr(service_module, 'utcnow', lambda: now)
    start = datetime(2026, 9, 27, 5, tzinfo=UTC)  # the app started on the 27th at 05:00 UTC
    with hub.store.session() as session:
        hour = 0
        while start + timedelta(hours=hour) < now:
            session.add(hourly_bar('BTC', start + timedelta(hours=hour), 100.0 + hour))
            hour += 1
        session.commit()
    weekday_closes(hub, 'US10Y', 'treasury', 14, 29)
    btc = hub.asset('BTC')
    last_hour = {day: 100.0 + (datetime(2026, 9, day, 23, tzinfo=UTC) - start).total_seconds() / 3600 for day in (27, 28, 29)}
    assert btc['daily_bars'] == [{'time': f'2026-09-{day}', 'open': None, 'high': None, 'low': None, 'close': close,
                                  'volume': None} for day, close in last_hour.items()]  # last hourly close of each UTC day
    from backend.providers.base import provider_label
    assert btc['daily_bars_basis'] == f"Daily closes from hourly bars: {provider_label('binance')}"
    assert btc['cross_asset_interval']['US10Y'] == 86400 and btc['cross_asset']['US10Y']
    assert hub.asset('US10Y')['daily_bars_basis'] == 'Daily closes: U.S. Treasury'  # official closes come first
    assert hub.hourly_closes('SP500', 'index', now, 90) == []  # a session venue has official closes or daily bars


def test_analysis_staleness_counts_from_the_end_of_the_last_successful_cycle(hub):
    """Regression (C1 review): measured from the cycle start, a slow cycle (AI explanations and audits) flipped the
    header to stale although every cycle succeeded."""
    now = utcnow()
    limit = rules()['refresh']['cadences']['interpretation']['seconds'] * rules()['ui']['analysis_stale_cadences']

    def stale(started, finished, running=None):
        hub.store.put('asset_scores', ANALYSIS_STATUS_KEY, {
            'last_success_at': (now - timedelta(seconds=started)).isoformat(), 'last_attempt_at': None,
            'last_success_completed_at': (now - timedelta(seconds=finished)).isoformat() if finished is not None else None,
            'last_error_type': None, 'running_since': (now - timedelta(seconds=running)).isoformat() if running else None})
        return hub.analysis_status(now)['stale']
    assert stale(limit + 200, limit - 100) is False  # started long ago, finished recently
    assert stale(limit + 200, limit + 100) is True
    assert stale(limit + 200, None) is True  # a record from before completion times: the start counts
    assert stale(limit + 400, limit + 50, running=100) is False  # the next cycle started on time and is running
    assert stale(limit * 3, limit * 2 + 60, running=10) is True  # started long after the last success (app stopped)
    assert stale(limit * 2, limit * 2 - 100, running=limit + 30) is True  # a cycle hanging past the limit


@pytest.mark.asyncio
async def test_a_cycle_records_when_it_runs_and_when_it_finished(hub, monkeypatch):
    btc_quote(hub)
    original, seen = decision.analyse, []

    async def watch(ctx, asset):
        seen.append(hub.store.get('asset_scores', ANALYSIS_STATUS_KEY)['running_since'])
        return await original(ctx, asset)
    monkeypatch.setattr(decision, 'analyse', watch)
    published = await hub.recalculate()
    record = hub.store.get('asset_scores', ANALYSIS_STATUS_KEY)
    assert seen[0] == published[0]['created_at'] and record['running_since'] is None
    assert record['last_success_completed_at'] >= record['last_success_at'] == published[0]['created_at']


@pytest.mark.asyncio
async def test_the_dashboard_names_the_assets_the_last_cycle_could_not_recalculate(hub, monkeypatch):
    btc_quote(hub)
    original = decision.analyse

    async def eth_fails(ctx, asset):
        if asset['symbol'] == 'ETH':
            raise KeyError('price')
        return await original(ctx, asset)
    monkeypatch.setattr(decision, 'analyse', eth_fails)
    await hub.recalculate()
    analysis = hub.dashboard()['analysis']
    assert analysis['failed_symbols'] == ['ETH'] and analysis['stale'] is False and analysis['last_error_type'] is None
    monkeypatch.setattr(decision, 'analyse', original)
    await hub.recalculate()
    assert hub.dashboard()['analysis']['failed_symbols'] == []


def test_a_document_import_survives_a_failing_analysis(client, monkeypatch):
    """Regression (DASH-06 review): the import was stored and audited, then answered a bare 500."""
    def broken(*args, **kwargs):
        raise TypeError('boom')
    monkeypatch.setattr(decision, 'CycleContext', broken)
    document = {'source_id': 'fed', 'title': 'Federal Reserve Board announces approval of application',
                'content': 'Federal Reserve Board announces approval of application. Official text.',
                'url': 'https://www.federalreserve.gov/newsevents/pressreleases/orders20260928a.htm',
                'published_at': utcnow().isoformat()}
    response = client.post('/api/admin/documents', json=document, headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body['id'] and 'imported' in body['analysis_error'] and 'TypeError' not in response.text
    assert client.app.state.hub.event(body['id'])['id'] == body['id']
    assert client.get('/api/dashboard').json()['analysis']['last_error_type'] == 'TypeError'


def test_the_dashboard_counts_every_stored_alert(client):
    hub = client.app.state.hub
    hub.store.put_many('alerts', {f'a{i}': {'kind': 'EVENT', 'priority': 'LOW', 'title': f'Alert {i}'} for i in range(45)})
    board = client.get('/api/dashboard').json()
    assert len(board['alerts']) == rules()['ui']['dashboard_alerts'] == 30 and board['alerts_total'] == 45


def test_date_only_calendar_rows_carry_their_release_window(client):
    hub = client.app.state.hub
    anchor = utcnow().replace(hour=4, minute=0, second=0, microsecond=0) + timedelta(days=2)
    hub.store.put_many('macro_calendar', {
        'nvda': {'id': 'nvda', 'name': 'NVIDIA earnings', 'country': 'US', 'category': 'EARNINGS', 'event_class': 'EARNINGS',
                 'scheduled_at': anchor.isoformat(), 'time_known': False, 'session': 'amc', 'impact': 7},
        'cpi': {'id': 'cpi', 'name': 'CPI', 'country': 'US', 'category': 'MACRO', 'scheduled_at': anchor.isoformat(),
                'time_known': True, 'impact': 9}})
    rows = {r['id']: r for r in client.get('/api/calendar').json()}
    window = rows['nvda']['date_window']
    assert window['label'] == 'after the US close' and window['zone'] == 'America/New_York'
    span = datetime.fromisoformat(window['end_utc']) - datetime.fromisoformat(window['start_utc'])
    assert span == timedelta(hours=4) and window['start_utc'].endswith('+00:00')  # 16:00-20:00 New York, in UTC
    assert 'date_window' not in rows['cpi']


def test_n_basis_names_the_count_actually_used(hub):
    from backend import calibration
    from backend import evaluation as ev
    bucket = ev.score_bucket(45)
    hub.store.put('calibration_models', ev.COMPARABLES_KEY,
                  {'counts': {f'BTC|4h|{bucket}|RISK_ON': {'n': 3}, f'BTC|4h|{bucket}|*': {'n': 9}}})
    same = calibration.historical_probability(hub.store, 'BTC', 45, 'RISK_ON', '4h', utcnow())
    assert same['n'] == 3 and same['n_basis'] == calibration.N_BASIS
    other = calibration.historical_probability(hub.store, 'BTC', 45, 'RISK_OFF', '4h', utcnow())
    assert other['n'] == 9 and other['n_basis'].endswith('in the same score bucket (any regime)')


def test_the_fx_block_names_central_banks_not_internal_ids():
    assert decision.fx_policy_block('EURUSD', 'fx', {}) == 'Relative central-bank comparison unavailable (ECB vs Fed)'
    assert decision.fx_policy_block('USDJPY', 'fx', {}) == 'Relative central-bank comparison unavailable (Fed vs BoJ)'
    assert decision.fx_policy_block('SP500', 'index', {}) is None


def test_performance_reports_directional_observations_by_name(hub):
    from backend.evaluation import performance
    report = performance(hub.store)
    assert report['directional_observations'] == report['observations'] == 0
    assert report['decisions_recorded'] == 0 and report['status'] == 'INSUFFICIENT_DATA'


def test_session_venues_have_plain_labels():
    from backend.sessions import session_info
    now = datetime(2026, 9, 28, 14, tzinfo=UTC)
    dxy = session_info('index', now, 'DXY')
    assert dxy['venue'] == 'ICE_US' and dxy['venue_label'] == 'ICE US futures'
    assert 'exchange_calendars' not in dxy['basis'] and 'IEPA' not in dxy['basis']
    assert session_info('bond', now, 'US10Y')['venue_label'] == 'US Treasury (CBOT futures hours)'
    for venue in rules()['market_sessions']['venues'].values():
        assert venue['label'] and '_' not in venue['label'] and 'exchange_calendars' not in venue['basis']
    assert session_info('unknown', now)['venue_label'] is None


# --- C14 follow-up: importance thresholds ------------------------------------------------------------------------------

def fomc_statement(importance_payload, status='CONFIRMED'):
    now = utcnow()
    event = {'id': 'fomc', 'summary_fact': 'FOMC statement: policy rate held, guidance turns hawkish',
             'event_type': 'CENTRAL_BANK', 'trigger': 'FED_HAWKISH', 'verification_status': status, 'fact_confidence': 95,
             'first_seen_utc': (now - timedelta(minutes=30)).isoformat(), 'event_time': (now - timedelta(minutes=31)).isoformat(),
             'last_updated_utc': now.isoformat(), 'sources': ['fed'], 'evidence': [], 'topic': 'FED_POLICY'}
    return dict(event, **importance_fields(importance_payload))


def threshold_outcomes(event):
    """What every importance threshold consumer decides for one event."""
    from backend import analytics, briefs, notifications, refresh, scoring
    now = utcnow()
    quote = {'freshness_status': 'FRESH', 'market_status': 'LIVE', 'provider': 'binance'}
    unverified = dict(event, verification_status='UNCONFIRMED')
    gates = scoring.decision_gates('US2Y', now, quote=quote, events=[unverified], event={}, provider_health={})
    alert = notifications.event_alert(event, now)
    # A news item of no breaking type from no central-bank source: only its importance can make it a trigger.
    news = dict(event, event_type='NEWS', sources=[])
    triggers = refresh.detect_triggers(refresh.trigger_state([], []), refresh.trigger_state([news], []))
    return {'blocks_setups_while_unverified': any(g.startswith('Important fact unverified') for g in gates),
            'reaction_measured': analytics._eligible(event, now), 'alert_priority': alert['priority'] if alert else None,
            'breaking_trigger': [t['trigger'] for t in triggers], 'major_in_brief': briefs.is_major(event),
            'urgent_research': event['importance'] >= rules()['refresh']['hot_topics']['urgent_min_importance']}


@pytest.mark.parametrize('extractor', [80, 95])
def test_an_official_statement_rated_important_passes_every_importance_threshold_as_before(extractor):
    """Decision (C14): importance = tier x extractor / 100 is kept. An official central-bank statement (tier 1.0) that
    the extractor rates >= 80 must pass every threshold it passed when importance was the tier weight alone."""
    before = fomc_statement({'tier_weight': 1.0, 'extraction': {'potential_importance': 100}})
    after = fomc_statement({'tier_weight': 1.0, 'extraction': {'potential_importance': extractor}})
    assert before['importance_basis'] == 'source tier x extractor importance' and after['importance'] == extractor / 100
    assert threshold_outcomes(after) == threshold_outcomes(before)
    outcome = threshold_outcomes(after)
    assert outcome['blocks_setups_while_unverified'] and outcome['urgent_research'] and outcome['breaking_trigger']


def test_a_minor_official_release_no_longer_counts_as_important():
    """The intended loosening (docs/DECISION_ENGINE.md §5): a tier-1 item the extractor rates 20 reads 0.2, below the
    unverified-fact gate (0.5), the reaction floor (0.3), event alerts (0.5) and the breaking trigger (0.6)."""
    minor = threshold_outcomes(fomc_statement({'tier_weight': 1.0, 'extraction': {'potential_importance': 20}}))
    assert minor == {'blocks_setups_while_unverified': False, 'reaction_measured': False, 'alert_priority': None,
                     'breaking_trigger': [], 'major_in_brief': False, 'urgent_research': False}


@pytest.mark.asyncio
async def test_hot_topic_research_priority_reads_its_threshold_from_config(hub, monkeypatch):
    from backend.runtime import Runtime
    runtime = Runtime(hub)
    events = [dict(fomc_statement({'tier_weight': 1.0, 'extraction': {'potential_importance': p}}, status='UNCONFIRMED'),
                   id=f'e{p}') for p in (85, 60)]
    monkeypatch.setattr(hub, 'events', lambda limit=100: events)
    calls = []

    async def corroborate(event, priority=1):
        calls.append((event['id'], priority))
        return True
    monkeypatch.setattr(runtime, 'corroborate', corroborate)
    await runtime.research_hot_topics()
    assert calls == [('e85', 0), ('e60', 1)]


# --- asset page bars: a short display reuse, never for decisions --------------------------------------------------------

def test_asset_page_reuses_its_minute_bars_briefly_but_decisions_read_fresh(hub, monkeypatch):
    reads = []
    real = service_module.technicals.ticks
    monkeypatch.setattr(service_module.technicals, 'ticks', lambda *a, **k: reads.append(a[1]) or real(*a, **k))
    now = datetime.now(UTC)
    hub.page_bars('BTC', now)
    hub.page_bars('BTC', now + timedelta(seconds=5))
    assert reads == ['BTC']  # reused within ui.asset_page_bars_cache_seconds
    hub.page_bars('BTC', now + timedelta(seconds=rules()['ui']['asset_page_bars_cache_seconds']))
    assert reads == ['BTC', 'BTC']  # expired
    hub.bars('BTC')
    assert reads == ['BTC', 'BTC', 'BTC']  # the decision cycle's reader never uses the page cache


# --- round 3: legacy alerts (C16), source registry (C17), event cut, crypto daily closes, labels ------------------------

def legacy_provider_alert(key, provider, status, previous, message, at):
    """A PROVIDER alert as stored before provider_alert wrote labels (raw id and exception name, no metrics.label)."""
    words = {'ONLINE': 'recovered', 'AUTH_ERROR': 'auth error', 'OFFLINE': 'offline', 'RATE_LIMITED': 'rate limited',
             'DEGRADED': 'degraded'}[status]
    return key, {'kind': 'PROVIDER', 'priority': 'HIGH' if status in {'OFFLINE', 'AUTH_ERROR'} else 'LOW',
                 'title': f'{provider}: {words}', 'message': message, 'symbol': None, 'created_at': at.isoformat(),
                 'metrics': {'provider': provider, 'previous_status': previous, 'status': status,
                             'error_type': 'HTTPStatusError' if 'HTTP' in message else None}}


def test_legacy_provider_alerts_read_with_labels_and_plain_causes(client):
    """Regression (UI audit DASH-16 / ALR-4, contract C16): alerts stored before round 1 still showed 'whitehouse: auth
    error / HTTPStatusError: HTTP 403' and 'finnhub: recovered / Status RATE_LIMITED -> ONLINE'."""
    from backend.providers.base import provider_label
    hub = client.app.state.hub
    now = utcnow()
    hub.store.put_many('alerts', dict([
        legacy_provider_alert('w1', 'whitehouse', 'AUTH_ERROR', 'ONLINE', 'HTTPStatusError: HTTP 403', now - timedelta(hours=2)),
        legacy_provider_alert('f1', 'finnhub', 'RATE_LIMITED', 'ONLINE', 'HTTPStatusError: HTTP 429', now - timedelta(hours=1)),
        legacy_provider_alert('f2', 'finnhub', 'ONLINE', 'RATE_LIMITED', 'Status RATE_LIMITED -> ONLINE',
                              now - timedelta(minutes=50)),
        legacy_provider_alert('d1', 'deribit', 'OFFLINE', 'ONLINE', 'ConnectError', now - timedelta(minutes=40)),
        legacy_provider_alert('g1', 'gdelt_events', 'DEGRADED', 'ONLINE', 'ReadTimeout: timed out', now - timedelta(minutes=30))]))
    hub.providers['deribit'].health['status'] = 'ONLINE'  # back online now, without a recovery alert
    hub.providers['whitehouse'].health['status'] = 'AUTH_ERROR'
    listed = {a['id']: a for a in client.get('/api/alerts').json()}
    board = {a['id']: a for a in client.get('/api/dashboard').json()['alerts']}
    for alerts in (listed, board):
        white, limited, back = alerts['w1'], alerts['f1'], alerts['f2']
        assert white['title'] == f"{provider_label('whitehouse')}: access rejected"
        assert white['message'] == 'Access denied (HTTP 403)' and white['metrics']['label'] == provider_label('whitehouse')
        assert limited['title'] == f"{provider_label('finnhub')}: rate limited" and limited['message'] == 'Rate limited (HTTP 429)'
        assert back['title'] == f"{provider_label('finnhub')}: recovered" and back['message'] == 'Back online (was rate limited)'
        assert alerts['d1']['message'] == 'Connection failed' and alerts['g1']['message'] == 'Timed out'
        # A failure is resolved by a later recovery alert of its provider, or by its current ONLINE status.
        assert limited['resolved'] is True and limited['resolved_at'] == back['created_at']
        assert alerts['d1']['resolved'] is True and alerts['d1']['resolved_at'] is None
        assert white['resolved'] is False and 'resolved' not in back
        text = ' '.join(f"{a['title']} {a['message']}" for a in alerts.values())
        assert 'Error' not in text and 'whitehouse' not in text and 'RATE_LIMITED' not in text
    stored = hub.store.get('alerts', 'w1')
    assert stored['title'] == 'whitehouse: auth error' and 'label' not in stored['metrics']  # rows are never rewritten


def test_new_provider_alerts_are_shown_as_written():
    from backend import notifications
    previous = {'name': 'massive', 'label': 'Massive (US stocks, indices and FX)', 'status': 'ONLINE'}
    current = dict(previous, status='AUTH_ERROR', error='Access denied (HTTP 403)', error_type='HTTPStatusError')
    alert = notifications.provider_alert(previous, current, utcnow())
    shown = notifications.readable_alert(alert)
    assert {k: shown[k] for k in alert} == alert and shown['resolved'] is False
    event = {'kind': 'EVENT', 'title': 'Confirmed: FED_POLICY', 'message': 'x', 'metrics': {}}
    assert notifications.readable_alert(event) is event


def test_source_registry_links_homepages_and_the_delivering_providers_status(client):
    """Regression (UI audit EV-21 / REG-events-2 / REG-events-3, contract C17): Site links were built from the bare
    ingestion domain (https://boj.or.jp/ has no site) and Benzinga, OFAC and the X rows read 'Unavailable' while the
    providers delivering them (alpaca, ofac_actions, x) were running."""
    hub = client.app.state.hub
    for name, status in (('alpaca', 'ONLINE'), ('ofac_actions', 'ONLINE'), ('ofac_sls', 'DEGRADED'), ('x', 'NOT CONFIGURED')):
        hub.providers[name].health['status'] = status
    rows = {s['id']: s for s in client.get('/api/sources').json()}
    assert rows['boj']['homepage'] == 'https://www.boj.or.jp/en/'
    assert rows['boe']['homepage'] == 'https://www.bankofengland.co.uk/'
    assert rows['ofac-sls']['homepage'] == 'https://ofac.treasury.gov/sanctions-list-service'
    assert rows['dev-stub']['homepage'] is None and rows['x-federalreserve']['homepage'] is None
    assert all(r['homepage'] is None or r['homepage'].startswith('https://') for r in rows.values())
    assert rows['benzinga']['status'] == 'ONLINE' and rows['ofac']['status'] == 'ONLINE'
    assert rows['ofac-sls']['status'] == 'DEGRADED' and rows['x-ecb']['status'] == 'NOT CONFIGURED'
    assert rows['sec-filings']['status'] == hub.providers['sec_edgar'].health['status']
    assert rows['fed']['status'] == hub.providers['fed'].health['status']  # delivered under its own id
    assert rows['opec']['status'] is None  # no provider delivers it


def test_every_configured_source_has_a_working_homepage_or_none():
    from urllib.parse import urlsplit
    for source in rules()['sources']:
        home = source.get('homepage')
        assert home is None or (home.startswith('https://') and not (urlsplit(home).hostname or '').endswith(
            ('.invalid', '.test', '.example', '.localhost'))), source['id']


def test_events_are_cut_and_ordered_on_the_same_publication_time(hub):
    """Regression (UI audit REG-events-9): the cut used the earliest document date while the order used event_time, so
    an event that later received an older document fell off a limited list although it sorts near the top."""
    now = utcnow()
    first = official(hub, 'Board announces discount rate action', now - timedelta(hours=1))
    for hours in (2, 3):
        official(hub, f'Board releases supervisory statement {hours}', now - timedelta(hours=hours))
    hub.ingest_document(DocumentInput(source_id='fed', title='Archive notice on the discount rate',
                                      content='Archive notice. Official text.', event_id=first['id'],
                                      url='https://www.federalreserve.gov/newsevents/archive-notice.htm',
                                      published_at=now - timedelta(days=90)), trusted_transport=True)
    top = hub.events(2)
    assert top[0]['id'] == first['id'] and [e['id'] for e in top] == [e['id'] for e in hub.events(10)[:2]]
    everything = hub.events(10)
    assert [e['event_time'] for e in everything] == sorted((e['event_time'] for e in everything), reverse=True)


def test_crypto_daily_bars_never_query_official_closes(hub, monkeypatch):
    """Regression (UI audit ASSET-17): the asset page walked every stored BTC tick of 400 days on each poll looking for
    official closes that no source publishes for a 24/7 venue."""
    calls = []
    real = hub.daily_closes
    monkeypatch.setattr(hub, 'daily_closes', lambda symbol, now: calls.append(symbol) or real(symbol, now))
    now = utcnow()
    hub.daily_bars('BTC', now, 20, 'crypto')
    hub.daily_bars('ETH', now, 20)
    assert calls == []
    hub.daily_bars('US10Y', now, 20, 'bond')
    assert calls == ['US10Y']


def test_refresh_labels_are_plain_english():
    """Regression (UI audit REG-shell_settings_providers-4): Settings > Refresh showed 'tick_seconds is the Celery beat
    check rate' and '+-15 min'."""
    import re
    cadences = rules()['refresh']['cadences']
    for kind, spec in cadences.items():
        label = spec['label']
        assert not re.search(r'\b[a-z]+_[a-z0-9_]+\b', label) and 'Celery' not in label and '+-' not in label, kind
    assert cadences['ui_snapshot_full']['seconds'] == 30 and cadences['ui_snapshot']['seconds'] == 5


def test_fred_quote_labels_name_no_series_id():
    """Regression (UI audit DASH-16): the market strip read 'as of FRED SP500, close Fri 25 Sep'."""
    from backend.providers.official_data import fred_quotes
    [quote] = fred_quotes([{'provider': 'fred', 'series': 'NASDAQCOM', 'value': 18000.0, 'period': '2026-09-25'}])
    assert 'NASDAQCOM' not in quote['as_of_label'] and 'close Fri 25 Sep' in quote['as_of_label']


def test_a_provider_failure_alert_is_resolved_when_the_provider_is_switched_off_or_gone():
    from backend import notifications
    failure = {'kind': 'PROVIDER', 'title': 'Finnhub (US equities): access rejected', 'created_at': '2026-09-28T08:00:00+00:00',
               'metrics': {'provider': 'finnhub', 'status': 'AUTH_ERROR', 'label': 'Finnhub (US equities)'}}
    assert notifications.readable_alert(failure, {'finnhub': 'AUTH_ERROR'})['resolved'] is False
    assert notifications.readable_alert(failure, {'finnhub': 'DISABLED'})['resolved'] is True
    assert notifications.readable_alert(failure, {'other': 'ONLINE'})['resolved'] is True  # no longer registered
    assert notifications.readable_alert(failure)['resolved'] is False  # no current status known: nothing is claimed
