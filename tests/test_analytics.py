from datetime import UTC, datetime, timedelta

import pytest

from backend import analytics
from backend.analytics import (
    basket_confirmation,
    capture_reactions,
    classify_reaction,
    correlate_events,
    correlation,
    detect_anomaly,
    event_reactions,
    expected_effects,
    measure_reaction,
    positioning,
    recent_prices,
    relative_policy,
    rolling_anomaly,
    scan_anomalies,
    unit_for,
)
from backend.database import Store, Tick

T0 = datetime(2026, 3, 18, 18, 0, tzinfo=UTC)


def test_fed_hike_alone_never_long_usdjpy():
    assert relative_policy({'rate':5.5}, None)['bias'] == 'WAIT'

def test_options_imbalance_not_direction_or_max_pain_target():
    result = positioning([{'instrument_name':'BTC-30SEP26-80000-C','open_interest':10000,'mark_iv':50},
                          {'instrument_name':'BTC-30SEP26-80000-P','open_interest':1,'mark_iv':60}])
    assert result['directional_score'] == 0
    assert 'target' not in result and 'max_pain' not in result
    assert result['put_call_ratio'] == .0001

def test_correlation_requires_sufficient_aligned_samples():
    assert correlation([1,2],[2,4]) is None
    assert abs(correlation(list(range(20)), list(range(20)))-1)<1e-10

def test_anomaly_uses_observed_change():
    assert rolling_anomaly([100,101,102],1)['triggered']
    assert rolling_anomaly([100,100.1],1)['triggered'] is False


# --- reaction engine ---

def test_yields_use_basis_points_and_other_assets_percent():
    assert unit_for('US10Y') == 'bp' and unit_for('DXY') == 'pct' and unit_for('BTC') == 'pct'
    row = measure_reaction('e', 'US2Y', '5m', (4.00, T0), (4.10, T0 + timedelta(minutes=5)), 1)
    assert row['unit'] == 'bp' and row['change'] == pytest.approx(10) and row['status'] == 'CONFIRMED'
    assert 'change_pct' not in row
    fx = measure_reaction('e', 'DXY', '5m', (100, T0), (100.6, T0 + timedelta(minutes=5)), 1)
    assert fx['unit'] == 'pct' and fx['change_pct'] == pytest.approx(0.6) and fx['status'] == 'CONFIRMED'


def test_reaction_statuses_use_per_asset_tolerances():
    assert classify_reaction(0.3, 0, 0.05, 0.3) == 'NOT_EXPECTED'
    assert classify_reaction(0.01, 1, 0.05, 0.3) == 'NO_REACTION'
    assert classify_reaction(0.1, 1, 0.05, 0.3) == 'PARTIAL'
    assert classify_reaction(0.4, 1, 0.05, 0.3) == 'CONFIRMED'
    assert classify_reaction(-0.4, 1, 0.05, 0.3) == 'DIVERGENT'
    assert classify_reaction(-0.4, 1, 0.05, 0.3, aligned_earlier=True) == 'REVERSED'
    # BTC tolerance is wider than DXY's: a 0.1% BTC move is noise-adjacent, the same DXY move is a reaction.
    btc = measure_reaction('e', 'BTC', '1h', (100, T0), (100.08, T0), 1)
    dxy = measure_reaction('e', 'DXY', '1h', (100, T0), (100.08, T0), 1)
    assert btc['status'] == 'NO_REACTION' and dxy['status'] == 'PARTIAL'


def test_hawkish_fed_basket_confirms_and_flags_nasdaq_divergence():
    expected = {'US2Y': 1, 'DXY': 1, 'NASDAQ': -1, 'GOLD': -1}
    def rows(nasdaq_after):
        return [measure_reaction('fed', 'US2Y', '1h', (4.0, T0), (4.10, T0), 1),
                measure_reaction('fed', 'DXY', '1h', (100, T0), (100.6, T0), 1),
                measure_reaction('fed', 'NASDAQ', '1h', (100, T0), (nasdaq_after, T0), -1)]
    high = basket_confirmation('fed', '1h', rows(98.9), expected)
    assert high['confirmation_pct'] == 100 and high['confirmation_level'] == 'HIGH' and not high['divergence']
    assert high['missing'] == ['GOLD'] and high['measured'] == 3
    split = basket_confirmation('fed', '1h', rows(101.1), expected)
    assert split['divergence'] and split['diverging'] == ['NASDAQ'] and split['confirmation_pct'] == pytest.approx(66.7)
    assert basket_confirmation('fed', '1h', [], expected)['confirmation_pct'] is None


