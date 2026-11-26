"""Deterministic event-risk engine (docs/EVENT_AWARENESS.md §2-§4). Synthetic calendar rows in the provider row
contract, published-decision asset entries, regimes and news events shaped like HubService outputs; no network and no
real .env (tests/conftest.py blanks every key). The helpers below are shared with test_event_aware_cases.py."""
import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import event_risk as er
from backend.config import get_settings, rules
from backend.database import Store, Tick, utcnow
from backend.providers.calendars import calendar_row
from backend.sessions import session_info

KINDS = {'BTC': 'crypto', 'ETH': 'crypto', 'SP500': 'index', 'NASDAQ': 'index', 'DXY': 'index', 'US2Y': 'bond',
         'US10Y': 'bond', 'EURUSD': 'fx', 'USDJPY': 'fx', 'GOLD': 'commodity', 'WTI': 'commodity', 'BRENT': 'commodity',
         'AAPL': 'equity', 'COIN': 'equity'}
NOW = datetime(2026, 10, 28, 17, 30, tzinfo=UTC)  # Wednesday; FOMC 2026-10-28 18:00 UTC = 20:00 Bucharest


# --- shared helpers ------------------------------------------------------------------------------------------------------

def fomc(at: datetime, **extra) -> dict:
    return calendar_row('FOMC rate decision', 'US', 'CENTRAL_BANK', at, 10, 'fomc_calendar',
                        'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm', **extra)


def cpi(at: datetime, **extra) -> dict:
    return calendar_row('Consumer Price Index', 'US', 'MACRO', at, 10, 'bls_calendar', time_known=True,
                        time_basis='BLS release schedule (ICS)', **extra)


def earnings(symbol: str, at: datetime, **extra) -> dict:
    return calendar_row(f'{symbol} earnings', 'US', 'EARNINGS', at, 6, 'finnhub_earnings', symbol=symbol, time_known=False,
                        time_basis='after close (amc); exact minute not published', **extra)


def decision(category: str, score: float, **extra) -> dict:
    return {'category': category, 'direction': category, 'score': score, 'audit_status': 'PASS',
            'confirmation': 'holds above resistance', 'invalidation': 'breaks below support', 'why': ['fact'], **extra}


def asset(symbol: str, now: datetime, published: dict | None = None, change: float | None = None) -> dict:
    kind = KINDS.get(symbol, 'index')
    info = session_info(kind, now, symbol)
    quote = {'price': 100.0, 'timestamp': now.isoformat(), 'session': info, 'market_state': info['state'],
             'freshness_status': 'FRESH'}
    if change is not None:
        quote['change_bp' if kind == 'bond' else 'change_pct'] = change
    return {'symbol': symbol, 'name': symbol, 'asset_class': kind, 'quote': quote, 'decision': published, 'session': info}


def regime(regimes: list[str], posture: str, drivers: dict[str, str] | None = None,
           features: dict[str, dict] | None = None, confidence: float = 80.0) -> dict:
    moving = drivers or {}
    return {'regimes': regimes, 'posture': posture, 'confidence': confidence, 'features': features or {},
            'basis': 'live', 'basis_label': 'live intraday',
            'evaluated': [{'regime': r, 'state': 'ACTIVE', 'confidence': confidence} for r in regimes],
            'drivers': [{'driver': name, 'direction': moving.get(name, 'flat'), 'arrow': None, 'inputs': [], 'missing': []}
                        for name in ('Rates', 'USD', 'Oil', 'Risk appetite')]}


RISK_ON = regime(['RISK_ON'], 'CONSTRUCTIVE', {'Rates': 'down', 'USD': 'down', 'Risk appetite': 'up'},
                 {'BTC': {'price': 100.0, 'change_pct': 2.0}, 'NASDAQ': {'price': 100.0, 'change_pct': 1.0},
                  'SP500': {'price': 100.0, 'change_pct': 0.8}})


def news(event_id: str, summary: str, status: str, first_seen: datetime, *, trigger: str | None = None,
         event_type: str = 'NEWS', sources: list[str] | None = None) -> dict:
    from backend import causal
    return {'id': event_id, 'event_id': event_id, 'summary_fact': summary, 'event_type': event_type,
            'verification_status': status, 'fact_confidence': 95.0 if status == 'CONFIRMED' else 20.0,
            'first_seen_utc': first_seen.isoformat(), 'last_updated_utc': first_seen.isoformat(),
            'event_time': first_seen.isoformat(), 'trigger': trigger, 'assets': [],
            'affected_assets': causal.affected_assets(trigger) if trigger else [], 'importance': 0.9,
            'sources': sources or ['Reuters'], 'evidence': []}


