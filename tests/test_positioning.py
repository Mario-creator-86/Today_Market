from datetime import UTC, datetime, timedelta

import pytest

from backend.database import Store
from backend.positioning import (
    black76,
    crowding_flags,
    expired_instruments,
    futures_positioning,
    option_positioning,
    option_strike_levels,
    parse_option,
    positioning_component,
    stored_rows,
    summarize,
)

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
FORWARD = 84000.0


def option(name, oi, iv, forward=FORWARD):
    # Shape of Deribit public/get_book_summary_by_currency?kind=option rows.
    return {'instrument_name': name, 'open_interest': oi, 'mark_iv': iv, 'underlying_price': forward,
            'estimated_delivery_price': forward, 'base_currency': 'BTC', 'quote_currency': 'BTC'}


def smile(expiry, put_iv=55.0, call_iv=45.0):
    rows = []
    for strike in range(60000, 110001, 2000):
        iv = put_iv if strike < FORWARD else call_iv
        rows += [option(f'BTC-{expiry}-{strike}-C', 100 if strike == 90000 else 5, iv),
                 option(f'BTC-{expiry}-{strike}-P', 300 if strike == 76000 else 5, iv)]
    return rows


FUTURES = [
    {'instrument_name': 'BTC-PERPETUAL', 'mark_price': 84046.56, 'estimated_delivery_price': 84031.26,
     'open_interest': 886837130, 'current_funding': 0.0, 'funding_8h': 0.0007, 'quote_currency': 'USD'},
    {'instrument_name': 'BTC-27SEP26', 'mark_price': 84015.5, 'estimated_delivery_price': 84031.26,
     'open_interest': 1348460, 'quote_currency': 'USD'},
    {'instrument_name': 'BTC-30OCT26', 'mark_price': 84404.15, 'estimated_delivery_price': 84031.26,
     'open_interest': 72440770, 'quote_currency': 'USD'},
    {'instrument_name': 'BTC-25SEP26', 'mark_price': 1, 'estimated_delivery_price': 1, 'open_interest': 5},
    {'instrument_name': 'BTC_USDC-PERPETUAL', 'mark_price': 84000, 'estimated_delivery_price': 84000},
]


def test_instrument_parsing_handles_short_days_and_decimal_strikes():
    assert parse_option('BTC-9OCT26-65000-P')['expiry'] == datetime(2026, 10, 9, 8, tzinfo=UTC)
    assert parse_option('ETH-30OCT26-1d5-C')['strike'] == 1.5
    assert parse_option('BTC-PERPETUAL') is None and parse_option('BTC-30OCT26-65000-X') is None


def test_black76_forward_delta_and_invalid_inputs():
    greeks = black76(100, 100, 50, 0.25)
    assert greeks['call_delta'] == pytest.approx(0.5497, abs=1e-3)  # N(sigma*sqrt(T)/2)
    assert greeks['put_delta'] == pytest.approx(greeks['call_delta'] - 1)
    assert black76(100, 100, 0, 0.25) is None


def test_per_expiry_positioning_skew_and_concentrations():
    rows = smile('30OCT26') + smile('25DEC26', put_iv=50, call_iv=48) + [option('BTC-25SEP26-80000-C', 999, 40)]
    result = option_positioning(rows, NOW)
    assert [e['expiry'][:10] for e in result['expiries']] == ['2026-10-30', '2026-12-25']  # expired row dropped
    october = result['expiries'][0]
    assert october['put_call_ratio'] == pytest.approx(october['put_oi'] / october['call_oi'])
    assert october['skew']['put_minus_call_iv'] == pytest.approx(10)
    assert abs(october['skew']['call_delta'] - 0.25) <= 0.1 and abs(october['skew']['put_delta'] + 0.25) <= 0.1
    assert october['strike_concentrations'][0] == (76000.0, 305.0)
    assert october['gamma_concentrations'] and october['days_to_expiry'] > 30
    assert result['directional_score'] == 0 and result['label'] == 'descriptive'
    assert 'max_pain' not in str(result) and 'target' not in result
    # One level per strike: the same strike in the two nearest expiries is one wall, not two.
    both = [october['expiry'], result['expiries'][1]['expiry']]
    assert option_strike_levels({'options': result}, 2) == [
        {'strike': 76000.0, 'open_interest': 610.0, 'expiry': october['expiry'], 'expiries': both},
        {'strike': 90000.0, 'open_interest': 210.0, 'expiry': october['expiry'], 'expiries': both}]