def test_expected_effects_net_competing_paths_and_fall_back_to_rules():
    paths = [{'effects': {'GOLD': 1, 'US10Y': 1}, 'strength': 40, 'active': True},
             {'effects': {'GOLD': -1}, 'strength': 90, 'active': True},
             {'effects': {'BTC': 1}, 'strength': 90, 'active': False}]
    assert expected_effects({'causal_paths': paths}) == {'GOLD': -1, 'US10Y': 1}
    # Paths that nearly cancel (|net| / gross below causal conflict_net_ratio 0.35) leave the asset unresolved: sign 0.
    close = [dict(paths[0]), dict(paths[1], strength=70)]  # GOLD +40 vs -70: net -30 of 110 = 0.27
    assert expected_effects({'causal_paths': close}) == {'GOLD': 0, 'US10Y': 1}
    assert analytics.unresolved_effects({'causal_paths': close}) == ['GOLD']
    assert analytics.unresolved_effects({'causal_paths': paths}) == []
    assert expected_effects({'event_type': 'INFLATION_HOT'})['DXY'] == 1
    assert expected_effects({'expected_effects': {'US2Y': 1}, 'event_type': 'INFLATION_HOT'}) == {'US2Y': 1}


def _tick(session, symbol, at, price, quantity=None):
    session.add(Tick(id=f'{symbol}:{at.isoformat()}', symbol=symbol, price=price, provider='test', timestamp=at,
                     ingested_at=at, payload={'trade_quantity': quantity} if quantity is not None else {}))


def _store(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/reaction.db')
    store.initialize()
    return store


def test_capture_reactions_covers_whole_basket_and_detects_reversal(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics, 'WINDOW_RESOLVERS', {})  # fixed windows only; session windows are tested below
    store = _store(tmp_path)
    path = {'effects': {'US2Y': 1, 'DXY': 1, 'NASDAQ': -1}, 'strength': 80, 'active': True}
    event = {'id': 'fed-1', 'first_seen_utc': T0.isoformat(), 'causal_paths': [path], 'assets': [], 'importance': 0.9}
    prices = {'US2Y': [4.0, 4.05, 4.10, 4.10, 4.10, 4.10, 4.10], 'DXY': [100, 100.2, 100.5, 100.6, 100.6, 100.6, 100.6],
              'NASDAQ': [100, 99.5, 99, 98.9, 98.9, 100.5, 101], 'BTC': [100] * 7}
    offsets = [0, 60, 300, 900, 1800, 3600, 14400]
    with store.session() as session:
        for symbol, series in prices.items():
            for offset, price in zip(offsets, series, strict=True):
                _tick(session, symbol, T0 + timedelta(seconds=offset) - timedelta(seconds=1), price)
        session.commit()
    now = T0 + timedelta(hours=5)
    added = capture_reactions(store, [event], stale_seconds=30, now=now)
    assert added == 4 * 6  # every basket asset with data, not only the extractor's mentioned assets
    result = event_reactions(store, 'fed-1')
    by = {(r['symbol'], r['horizon']): r for r in result['reactions']}
    assert by[('BTC', '1h')]['status'] == 'NOT_EXPECTED'
    assert by[('US2Y', '5m')]['unit'] == 'bp' and by[('US2Y', '5m')]['change'] == pytest.approx(10)
    assert by[('NASDAQ', '30m')]['status'] == 'CONFIRMED' and by[('NASDAQ', '4h')]['status'] == 'REVERSED'
    baskets = {b['horizon']: b for b in result['baskets']}
    assert baskets['30m']['confirmation_pct'] == 100 and not baskets['30m']['divergence']
    assert baskets['4h']['divergence'] and baskets['4h']['diverging'] == ['NASDAQ']
    assert capture_reactions(store, [event], stale_seconds=30, now=now) == 0  # marked complete, idempotent


def test_capture_skips_unimportant_and_waits_for_future_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics, 'WINDOW_RESOLVERS', {})
    store = _store(tmp_path)
    with store.session() as session:
        _tick(session, 'BTC', T0 - timedelta(seconds=1), 100)
        _tick(session, 'BTC', T0 + timedelta(seconds=59), 101)
        session.commit()
    low = {'id': 'minor', 'first_seen_utc': T0.isoformat(), 'importance': 0.1, 'expected_effects': {'BTC': 1}}
    assert capture_reactions(store, [low], 30, now=T0 + timedelta(minutes=2)) == 0
    event = {'id': 'e2', 'first_seen_utc': T0.isoformat(), 'expected_effects': {'BTC': 1}}
    assert capture_reactions(store, [event], 30, now=T0 + timedelta(minutes=2), basket=['BTC']) == 1
    assert not store.get('event_relationships', 'reactions-complete:e2')
    # Session-bound windows plug in as resolvers without changing the engine.
    monkeypatch.setitem(analytics.WINDOW_RESOLVERS, 'close', lambda t0, kind: t0 + timedelta(seconds=59))
    assert [w['label'] for w in analytics.reaction_windows()][-1] == 'close'
    assert capture_reactions(store, [event], 30, now=T0 + timedelta(minutes=2), basket=['BTC']) == 1
    assert store.get('event_relationships', 'reaction:e2:BTC:close')['status'] == 'CONFIRMED'