def statement(news_id: str, title: str, published: datetime, *, source_id: str = 'fed', official: bool = True,
              status: str = 'CONFIRMED', ingested: datetime | None = None, trigger: str | None = None) -> dict:
    """A news event carrying one official document (the Ingestion.event evidence shape)."""
    evidence = {'id': f'ev-{news_id}', 'document_id': f'doc-{news_id}', 'source_id': source_id, 'source': source_id.upper(),
                'url': f'https://example.org/{news_id}', 'title': title, 'excerpt': 'Official text.',
                'published_at': published.isoformat(), 'ingested_at': (ingested or published).isoformat(),
                'relation': 'supports', 'official': official, 'direct': True, 'kind': 'central_bank', 'group': source_id}
    return dict(news(news_id, title, status, published, trigger=trigger, event_type='CENTRAL_BANK', sources=[source_id]),
                evidence=[evidence])


def make_hub(tmp_path) -> SimpleNamespace:
    store = Store(f'sqlite:///{tmp_path}/event_risk.db')
    store.initialize()
    return SimpleNamespace(store=store, settings=SimpleNamespace(quote_stale_seconds=15))


def tick(store: Store, symbol: str, at: datetime, price: float, provider: str = 'binance') -> None:
    with store.session() as session:
        session.add(Tick(symbol=symbol, price=price, timestamp=at, ingested_at=at, provider=provider, payload={}))
        session.commit()


def price_path(store: Store, symbol: str, t0: datetime, before: float, after: dict[int, float]) -> None:
    """Baseline 30 s before t0, then one tick 10 s before each window end (minutes -> price)."""
    tick(store, symbol, t0 - timedelta(seconds=30), before)
    for minutes, price in after.items():
        tick(store, symbol, t0 + timedelta(minutes=minutes) - timedelta(seconds=10), price)


def snap(hub, now, calendar=(), news_events=(), assets=(), regime_=None, interpretation=None) -> dict:
    return er.snapshot(hub, now, assets=list(assets), events=list(news_events), calendar=list(calendar), regime=regime_,
                       interpretation=interpretation)


def by_id(events: list[dict], prefix: str) -> dict:
    return next(e for e in events if e['event_id'].startswith(prefix))


# --- configuration ---------------------------------------------------------------------------------------------------------

def test_versioned_config_validates_and_rejects_unknown_assets():
    config = rules()
    assert config['event_risk_version'] and config['event_exposure_version']
    er.validate(config)
    broken = copy.deepcopy(config)
    broken['event_exposure']['exposure']['FOMC']['NOT_AN_ASSET'] = 'high'
    with pytest.raises(ValueError, match='unknown asset'):
        er.validate(broken)
    broken = copy.deepcopy(config)
    broken['event_exposure']['scenario_templates']['fed_policy']['scenarios'][1]['trigger'] = 'NO_SUCH_TRIGGER'
    with pytest.raises(ValueError, match='no causal rule'):
        er.validate(broken)
    broken = copy.deepcopy(config)
    broken['event_risk']['relevance']['weights']['importance'] = 0.5
    with pytest.raises(ValueError, match='sum to 1'):
        er.validate(broken)


def test_exposure_levels_come_from_config_and_earnings_template():
    fed = {a['symbol']: (a['level'], a['weight']) for a in er.exposure('FOMC')}
    assert fed['US2Y'] == ('very_high', 1.0) and fed['BTC'] == ('high', 0.75) and 'WTI' not in fed
    apple = {a['symbol']: a['level'] for a in er.exposure('EARNINGS', 'AAPL')}
    assert apple == {'AAPL': 'very_high', 'NASDAQ': 'high', 'SP500': 'medium', 'BTC': 'low'}
    coin = {a['symbol']: a['level'] for a in er.exposure('EARNINGS', 'COIN')}
    assert coin['BTC'] == 'medium' and coin['ETH'] == 'medium'
    hormuz = {a['symbol']: a['level'] for a in er.exposure('GEO_ENERGY')}
    assert hormuz['BRENT'] == hormuz['WTI'] == 'very_high' and hormuz['DXY'] == 'medium'


