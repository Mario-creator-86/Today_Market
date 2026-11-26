from datetime import UTC, datetime, timedelta

import pytest

from backend.regime import classify, features

NOW = datetime(2026, 3, 10, 14, tzinfo=UTC)


def asset(symbol, change, price=100.0, freshness='FRESH'):
    return {'symbol': symbol, 'quote': {'price': price, 'change_pct': change, 'freshness_status': freshness}}


def event(kind, hours=1, status='CONFIRMED'):
    return {'event_type': kind, 'verification_status': status, 'first_seen_utc': (NOW - timedelta(hours=hours)).isoformat()}


def test_insufficient_fresh_features_produce_no_regime():
    result = classify(features([asset('BTC', -4)]), [], NOW)
    assert result['regimes'] == [] and result['posture'] == 'INSUFFICIENT_DATA' and result['confidence'] is None


def test_stale_quotes_never_describe_current_regime():
    observed = features([asset('DXY', 1, freshness='STALE'), asset('BRENT', 5, freshness='STALE')])
    assert observed == {}


def test_inflation_shock_with_usd_strength_is_defensive_multi_regime():
    observed = features([asset('BRENT', 2.8), asset('NASDAQ', -1.4), asset('DXY', 0.6), asset('US2Y', 1.2)])
    result = classify(observed, [], NOW)
    assert set(result['regimes']) == {'INFLATION_SHOCK', 'USD_STRENGTH'}
    assert result['posture'] == 'DEFENSIVE'
    inflation = next(r for r in result['evaluated'] if r['regime'] == 'INFLATION_SHOCK')
    assert 'GOLD' in inflation['missing_inputs'] and 0 < inflation['confidence'] < 100
    assert 'not a calibrated probability' in result['method'] and result['basis'] == 'live'  # contract: basis is the data basis


def test_confirmed_event_supports_but_cannot_activate_without_prices():
    observed = features([asset('BRENT', 0.2), asset('GOLD', 0.1)])
    assert 'GEOPOLITICAL_SHOCK' not in classify(observed, [event('GEOPOLITICAL_ENERGY')], NOW)['regimes']
    shocked = features([asset('BRENT', 3), asset('GOLD', 1.5)])
    fresh = next(r for r in classify(shocked, [event('GEOPOLITICAL_ENERGY')], NOW)['evaluated'] if r['regime'] == 'GEOPOLITICAL_SHOCK')
    old = next(r for r in classify(shocked, [event('GEOPOLITICAL_ENERGY', hours=48)], NOW)['evaluated'] if r['regime'] == 'GEOPOLITICAL_SHOCK')
    rumor = next(r for r in classify(shocked, [event('GEOPOLITICAL_ENERGY', status='UNCONFIRMED')], NOW)['evaluated'] if r['regime'] == 'GEOPOLITICAL_SHOCK')
    assert fresh['state'] == 'ACTIVE' and fresh['confidence'] > old['confidence'] == rumor['confidence']


def test_quiet_observed_market_is_mixed_and_level_rules_use_price():
    result = classify(features([asset('DXY', 0.1), asset('SP500', 0.1), asset('VIX', 0.5, price=20)]), [], NOW)
    assert result['regimes'] == ['MIXED'] and result['posture'] == 'MIXED'
    calm = classify(features([asset('DXY', 0.1), asset('SP500', 0.1), asset('VIX', 0.5, price=12)]), [], NOW)
    assert 'LOW_VOLATILITY' in calm['regimes'] and calm['posture'] == 'CONSTRUCTIVE'


def minute_pair():
    times = [f'2026-03-10T13:{m:02d}:00+00:00' for m in range(30)]
    return {'BTC': [{'time': t, 'close': 100 + i + (i % 3)} for i, t in enumerate(times)],
            'NASDAQ': [{'time': t, 'close': 200 + 2 * i + (i % 3)} for i, t in enumerate(times)]}


def test_correlation_feature_requires_aligned_bars():
    bars, legs = minute_pair(), [asset('BTC', 0.2), asset('NASDAQ', 0.1)]
    sources: dict = {}
    observed = features(legs, bars, provenance=sources)
    assert observed['CORR:BTC:NASDAQ']['observations'] == 29 and observed['CORR:BTC:NASDAQ']['value'] > 0.5
    assert sources['CORR:BTC:NASDAQ']['basis'] == 'live'
    assert 'CORR:BTC:NASDAQ' not in features(legs, {'BTC': bars['BTC'][:5], 'NASDAQ': bars['NASDAQ'][:5]})