# --- anomaly detector ---

def _points(start, prices, step=60):
    return [(start + timedelta(seconds=step * i), p) for i, p in enumerate(prices)]


def test_anomaly_uses_clock_windows_and_config_thresholds():
    now = T0 + timedelta(minutes=5)
    btc = _points(T0, [100, 100.2, 100.4, 100.6, 100.8, 101.2])
    found = scan_anomalies({'BTC': btc}, now)
    assert [(a['window_seconds'], a['unit']) for a in found] == [(300, 'pct')]
    assert found[0]['change'] == pytest.approx(1.2) and found[0]['threshold'] == 1.0
    # A data gap wider than the baseline tolerance means no measurement, never a fabricated move.
    gapped = [(T0 - timedelta(minutes=30), 100), (now, 103)]
    assert detect_anomaly('BTC', gapped, now, {'window_seconds': 300, 'threshold': 1}) is None


def test_yield_anomaly_in_basis_points():
    now = T0 + timedelta(minutes=15)
    rising = _points(T0, [4.00 + 0.004 * i for i in range(16)])
    found = scan_anomalies({'US10Y': rising}, now)
    assert found and found[0]['unit'] == 'bp' and found[0]['change'] == pytest.approx(6) and found[0]['threshold'] == 5


def test_volatility_adjusted_threshold_rises_in_noisy_markets():
    now = T0 + timedelta(hours=4)
    noisy = [(T0 + timedelta(minutes=i), 100 * (1.004 if i % 2 else 0.996)) for i in range(236)]
    noisy += [(T0 + timedelta(minutes=236 + i), 100 * (1 + 0.005 * i)) for i in range(5)]
    rule = {'window_seconds': 300, 'threshold': 1.0}
    fixed = detect_anomaly('BTC', noisy, now, rule, 'fixed')
    adjusted = detect_anomaly('BTC', noisy, now, rule, 'volatility_adjusted')
    assert fixed['triggered'] and fixed['threshold_basis'] == 'fixed'
    assert adjusted['threshold'] > fixed['threshold'] and not adjusted['triggered']
    assert adjusted['threshold_basis'] == 'volatility_adjusted'
    thin = detect_anomaly('BTC', noisy[-6:], now, rule, 'volatility_adjusted')
    assert thin['threshold_basis'].startswith('fixed')


def test_anomaly_event_correlation_lists_candidates_not_causes():
    anomaly = {'symbol': 'BRENT', 'window_seconds': 600, 'end_timestamp': (T0 + timedelta(minutes=10)).isoformat(),
               'direction': 1}
    events = [{'id': 'hormuz', 'first_seen_utc': (T0 - timedelta(minutes=2)).isoformat(), 'verification_status': 'CONFIRMED',
               'affected_assets': ['BRENT'], 'event_type': 'GEOPOLITICAL_ENERGY', 'summary_fact': 'Tanker attacked'},
              {'id': 'old', 'first_seen_utc': (T0 - timedelta(hours=5)).isoformat(), 'verification_status': 'CONFIRMED'},
              {'id': 'rumor', 'first_seen_utc': T0.isoformat(), 'verification_status': 'UNCONFIRMED', 'assets': []}]
    releases = [{'id': 'eia', 'name': 'EIA crude inventories', 'scheduled_at': (T0 + timedelta(minutes=1)).isoformat()}]
    candidates = correlate_events(anomaly, events, releases)
    assert [c['id'] for c in candidates] == ['hormuz', 'rumor', 'eia']
    assert candidates[0]['direction_consistent'] is True and candidates[0]['lag_seconds'] == 120
    moved = _points(T0, [80 + 0.15 * i for i in range(11)])
    found = scan_anomalies({'BRENT': moved}, T0 + timedelta(minutes=10), events, releases)
    assert found[0]['explained_by_confirmed_event'] and not found[0]['research_recommended']
    assert scan_anomalies({'BRENT': moved}, T0 + timedelta(minutes=10))[0]['research_recommended']


def test_recent_prices_reads_only_known_ticks(tmp_path):
    store = _store(tmp_path)
    with store.session() as session:
        _tick(session, 'BTC', T0, 100)
        _tick(session, 'BTC', T0 + timedelta(minutes=10), 101)
        session.commit()
    assert recent_prices(store, 'BTC', T0 + timedelta(minutes=5), 300) == [(T0, 100)]


# --- review fixes ---