def test_rows_are_classified_by_taxonomy():
    assert er.classify_row(fomc(NOW)) == 'FOMC'
    assert er.classify_row(cpi(NOW)) == 'US_CPI'
    assert er.classify_row(calendar_row('9-Year 10-Month Note auction', 'US', 'TREASURY_AUCTION', NOW, 6, 'treasury_auctions')) \
        == 'UST_AUCTION_10Y'
    assert er.classify_row(calendar_row('Weekly Petroleum Status Report', 'US', 'MACRO', NOW, 6, 'eia_wpsr_calendar')) == 'EIA_WPSR'
    assert er.classify_row(calendar_row('Initial Jobless Claims', 'US', 'MACRO', NOW, 6, 'dol_rule')) == 'US_CLAIMS'
    assert er.classify_row(calendar_row('FOMC Minutes', 'US', 'CENTRAL_BANK', NOW, 6, 'fed_calendar')) == 'FOMC_MINUTES'
    assert er.classify_row(calendar_row('26-Week Bill auction', 'US', 'TREASURY_AUCTION', NOW, 3, 'treasury_auctions')) == 'OTHER'
    assert er.classify_row(dict(fomc(NOW), event_class='FOMC_MINUTES')) == 'FOMC_MINUTES'  # the row's own class wins


# --- phases ------------------------------------------------------------------------------------------------------------------

def test_fomc_phases_never_resolve_before_the_press_conference_ends():
    t0 = NOW
    marks = er.timeline(t0, er._windows(er.class_config('FOMC')), [t0 + timedelta(minutes=30)])
    expected = [(-400, 'SCHEDULED'), (-300, 'APPROACHING'), (-10, 'IMMINENT'), (1, 'RELEASED'), (20, 'IN_PROGRESS'),
                (80, 'IN_PROGRESS'), (95, 'POST_EVENT'), (30 + 60 + 121, 'RESOLVED')]
    assert [(m, er.phase_at(t0 + timedelta(minutes=m), marks)) for m, _ in expected] == expected
    assert marks['in_progress_end'] == t0 + timedelta(minutes=90)  # press conference 14:30 ET + 60 min


def test_date_only_rows_never_become_imminent_in_progress_or_risk_mode(tmp_path):
    row = fomc(NOW + timedelta(minutes=10), time_known=False, time_basis='date only')
    seen = set()
    for minutes in range(-60 * 26, 60 * 30, 5):
        built = er.build_events([row], [], NOW + timedelta(minutes=minutes), regime=RISK_ON)
        seen |= {e['phase'] for e in built['scheduled']}
        for event in built['scheduled']:
            assert event['minutes_until_event'] is None and event['time_bucharest'] is None and not event['sub_events']
    # After its day (New York) the row is POST_EVENT, then RESOLVED: never IMMINENT, RELEASED or IN_PROGRESS.
    assert seen <= {'SCHEDULED', 'APPROACHING', 'POST_EVENT'} and {'APPROACHING', 'POST_EVENT'} <= seen
    result = snap(make_hub(tmp_path), NOW, [row], regime_=RISK_ON)
    fed = result['events'][0]
    assert not result['mode']['active'] and 'EXACT TIME UNKNOWN' in fed['data_incomplete']
    assert er.trigger_state(result)['windows'][fed['event_id']] is None
    assert fed['status_note'].endswith('(time TBA)')


def test_decision_and_press_conference_rows_form_one_event_with_a_sub_event():
    decision_at = NOW + timedelta(hours=2)
    rows = [calendar_row('ECB monetary policy decision', 'EU', 'CENTRAL_BANK', decision_at, 9, 'ecb_calendar', bank='ecb',
                         time_known=True, time_basis='configured 14:15 CET'),
            calendar_row('ECB monetary policy press conference', 'EU', 'CENTRAL_BANK', decision_at + timedelta(minutes=30), 8,
                         'ecb_calendar', bank='ecb', time_known=True, time_basis='configured 14:45 CET')]
    events = er.build_events(rows, [], NOW)['scheduled']
    assert len(events) == 1 and events[0]['event_id'] == f"ECB-{decision_at:%Y%m%d}-ECB"
    sub = events[0]['sub_events'][0]
    assert sub['sub_event'] == 'PRESS_CONFERENCE' and sub['row_id'] == rows[1]['id']
    during = er.build_events(rows, [], decision_at + timedelta(minutes=10))['scheduled'][0]
    assert during['phase'] == 'IN_PROGRESS' and 'next risk: press conference' in during['status_note']