def test_correlation_needs_fresh_quotes_for_both_legs():
    bars = minute_pair()  # e.g. Friday's one-minute bars read on Saturday: the NASDAQ leg stopped updating
    assert 'CORR:BTC:NASDAQ' not in features([], bars)
    assert 'CORR:BTC:NASDAQ' not in features([asset('BTC', 0.2), asset('NASDAQ', 0.1, freshness='STALE')], bars)


def test_yield_features_carry_basis_points_and_curve_change():
    observed = features([asset('US2Y', 2.5, price=4.10), asset('US10Y', 1.0, price=4.04)])
    assert observed['US2Y']['change_bp'] == pytest.approx((4.10 - 4.10 / 1.025) * 100)
    curve = observed['CURVE:US10Y-US2Y']
    assert curve['change_bp'] == pytest.approx(observed['US10Y']['change_bp'] - observed['US2Y']['change_bp'])
    assert curve['level_bp'] == pytest.approx(-6) and 'change_bp' not in observed.get('DXY', {})
    assert 'CURVE:US10Y-US2Y' not in features([asset('US2Y', 2.5, price=4.10), asset('US10Y', 1.0, freshness='STALE')])


def test_bear_flattening_supports_inflation_shock():
    base = [asset('BRENT', 2.8), asset('NASDAQ', -1.4)]
    flat = classify(features(base + [asset('US2Y', 2.5, price=4.1), asset('US10Y', 0.5, price=4.2)]), [], NOW)
    steep = classify(features(base + [asset('US2Y', -1, price=4.1), asset('US10Y', 1.0, price=4.2)]), [], NOW)
    conf = {r['regime']: r for r in flat['evaluated']}['INFLATION_SHOCK']['confidence']
    assert conf > {r['regime']: r for r in steep['evaluated']}['INFLATION_SHOCK']['confidence']


def test_credit_hook_accepts_only_configured_fresh_spreads():
    credit = [{'name': 'BAA10Y', 'value': 2.1, 'change_bp': 8, 'freshness_status': 'FRESH'},
              {'name': 'UNKNOWN', 'value': 1, 'change_bp': 1, 'freshness_status': 'FRESH'},
              {'name': 'IG_OAS', 'value': 120, 'change_bp': 3, 'freshness_status': 'STALE'}]
    observed = features([asset('VIX', 20), asset('DXY', 0.7)], credit=credit)
    assert observed['CREDIT:BAA10Y'] == {'value': 2.1, 'change_bp': 8}
    assert 'CREDIT:UNKNOWN' not in observed and 'CREDIT:IG_OAS' not in observed
    stress = {r['regime']: r for r in classify(observed, [], NOW)['evaluated']}['LIQUIDITY_STRESS']
    assert stress['state'] == 'ACTIVE' and 'CREDIT:BAA10Y' not in stress['missing_inputs']


def test_driver_arrows_and_unavailable_inputs():
    observed = features([asset('US2Y', 2.5, price=4.1), asset('US10Y', 1.0, price=4.2), asset('DXY', 0.6),
                         asset('SP500', -1.2), asset('VIX', 12, price=22)])
    result = {d['driver']: d for d in classify(observed, [], NOW)['drivers']}
    assert result['Rates']['direction'] == 'up' and result['Rates']['arrow'] == '↑'
    assert result['USD']['direction'] == 'up' and result['Risk appetite']['direction'] == 'down'
    assert result['Oil'] == {'driver': 'Oil', 'direction': 'unavailable', 'arrow': None, 'inputs': [],
                             'missing': ['BRENT', 'WTI'], 'input': None, 'basis': None}
    assert result['Rates']['input'] == 'US2Y +10 bp, US10Y +4.2 bp' and result['Rates']['basis'] == 'live'
    assert 'NASDAQ' in result['Risk appetite']['missing']
    quiet = {d['driver']: d for d in classify(features([asset('DXY', 0.05), asset('SP500', 0.1)]), [], NOW)['drivers']}
    assert quiet['USD']['direction'] == 'flat'


def test_correlation_feature_counts_toward_rules():
    observed = features([asset('VIX', 20), asset('DXY', 0.7), asset('BTC', 0.2), asset('NASDAQ', 0.1)], minute_pair())
    result = classify(observed, [], NOW)
    stress = {r['regime']: r for r in result['evaluated']}['LIQUIDITY_STRESS']
    assert 'CORR:BTC:NASDAQ' not in stress['missing_inputs'] and stress['confidence'] > 0
