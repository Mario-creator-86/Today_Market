"""UI-audit fixes in the provider, ingestion, brief and alert layer: plain-English provider health and alerts with human
provider labels, per-provider alert dedupe, brief quote labels that follow the dashboard strip, one document per page
whatever its URL scheme, recurring releases kept apart, legacy events classified on read, readable provider label
times and the IEX regular-session close; feed text stored and read as plain text, adapter wording in words (topic
names, dates, counts) and one publication per event, with a one-off re-split of events merged under earlier rules.
Settings never read the real .env; nothing touches the network."""
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select

from backend import briefs, causal, dedupe
from backend import notifications as n
from backend import provider_settings as ps
from backend.config import Settings, rules
from backend.database import Document, Event, Evidence, Store, Tick, utcnow
from backend.engines import url_variants
from backend.ingestion import RESPLIT_KEY, readable
from backend.notifications import NotificationSettings
from backend.providers import discovery, econ_calendars
from backend.providers.alpaca import AlpacaDataProvider
from backend.providers.base import (
    HTTPProvider,
    ProviderUnavailable,
    plain_text,
    provider_category,
    provider_label,
)
from backend.providers.discovery import (
    EdgarFullTextProvider,
    OfacRecentActionsProvider,
    OfacSanctionsListProvider,
)
from backend.providers.finnhub import FinnhubProvider
from backend.providers.news import RSSProvider, SECProvider
from backend.providers.official_data import fred_quotes
from backend.providers.registry import build_providers
from backend.schemas import DocumentInput, Quote, iso
from backend.service import HubService

NOW = datetime(2026, 7, 1, 14, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def hub(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/audit.db')
    store.initialize()
    result = HubService(store, Settings(_env_file=None, enable_ingestion=False))
    result.initialize()
    return result


# --- provider health: plain-English causes, the exception type kept apart (C12) ----------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize('outcome, text, kind', [
    (401, 'Authentication rejected (HTTP 401)', 'HTTPStatusError'),
    (403, 'Access denied (HTTP 403)', 'HTTPStatusError'),
    (404, 'Not found (HTTP 404)', 'HTTPStatusError'),
    (429, 'Rate limited (HTTP 429)', 'HTTPStatusError'),
    (503, 'Server error (HTTP 503)', 'HTTPStatusError'),
    (httpx.ReadTimeout('slow'), 'Timed out', 'ReadTimeout'),
    (httpx.ConnectError('refused'), 'Connection failed', 'ConnectError'),
    (httpx.ReadError('reset by peer'), 'Network error', 'ReadError')])
async def test_provider_failures_are_plain_english_with_the_exception_type_kept_apart(monkeypatch, outcome, text, kind):
    async def request(self, method, url, **kwargs):
        if isinstance(outcome, int):
            return httpx.Response(outcome, text='denied for key=secret-123', request=httpx.Request(method, url))
        raise outcome
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = HTTPProvider('finnhub', 'https://finnhub.test/api?token=secret-123')
    with pytest.raises(ProviderUnavailable) as caught:
        await provider.request('GET', '/quote', attempts=1)
    assert provider.health['error'] == text and str(caught.value) == text and provider.health['error_type'] == kind
    stored = json.dumps(provider.health)
    assert 'secret-123' not in stored and 'finnhub.test' not in stored and 'Error:' not in provider.health['error']


@pytest.mark.asyncio
async def test_a_success_clears_the_error_and_its_type(monkeypatch):
    answers = iter([httpx.Response(429), httpx.Response(200, json={})])

    async def request(self, method, url, **kwargs):
        response = next(answers)
        response.request = httpx.Request(method, url)
        return response
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = HTTPProvider('fred', 'https://fred.test')
    with pytest.raises(ProviderUnavailable):
        await provider.request('GET', '/series', attempts=1)
    provider.next_attempt = 0.0  # skip the circuit wait
    await provider.request('GET', '/series', attempts=1)
    assert provider.health['status'] == 'ONLINE' and provider.health['error'] is None and provider.health['error_type'] is None


# --- provider labels (C13) ---------------------------------------------------------------------------------------

def test_every_provider_row_carries_a_human_label_and_a_settings_category():
    assert provider_label('finnhub') == 'Finnhub (US equities)' and provider_category('finnhub') == 'market'
    assert provider_label('fed') == 'Federal Reserve news feed' and provider_category('fed') == 'news'
    assert provider_label('bls-empsit') == 'BLS employment situation feed'
    assert provider_label('treasury-auction_results') == 'US Treasury auction results feed'  # an extra feed without an entry
    assert provider_label('massive-stub') == 'Development stub for Massive (US stocks, indices and FX)'
    assert provider_category('calendar-stub') == 'calendars'
    assert provider_label('not_a_provider') == 'not_a_provider' and provider_category('not_a_provider') is None
    built = build_providers(Settings(_env_file=None, provider_mode='stub'))
    categories = {c['id'] for c in rules()['provider_settings']['categories']}
    for name, provider in built.items():
        assert provider.health['name'] == name and provider.health['label'] != name, name  # no raw id as a label
        assert provider.health['category'] in categories, name
    assert built['dol_claims_rule'].health['label'] == 'Jobless claims schedule (weekly rule)'


def test_hub_health_and_settings_rows_expose_the_label(hub):
    rows = {row['name']: row for row in hub.health()}
    assert rows['nyfed_rates']['label'] == 'New York Fed reference rates' and rows['nyfed_rates']['error_type'] is None
    assert rows['ollama']['label'].startswith('Ollama')
    settings_rows = {row['name']: row for row in ps.summary(hub)['providers']}
    assert settings_rows['calendar']['label'] == 'Financial Modeling Prep (economic calendar)'


# --- provider alerts: labels, plain words, one alert per provider and status ---------------------------------------