def test_linked_press_conference_row_is_used_instead_of_the_configured_offset():
    decision_row = fomc(NOW + timedelta(hours=1), event_class='FOMC', sub_event='DECISION', time_known=True, time_source='config')
    press = calendar_row('FOMC press conference', 'US', 'CENTRAL_BANK', NOW + timedelta(hours=1, minutes=30), 9, 'fomc_calendar',
                         event_class='FOMC', sub_event='PRESS_CONFERENCE', parent_event_id=decision_row['id'], time_known=True,
                         time_basis="Chair's press conference 2:30 p.m. ET (configured)", duplicates=['fed_events_calendar'])
    events = er.build_events([press, decision_row], [], NOW)['scheduled']
    assert len(events) == 1 and events[0]['row_id'] == decision_row['id']
    assert [(s['sub_event'], s['row_id']) for s in events[0]['sub_events']] == [('PRESS_CONFERENCE', press['id'])]


def test_fomc_without_a_press_conference_row_gets_a_labelled_configured_sub_event():
    event = er.build_events([fomc(NOW + timedelta(hours=1))], [], NOW)['scheduled'][0]
    sub = event['sub_events'][0]
    assert sub['sub_event'] == 'PRESS_CONFERENCE' and 'configured offset' in sub['time_basis']
    assert sub['time_utc'] == (NOW + timedelta(hours=1, minutes=30)).isoformat()


def test_same_release_from_two_rows_is_deduplicated_preferring_the_exact_time():
    timed = fomc(datetime(2026, 10, 28, 18, 0, tzinfo=UTC))
    dated = calendar_row('FOMC Meeting', 'US', 'CENTRAL_BANK', datetime(2026, 10, 28, 4, 0, tzinfo=UTC), 10, 'fed_calendar',
                         time_known=False, time_basis='date only')
    events = er.build_events([dated, timed], [], NOW)['scheduled']
    assert len(events) == 1 and events[0]['time_known'] and events[0]['row_id'] == timed['id']
    assert events[0]['source_ids'] == ['fomc_calendar', 'fed_calendar']


def test_cancelled_rows_are_dropped():
    assert er.build_events([fomc(NOW + timedelta(hours=1), status='CANCELLED')], [], NOW)['scheduled'] == []


# --- scores ------------------------------------------------------------------------------------------------------------------

def test_relevance_rises_as_the_event_approaches_and_levels_follow_the_bands():
    scores = [er.build_events([fomc(NOW + timedelta(minutes=m))], [], NOW, regime=RISK_ON)['scheduled'][0]
              for m in (3 * 1440, 360, 30)]
    values = [e['relevance_score'] for e in scores]
    assert values == sorted(values) and values[0] < values[-1]
    assert scores[-1]['relevance_level'] == 'CRITICAL' and scores[-1]['impact_10'] == round(values[-1] / 10, 1)
    assert er.level_for(95) == 'CRITICAL' and er.level_for(80) == 'VERY HIGH' and er.level_for(65) == 'HIGH'
    assert er.level_for(45) == 'MODERATE' and er.level_for(10) == 'LOW'
    assert scores[0]['dominance_score'] <= 75 < scores[-1]['dominance_score']


def test_missing_components_are_named_and_renormalised_never_scored_zero():
    other = calendar_row('Some unclassified release', 'US', 'MACRO', NOW + timedelta(minutes=30), 5, 'calendar')
    event = er.build_events([other], [], NOW)['scheduled'][0]
    assert event['event_class'] == 'OTHER' and event['relevance_missing'] == ['expected_volatility']
    parts = event['relevance_components']
    weights = rules()['event_risk']['relevance']['weights']
    measured = {k: v for k, v in parts.items() if v is not None}
    expected = sum(weights[k] * v for k, v in measured.items()) / sum(weights[k] for k in measured)
    assert event['relevance_score'] == round(expected, 1) and parts['importance'] == 50


