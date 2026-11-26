from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend import briefs
from backend.config import Settings
from backend.database import Store, utcnow
from backend.schemas import Quote
from backend.service import HubService

NOW = datetime(2026, 7, 1, 9, tzinfo=UTC)
REGIME = {'regimes': ['MIXED'], 'posture': 'MIXED', 'confidence': None}


def asset(symbol, score=0.0, direction='WAIT', price=100.0, levels=None, kind='crypto'):
    return {'symbol': symbol, 'asset_class': kind,
            'quote': {'price': price, 'change_pct': 1.0, 'freshness_status': 'FRESH', 'market_status': 'LIVE'},
            'decision': {'score': score, 'direction': direction, 'levels': levels or {}, 'reasons': ['Required price confirmation absent'],
                         'confirmation': 'Break resistance', 'invalidation': 'Opposing break'}}


def event(event_id, status='CONFIRMED', kind='GEOPOLITICAL_ENERGY'):
    return {'id': event_id, 'summary_fact': f'Fact {event_id}', 'event_type': kind, 'verification_status': status,
            'fact_confidence': 95.0, 'sources': ['reuters'], 'first_seen_utc': NOW.isoformat()}


def test_morning_schedule_follows_bucharest_dst():
    assert briefs.morning_due(datetime(2026, 7, 1, 4, 29, tzinfo=UTC)) is None
    assert briefs.morning_due(datetime(2026, 7, 1, 4, 30, tzinfo=UTC)) == '2026-07-01'
    assert briefs.morning_due(datetime(2026, 1, 15, 5, 29, tzinfo=UTC)) is None
    assert briefs.morning_due(datetime(2026, 1, 15, 5, 30, tzinfo=UTC)) == '2026-01-15'


def test_first_cycle_is_baseline_and_material_changes_are_explicit():
    before = briefs.snapshot(NOW, [asset('BTC', 5, levels={'support': 90, 'resistance': 110})], [], [], REGIME)
    assert briefs.changes(None, before, [], []) == []
    assert briefs.changes(before, before, [], []) == []
    events = [event('e1'), event('e2', status='UNCONFIRMED')]
    calendar = [{'id': 'cpi', 'name': 'US CPI', 'impact': 10, 'actual': 3.4, 'consensus': 3.1, 'raw_surprise': 0.3}]
    after = briefs.snapshot(NOW, [asset('BTC', -12, 'BEARISH LEAN / WAIT', price=89, levels={'support': 90, 'resistance': 110})],
                            events, calendar, {'regimes': ['GEOPOLITICAL_SHOCK']})
    changed = briefs.changes(before, after, events, calendar)
    assert {c['trigger'] for c in changed} == {'MAJOR_EVENT', 'MACRO_RELEASE', 'REGIME_CHANGE', 'SCORE_CHANGE', 'LEVEL_BREAK'}
    assert not any('e2' in c['text'] for c in changed)
    small = dict(before, scores={'BTC': 10})
    assert not briefs.changes(before, small, [], [])


def test_compose_uses_local_times_and_omits_empty_sections():
    calendar = [{'id': 'a', 'name': 'US CPI', 'country': 'US', 'impact': 10, 'scheduled_at': '2026-07-01T12:30:00+00:00'},
                {'id': 'b', 'name': 'Low impact', 'country': 'US', 'impact': 1, 'scheduled_at': '2026-07-01T13:00:00+00:00'},
                {'id': 'c', 'name': 'Tomorrow', 'country': 'US', 'impact': 10, 'scheduled_at': '2026-07-02T12:30:00+00:00'}]
    result = briefs.compose('MORNING', NOW, [asset('BTC')], [event('e1'), event('e2', status='UNCONFIRMED')], calendar, REGIME)
    titles = [s['title'] for s in result['sections']]
    schedule = next(s for s in result['sections'] if s['title'].startswith("Today's schedule"))
    assert schedule['items'] == ['15:30 US CPI (US, impact 10)']
    # e1 (confirmed, causal trigger) is a major catalyst: it is stated once, under 'Top catalysts', and its topic
    # section is not repeated for it (each fact appears in one section).
    assert 'Top catalysts' in titles and 'Geopolitics' not in titles and 'Bonds' not in titles and 'Regulation' not in titles
    assert sum('Fact e1' in item for s in result['sections'] for item in s['items']) == 1
    assert 'What changed since last update' not in titles
    assert not any('Fact e2' in item for s in result['sections'] for item in s['items'])
    intraday = briefs.compose('INTRADAY', NOW, [asset('BTC')], [], [], REGIME, [{'trigger': 'REGIME_CHANGE', 'text': 'x'}])
    assert intraday['sections'][0] == {'title': 'What changed since last update', 'items': ['x']}