def test_expected_effects_use_causal_contributions_and_respect_suppression():
    inverted = {'effects': {'GOLD': 1}, 'contributions': {'GOLD': -35.0}, 'strength': 50, 'active': True}
    assert expected_effects({'causal_paths': [inverted]}) == {'GOLD': -1}
    suppressed = [{'effects': {'BTC': -1}, 'contributions': {}, 'strength': 70, 'active': False}]
    # Supplied-but-suppressed paths mean "no expectation"; the raw rule must not come back through the fallback.
    assert expected_effects({'causal_paths': suppressed, 'event_type': 'INFLATION_HOT'}) == {}


def test_basket_level_needs_enough_measured_assets():
    one = [measure_reaction('fed', 'US2Y', '1h', (4.0, T0), (4.10, T0), 1)]
    result = basket_confirmation('fed', '1h', one, {'US2Y': 1, 'DXY': 1, 'NASDAQ': -1})
    assert result['confirmation_pct'] == 100 and result['confirmation_level'] is None and result['missing'] == ['DXY', 'NASDAQ']


def test_stub_ticks_never_feed_reactions_or_anomalies(tmp_path):
    store = _store(tmp_path)
    with store.session() as session:
        for offset, price in [(-1, 100), (59, 105)]:
            at = T0 + timedelta(seconds=offset)
            session.add(Tick(id=f'stub:{offset}', symbol='BTC', price=price, provider='binance-stub', timestamp=at,
                             ingested_at=at, payload={'stub': True}))
        session.commit()
    event = {'id': 's1', 'first_seen_utc': T0.isoformat(), 'expected_effects': {'BTC': 1}}
    assert capture_reactions(store, [event], 30, now=T0 + timedelta(minutes=2), basket=['BTC']) == 0
    assert recent_prices(store, 'BTC', T0 + timedelta(minutes=2), 300) == []


def test_volatility_ignores_ticks_carried_across_grid_points():
    now = T0 + timedelta(hours=4)
    sparse = [(T0 + timedelta(seconds=110 * i), 100 + (i % 2)) for i in range(int(4 * 3600 / 110) + 1)]
    volatility = analytics.window_volatility(sparse, now, 'pct')
    assert volatility is not None and volatility > 0.9  # carried samples would add fake zero changes


def test_basket_names_its_expected_symbols_and_the_cycle_finds_them():
    """Regression (live 2026-09-28): 'expected' is a count, so `symbol in basket['expected']` raised TypeError and
    aborted every analysis cycle once a confirmed event had stored baskets."""
    from backend.decision import CycleContext, basket_symbols
    rows = [measure_reaction('fed', 'US2Y', '1h', (4.0, T0), (4.10, T0), 1)]
    basket = basket_confirmation('fed', '1h', rows, {'US2Y': 1, 'DXY': 1, 'NASDAQ': -1, 'BTC': 0})
    assert basket['expected'] == 3 and basket['expected_symbols'] == ['DXY', 'NASDAQ', 'US2Y']
    legacy = {k: v for k, v in basket.items() if k != 'expected_symbols'}  # stored before the field existed
    assert basket_symbols(basket) == basket_symbols(legacy) == {'DXY', 'NASDAQ', 'US2Y'}
    ctx = object.__new__(CycleContext)
    ctx.events, ctx.baskets = [{'id': 'fed'}], {'fed': [legacy]}
    assert ctx.latest_basket('NASDAQ')[0] is legacy and ctx.latest_basket('BTC')[0] is None


def test_a_release_first_seen_long_after_publication_is_never_measured(tmp_path, monkeypatch):
    """Regression (UI audit EV-03): backfilled releases were 'measured' from their ingestion time, hours or months after
    publication, and shown as the release's reaction (DIVERGENT badges)."""
    monkeypatch.setattr(analytics, 'WINDOW_RESOLVERS', {})
    store = _store(tmp_path)
    with store.session() as session:
        _tick(session, 'BTC', T0 - timedelta(seconds=1), 100)
        _tick(session, 'BTC', T0 + timedelta(seconds=59), 101)
        session.commit()
    lag = analytics.rules_view()['reaction']['max_publication_lag_seconds']
    backfill = {'id': 'old', 'first_seen_utc': T0.isoformat(), 'event_time': (T0 - timedelta(days=3)).isoformat(),
                'expected_effects': {'BTC': 1}}
    assert analytics.publication_lag_seconds(backfill) == 3 * 86400
    assert capture_reactions(store, [backfill], 30, now=T0 + timedelta(minutes=2), basket=['BTC']) == 0
    assert event_reactions(store, 'old')['reactions'] == []
    prompt = dict(backfill, id='prompt', event_time=(T0 - timedelta(seconds=lag - 1)).isoformat())
    assert capture_reactions(store, [prompt], 30, now=T0 + timedelta(minutes=2), basket=['BTC']) == 1
    undated = {k: v for k, v in dict(backfill, id='undated').items() if k != 'event_time'}  # no publication time: as before
    assert capture_reactions(store, [undated], 30, now=T0 + timedelta(minutes=2), basket=['BTC']) == 1