def test_missing_expectations_are_explicit_and_critical_events_flag_incomplete_data(tmp_path):
    result = snap(make_hub(tmp_path), NOW, [fomc(NOW + timedelta(minutes=30))], regime_=RISK_ON)
    fed = result['next_event']
    assert fed['consensus'] == 'NO CONSENSUS' and fed['market_pricing'] == 'UNKNOWN'
    assert fed['data_incomplete'] == ['NO CONSENSUS', 'MARKET PRICING UNKNOWN']
    flagged = result['data_incomplete'][0]
    assert flagged['event_id'] == fed['event_id'] and flagged['flag'] == 'EVENT DATA INCOMPLETE'
    assert fed['relevance_components']['pricing_uncertainty'] == 50  # configured default, not a measurement


def test_pricing_uncertainty_uses_explicit_probabilities_only():
    assert er.pricing_uncertainty('UNKNOWN')[0] == 50
    assert er.pricing_uncertainty({'hike_probability': 0.95, 'hold_probability': 0.05})[0] == 10
    assert er.pricing_uncertainty({'hike_probability': 0.5, 'hold_probability': 0.5})[0] == 100


def test_adjusted_confidence_reproduces_the_spec_example_and_never_edits_the_decision():
    published = decision('LONG SETUP', 54)
    event = {'event_id': 'X', 'name': 'X', 'event_class': 'FOMC', 'timing': 'SCHEDULED', 'phase': 'SCHEDULED',
             'time_known': True, 'minutes_until_event': 600, 'time_utc': (NOW + timedelta(minutes=600)).isoformat(),
             'relevance_score': 92.0, 'affected_assets': [{'symbol': 'BTC', 'level': 'very_high', 'weight': 1.0}],
             'symbol': None}
    before = copy.deepcopy(published)
    overlay = er.trade_status('BTC', published, [event], NOW)
    assert overlay['adjusted_confidence'] == 42.3 and overlay['score'] == 92.0  # 54 x (1 - 0.867 x 0.25)
    assert overlay['trade_status'] == 'CONDITIONAL LONG'  # event risk >= conditional threshold, not within the wait window
    assert published == before


def test_trade_status_bands():
    assert er.trade_status('BTC', None, [], NOW)['trade_status'] == 'WAIT'
    assert er.trade_status('BTC', decision('WAIT', 3), [], NOW)['trade_status'] == 'WAIT'
    assert er.trade_status('BTC', decision('LONG SETUP', 40), [], NOW)['trade_status'] == 'LONG'
    assert er.trade_status('BTC', decision('SHORT SETUP', -40), [], NOW)['trade_status'] == 'SHORT'
    lean = er.trade_status('BTC', decision('BULLISH LEAN / WAIT', 20), [], NOW)
    assert lean['trade_status'] == 'CONDITIONAL LONG' and 'lean' in lean['status_reason']
    weak = er.trade_status('BTC', decision('LONG SETUP', 40, market_confirmation=30), [], NOW)
    assert weak['trade_status'] == 'CONDITIONAL LONG' and 'market confirmation 30' in weak['status_reason']


def test_scenarios_take_directions_from_the_causal_graph_and_are_only_possible():
    event = er.build_events([fomc(NOW + timedelta(hours=2))], [], NOW)['scheduled'][0]
    scenarios = {s['id']: s for s in event['scenarios']}
    assert set(scenarios) == {'A', 'B', 'C', 'D'} and event['possible_surprises'] == ['A', 'B', 'C', 'D']
    assert scenarios['B']['expected']['US2Y'] == 1 and scenarios['B']['expected']['NASDAQ'] == -1
    assert scenarios['C']['expected']['DXY'] == -1 and scenarios['C']['expected']['BTC'] == 1
    assert scenarios['A']['expected'] == {} and 'reason matters' in scenarios['D']['note'].lower()
    assert all(s['likelihood'] == 'possible' for s in scenarios.values())
    assert set(scenarios['B']['expected']) <= {a['symbol'] for a in event['affected_assets']}
    hot = er.build_events([cpi(NOW + timedelta(hours=2))], [], NOW)['scheduled'][0]
    assert {s['id']: s['trigger'] for s in hot['scenarios']}['B'] == 'INFLATION_HOT'


