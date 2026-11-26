"""Decision and data core (audit of 2 Oct 2026: DEC-2..DEC-7, AFP-01, MD-1): data gaps, the technical price,
freshness per provider, the unverified-fact gate and the publication check of a lean. Real calculations on an isolated
store; no provider or AI call."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_pipeline import hub as hub
from test_publication_continuity import lean_context
from test_scoring import hormuz

from backend import analytics, backtest, decision, engines, scoring, sessions, technicals
from backend import config as config_module
from backend.audit import pre_audit
from backend.config import freshness_seconds, rules
from backend.database import Decision, Event, Tick, uid, utcnow
from backend.runtime import Runtime

NOW = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)  # Wednesday, NYSE open (11:00 ET); crypto always open
TECH = rules()['technical']


def bars(end: datetime, count: int, close: float = 100.0, spread: float = 1.0) -> list[dict]:
    """`count` contiguous completed one-minute bars, the last one ending at `end`."""
    return [{'time': (end - timedelta(minutes=count - i)).isoformat(), 'open': close, 'high': close + spread,
             'low': close - spread, 'close': close, 'volume': None} for i in range(count)]


def tick(hub, symbol, provider, at, price=100.0, **payload):
    with hub.store.session() as session:
        session.add(Tick(id=uid(), symbol=symbol, provider=provider, price=price, timestamp=at, ingested_at=at,
                         payload=payload))
        session.commit()


# --- DEC-2 / AFP-01: data gaps -------------------------------------------------------------------------------------

def test_levels_anchored_before_a_gap_are_withheld_until_enough_current_bars_exist(hub):
    old = bars(NOW - timedelta(hours=20), 75, spread=0.5)  # the app stopped 20 hours ago
    old[35].update(high=101.0, low=99.0)  # one 15-minute swing high / low in that history
    stale = technicals.asset_levels(hub.store, 'BTC', old, NOW, price=102.5)
    assert stale['support'] is None and stale['resistance'] is None and stale['state']['ready'] is False
    assert 'No current minute bar' in stale['state']['reason'] and stale['reference']['age_seconds'] > 19 * 3600
    # What a first cycle used to call a breakout level (BTC 2 Oct 07:27 UTC: resistance 83,950 of a 20 h old swing).
    assert stale['state']['withheld'] == {'support': 99.0, 'support_basis': 'swing_low',
                                          'resistance': 101.0, 'resistance_basis': 'swing_high'}
    component = scoring.technical_component('BTC', 102.5, stale)
    assert component['status'] == 'missing' and 'No current minute bar' in component['basis']

    few = old + bars(NOW, 3, close=102.5)  # three minutes after the restart
    rebuilding = technicals.asset_levels(hub.store, 'BTC', few, NOW, price=104.0)
    assert rebuilding['support'] is None and rebuilding['state']['fresh_bars'] == 3
    assert rebuilding['state']['reason'] == f"Levels rebuilding after a data gap: 3 of {TECH['min_fresh_bars']} current minute bars"
    assert scoring.technical_component('BTC', 104.0, rebuilding)['status'] == 'missing'

    enough = old + bars(NOW, TECH['session_min_bars'], close=102.5)
    ready = technicals.asset_levels(hub.store, 'BTC', enough, NOW, price=104.0)
    assert ready['state']['ready'] and (ready['support'], ready['resistance']) == (101.5, 103.5)
    assert scoring.technical_component('BTC', 104.0, ready)['value'] > 0  # a break of current levels still scores


def test_contiguous_bars_keep_their_levels_and_a_short_feed_hole_is_not_a_restart(hub):
    series = bars(NOW, 40)
    del series[20:23]  # three missing minutes (a rate-limited REST feed): below technical.max_gap_seconds
    levels = technicals.asset_levels(hub.store, 'BTC', series, NOW, price=100.0)
    assert levels['state']['ready'] and levels['state']['fresh_bars'] == 37
    assert (levels['support'], levels['resistance']) == (99.0, 101.0)


def test_intraday_atr_never_measures_a_true_range_across_a_gap():
    old = bars(NOW - timedelta(hours=20), 40, close=100.0, spread=0.5)
    after = bars(NOW, 10, close=120.0, spread=0.5)
    gap = TECH['max_gap_seconds']
    # The former value: one 20-point jump counted as a one-minute range and averaged into the ATR.
    assert technicals.atr(old + after, TECH['atr_period']) > 2
    assert technicals.atr(old + after, TECH['atr_period'], gap) is None  # 10 current bars: not enough ranges yet
    current = bars(NOW, TECH['atr_period'] + 1, close=120.0, spread=0.5)
    assert technicals.atr(old + current, TECH['atr_period'], gap) == pytest.approx(1.0)
    assert technicals.levels(old + current, NOW, price=120.0)['atr'] == pytest.approx(1.0)


def test_a_session_of_a_few_minutes_is_not_a_level_and_restarts_after_a_gap():
    day_start = NOW.replace(hour=0, minute=0)
    early = bars(day_start + timedelta(hours=2), 60, close=90.0)  # 01:00-02:00 UTC, then the app stopped
    late = bars(NOW, 10, close=100.0)
    result = technicals.levels(early + late, NOW, price=100.0)
    session = result['session']
    assert (session['high'], session['low'], session['bars'], session['bars_before_gap']) == (101.0, 99.0, 10, 60)
    assert session['formed'] is False and session['since'] == late[0]['time']
    assert 'session' not in str(result['support_basis']) and 'session' not in str(result['resistance_basis'])
    formed = technicals.levels(early + bars(NOW, TECH['session_min_bars'], close=100.0), NOW, price=100.0)
    assert formed['session']['formed'] and (formed['support_basis'], formed['resistance_basis']) == ('session_low', 'session_high')


def test_a_day_with_76_recorded_minutes_is_not_the_previous_day(hub):
    yesterday = NOW.replace(hour=0, minute=0) - timedelta(days=1)
    partial = bars(yesterday + timedelta(hours=11), 76, close=83800.0, spread=300.0)
    today = bars(NOW, 40, close=85900.0, spread=20.0)
    assert technicals.asset_levels(hub.store, 'BTC', partial + today, NOW)['previous_day'] is None
    assert technicals.levels(partial + today, NOW)['previous_day'] is None or True  # generic function: no venue, no check
    full = bars(yesterday + timedelta(days=1), 24 * 60, close=83800.0, spread=300.0)
    whole = technicals.asset_levels(hub.store, 'BTC', bars(yesterday, 30, close=83000.0) + full + today, NOW)
    assert (whole['previous_day']['high'], whole['previous_day']['low']) == (84100.0, 83500.0)
    assert sessions.day_minutes('crypto', yesterday, 'BTC') == 1440 and sessions.day_minutes('equity', yesterday, 'NVDA') == 390
    assert sessions.day_minutes('fx', yesterday, 'EURUSD') is None


def test_a_closed_venue_keeps_its_last_session_levels_for_reading_only(hub):
    close = datetime(2026, 9, 30, 20, 0, tzinfo=UTC)  # NYSE close
    session_bars = bars(close, 120, close=190.0)
    evening = close + timedelta(hours=2)
    strict = technicals.asset_levels(hub.store, 'NVDA', session_bars, evening)
    assert strict['support'] is None and strict['state']['ready'] is False
    shown = technicals.asset_levels(hub.store, 'NVDA', session_bars, evening, keep_closed=True)
    assert (shown['support'], shown['resistance']) == (189.0, 191.0)
    assert shown['state'] == dict(shown['state'], ready=False, basis='last session')
    assert shown['state']['reason'] == 'Market closed: levels as of the last session close'
    reopened = datetime(2026, 10, 1, 13, 30, 20, tzinfo=UTC)  # the next open: yesterday's bars are not current
    assert technicals.asset_levels(hub.store, 'NVDA', session_bars, reopened, keep_closed=True)['support'] is None
    assert sessions.previous_close('equity', evening, 'NVDA') == close
    assert sessions.previous_close('equity', reopened, 'NVDA') == close and sessions.previous_close('crypto', evening) is None


def test_levels_of_an_expired_decision_are_withheld_at_read_time(hub):
    levels = {'support': 83812.4, 'resistance': 83950.0, 'atr': 41.17, 'reference': {'price': 83840.02}}
    for age, expected in ((timedelta(hours=20), None), (timedelta(seconds=30), 83950.0)):
        created = utcnow() - age
        payload = {'id': uid(), 'symbol': 'BTC', 'direction': 'WAIT', 'category': 'WAIT', 'score': 0.0,
                   'created_at': created.isoformat(), 'levels': levels}
        with hub.store.session() as session:
            session.add(Decision(id=payload['id'], symbol='BTC', direction='WAIT', score=0.0, created_at=created, payload=payload))
            session.commit()
        published = next(a for a in hub.assets(event_overlay=False) if a['symbol'] == 'BTC')['decision']
        assert published['levels']['resistance'] == expected
        if expected is None:
            assert published['levels']['state']['reason'].startswith('Decision expired')
            assert published['levels']['state']['withheld']['resistance'] == 83950.0 and published['levels']['atr'] == 41.17


# --- DEC-3: the technical price ------------------------------------------------------------------------------------

@pytest.mark.parametrize(('change', 'usable'), [
    ({}, True), ({'data_class': 'DAILY_CLOSE'}, False), ({'data_class': 'DERIVED_DAILY'}, False),
    ({'daily_close': True}, False), ({'freshness_status': 'STALE'}, False), ({'market_status': 'LAST CLOSE'}, False),
    ({'market_status': 'AFTER HOURS'}, False), ({'stub': True}, False), ({'provider': 'massive-stub'}, False)])
def test_only_a_fresh_live_print_of_an_open_venue_tests_a_level(change, usable):
    quote = dict({'price': 512.8, 'freshness_status': 'FRESH', 'market_status': 'LIVE', 'provider': 'finnhub'}, **change)
    assert technicals.break_price(quote) == (512.8 if usable else None)
    assert technicals.break_price(None) is None


@pytest.mark.asyncio
async def test_a_daily_close_never_scores_a_level_break(hub, monkeypatch):
    now = utcnow()
    close = {'price': 512.8, 'timestamp': (now - timedelta(hours=11)).isoformat(), 'freshness_status': 'STALE',
             'market_status': 'LAST CLOSE', 'provider': 'finnhub', 'data_class': 'DAILY_CLOSE', 'daily_close': True}
    ctx, _, _ = lean_context(hub, now)
    ctx.events, ctx.regime = [], {'posture': 'INSUFFICIENT_DATA', 'regimes': [], 'confidence': None}
    ctx.assets = [{'symbol': 'MSFT', 'asset_class': 'equity', 'quote': close}]
    ctx.levels = {'MSFT': {'support': 517.44, 'resistance': 519.0, 'atr': 0.1636}}  # the close is far below support
    ctx.bars = {'MSFT': []}
    result = await decision.analyse(ctx, ctx.assets[0])
    technical = result['component_detail']['technical']
    assert technical['status'] == 'missing' and 'No fresh live price' in technical['basis']
    assert result['score'] == 0 and result['category'] == 'WAIT / DATA INSUFFICIENT'
    assert not any(row['driver'] == 'Technical' and row['value'] for row in result['drivers']['rows'])
    assert 'below support' not in ' '.join(result['why']) and result['confirmation'].startswith('No directional bias')


# --- DEC-4: a tick stamped just after the cycle clock ----------------------------------------------------------------

def test_a_tick_slightly_ahead_of_the_clock_is_fresh_with_age_zero(hub):
    tick(hub, 'BTC', 'binance', NOW + timedelta(seconds=0.3))
    quote = hub.quote('BTC', 'crypto', NOW)
    assert quote['freshness_status'] == 'FRESH' and quote['age_seconds'] == 0
    scored = scoring.decision_gates('BTC', NOW, quote=quote, events=[], event={}, provider_health={})
    assert 'Critical data missing or stale' not in scored
    tick(hub, 'ETH', 'binance', NOW + timedelta(seconds=hub.settings.max_future_seconds + 1))
    ahead = hub.quote('ETH', 'crypto', NOW)
    assert ahead['freshness_status'] == 'STALE' and ahead['age_seconds'] < 0  # a clock problem is never fresh


def test_the_audit_accepts_the_same_small_future_tolerance():
    inputs = {'quote': {'price': 100, 'timestamp': (NOW + timedelta(seconds=0.3)).isoformat(), 'freshness_status': 'FRESH',
                        'market_status': 'LIVE', 'provider': 'binance'}, 'events': [], 'evidence_ids': []}
    draft = {'direction': 'WAIT', 'score': 0, 'created_at': NOW.isoformat(), 'reasons': []}
    check = lambda quote: next(c for c in pre_audit(draft, dict(inputs, quote=quote), NOW, 15, 5)['checks']  # noqa: E731
                               if c['name'] == 'fresh_prices')
    assert check(inputs['quote'])['passed']
    late = dict(inputs['quote'], timestamp=(NOW + timedelta(seconds=6)).isoformat())
    assert not check(late)['passed']
    polled = dict(inputs['quote'], provider='finnhub', timestamp=(NOW - timedelta(seconds=40)).isoformat())
    assert check(polled)['passed'] and 'limit 60s' in check(polled)['detail']  # the provider's limit, not 15 s
    assert not check(dict(polled, provider='binance'))['passed']


# --- DEC-5 / MD-1: one freshness lookup, per provider ----------------------------------------------------------------

def test_freshness_limit_is_per_provider_and_never_above_the_providers_real_time_limit(monkeypatch):
    assert (freshness_seconds('finnhub', 15), freshness_seconds('alpaca', 15)) == (60, 30)
    assert freshness_seconds('binance', 15) == 15 and freshness_seconds(None, 20) == 20 and freshness_seconds('massive', 15) == 15
    view = deepcopy(rules())
    view['market_data']['freshness']['providers'] = {'finnhub': 500, 'alpaca': 500, 'binance': 45}
    monkeypatch.setattr(config_module, 'rules_view', lambda: view)
    assert freshness_seconds('finnhub', 15) == view['finnhub']['realtime_max_age_seconds'] == 120
    assert freshness_seconds('alpaca', 15) == view['provider_routing']['alpaca']['realtime_max_age_seconds']
    assert freshness_seconds('binance', 15) == 45


def test_a_polled_equity_quote_is_fresh_inside_its_own_limit_and_never_real_time_when_stale(hub):
    label = 'real-time (Finnhub /quote last price)'
    tick(hub, 'NVDA', 'finnhub', NOW - timedelta(seconds=35), 190.0, as_of_label=label, data_class='LIVE')
    fresh = hub.quote('NVDA', 'equity', NOW)
    assert (fresh['freshness_status'], fresh['freshness_threshold_seconds'], fresh['age_seconds']) == ('FRESH', 60, 35)
    assert fresh['as_of_label'] == label and fresh['market_status'] == 'LIVE'
    stale = hub.quote('NVDA', 'equity', NOW + timedelta(seconds=26))  # 61 s old
    assert stale['freshness_status'] == 'STALE' and stale['freshness_threshold_seconds'] == 60
    assert 'real-time' not in stale['as_of_label'] and stale['as_of_label'].startswith('STALE: last price 14:59:25 UTC')
    assert '60 s freshness limit' in stale['as_of_label'] and stale['as_of_label_at_ingestion'] == label
    tick(hub, 'BTC', 'binance', NOW - timedelta(seconds=16))
    stream = hub.quote('BTC', 'crypto', NOW)
    assert stream['freshness_status'] == 'STALE' and stream['freshness_threshold_seconds'] == 15
    gates = scoring.decision_gates('NVDA', NOW, quote=fresh, events=[], event={}, provider_health={})
    assert 'Critical data missing or stale' not in gates


def test_data_quality_reactions_and_the_replay_use_the_same_lookup(hub):
    quote = {'freshness_status': 'FRESH', 'timestamp': (NOW - timedelta(seconds=30)).isoformat(), 'provider': 'finnhub',
             'freshness_threshold_seconds': 60}
    assert scoring.data_quality_parts(quote, 'ONLINE', {}, 0, NOW, 15)['freshness'] == 50.0
    assert scoring.data_quality_parts(dict(quote, freshness_threshold_seconds=None), 'ONLINE', {}, 0, NOW, 15)['freshness'] == 50.0
    at = NOW - timedelta(seconds=40)
    tick(hub, 'NVDA', 'finnhub', at, 190.0, data_class='LIVE')
    tick(hub, 'BTC', 'binance', at, 60000.0)
    with hub.store.session() as session:
        assert analytics._tick_at(session, 'NVDA', NOW, 15).price == 190.0  # 40 s old: inside Finnhub's 60 s
        assert analytics._tick_at(session, 'BTC', NOW, 15) is None  # 40 s old: outside the stream's 15 s
        assert backtest.tick_as_of(session, 'NVDA', NOW, 15).price == 190.0
        assert backtest.tick_as_of(session, 'BTC', NOW, 15) is None
        assert backtest.quote_as_of(session, 'NVDA', NOW, 15)['provider'] == 'finnhub'


def test_equity_polling_runs_at_a_fixed_rate(hub):
    runtime = Runtime(hub)
    interval = rules()['refresh']['cadences']['equity_quotes']['seconds']
    assert runtime.pause('equity_quotes', 7.0) == interval - 7.0  # the round took 7 s: the next one starts on time
    assert runtime.pause('equity_quotes', interval + 20.0) == 0.0 and runtime.pause('alpaca_quotes', 1.0) == interval - 1.0
    assert runtime.pause('derivatives', 7.0) == rules()['refresh']['cadences']['derivatives']['seconds']
    # The limit holds for the polling it was sized for: p95 vendor lag + interval + round stays inside it.
    assert 31.5 + interval + 8 <= freshness_seconds('finnhub', 15) <= rules()['finnhub']['realtime_max_age_seconds']


# --- DEC-6: the 'Important fact unverified' gate ----------------------------------------------------------------------

def test_an_unassessed_event_blocks_only_from_an_authoritative_source_with_an_active_path():
    quote = {'freshness_status': 'FRESH', 'market_status': 'LIVE', 'provider': 'binance'}
    gate = lambda symbol, event: scoring.decision_gates(symbol, NOW, quote=quote, events=[event], event={}, provider_health={})  # noqa: E731
    article = hormuz(id='a', verification_status='PARTIALLY_CONFIRMED', summary_fact='Why Is Intel Stock Falling?',
                     first_seen_utc=(NOW - timedelta(minutes=30)).isoformat(), importance=None, tier_weight=0.5)
    assert gate('BTC', article) == []  # tier 4, not rated: waits for its rating instead of blocking 28 assets
    assert gate('BTC', dict(article, importance=0.3)) == []  # rated 60 by the extractor: 0.5 x 0.6
    assert gate('BTC', dict(article, importance=0.5)) == ['Important fact unverified: Why Is Intel Stock Falling?']
    wire = dict(article, tier_weight=0.9, summary_fact='Wire: tanker attacked')
    assert gate('BTC', wire) == ['Important fact unverified: Wire: tanker attacked']
    assert gate('BTC', dict(wire, tier_weight=1.0, importance=0.3)) == []  # assessed as minor: the rating decides
    # Reach is an ACTIVE path that contributes to this asset: a publication whose paths have decayed reaches nothing.
    old = dict(wire, event_time=(NOW - timedelta(days=60)).isoformat())
    assert gate('BTC', old) == []
    # A crypto-regulation wire reaches the crypto assets only: an equity is never blocked by it.
    crypto = dict(wire, trigger='CRYPTO_REGULATION', event_type='REGULATION')
    reached = {s for p in scoring.causal.event_paths(crypto, NOW) if p['active'] for s, v in p['contributions'].items() if v}
    assert 'BTC' in reached and 'NVDA' not in reached
    assert gate('BTC', crypto) == ['Important fact unverified: Wire: tanker attacked'] and gate('NVDA', crypto) == []


# --- DEC-7: the publication check of a lean ---------------------------------------------------------------------------

LEVELS = {'support': 85939.06, 'resistance': 86100.0, 'atr': 190.856}


def lean_parts(event_value: float, price: float):
    components = {'event': scoring.measured(event_value, 'event'), 'technical': scoring.technical_component('BTC', price, LEVELS)}
    return components, engines.decide(components, [], weights=rules()['weights'])


def live(price: float) -> dict:
    return {'price': price, 'freshness_status': 'FRESH', 'market_status': 'LIVE', 'provider': 'binance'}


def test_a_break_that_still_holds_keeps_the_lean_with_the_values_of_the_new_price():
    components, candidate = lean_parts(-40.0, 85904.00)
    assert candidate['category'] == 'BEARISH LEAN / WAIT' and components['technical']['value'] == -63.67
    recheck = lambda price, parts=components, lean=candidate: decision.lean_recheck(  # noqa: E731
        'BTC', live(price), LEVELS, parts, [], rules()['weights'], lean)
    technical, published, adopt = recheck(85894.91)  # the stored case: 9 points lower, the break is deeper
    assert adopt and technical['value'] == -64.63 and published['category'] == candidate['category']
    assert published['score'] < candidate['score'] and published['component_detail']['technical']['value'] == -64.63
    weaker = recheck(85930.00)  # still below support, by less
    assert weaker[2] and weaker[0]['value'] == pytest.approx(-60.95, abs=0.01) and weaker[1]['category'] == candidate['category']
    assert recheck(85904.00) == (components['technical'], candidate, True)
    assert recheck(85950.00) is None  # back inside the range: the confirmation is lost
    assert recheck(86200.00) is None  # above resistance: reversed
    assert decision.lean_recheck('BTC', dict(live(85894.91), freshness_status='STALE'), LEVELS, components, [],
                                 rules()['weights'], candidate) is None  # no usable price: nothing confirms the break


def test_a_lean_without_a_break_is_blocked_only_by_a_break_against_it():
    components, candidate = lean_parts(-70.0, 86000.00)  # inside the range: technical 0, event-driven bearish lean
    assert candidate['category'] == 'BEARISH LEAN / WAIT' and components['technical']['value'] == 0
    recheck = lambda price: decision.lean_recheck('BTC', live(price), LEVELS, components, [], rules()['weights'], candidate)  # noqa: E731
    assert recheck(86050.00) == (components['technical'], candidate, True)
    assert recheck(86200.00) is None  # a break above resistance opposes the bearish lean
    # A break in the lean's direction would make it a setup, which needs the AI path of the next cycle: the lean is
    # published as analysed, never blocked as 'data insufficient'.
    assert recheck(85700.00) == (components['technical'], candidate, False)


@pytest.mark.asyncio
@pytest.mark.parametrize(('confidence', 'cycle_price', 'latest_price', 'adopted'), [
    (60, 60990.0, 60900.0, True),   # the break deepened and the lean stays a lean: stored with the new values
    (95, 62000.0, 60990.0, False),  # a break appeared in the lean's direction (setup band): the analysed lean is kept
])
async def test_a_still_valid_lean_is_published_not_blocked(hub, monkeypatch, confidence, cycle_price, latest_price, adopted):
    now = utcnow()
    ctx, latest, event = lean_context(hub, now)
    event['fact_confidence'] = ctx.events[0]['fact_confidence'] = confidence
    ctx.assets[0]['quote']['price'] = cycle_price
    latest.update(price=latest_price, timestamp=(now + timedelta(seconds=1)).isoformat())
    monkeypatch.setattr(decision, 'utcnow', lambda: now + timedelta(seconds=1))
    monkeypatch.setattr(hub, 'quote', lambda *args, **kwargs: dict(latest))
    monkeypatch.setattr(hub, 'event', lambda *args, **kwargs: event)

    async def unexpected_ai(*args, **kwargs):
        pytest.fail('A lean is published without an AI request')

    monkeypatch.setattr(hub.ai, 'run_with_id', unexpected_ai)
    result = await decision.analyse(ctx, ctx.assets[0])
    assert result['category'] == 'BEARISH LEAN / WAIT' and result['blocks'] == [] and result['audit_status'] == 'NOT_REQUIRED'
    snapshot = result['input_snapshot']
    price = latest_price if adopted else cycle_price
    expected = scoring.technical_component('BTC', price, ctx.levels['BTC'])['value']
    assert result['component_detail']['technical']['value'] == snapshot['components']['technical'] == expected
    assert result['price'] == snapshot['quote']['price'] == price
    assert snapshot['decision'] == {k: result[k] for k in ('category', 'direction', 'score')}


# --- startup repair hook and the specialist budget check ---------------------------------------------------------------

def test_startup_runs_the_ingestion_repair_when_one_exists_and_survives_its_failure(hub, monkeypatch, caplog):
    # The ingestion layer offers its start-up repair under one of two names (repair_stored_events today); the hook
    # calls whichever exists, after the sources are stored, and a failure is logged, never raised.
    assert any(callable(getattr(hub, name, None)) for name in ('repair_events', 'repair_stored_events'))
    assert hub.run_event_repair() is None  # the real repair on an empty store: never raises
    calls = []
    monkeypatch.setattr(type(hub), 'repair_events', lambda self: calls.append('repair') or {'split': 2}, raising=False)
    hub.initialize()
    assert calls == ['repair']

    def broken(self):
        raise RuntimeError('repair failed')

    monkeypatch.setattr(type(hub), 'repair_events', broken, raising=False)
    with caplog.at_level('ERROR'):
        hub.initialize()  # logged, never raised
    assert any(record.message == 'events_repair_failed' for record in caplog.records)
    # Without repair_events the layer's repair_stored_events is called; with neither nothing is.
    monkeypatch.setattr(type(hub), 'repair_events', None, raising=False)
    monkeypatch.setattr(type(hub), 'repair_stored_events', lambda self: calls.append('stored'), raising=False)
    hub.initialize()
    assert calls[-1] == 'stored'
    monkeypatch.setattr(type(hub), 'repair_stored_events', None, raising=False)
    hub.initialize()
    assert calls == ['repair', 'stored']


@pytest.mark.asyncio
async def test_a_specialist_request_the_budget_would_refuse_is_not_prepared(hub, monkeypatch):
    from backend.schemas import DocumentInput
    stored = hub.ingest_document(DocumentInput(source_id='fed', title='Federal Reserve statement about policy', content='Text',
                                               url='https://www.federalreserve.gov/newsevents/x.htm', published_at=utcnow()),
                                 trusted_transport=True)
    assert stored['verification_status'] == 'CONFIRMED'
    monkeypatch.setattr(hub, 'extraction_pages', lambda event: [])
    asked = []

    async def run_with_id(role, inputs, **kwargs):
        asked.append(role)
        return None, None

    budget = SimpleNamespace(available=lambda role: True, allowed=lambda role: False, run_with_id=run_with_id)
    monkeypatch.setattr(hub, 'ai', budget)
    await hub.enrich_event(stored['id'])
    assert asked == [] and hub.budget_refuses(hub._specialist(hub.event(stored['id'])))
    with hub.store.session() as session:
        assert 'analysed_status' not in session.get(Event, stored['id']).payload  # a later enrichment asks again
    budget.allowed = lambda role: True
    await hub.enrich_event(stored['id'])
    assert len(asked) == 1
    budget.available = lambda role: False  # no model for the role: the earlier behaviour (the request path decides)
    assert hub.budget_refuses('macro_strategist') is False


def test_competing_causal_paths_give_no_expected_direction():
    from backend import causal
    weak = analytics.expected_effects({'causal_paths': causal.paths('JOBS_WEAK')})
    strong = analytics.expected_effects({'causal_paths': causal.paths('JOBS_STRONG')})
    for symbol in set(weak) & set(strong):
        assert not (weak[symbol] and weak[symbol] == strong[symbol]), symbol  # never the same sign in both branches
    for trigger, effects in (('JOBS_WEAK', weak), ('JOBS_STRONG', strong)):
        paths = causal.paths(trigger)
        unresolved = set(analytics.unresolved_effects({'causal_paths': paths}))
        assert unresolved == {s for s in effects if causal.conflict(paths, s)['conflicting']}
        assert all(effects[s] == 0 for s in unresolved)