def test_futures_funding_basis_and_open_interest():
    result = futures_positioning(FUTURES, 'BTC', NOW)
    assert result['perpetual']['funding_8h'] == 0.0007
    assert result['perpetual']['basis_pct'] == pytest.approx((84046.56 / 84031.26 - 1) * 100)
    assert [d['instrument'] for d in result['dated']] == ['BTC-27SEP26', 'BTC-30OCT26']
    october = result['dated'][1]
    assert october['annualized_basis_pct'] == pytest.approx(october['basis_pct'] * 365 / october['days_to_expiry'], rel=1e-4)
    assert result['total_open_interest'] == 886837130 + 1348460 + 72440770 and result['directional_score'] == 0


def test_crowding_flags_and_component_are_deterministic_and_fresh_only():
    options = option_positioning(smile('30OCT26', put_iv=60, call_iv=45), NOW)
    futures = futures_positioning(FUTURES, 'BTC', NOW)
    flags = {f['flag']: f for f in crowding_flags(options, futures)}
    assert set(flags) == {'PERP_FUNDING_HIGH', 'PUT_SKEW_ELEVATED', 'PUT_HEAVY_OI'}
    # Skew and put/call are descriptive: they never contribute direction.
    assert flags['PUT_SKEW_ELEVATED']['points'] == flags['PUT_HEAVY_OI']['points'] == 0
    summary = summarize('BTC', smile('30OCT26', 60, 45), FUTURES, NOW, NOW - timedelta(seconds=60))
    component = positioning_component(summary)
    assert component == {'value': -20, 'status': 'measured', 'basis': component['basis']} and 'contrarian' in component['basis']
    stale = summarize('BTC', smile('30OCT26'), FUTURES, NOW, NOW - timedelta(hours=2))
    assert stale['freshness_status'] == 'STALE' and positioning_component(stale)['status'] == 'missing'
    assert positioning_component(summarize('BTC', [], [], NOW, None)) == {
        'value': None, 'status': 'missing', 'basis': 'Fresh derivatives positioning unavailable'}


def test_stored_rows_split_and_expired_pruning(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/deriv.db')
    store.initialize()
    stamp = NOW.isoformat()
    rows = {r['instrument_name']: dict(r, ingested_at=stamp) for r in smile('30OCT26')[:4] + FUTURES[:2]}
    rows['ETH-30OCT26-3000-C'] = dict(option('ETH-30OCT26-3000-C', 1, 50), ingested_at=stamp)
    store.put_many('derivative_metrics', rows)
    options, futures, as_of = stored_rows(store, 'BTC')
    assert len(options) == 4 and [f['instrument_name'] for f in futures] == ['BTC-PERPETUAL', 'BTC-27SEP26'] and as_of == NOW
    assert expired_instruments(options + futures, NOW + timedelta(days=2)) == ['BTC-27SEP26']


def test_expiry_parsing_is_locale_independent_and_strict():
    from backend.positioning import parse_expiry
    assert parse_expiry('27DEC24') == datetime(2024, 12, 27, 8, tzinfo=UTC)
    assert parse_expiry('31FEB26') is None and parse_expiry('27Dec24') is None and parse_expiry('PERPETUAL') is None


def test_stored_rows_use_latest_snapshot_so_leftover_rows_do_not_go_stale(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/snap.db')
    store.initialize()
    old, fresh = (NOW - timedelta(days=3)).isoformat(), NOW.isoformat()
    rows = {r['instrument_name']: dict(r, ingested_at=fresh) for r in smile('30OCT26')[:2]}
    rows['BTC-25SEP26-80000-C'] = dict(option('BTC-25SEP26-80000-C', 9, 40), ingested_at=old)  # expired, never pruned
    store.put_many('derivative_metrics', rows)
    options, futures, as_of = stored_rows(store, 'BTC')
    assert sorted(o['instrument_name'] for o in options) == sorted(smile('30OCT26')[i]['instrument_name'] for i in range(2))
    assert futures == [] and as_of == NOW
    assert summarize('BTC', options, futures, NOW + timedelta(seconds=30), as_of)['freshness_status'] == 'FRESH'


def test_a_snapshot_written_after_the_readers_clock_is_current_not_stale():
    """Regression (UI audit REG-assets-1): a snapshot ingested after the caller took `now` (a decision cycle or an asset
    page read in progress) had a small negative age and was labelled STALE, so the cycle recorded positioning missing."""
    from backend.config import rules
    tolerance = rules()['positioning']['future_tolerance_seconds']
    just_written = summarize('BTC', smile('30OCT26'), FUTURES, NOW, NOW + timedelta(seconds=0.9))
    assert just_written['freshness_status'] == 'FRESH' and just_written['age_seconds'] == 0
    assert positioning_component(just_written)['status'] == 'measured'
    too_far = summarize('BTC', smile('30OCT26'), FUTURES, NOW, NOW + timedelta(seconds=tolerance + 1))
    assert too_far['freshness_status'] == 'STALE'  # a clock problem is never fresh
    assert just_written['reason'].endswith('.')  # a full sentence the page can append to