@pytest.fixture
def hub(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/hub.db')
    store.initialize()
    result = HubService(store, Settings(_env_file=None, enable_ingestion=False))  # never the real .env keys
    result.initialize()
    return result


@pytest.mark.asyncio
async def test_cycle_publishes_one_morning_brief_per_local_day(hub):
    hub.ingest_quote(Quote(symbol='BTC', price=100, timestamp=utcnow(), provider='binance', bid=99.9, ask=100.1))
    await hub.recalculate()
    morning = datetime(2026, 7, 1, 5, tzinfo=UTC)
    first = hub.publish_briefs(morning)
    again = hub.publish_briefs(morning + timedelta(hours=1))
    assert [b['kind'] for b in first] == ['MORNING'] and not any(b['kind'] == 'MORNING' for b in again)
    assert hub.store.get('briefs', 'morning:2026-07-01')['timezone'] == 'Europe/Bucharest'
    assert hub.dashboard()['regime']['posture'] == 'INSUFFICIENT_DATA'


def test_brief_and_regime_endpoints(tmp_path, monkeypatch):
    from backend.config import get_settings
    from backend.main import create_app
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/api.db')
    monkeypatch.setenv('ENABLE_INGESTION', 'false')
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        hub = client.app.state.hub
        hub.publish_briefs(datetime(2026, 7, 1, 5, tzinfo=UTC))
        # RI-5: the morning brief of an earlier day is never served as today's.
        old = client.get('/api/brief', params={'kind': 'MORNING'}).json()
        assert old['kind'] == 'MORNING' and old['generated_at'] is None and old['sections'] == []
        assert old['reason'].startswith('No morning brief for ')
        today = briefs.compose('MORNING', utcnow(), [], [], [], REGIME)
        hub.store.put('briefs', f"morning:{today['local_date']}", today)
        served = client.get('/api/brief', params={'kind': 'MORNING'}).json()
        assert served['generated_at'] == today['generated_at'] and served['format'] == briefs.BRIEF_FORMAT
        assert client.get('/api/brief', params={'kind': 'INTRADAY'}).json()['generated_at'] is None
        assert client.get('/api/brief', params={'kind': 'OTHER'}).status_code == 422
        assert client.get('/api/regime').json()['posture'] == 'INSUFFICIENT_DATA'
        assert client.get('/api/briefs').json()[0]['kind'] == 'MORNING'
    get_settings.cache_clear()


def test_topics_match_source_names_and_filler_is_excluded():
    # A partially confirmed Fed item with an asset impact is listed under its topic (matched by the source name).
    fed = dict(event('f1', status='PARTIALLY_CONFIRMED', kind='OFFICIAL_RELEASE'), sources=['Federal Reserve'], assets=['US2Y'])
    duplicate = dict(fed, id='f2')
    filler = dict(event('f3', status='PARTIALLY_CONFIRMED', kind='OFFICIAL_RELEASE'), summary_fact='Bank merger approval',
                  sources=['Federal Reserve'], extraction={'potential_importance': 10})
    unassessed = dict(event('f4', status='PARTIALLY_CONFIRMED', kind='OFFICIAL_RELEASE'), summary_fact='Monetary base (Sept.)',
                      sources=['Federal Reserve'])  # never assessed, no asset impact: not promoted
    result = briefs.compose('MORNING', NOW, [], [fed, duplicate, filler, unassessed], [], REGIME)
    central = next(s for s in result['sections'] if s['title'] == 'Central banks')
    assert central['items'] == ['Fact f1 (partially confirmed, 95% confidence)']


def config_with(**overrides):
    original = briefs._config

    def patched():
        return dict(original(), **overrides)
    return patched


def test_morning_brief_window_labels_late_and_skips_past_the_limit(monkeypatch):
    on_time = briefs.morning_schedule(datetime(2026, 7, 1, 5, 0, tzinfo=UTC))  # 08:00 Bucharest
    assert on_time['late'] is False and on_time['scheduled_local'] == '07:30'
    late = briefs.morning_schedule(datetime(2026, 7, 1, 6, 0, tzinfo=UTC))  # 09:00, past the 60 min window
    assert late['late'] is True and late['minutes_late'] == 90
    assert briefs.morning_due(datetime(2026, 7, 1, 18, 3, tzinfo=UTC)) is None  # 21:03: too late to publish
    brief = briefs.compose('MORNING', datetime(2026, 7, 1, 6, 0, tzinfo=UTC), [asset('BTC')], [], [], REGIME)
    assert brief['late'] is True and brief['scheduled_at'] == '2026-07-01T04:30:00+00:00'
    monkeypatch.setattr(briefs, '_config', config_with(morning_late_policy='skip'))
    assert briefs.morning_due(datetime(2026, 7, 1, 6, 0, tzinfo=UTC)) is None
    assert briefs.morning_due(datetime(2026, 7, 1, 5, 0, tzinfo=UTC)) == '2026-07-01'


def test_asset_sections_show_last_close_and_stale_prices_without_setups():
    # A live-class print of a closed venue is its LAST PRINT, as on the dashboard strip (the session state alone, e.g.
    # LAST CLOSE or FRIDAY CLOSE, is no longer used as the value's label: it said nothing about the value's own date).
    closed = {'symbol': 'SP500', 'asset_class': 'index', 'decision': {'score': 0, 'direction': 'WAIT', 'levels': {}},
              'quote': {'price': 5400.5, 'change_pct': -0.4, 'freshness_status': 'STALE', 'market_status': 'LAST CLOSE',
                        'timestamp': '2026-06-30T20:00:00+00:00'}}
    stale = {'symbol': 'US10Y', 'asset_class': 'bond', 'decision': None,
             'quote': {'price': 4.25, 'freshness_status': 'STALE', 'market_status': 'UNKNOWN',
                       'timestamp': '2026-06-30T21:00:00+00:00'}}
    live = asset('BTC', direction='LONG')
    result = briefs.compose('MORNING', NOW, [closed, stale, live, {'symbol': 'GOLD', 'asset_class': 'commodity', 'quote': None}],
                            [], [], REGIME)
    sections = {s['title']: s['items'] for s in result['sections']}
    assert sections['Equities'] == ['SP500 5,400.5 (-0.40%) · LAST PRINT as of 30 Jun 23:00']
    assert sections['Bonds'] == ['US10Y 4.25 · STALE as of 01 Jul 00:00']
    assert sections['Crypto'] == ['BTC 100 (+1.00%) · LIVE · LONG']
    assert 'Gold' not in sections  # no quote at all: omitted, never invented


def test_major_event_trigger_requires_importance_or_a_causal_trigger():
    base = briefs.snapshot(NOW, [], [], [], REGIME)
    events = [dict(event('low', kind='CENTRAL_BANK'), importance=0.2),
              dict(event('high', kind='CENTRAL_BANK'), importance=0.9),
              dict(event('extracted', kind='EARNINGS'), extraction={'potential_importance': 80}),
              event('causal'),  # GEOPOLITICAL_ENERGY has a causal rule
              event('plain', kind='NEWS')]
    after = briefs.snapshot(NOW, [], events, [], REGIME)
    texts = [c['text'] for c in briefs.changes(base, after, events, [])]
    assert texts == ['Newly confirmed: Fact causal', 'Newly confirmed: Fact extracted', 'Newly confirmed: Fact high']


def test_intraday_diffs_against_the_last_published_update_so_drift_accumulates():
    cycles = [briefs.snapshot(NOW, [asset('BTC', score)], [], [], REGIME) for score in (0, 8, 16)]
    changed, baseline = briefs.intraday(None, cycles[0], [], [])
    assert changed == [] and baseline is cycles[0]
    changed, baseline = briefs.intraday(baseline, cycles[1], [], [])
    assert changed == [] and baseline is cycles[0]  # +8 below threshold: nothing published, baseline kept
    changed, baseline = briefs.intraday(baseline, cycles[2], [], [])
    assert [c['trigger'] for c in changed] == ['SCORE_CHANGE'] and 'score 0 → 16' in changed[0]['text']
    assert baseline is cycles[2]
    assert briefs.changes(cycles[1], cycles[2], [], []) == []  # the old cycle-to-cycle diff missed it


def test_top_catalysts_include_confirmed_high_importance_events_without_causal_rules():
    fed = dict(event('fed', kind='CENTRAL_BANK'), summary_fact='Fed raised rates', importance=0.9)
    minor = dict(event('minor', kind='CENTRAL_BANK'), summary_fact='Fed minor note', importance=0.3)
    old = dict(event('old'), summary_fact='Old shock', first_seen_utc=(NOW - timedelta(days=3)).isoformat())
    result = briefs.compose('MORNING', NOW, [], [fed, minor, old], [], REGIME)
    catalysts = next(s for s in result['sections'] if s['title'] == 'Top catalysts')['items']
    assert catalysts[0].startswith('Fed raised rates') and not any('minor' in c for c in catalysts)
    assert any(c.startswith('Old shock') for c in catalysts)  # still listed, ranked after recent ones


def test_level_state_needs_a_clear_break_so_it_does_not_flap_at_the_level():
    levels = {'support': 95.0, 'resistance': 100.0, 'atr': 2.0}  # margin = max(0.1% of level, 0.25 ATR) = 0.5
    assert briefs.level_state(asset('BTC', price=100.3, levels=levels)) == 'INSIDE_RANGE'
    assert briefs.level_state(asset('BTC', price=100.6, levels=levels)) == 'ABOVE_RESISTANCE'
    assert briefs.level_state(asset('BTC', price=94.7, levels=levels)) == 'INSIDE_RANGE'
    assert briefs.level_state(asset('BTC', price=94.4, levels=levels)) == 'BELOW_SUPPORT'


def test_level_state_ignores_missing_levels_instead_of_failing():
    assert briefs.level_state(asset('BTC', price=90, levels={'support': None, 'resistance': 120})) == 'INSIDE_RANGE'
    assert briefs.level_state(asset('BTC', price=130, levels={'support': 95, 'resistance': None})) == 'INSIDE_RANGE'
    assert briefs.level_state(asset('BTC', price=90, levels={'support': 95, 'resistance': None})) == 'BELOW_SUPPORT'


# --- regime basis in briefs (display: labelled last-close basis on weekends; decisions stay live) ---------------------

LAST_CLOSE_REGIME = {'regimes': ['RISK_OFF'], 'posture': 'DEFENSIVE', 'confidence': 50.0, 'basis': 'last_close',
                     'basis_label': 'last close Fri 25 Sep, markets closed'}


def test_brief_states_the_regime_basis():
    brief = briefs.compose('MORNING', NOW, [asset('BTC')], [], [], LAST_CLOSE_REGIME)
    state = next(s for s in brief['sections'] if s['title'] == 'Global market state')['items']
    assert state[0] == 'Regime: RISK OFF · posture DEFENSIVE · rule coverage 50% · basis: last close Fri 25 Sep, markets closed'
    assert brief['regime'] == LAST_CLOSE_REGIME
    unlabelled = briefs.compose('MORNING', NOW, [asset('BTC')], [], [], REGIME)
    assert next(s for s in unlabelled['sections'] if s['title'] == 'Global market state')['items'][0] ==         'Regime: MIXED · posture MIXED'


def test_regime_change_names_a_basis_switch():
    live = briefs.snapshot(NOW, [], [], [], {'regimes': [], 'basis': 'live'})
    closed = briefs.snapshot(NOW, [], [], [], LAST_CLOSE_REGIME)
    assert [c['text'] for c in briefs.changes(live, closed, [], [])] == ['Regime unavailable → RISK_OFF (basis live → last close)']
    assert [c['text'] for c in briefs.changes(dict(closed, regimes=[]), closed, [], [])] == ['Regime unavailable → RISK_OFF']
    assert briefs.changes(dict(closed, regime_basis='live'), closed, [], []) == []  # same regimes: no change to report


def test_weekend_morning_brief_uses_the_labelled_last_close_regime(hub, monkeypatch):
    from backend import service as service_module
    from backend.database import Tick
    sunday = datetime(2026, 9, 27, 4, 45, tzinfo=UTC)  # 07:45 Bucharest
    thursday, friday = datetime(2026, 9, 24, 20, tzinfo=UTC), datetime(2026, 9, 25, 20, tzinfo=UTC)
    rows = [('SP500', 6500.0, thursday, 'fred', {'change_pct': -1.2}), ('NASDAQ', 21500.0, thursday, 'fred', {'change_pct': -1.5}),
            ('VIX', 22.0, thursday, 'fred', {'change_pct': 12.0}), ('US10Y', 4.1, friday, 'treasury', {'change_bp': -6.0})]
    with hub.store.session() as session:
        for symbol, price, stamp, provider, change in rows:
            payload = Quote(symbol=symbol, price=price, timestamp=stamp, provider=provider, daily_close=True,
                            data_class='DAILY_CLOSE', change_basis='previous_close', **change).model_dump(mode='json')
            session.add(Tick(symbol=symbol, price=price, timestamp=stamp, ingested_at=stamp, provider=provider, payload=payload))
        session.commit()
    monkeypatch.setattr(service_module, 'utcnow', lambda: sunday)
    morning = next(b for b in hub.publish_briefs(sunday) if b['kind'] == 'MORNING')
    assert morning['regime']['basis'] == 'last_close' and morning['regime']['posture'] == 'DEFENSIVE'
    state = next(s for s in morning['sections'] if s['title'] == 'Global market state')['items'][0]
    assert 'posture DEFENSIVE' in state and 'basis: last close' in state and state.endswith('markets closed')
    assert hub.regime()['basis'] == 'live' and hub.regime()['posture'] == 'INSUFFICIENT_DATA'  # what decisions read