def test_trigger_state_buckets_windows_and_names_sub_event_phases(tmp_path):
    rows = [fomc(NOW + timedelta(minutes=10)), cpi(NOW + timedelta(minutes=45))]
    result = snap(make_hub(tmp_path), NOW, rows, regime_=RISK_ON)
    state = er.trigger_state(result)
    fed, hot = by_id(result['events'], 'FED-'), by_id(result['events'], 'BLS-')
    assert state['windows'][fed['event_id']] == 'W15' and state['windows'][hot['event_id']] == 'W60'
    assert state['phases'][fed['event_id']] == 'IMMINENT'
    assert state['phases'][f"{fed['event_id']}:PRESS_CONFERENCE"] == 'APPROACHING'
    assert state['risk_mode'] is True and state['posture'] == 'CONSTRUCTIVE'
    assert fed['event_id'] in state['critical_ids'] and state['dominant_event_id'] == fed['event_id']
    assert set(state) >= {'phases', 'windows', 'risk_mode', 'dominant_event_id', 'posture', 'critical_ids'}


def test_scheduled_reactions_use_the_price_just_before_the_release(tmp_path):
    hub = make_hub(tmp_path)
    t0 = NOW - timedelta(minutes=20)
    tick(hub.store, 'US2Y', t0 - timedelta(minutes=10), 4.10)  # older tick, not the baseline
    price_path(hub.store, 'US2Y', t0, 3.90, {1: 3.89, 5: 3.86, 15: 3.84})
    rows = er.analytics.scheduled_reactions(hub.store, 'E', t0, ['US2Y'], {'US2Y': 1}, NOW, labels=['1m', '5m', '15m', '30m'],
                                            baseline_seconds=120, gap_seconds=90)
    assert [r['horizon'] for r in rows] == ['1m', '5m', '15m']  # 30m has not ended
    assert rows[-1]['baseline_price'] == 3.90 and rows[-1]['change'] == pytest.approx(-6.0) and rows[-1]['unit'] == 'bp'
    assert rows[-1]['status'] == 'DIVERGENT'


def test_history_switches_the_dominance_basis_from_prior_to_stored(tmp_path):
    hub = make_hub(tmp_path)
    spec = er.class_config('US_CPI')
    assert er.historical_impact('US_CPI', spec, er.history(hub.store))[1].startswith('configured prior')
    prefix = rules()['event_risk']['dominance']['history_prefix']
    hub.store.put_many('event_relationships', {f'{prefix}e{i}': {'event_class': 'US_CPI', 'impact': 40.0} for i in range(8)})
    value, basis = er.historical_impact('US_CPI', spec, er.history(hub.store))
    assert value == 40.0 and basis == 'stored reactions (n=8)'


# --- API ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/api.db')
    monkeypatch.setenv('ENABLE_INGESTION', 'false')
    monkeypatch.setenv('ADMIN_TOKEN', 'test-admin-token')
    get_settings.cache_clear()
    from backend.main import create_app
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def test_api_serves_the_event_risk_block_upcoming_events_and_asset_overlays(client):
    hub = client.app.state.hub
    now = utcnow()
    rows = [fomc(now + timedelta(minutes=40)),
            calendar_row('13-Week Bill auction', 'US', 'TREASURY_AUCTION', now + timedelta(days=2), 3, 'treasury_auctions',
                         time_known=True, time_basis='competitive close (TreasuryDirect)')]
    hub.store.put_many('macro_calendar', {r['id']: r for r in rows})
    body = client.get('/api/dashboard').json()
    risk = body['event_risk']
    assert 'interpretation' in body and risk['next_event']['event_id'].startswith('FED-')
    assert {'generated_at', 'markets_open', 'mode', 'next_event', 'dominant_event', 'events', 'active_events',
            'potential_events', 'asset_event_risk', 'favored', 'pressured', 'non_confirming', 'what_can_break',
            'trade_watch', 'post_event', 'data_incomplete'} <= set(risk)
    assert all(e['relevance_score'] >= 40 for e in risk['events'])
    assert body['assets'][0]['event_risk'] == risk['asset_event_risk'][body['assets'][0]['symbol']]
    listed = client.get('/api/events/upcoming', params={'min_relevance': 0}).json()
    assert len(listed) == 2 and any(e['relevance_score'] < 40 for e in listed)  # the bill auction is hidden on the dashboard
    assert len(client.get('/api/events/upcoming', params={'min_relevance': 40}).json()) == 1
    alone = client.get('/api/event-risk').json()
    assert alone['next_event']['event_id'] == risk['next_event']['event_id']
    entries = client.get('/api/assets').json()
    assert {'score', 'level', 'event_id', 'minutes_until', 'trade_status', 'adjusted_confidence'} <= set(entries[0]['event_risk'])
    assert client.get('/api/events/not-an-id').status_code == 404
    json.dumps(risk)  # serialisable as served