@pytest.mark.asyncio
async def test_provider_alerts_are_plain_english_and_deduplicated_per_provider(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/alerts.db')
    store.initialize()
    dispatcher = n.Dispatcher(store, [n.InAppChannel(store)])

    def online(name, label):
        return {'name': name, 'label': label, 'status': 'ONLINE'}
    ollama = {'name': 'ollama', 'label': 'Ollama (AI)', 'status': 'RATE_LIMITED', 'error': 'Rate limited (HTTP 429)',
              'error_type': 'HTTPStatusError'}
    finnhub = {'name': 'finnhub', 'label': 'Finnhub (US equities)', 'status': 'RATE_LIMITED',
               'error': 'Rate limited (HTTP 429)', 'error_type': 'HTTPStatusError'}
    first = n.provider_alert(online('ollama', 'Ollama (AI)'), ollama, NOW)
    second = n.provider_alert(online('finnhub', 'Finnhub (US equities)'), finnhub, NOW + timedelta(minutes=9))
    assert first['title'] == 'Ollama (AI): rate limited' and first['message'] == 'Rate limited (HTTP 429)'
    assert second['metrics']['provider'] == 'finnhub' and second['metrics']['label'] == 'Finnhub (US equities)'
    assert second['metrics']['error_type'] == 'HTTPStatusError' and second['symbol'] is None
    # A second provider failing in the same window is its own alert, not a duplicate of the first.
    assert [(await dispatcher.dispatch(a))['status'] for a in (first, second)] == ['NEW', 'NEW']
    again = n.provider_alert(online('finnhub', 'Finnhub (US equities)'), finnhub, NOW + timedelta(minutes=12))
    assert (await dispatcher.dispatch(again))['status'] == 'DUPLICATE'
    recovered = n.provider_alert(finnhub, online('finnhub', 'Finnhub (US equities)'), NOW + timedelta(minutes=20))
    assert recovered['title'] == 'Finnhub (US equities): recovered' and recovered['message'] == 'Back online (was rate limited)'
    denied = n.provider_alert({'name': 'massive', 'status': 'ONLINE'}, {'name': 'massive', 'status': 'AUTH_ERROR'}, NOW)
    assert denied['title'] == 'massive: access rejected' and denied['message'] == 'Was online, now access rejected'
    for alert in (first, second, recovered, denied):
        assert '_' not in alert['title'] + alert['message'] and 'Error' not in alert['message']


# --- provider settings: placeholders and test details ---------------------------------------------------------------

def test_a_rejected_default_is_not_shown_as_a_value_but_a_saved_one_is():
    field = ps.fields()['sec_user_agent']
    placeholder = Settings(_env_file=None, sec_user_agent='MarketIntelligence contact@example.com')
    notify = NotificationSettings(_env_file=None)
    state = ps.field_state(field, placeholder, notify, set(), set())
    assert state['value'] is None and state['configured'] is False and state['source'] == 'default'
    saved = ps.field_state(field, placeholder, notify, {'SEC_USER_AGENT'}, set())  # the user's own entry: correctable
    assert saved['value'] == 'MarketIntelligence contact@example.com' and saved['configured'] is False
    assert Settings(_env_file=None).sec_user_agent == ''  # no invented contact by default
    assert not SECProvider('').configured and not SECProvider('MarketIntelligence contact@example.com').configured
    assert SECProvider('Jane Doe jane@firm.io').configured


def test_bls_help_is_not_prefixed_twice():
    help_text = ps.fields()['bls_api_key']['help']
    assert not help_text.lower().startswith('optional')  # the UI prefixes 'Optional.' for every non-required field


@pytest.mark.parametrize('reason, text', [
    ('transport:ConnectError', 'Delivery failed: the service could not be reached.'),
    ('smtp:SMTPServerDisconnected', 'Delivery failed: the mail server could not be reached or refused the message.')])
def test_channel_test_failures_never_show_exception_names(reason, text):
    assert ps._delivery_failure('email', reason) == ('OFFLINE', text)


# --- briefs follow the dashboard strip --------------------------------------------------------------------------

def quote(**fields):
    return dict({'price': 4.81, 'timestamp': '2026-09-25T20:00:00+00:00', 'freshness_status': 'STALE'}, **fields)


@pytest.mark.parametrize('fields, label', [
    # a current official close stays DAILY CLOSE even once its venue has reopened
    ({'data_class': 'DAILY_CLOSE', 'close_status': 'CURRENT', 'close_date': '2026-09-25', 'market_status': 'LIVE'},
     'DAILY CLOSE 25 Sep'),
    # a Tuesday close that is overdue is a STALE close, never a FRIDAY CLOSE
    ({'data_class': 'DAILY_CLOSE', 'close_status': 'STALE', 'close_date': '2026-09-22', 'market_status': 'FRIDAY CLOSE'},
     'STALE close 22 Sep'),
    ({'data_class': 'DERIVED_DAILY', 'derived': True, 'close_status': 'CURRENT', 'close_date': '2026-09-25',
      'market_status': 'FRIDAY CLOSE'}, 'DERIVED 25 Sep'),
    ({'daily_close': True, 'close_status': 'CURRENT', 'close_date': '2026-09-25', 'market_status': 'LIVE'},
     'DAILY CLOSE 25 Sep'),  # rows stored before data_class existed
    ({'freshness_status': 'FRESH', 'market_status': 'LIVE'}, 'LIVE'),
    ({'market_status': 'FRIDAY CLOSE'}, 'LAST PRINT'),
    ({'freshness_status': 'FRESH', 'market_status': 'AFTER HOURS'}, 'AFTER HOURS'),
    ({'market_status': 'LIVE'}, 'STALE'),
    ({'freshness_status': 'FRESH', 'market_status': 'LIVE', 'stub': True}, 'STUB')])
def test_brief_quote_labels_match_the_strip(fields, label):
    assert briefs.quote_label(quote(**fields)) == label


def test_brief_asset_lines_use_bp_for_yields_and_name_the_close_date():
    close = {'data_class': 'DAILY_CLOSE', 'close_status': 'CURRENT', 'close_date': '2026-09-25', 'market_status': 'LIVE'}
    derived = {'symbol': 'US2Y', 'asset_class': 'bond', 'quote': quote(change_pct=-1.23, **close), 'decision': None}
    provided = {'symbol': 'US10Y', 'asset_class': 'bond', 'quote': quote(price=4.2, change_bp=-3.5, change_pct=-0.8, **close),
                'decision': None}
    gold = {'symbol': 'GOLD', 'asset_class': 'commodity', 'decision': None,
            'quote': quote(price=2400.0, change_pct=0.5, data_class='DAILY_CLOSE', close_status='STALE', close_date='2026-09-22',
                           market_status='FRIDAY CLOSE')}
    fx = {'symbol': 'EURUSD', 'asset_class': 'fx', 'quote': quote(price=1.1, freshness_status='FRESH', market_status='LIVE'),
          'decision': {'score': 0, 'direction': 'WAIT', 'levels': {},
                       'reasons': ['Relative central-bank comparison unavailable (ECB vs Fed)']}}
    result = briefs.compose('MORNING', NOW, [derived, provided, gold, fx], [], [], {'regimes': [], 'posture': 'MIXED'})
    sections = {s['title']: s['items'] for s in result['sections']}
    assert sections['Bonds'] == ['US2Y 4.81 (-6 bp) · DAILY CLOSE 25 Sep', 'US10Y 4.2 (-3.5 bp) · DAILY CLOSE 25 Sep']
    assert sections['Gold'] == ['GOLD 2,400 (+0.50%) · STALE close 22 Sep']
    assert sections['Risk map'] == ['Relative central-bank comparison unavailable (ECB vs Fed) (1 asset)']
    assert sections['Long / short / wait setups'] == ['No actionable setups: 1 asset WAIT']


# --- ingestion: one document per page, recurring releases apart, honest timestamps ----------------------------------

def test_url_variants_cover_scheme_and_www_only():
    assert set(url_variants('http://www.boj.or.jp/en/g260731.pdf')) == {
        'http://www.boj.or.jp/en/g260731.pdf', 'https://www.boj.or.jp/en/g260731.pdf',
        'http://boj.or.jp/en/g260731.pdf', 'https://boj.or.jp/en/g260731.pdf'}
    assert url_variants('ftp://files.test/a') == ['ftp://files.test/a']


def document(url, title='Bank of Japan: Monetary Base', published=None, content='Monetary base table'):
    return DocumentInput(source_id='boj', title=title, content=content, url=url, published_at=published or utcnow())


def test_the_same_page_over_http_and_https_is_one_document(hub):
    first = hub.ingest_document(document('http://www.boj.or.jp/en/statistics/g260731.pdf'), trusted_transport=True)
    second = hub.ingest_document(document('https://www.boj.or.jp/en/statistics/g260731.pdf'), trusted_transport=True)
    third = hub.ingest_document(document('https://boj.or.jp/en/statistics/g260731.pdf', content='Revised table'),
                                trusted_transport=True)
    assert first['id'] == second['id'] == third['id']
    assert [e['url'] for e in second['evidence']] == ['http://www.boj.or.jp/en/statistics/g260731.pdf']
    assert len(third['evidence']) == 2 and third['evidence'][1]['dedupe']['basis'] == 'canonical_url'  # a revision


def test_an_identical_headline_days_apart_is_a_new_release_not_the_same_story(hub):
    older = hub.ingest_document(document('https://www.boj.or.jp/en/mei260910.xlsx', title='Japan main economic indicators',
                                         published=utcnow() - timedelta(days=14)), trusted_transport=True)
    newer = hub.ingest_document(document('https://www.boj.or.jp/en/mei260918.xlsx', title='Japan main economic indicators',
                                         published=utcnow() - timedelta(days=3)), trusted_transport=True)
    assert older['id'] != newer['id']
    repost = hub.ingest_document(document('https://www.boj.or.jp/en/mei260918-note.htm', title='Japan main economic indicators',
                                          published=utcnow() - timedelta(days=3) + timedelta(hours=2)), trusted_transport=True)
    assert repost['id'] == newer['id']  # the same release a couple of hours later still merges


def test_headline_matches_need_publication_times_close_together():
    features = dedupe.features('Japan main economic indicators')
    config = rules()['dedupe']
    within = dedupe.score(features, features, NOW, NOW - timedelta(hours=config['headline_time_window_hours']))
    beyond = dedupe.score(features, features, NOW, NOW - timedelta(hours=config['headline_time_window_hours'] + 1))
    assert within['match'] and not beyond['match'] and beyond['lexical'] == 1.0


def test_updated_is_never_before_first_seen(hub):
    event = hub.ingest_document(document('https://www.boj.or.jp/en/new.htm', title='Bank of Japan releases new data'),
                                trusted_transport=True)
    assert event['last_updated_utc'] == event['first_seen_utc']
    start = utcnow() - timedelta(hours=1)
    with hub.store.session() as session:  # a row stored before created_at was set explicitly
        session.add(Event(id='old', summary_fact='Older row', event_type='NEWS', created_at=start + timedelta(seconds=7),
                          updated_at=start, payload={'trigger': None, 'trigger_basis': 'none'}))
        session.commit()
    old = hub.event('old')
    assert old['last_updated_utc'] == old['first_seen_utc']


def legacy(hub, event_id, title, payload, content=None):
    with hub.store.session() as session:
        session.add(Event(id=event_id, summary_fact=title, event_type='CENTRAL_BANK', payload=payload))
        if content is not None:
            session.add(Document(id=f'{event_id}-doc', source_id='fed', fingerprint=f'{event_id:0>64}', title=title,
                                 content=content, url=f'https://www.federalreserve.gov/{event_id}.htm',
                                 published_at=utcnow() - timedelta(days=3), payload={}))
            session.add(Evidence(event_id=event_id, document_id=f'{event_id}-doc'))
        session.commit()


def test_events_stored_before_classification_are_classified_on_read(hub):
    legacy(hub, 'fomc', 'Federal Reserve issues FOMC statement', {'provenance': 'provider_retrieval'},
           'The Committee decided to lower the target range for the federal funds rate by 1/4 percentage point.')
    legacy(hub, 'board', 'China board meets', {'topic': 'CHINA_BOARD'})
    legacy(hub, 'policy', 'Fed policy note', {'topic': 'FED_POLICY', 'trigger': None, 'trigger_basis': 'none'})
    fomc = hub.event('fomc')
    assert fomc['trigger'] == 'FED_DOVISH' and fomc['trigger_basis'] == 'rule'
    assert fomc['topic'] == 'FED_POLICY' and fomc['topic_basis'] == 'rule' and 'US2Y' in fomc['affected_assets']
    assert causal.event_paths(fomc, utcnow()) and fomc['why_it_matters']
    assert fomc['classified_on_read'] is True
    board = hub.event('board')
    assert board['trigger'] is None and board['trigger_basis'] == 'none'
    assert 'classified_on_read' not in board  # nothing matched: its stored reactions are not 'ungraded'
    assert board['topic'] == 'CHINA_BOARD' and board['topic_basis'] == 'entities'  # a heuristic, not a curated topic
    assert hub.event('policy')['topic_basis'] == 'rule'
    with hub.store.session() as session:  # read-only: the stored rows are never rewritten
        assert 'trigger' not in session.get(Event, 'fomc').payload


# --- causal paths name their rule --------------------------------------------------------------------------------

def test_every_causal_rule_has_a_plain_label_on_its_paths():
    assert all(rule.label for rule in causal.graph().rules)
    path = causal.paths('CRYPTO_REGULATION')[0]
    assert path['rule_id'] == 'crypto-regulation' and path['label'] == 'Tighter crypto regulation'


# --- provider labels carry readable times ------------------------------------------------------------------------

def bar(t, close):
    return {'t': t, 'o': close, 'h': close + 1, 'l': close - 1, 'c': close, 'v': 1000, 'n': 10, 'vw': close}


def test_an_after_hours_iex_trade_is_never_stored_as_the_daily_close():
    provider = AlpacaDataProvider('PKTESTKEYID', 'test-secret-value')
    sunday = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    snapshot = {'latestTrade': {'t': '2026-09-25T20:59:34.720445Z', 'p': 225.05},  # 16:59:34 ET, after the close
                'dailyBar': bar('2026-09-25T04:00:00Z', 225.04), 'prevDailyBar': bar('2026-09-24T04:00:00Z', 220.0)}
    record = provider._record('NVDA', snapshot, sunday)
    assert record['price'] == 225.04 and record['timestamp'] == '2026-09-25T20:00:00Z' and record['daily_close'] is True
    assert record['delay_label'] == 'FRIDAY CLOSE (IEX regular-session close Fri 25 Sep 16:00 ET)'
    assert record['previous_close'] == 220.0 and '2026-09' not in record['as_of_label']
    regular = dict(snapshot, latestTrade={'t': '2026-09-25T19:59:58Z', 'p': 225.1})  # the session's own last trade
    record = provider._record('NVDA', regular, sunday)
    assert record['price'] == 225.1 and record['delay_label'] == 'FRIDAY CLOSE (IEX last trade Fri 25 Sep 15:59 ET)'
    bare = dict(snapshot, dailyBar=None, prevDailyBar=None)  # no bar to take the close from: the trade, labelled
    record = provider._record('NVDA', bare, sunday)
    assert record['price'] == 225.05 and record['daily_close'] is False
    assert record['delay_label'] == 'IEX extended-hours trade Fri 25 Sep 16:59 ET (outside the regular session; not a close)'


def test_finnhub_and_fred_labels_use_readable_dates():
    record = FinnhubProvider('k').record('MSFT', 500.0, datetime(2026, 9, 25, 20, 0, tzinfo=UTC), 'rest',
                                         now=datetime(2026, 9, 26, 15, 0, tzinfo=UTC))
    assert record['delay'] == 'FRIDAY CLOSE (last trade Fri 25 Sep 16:00 ET)'
    [sp] = fred_quotes([{'provider': 'fred', 'series': 'SP500', 'value': 7743.41, 'period': '2026-09-25',
                         'previous_value': 7700.0, 'previous_period': '2026-09-24', 'attribution': 'Source: FRED'}])
    assert 'close Fri 25 Sep' in sp['as_of_label'] and '2026-09-25' not in sp['as_of_label']


# --- round 2: extended-hours trades, restated labels, legacy duplicates, briefs on old decisions --------------------

@pytest.mark.parametrize('trade, now, label', [
    # a fresh after-hours print: never real-time, never stored after the close it would hide
    ('2026-09-25T20:59:34Z', datetime(2026, 9, 25, 21, 0, tzinfo=UTC),
     'AFTER HOURS (IEX regular-session close Fri 25 Sep 16:00 ET)'),
    # a premarket print on Monday: Friday's close is still the regular-session price
    ('2026-09-28T11:59:00Z', datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
     'PREMARKET (IEX regular-session close Fri 25 Sep 16:00 ET)'),
    # the market has opened but IEX has no regular-session trade yet
    ('2026-09-28T13:29:50Z', datetime(2026, 9, 28, 13, 30, 30, tzinfo=UTC),
     'IEX regular-session close Fri 25 Sep 16:00 ET (no regular-session IEX trade yet today)')])
def test_a_fresh_extended_hours_iex_trade_gives_way_to_the_regular_session_close(trade, now, label):
    provider = AlpacaDataProvider('PKTESTKEYID', 'test-secret-value')
    snapshot = {'latestTrade': {'t': trade, 'p': 231.5},
                'dailyBar': bar('2026-09-25T04:00:00Z', 225.04), 'prevDailyBar': bar('2026-09-24T04:00:00Z', 220.0)}
    record = provider._record('NVDA', snapshot, now)
    assert record['price'] == 225.04 and record['timestamp'] == '2026-09-25T20:00:00Z' and record['daily_close'] is True
    assert record['delay_label'] == label and 'real-time' not in record['as_of_label']
    assert record['previous_close'] == 220.0


def test_a_regular_session_trade_is_still_real_time_while_fresh():
    provider = AlpacaDataProvider('PKTESTKEYID', 'test-secret-value')
    snapshot = {'latestTrade': {'t': '2026-09-28T14:00:00Z', 'p': 230.0},
                'dailyBar': bar('2026-09-28T04:00:00Z', 229.0), 'prevDailyBar': bar('2026-09-25T04:00:00Z', 225.04)}
    record = provider._record('NVDA', snapshot, datetime(2026, 9, 28, 14, 0, 30, tzinfo=UTC))
    assert record['price'] == 230.0 and record['daily_close'] is False and record['previous_close'] == 225.04
    assert record['delay_label'] == rules()['provider_routing']['alpaca']['delay_labels']['realtime']


@pytest.mark.asyncio
async def test_a_refused_news_api_leaves_the_market_data_health_as_it_was(monkeypatch):
    async def request(self, method, url, **kwargs):
        return httpx.Response(403, json={'message': 'forbidden'}, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = AlpacaDataProvider('PKTESTKEYID', 'test-secret-value')
    provider.health.update(status='ONLINE', error=None, error_type=None)
    assert await provider.news(['NVDA'], NOW) == []
    assert provider.health['status'] == 'ONLINE' and provider.health['error'] is None
    assert provider.health['error_type'] is None and provider.health['news'].startswith('NOT AVAILABLE')


def test_every_provider_row_has_an_error_type_even_without_requests():
    for mode in ('stub', 'live'):
        for name, provider in build_providers(Settings(_env_file=None, provider_mode=mode)).items():
            assert 'error_type' in provider.health and provider.health['error_type'] is None, name


def test_a_provider_has_the_same_category_on_providers_as_in_its_settings_group():
    assert provider_category('bls_release_calendar') == 'filings'  # set up with the SEC contact
    for group in rules()['provider_settings']['groups']:
        for name in group.get('providers') or []:
            assert provider_category(name) == group['category'], name


def stored(hub, symbol):
    with hub.store.session() as session:
        return session.scalar(select(Tick).where(Tick.symbol == symbol).order_by(Tick.timestamp.desc()).limit(1))


def test_a_republished_observation_restates_its_label_and_close_class_but_never_its_values(hub):
    stamp = datetime(2026, 9, 25, 19, 59, 58, tzinfo=UTC)
    fresh = Quote(symbol='NVDA', price=225.1, timestamp=stamp, provider='alpaca', feed='iex', change_pct=1.0,
                  as_of_label='real-time IEX (single exchange, not consolidated), AFTER HOURS', daily_close=False)
    assert hub.ingest_quote(fresh)
    closed = Quote(symbol='NVDA', price=225.1, timestamp=stamp, provider='alpaca', feed='iex', change_pct=2.0,
                   previous_close=220.0, as_of_label='FRIDAY CLOSE (IEX last trade Fri 25 Sep 15:59 ET)', daily_close=True)
    assert not hub.ingest_quote(closed)  # the same observation: no new tick
    tick = stored(hub, 'NVDA')
    assert tick.price == 225.1 and tick.payload['as_of_label'] == 'FRIDAY CLOSE (IEX last trade Fri 25 Sep 15:59 ET)'
    assert tick.payload['daily_close'] is True and tick.payload['previous_close'] == 220.0
    assert tick.payload['change_pct'] == 1.0  # a value already stored is never changed
    assert hub.quote('NVDA', now=datetime(2026, 9, 27, 12, 0, tzinfo=UTC))['data_class'] == 'DAILY_CLOSE'
    old = Quote(symbol='SP500', price=7743.41, timestamp=datetime(2026, 9, 25, 20, 0, tzinfo=UTC), provider='fred',
                daily_close=True, as_of_label='FRED SP500, close 2026-09-25')
    assert hub.ingest_quote(old)
    assert not hub.ingest_quote(old.model_copy(update={'as_of_label': 'FRED SP500, close Fri 25 Sep'}))
    assert stored(hub, 'SP500').payload['as_of_label'] == 'FRED SP500, close Fri 25 Sep'


def test_copies_of_one_page_stored_before_the_fix_are_listed_and_counted_once(hub):
    published = utcnow() - timedelta(hours=2)
    title = 'Bank of Japan: Monetary Base'
    with hub.store.session() as session:
        session.add(Event(id='boj-old', summary_fact=title, event_type='NEWS',
                          payload={'trigger': None, 'trigger_basis': 'none'}))
        for index, (doc_id, url, content) in enumerate((
                ('d1', 'http://www.boj.or.jp/en/g260731.pdf', 'Monetary base table'),
                ('d2', 'https://www.boj.or.jp/en/g260731.pdf', 'Monetary base table'),  # the same page over https
                ('d3', 'https://boj.or.jp/en/g260731.pdf', 'Revised monetary base table'))):  # a revision
            session.add(Document(id=doc_id, source_id='boj', fingerprint=doc_id * 32, title=title, content=content, url=url,
                                 published_at=published, created_at=published + timedelta(minutes=index),
                                 payload={'direct': True}))
            session.add(Evidence(event_id='boj-old', document_id=doc_id))
        session.commit()
    event = hub.event('boj-old')
    assert [e['document_id'] for e in event['evidence']] == ['d1', 'd3']
    assert event['evidence'][0]['duplicate_document_ids'] == ['d2'] and 'duplicate_document_ids' not in event['evidence'][1]
    assert len(event['sources']) == 1


def test_legacy_classification_reads_the_same_text_as_ingestion_and_lists_the_event_under_its_assets(hub):
    filler = 'The Committee reviewed recent economic data. ' * 100  # past the 4000-character evidence excerpt
    assert 4000 < len(filler) < rules()['causal_triggers']['max_text_chars'] - 200
    legacy(hub, 'late', 'Federal Reserve issues FOMC statement', {'assets': ['BTC'], 'provenance': 'provider_retrieval'},
           filler + 'The Committee decided to lower the target range for the federal funds rate by 1/4 percentage point.')
    event = hub.event('late')
    assert event['trigger'] == 'FED_DOVISH' and event['classified_on_read'] is True
    assert event['assets'][0] == 'BTC' and 'US2Y' in event['assets']  # listed on the pages of its affected assets
    assert any(e['id'] == 'late' and 'US2Y' in e['assets'] for e in hub.events())
    with hub.store.session() as session:
        assert session.get(Event, 'late').payload == {'assets': ['BTC'], 'provenance': 'provider_retrieval'}


BRIEF_AT = datetime(2026, 9, 27, 4, 30, tzinfo=UTC)  # 07:30 Bucharest
BUCHAREST = ZoneInfo('Europe/Bucharest')


def live_asset(symbol, direction, created_at, score=0.0):
    return {'symbol': symbol, 'asset_class': 'crypto',
            'quote': {'price': 65000.0, 'change_pct': 1.2, 'freshness_status': 'FRESH', 'market_status': 'LIVE'},
            'decision': {'score': score, 'direction': direction, 'levels': {}, 'reasons': [],
                         'created_at': created_at.isoformat(), 'confirmation': 'x', 'invalidation': 'y'}}


def test_a_brief_says_when_an_old_setup_was_decided():
    regime = {'regimes': [], 'posture': 'MIXED'}
    old = live_asset('BTC', 'WAIT', datetime(2026, 9, 26, 23, 10, tzinfo=UTC))  # 02:10 Bucharest
    new = live_asset('ETH', 'WAIT', BRIEF_AT - timedelta(seconds=30))  # this cycle's decision
    result = briefs.compose('MORNING', BRIEF_AT, [old, new], [], [], regime)
    crypto = {s['title']: s['items'] for s in result['sections']}['Crypto']
    assert crypto == ['BTC 65,000 (+1.20%) · LIVE · WAIT · decided 02:10 (Bucharest)', 'ETH 65,000 (+1.20%) · LIVE · WAIT']
    yesterday = live_asset('BTC', 'LONG', datetime(2026, 9, 26, 20, 10, tzinfo=UTC), score=62)  # 23:10 the day before
    assert briefs.decided_text(yesterday['decision'], BRIEF_AT, BUCHAREST) == ' · decided 26 Sep 23:10 (Bucharest)'
    setups = briefs.compose('MORNING', BRIEF_AT, [yesterday], [], [], regime)
    assert {s['title']: s['items'] for s in setups['sections']}['Long / short / wait setups'] == [
        'BTC: LONG (score 62) · decided 26 Sep 23:10 (Bucharest)']
    assert briefs.decided_text({'direction': 'WAIT'}, BRIEF_AT, BUCHAREST) == ''  # no time: nothing claimed


def test_a_brief_warns_when_the_analysis_is_not_current():
    regime = {'regimes': [], 'posture': 'MIXED'}

    def state(analysis, assets=()):
        brief = briefs.compose('MORNING', BRIEF_AT, list(assets), [], [], regime, analysis=analysis)
        return {s['title']: s['items'] for s in brief['sections']}['Global market state']
    stale = {'stale': True, 'last_success_at': '2026-09-26T23:10:00+00:00'}
    old = live_asset('BTC', 'WAIT', datetime(2026, 9, 26, 23, 10, tzinfo=UTC))
    assert state(stale, [old])[-1] == 'Analysis not current: last completed 02:10 (Bucharest); setups are from then'
    assert 'no analysis cycle has completed yet' in state({'stale': True, 'last_success_at': None})[-1]
    assert not any('Analysis not current' in item for item in state({'stale': False, 'last_success_at': 'x'}))
    assert not any('Analysis not current' in item for item in state(None))
    # the brief of the cycle that has just decided is current, whatever the recorded status still says
    assert not any('Analysis not current' in item for item in state(stale, [live_asset('BTC', 'WAIT', BRIEF_AT)]))


# --- round 3: feed text as plain text, adapter wording in words, one publication per event ---------------------------

def test_feed_markup_and_entities_become_plain_text():
    feed = ('<p>For over 250 years, America’s hunters&#160;care for <a href="/lands">lands</a> &amp; waters.</p>'
            '<p>Released <time datetime="2026-07-22T14:38:46-04:00">July 22, 2026</time>&#8230;</p>')
    assert plain_text(feed) == 'For over 250 years, America’s hunters care for lands & waters. Released July 22, 2026 …'
    assert plain_text('Storage &amp; Peripherals Industry') == 'Storage & Peripherals Industry'
    assert plain_text('&lt;p&gt;Escaped twice &amp;amp; decoded&lt;/p&gt;') == 'Escaped twice & decoded'
    assert plain_text('Rates: a < b and c > d') == 'Rates: a < b and c > d'  # no tag: nothing removed
    assert plain_text('Line one\nLine two') == 'Line one\nLine two'  # plain text keeps its line breaks
    assert plain_text('<p>Cut off at the limit <a href="https://www.exa') == 'Cut off at the limit'
    assert plain_text(None) == ''


RSS_HTML = b'''<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><item>
<title>National Hunting &amp;amp; Fishing Day, 2026</title>
<link>https://www.whitehouse.gov/presidential-actions/2026/09/hunting-day/</link>
<pubDate>Fri, 25 Sep 2026 14:00:00 GMT</pubDate>
<description>&lt;p&gt;For over 250 years, hunters&amp;#160;care for lands &amp;amp; waters.&lt;/p&gt;&lt;p&gt;&lt;time datetime="2026-09-25T10:00:00-04:00"&gt;September 25, 2026&lt;/time&gt;&lt;/p&gt;</description>
</item></channel></rss>'''


@pytest.mark.asyncio
async def test_rss_titles_and_summaries_are_stored_as_plain_text(monkeypatch):
    async def request(self, method, url, **kwargs):
        return httpx.Response(200, content=RSS_HTML, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    [doc] = await RSSProvider({'id': 'whitehouse', 'rss': 'https://www.whitehouse.gov/feed/'}).documents()
    assert doc['title'] == 'National Hunting & Fishing Day, 2026'
    assert doc['content'] == 'For over 250 years, hunters care for lands & waters. September 25, 2026'


@pytest.mark.asyncio
async def test_benzinga_headlines_are_stored_with_their_entities_decoded(monkeypatch):
    article = {'id': 7, 'headline': 'Inquiry Into Apple&#39;s Competitor Dynamics In Storage &amp; Peripherals Industry',
               'created_at': '2026-09-28T10:00:00Z', 'updated_at': '2026-09-28T10:00:00Z',
               'summary': '<p>Apple &amp; peers</p>', 'url': 'https://www.benzinga.com/news/26/09/1/inquiry',
               'symbols': ['AAPL'], 'source': 'benzinga'}

    async def request(self, method, url, **kwargs):
        return httpx.Response(200, json={'news': [article], 'next_page_token': None}, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    [doc] = await AlpacaDataProvider('PKTESTKEYID', 'test-secret-value').news(['AAPL'], NOW)
    assert doc['title'] == "Inquiry Into Apple's Competitor Dynamics In Storage & Peripherals Industry"
    assert doc['content'] == 'Apple & peers'


@pytest.mark.asyncio
async def test_fed_calendar_titles_and_locations_are_plain_text(monkeypatch):
    payload = {'events': [{'title': 'Speech - Vice Chair Philip N. Jefferson', 'time': '9:00 a.m.', 'month': '2026-10',
                           'days': '8', 'type': 'Speeches', 'description': '', 'live': '', 'link': '',
                           'location': 'At a Talk and Q&amp;A with Federal Reserve Board Vice Chair Philip N. Jefferson'}],
               'announcement': []}

    async def request(self, method, url, **kwargs):
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    [row] = await econ_calendars.FedEventsCalendar().calendar('2026-10-01', '2026-10-31')
    assert row['location'] == 'At a Talk and Q&A with Federal Reserve Board Vice Chair Philip N. Jefferson'


def stored_document(hub, event_id, doc_id, source, title, content, url, published, created=None, **payload):
    created = created or published
    with hub.store.session() as session:
        if session.get(Event, event_id) is None:
            session.add(Event(id=event_id, summary_fact=title, event_type='OFFICIAL_RELEASE', created_at=published,
                              updated_at=published, payload={'trigger': None, 'trigger_basis': 'none',
                                                             'provenance': 'provider_retrieval',
                                                             'event_time': published.isoformat()}))
        session.add(Document(id=doc_id, source_id=source, fingerprint=f'{doc_id:x>64}', title=title, content=content,
                             url=url, published_at=published, created_at=created,
                             payload={'provenance': 'provider_retrieval', 'direct': True, **payload}))
        session.add(Evidence(id=f'e-{doc_id}', event_id=event_id, document_id=doc_id))
        session.commit()


def document_count(hub):
    with hub.store.session() as session:
        return session.scalar(select(func.count(Document.id)))


def test_documents_stored_as_raw_feed_html_read_as_text_and_are_not_stored_again(hub):
    published = utcnow() - timedelta(days=5)  # older than the candidate window: only the page itself can match
    raw_title, raw = 'Inquiry Into Storage &amp; Peripherals', '<p>For over 250 years, hunters&#160;care for lands.</p>'
    stored_document(hub, 'feed-old', 'feed-doc', 'whitehouse', raw_title, raw,
                    'https://www.whitehouse.gov/briefings/hunting-day', published)
    event = hub.event('feed-old')
    assert event['summary_fact'] == event['evidence'][0]['title'] == 'Inquiry Into Storage & Peripherals'
    assert event['evidence'][0]['excerpt'] == 'For over 250 years, hunters care for lands.'
    assert event['same_story_window_hours'] == rules()['dedupe']['headline_time_window_hours']
    again = hub.ingest_document(DocumentInput(source_id='whitehouse', title=plain_text(raw_title), content=plain_text(raw),
                                              url='http://whitehouse.gov/briefings/hunting-day/', published_at=published),
                                trusted_transport=True)
    assert again['id'] == 'feed-old' and len(again['evidence']) == 1 and document_count(hub) == 1
    with hub.store.session() as session:  # read-only: the stored rows are never rewritten
        assert session.get(Document, 'feed-doc').title == raw_title and session.get(Event, 'feed-old').summary_fact == raw_title


LEGACY_ACTION = ("OFAC published 'Russia-related Designations; Venezuela-related General License' in Sanctions List "
                 "Updates on 2026-09-16. Programs matched: SANCTIONS_RUSSIA, SANCTIONS_VENEZUELA. Timing: same-day "
                 "publication. Source: OFAC.")
ACTION = {'title': 'Russia-related Designations; Venezuela-related General License', 'category': 'Sanctions List Updates',
          'date': '2026-09-16', 'topics': ['SANCTIONS_RUSSIA', 'SANCTIONS_VENEZUELA'], 'delay': 'same-day publication',
          'attribution': 'Source: OFAC', 'url': 'https://ofac.treasury.gov/recent-actions/20260916',
          'published_at': '2026-09-16T04:00:00+00:00', 'assets': ['BRENT', 'GOLD', 'WTI']}


def test_ofac_and_edgar_documents_name_programs_dates_and_counts_in_words():
    content = OfacRecentActionsProvider.document(ACTION)['content']
    assert 'on 16 Sep 2026. Programs matched: Russia, Venezuela. Timing' in content and 'SANCTIONS_' not in content
    assert readable(LEGACY_ACTION) == content  # the earlier wording, stored before this fix, reads the same
    sls = {'publication_id': 984, 'publication_type': 'Standard Action', 'published_at': '2026-09-23T14:07:28.650545+00:00',
           'counts': {'remove': 2, 'update': 1}, 'programs': ['DRCONGO'], 'digital_currency_addresses': {},
           'entities': [{'entity_id': '9', 'action': 'remove', 'name': 'OLENGA, Francois', 'entity_type': 'Individual',
                         'programs': ['DRCONGO']},
                        {'entity_id': '58651', 'action': 'update', 'name': None, 'entity_type': None, 'programs': []}],
           'delay': 'same-day publication', 'attribution': 'Source: OFAC SLS',
           'url': 'https://sanctionslistservice.ofac.treas.gov/changes/984', 'assets': []}
    document = OfacSanctionsListProvider.document(sls)
    assert document['title'] == 'OFAC sanctions list update (publication 984): 2 removals, 1 amendment [DRCONGO]'
    assert document['content'] == (
        'OFAC Sanctions List Service publication 984 (Standard Action) at 10:07 EDT on 23 Sep 2026: 2 removals, '
        '1 amendment.\nRemoved: OLENGA, Francois (Individual; DRCONGO)\nAmended: entity 58651 (type n/a; no program)\n'
        'Timing: same-day publication. Source: OFAC SLS.')
    assert readable('OFAC sanctions list update (publication 984): 2 removals, 1 amendments [DRCONGO]') == document['title']
    assert readable('OFAC Sanctions List Service publication 984 (Standard Action) at 2026-09-23T14:07:28.650545+00:00: '
                    '2 removals, 1 amendments.\nremove: OLENGA, Francois (Individual; DRCONGO)\nupdate: entity 58651 '
                    '(type n/a; no program)\nTiming: same-day publication. Source: OFAC SLS.') == document['content']
    assert discovery.counted(11, 'add') == '11 additions' and discovery.counted(1, 'remove') == '1 removal'
    filing = EdgarFullTextProvider.document({
        'company': 'NVIDIA CORP', 'tickers': ['NVDA'], 'form': '8-K', 'items': ['2.02'], 'file_date': '2026-09-25',
        'adsh': '0001045810-26-000073', 'query_labels': ['Results of operations'], 'delay': 'day-granular',
        'attribution': 'Source: SEC EDGAR', 'url': 'https://www.sec.gov/Archives/edgar/data/1045810/a.htm',
        'as_of': '2026-09-25T04:00:00+00:00', 'event_type': 'EARNINGS', 'assets': ['NVDA']})
    assert 'with the SEC on 25 Sep 2026 (accession' in filing['content']
    assert readable(filing['content'].replace('25 Sep 2026', '2026-09-25')) == filing['content']


def test_an_adapter_record_stored_under_its_earlier_wording_is_not_stored_again(hub):
    published = utcnow() - timedelta(days=4)
    stored_document(hub, 'ofac-old', 'ofac-doc', 'ofac', f"OFAC: {ACTION['title']}", LEGACY_ACTION, ACTION['url'], published)
    current = OfacRecentActionsProvider.document(dict(ACTION, published_at=published.isoformat()))
    again = hub.ingest_document(DocumentInput.model_validate(current), trusted_transport=True)
    assert again['id'] == 'ofac-old' and document_count(hub) == 1
    assert 'Programs matched: Russia, Venezuela.' in again['evidence'][0]['excerpt']


def ofsi_licence(hub, number, at):
    return hub.ingest_document(DocumentInput(
        source_id='ofsi', title=f'Guidance: OFSI General Licence INT/2025/{number}', content='OFSI general licence',
        url=f'https://www.gov.uk/government/publications/ofsi-general-licence-int2025{number}', published_at=at),
        trusted_transport=True)


def test_licences_with_other_numbers_and_other_pages_of_one_official_source_stay_apart(hub):
    at = utcnow() - timedelta(hours=1)
    first, second = ofsi_licence(hub, 7895596, at), ofsi_licence(hub, 8031092, at + timedelta(minutes=1))
    assert first['id'] != second['id']
    signals = dedupe.score(dedupe.features('Guidance: OFSI General Licence INT/2025/8031092'),
                           dedupe.features('Guidance: OFSI General Licence INT/2025/7895596'), at, at)
    assert signals['match'] is False and signals['distinct_identifiers'] is True
    assert signals['lexical'] >= rules()['dedupe']['headline_match']  # alike wording, different licences
    base = hub.ingest_document(document('https://www.boj.or.jp/en/mbt2608.pdf', published=at,
                                        title="Monetary Base and the Bank of Japan's Transactions (Aug.)"), trusted_transport=True)
    other = hub.ingest_document(document('https://www.boj.or.jp/en/seifu2608.pdf', published=at,
                                         title="Bank of Japan's Transactions with the Government (Aug.)"), trusted_transport=True)
    assert base['id'] != other['id'] and other['evidence'][0]['dedupe'].get('match') is False
    # Other sources reporting a story still join it, and the same headline on another page of one source is one release.
    story = 'Chancellor announces Bank of England appointments'
    boe = hub.ingest_document(DocumentInput(source_id='boe', title=story, content='Appointments', published_at=at,
                                            url='https://www.bankofengland.co.uk/news/2026/september/appointments'),
                              trusted_transport=True)
    treasury = hub.ingest_document(DocumentInput(source_id='hm-treasury', title=story, content='Appointments',
                                                 url='https://www.gov.uk/government/news/chancellor-announces-boe-appointments',
                                                 published_at=at + timedelta(minutes=16)), trusted_transport=True)
    assert boe['id'] == treasury['id']

    def post(status, text, minutes):  # an official social account: one story often spans several posts
        return hub.ingest_document(DocumentInput(source_id='x-bls', title=text, content=text, published_at=at + timedelta(
            minutes=minutes), url=f'https://x.com/BLS_gov/status/{status}'), trusted_transport=True)
    assert post(1, 'CPI for all items rises 0.4% in August; shelter up', 0)['id'] == \
        post(2, 'CPI for all items rises 0.4% in August; shelter and food up', 5)['id']


def merged_jolts(hub):
    """A JOLTS event merged before the time window existed; every document ingested in one run, July's first."""
    july = utcnow() - timedelta(days=3)
    june = july - timedelta(days=28)
    run = utcnow() - timedelta(days=1)
    july_title = 'Job openings, hires, and total separations change little in July'
    june_title = 'Job openings and total separations change little in June; hires unchanged'
    stored_document(hub, 'jolts', 'july', 'bls', july_title, 'JOLTS July', 'https://www.bls.gov/jolts_09012026.htm', july,
                    run)
    joined = {'dedupe': {'match': True, 'basis': 'similarity', 'lexical': 0.88}}
    stored_document(hub, 'jolts', 'june', 'bls', june_title, 'JOLTS June', 'http://www.bls.gov/jolts_08042026.htm', june,
                    run + timedelta(seconds=1), **joined)  # last month's release, merged before the time window existed
    stored_document(hub, 'jolts', 'june-https', 'bls', june_title, 'JOLTS June',
                    'https://www.bls.gov/jolts_08042026.htm', june, run + timedelta(seconds=2), **joined)  # the same page over https
    stored_document(hub, 'jolts', 'july-x', 'x-bls', july_title, 'JOLTS July', 'https://x.com/BLS_gov/status/1',
                    july + timedelta(hours=1), run + timedelta(seconds=3), **joined)  # another source reporting the same release
    stored_document(hub, 'jolts', 'july-copy', 'bls', july_title, 'JOLTS July (revised)',
                    'http://bls.gov/jolts_09012026.htm', july, run + timedelta(seconds=4),
                    dedupe={'match': True, 'basis': 'canonical_url'})
    with hub.store.session() as session:
        row = session.get(Event, 'jolts')
        row.payload = dict(row.payload, ai_disputed_evidence=['e-june', 'e-july-x'])
        session.commit()
    return june, run + timedelta(seconds=1)


def test_a_dry_run_lists_the_split_and_changes_nothing(hub):
    merged_jolts(hub)
    plan = hub.split_merged_events(dry_run=True)
    assert plan['events_split'] == 1 and plan['documents_moved'] == 2 and plan['events_created'] == 1
    assert plan['plan'][0]['split_off'][0]['document_ids'] == ['june', 'june-https']
    assert len(hub.event('jolts')['evidence']) == 4  # june-https folds into june; nothing moved
    assert (hub.store.get('configuration_versions', RESPLIT_KEY) or {}).get('events_split', 0) == 0


def test_events_merged_under_earlier_rules_are_split_once(hub):
    june, june_ingested = merged_jolts(hub)
    result = hub.split_merged_events(force=True)  # force: start-up may already have recorded this dedupe version
    assert (result['events_split'], result['events_created'], result['documents_moved']) == (1, 1, 2)
    jolts = hub.event('jolts')
    assert [e['document_id'] for e in jolts['evidence']] == ['july', 'july-x', 'july-copy']
    assert jolts['ai_disputed_evidence'] == ['e-july-x']
    [split] = [e for e in hub.events() if e.get('split_from') == 'jolts']
    assert split['summary_fact'].endswith('change little in June; hires unchanged')
    assert [e['document_id'] for e in split['evidence']] == ['june']
    assert split['evidence'][0]['duplicate_document_ids'] == ['june-https']  # the copy followed its page
    assert split['evidence'][0]['dedupe']['basis'] == 'split_from_merged_event'
    assert split['event_time'] == june.isoformat() and split['first_seen_utc'] == iso(june_ingested)
    with hub.store.session() as session:  # verification recomputed and stored for the new event
        assert session.get(Event, split['id']).verification_status == split['verification_status']
    audit = [a for a in hub.store.list('audit_logs') if a.get('action') == 'events_resplit']
    assert audit and audit[0]['plan'][0]['event_id'] == 'jolts'
    assert hub.split_merged_events()['skipped'] is True
    assert hub.split_merged_events(force=True)['documents_moved'] == 0  # nothing left that the rules would split


def test_the_split_script_prints_the_plan_in_plain_words(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('split_merged_events', ROOT / 'scripts' / 'split_merged_events.py')
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    settings = Settings(_env_file=None, database_url=f'sqlite:///{tmp_path}/script.db', enable_ingestion=False)
    store = Store(settings.database_url)
    store.initialize()
    hub = HubService(store, settings)
    hub.initialize()
    merged_jolts(hub)
    monkeypatch.setattr(script, 'get_settings', lambda: settings)
    monkeypatch.setattr(sys, 'argv', ['split_merged_events.py', '--dry-run', '--force'])
    assert script.main() == 0
    out = capsys.readouterr().out
    assert '-> separate event: Job openings and total separations change little in June; hires unchanged (2 documents)' in out
    assert 'would move 2 documents into 1 new event.' in out
    assert len(hub.event('jolts')['evidence']) == 4


def test_plain_text_decodes_only_complete_character_references():
    """Review finding: html.unescape also reads legacy entities without ';', so '&region=' in a URL became '®ion='."""
    from backend.providers.base import plain_text
    text = '<p>See https://example.org/a?id=1&region=US&amp;lang=en &copy_id kept &amp; done</p>'
    assert plain_text(text) == 'See https://example.org/a?id=1&region=US&lang=en &copy_id kept & done'
