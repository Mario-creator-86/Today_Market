"""Regime last-close basis (docs/MARKET_COVERAGE.md, Regime): close-to-close changes of official daily closes plus
configured proxy changes (direction only), used only when the live basis is insufficient, and INSUFFICIENT_DATA
instead of a MIXED posture that no rule was evaluated for."""
from datetime import UTC, datetime, timedelta

import pytest

from backend.regime import classify, evaluate, features

SUNDAY = datetime(2026, 9, 27, 12, tzinfo=UTC)
FRIDAY_CLOSE = datetime(2026, 9, 25, 20, tzinfo=UTC)  # 16:00 New York
THURSDAY_CLOSE = datetime(2026, 9, 24, 20, tzinfo=UTC)


def at(*parts):
    return datetime(*parts, tzinfo=UTC)


def close(symbol, price, provider='treasury', data_class='DAILY_CLOSE', status='CURRENT', state='FRIDAY CLOSE',
          stamp=FRIDAY_CLOSE, **change):
    quote = {'price': price, 'timestamp': stamp.isoformat(), 'provider': provider, 'data_class': data_class,
             'close_status': status, 'close_date': stamp.date().isoformat(), 'freshness_status': 'STALE', 'market_state': state,
             'session': {'state': state}, **change}
    return {'symbol': symbol, 'quote': quote, 'session': {'state': state}, 'proxy': None}


def proxied(symbol, via, change_pct, change_basis='previous_close', provider='finnhub', stamp=FRIDAY_CLOSE):
    return {'symbol': symbol, 'quote': None, 'session': {'state': 'FRIDAY CLOSE'},
            'proxy': {'symbol': via, 'price': 580.0, 'change_pct': change_pct, 'change_basis': change_basis,
                      'timestamp': stamp.isoformat(), 'provider': provider, 'market_status': 'FRIDAY CLOSE'}}


def weekend_assets():
    btc = {'symbol': 'BTC', 'session': {'state': 'LIVE'}, 'proxy': None,
           'quote': {'price': 65000, 'change_pct': 2.0, 'change_basis': 'rolling_24h', 'freshness_status': 'FRESH',
                     'data_class': 'LIVE', 'provider': 'binance', 'timestamp': SUNDAY.isoformat(), 'session': {'state': 'LIVE'}}}
    return [close('US2Y', 3.5, change_bp=-6.0), close('US10Y', 4.1, change_bp=-4.0),
            close('EURUSD', 1.18, provider='ecb', change_pct=-0.35),
            close('DXY', 97.9, provider='ecb-derived', data_class='DERIVED_DAILY', change_pct=0.5),
            proxied('SP500', 'SPY', -0.8), proxied('GOLD', 'PAXG', 0.4, 'rolling_24h', 'binance', SUNDAY), btc]


def test_last_close_basis_uses_closes_and_labelled_proxies():
    result = evaluate(weekend_assets(), [], SUNDAY, last_close=True)
    assert result['basis'] == 'last_close'
    assert result['basis_label'] == 'last close Fri 25 Sep + proxies (PAXG, SPY), markets closed'
    features_ = result['features']
    assert features_['SP500'] == {'change_pct': -0.8} and features_['GOLD'] == {'change_pct': 0.4}  # never a level
    assert features_['US2Y']['change_bp'] == -6.0 and features_['CURVE:US10Y-US2Y']['change_bp'] == pytest.approx(2.0)
    assert 'BTC' not in features_ and result['excluded'][0]['feature'] == 'BTC'  # rolling 24h is not close-to-close
    sources = result['feature_sources']
    assert sources['SP500']['basis'] == 'proxy' and sources['SP500']['via'] == 'SPY'
    assert sources['GOLD']['change_basis'] == 'rolling_24h' and sources['GOLD']['via'] == 'PAXG'
    assert sources['US2Y']['basis'] == 'last_close' and sources['US2Y']['source'] == 'treasury'
    assert set(result['regimes']) == {'RISK_OFF', 'USD_STRENGTH'} and result['posture'] == 'DEFENSIVE'
    low_vol = {r['regime']: r for r in result['evaluated']}['LOW_VOLATILITY']
    assert low_vol['state'] == 'UNAVAILABLE' and 'VIX' in result['missing_required']  # no proxy for a level
    drivers = {d['driver']: d for d in result['drivers']}
    assert drivers['Rates']['input'] == 'US2Y −6 bp, US10Y −4 bp' and drivers['Rates']['basis'] == 'last_close'
    assert drivers['Rates']['direction'] == 'down' and drivers['USD']['input'] == 'DXY +0.50%'
    assert drivers['Risk appetite']['input'] == 'SP500 −0.80% via SPY'
    assert drivers['Risk appetite']['inputs'][0]['basis'] == 'proxy'


def test_decisions_keep_the_live_basis():
    live = evaluate(weekend_assets(), [], SUNDAY)  # what decision scoring calls: no last_close
    assert live['basis'] == 'live' and live['posture'] == 'INSUFFICIENT_DATA' and live['regimes'] == []


def test_stale_close_in_a_live_session_and_unapproved_proxy_bases_are_left_out():
    friday, fix = at(2026, 9, 25, 16), at(2026, 9, 25, 12, 15)
    assets = [close('EURUSD', 1.18, provider='ecb', status='STALE', state='LIVE', stamp=at(2026, 9, 23, 12, 15),
                    change_pct=-0.35),
              close('DXY', 97.9, provider='ecb-derived', data_class='DERIVED_DAILY', change_pct=0.5, state='LIVE', stamp=fix),
              close('US2Y', 3.5, change_bp=-6.0, state='LIVE', status='CURRENT', stamp=THURSDAY_CLOSE),
              proxied('SP500', 'SPY', -0.8, change_basis='provider', stamp=friday)]
    result = evaluate(assets, [], friday, last_close=True)
    assert 'EURUSD' not in result['features'] and 'SP500' not in result['features']
    assert {'DXY', 'US2Y'} <= result['features'].keys() and not result['basis_label'].endswith('markets closed')


def test_no_evaluable_rule_is_insufficient_data_not_mixed():
    observed = features([{'symbol': s, 'quote': {'price': 100.0, 'change_pct': c, 'freshness_status': 'FRESH'}}
                         for s, c in [('BTC', 2.0), ('GOLD', 0.5), ('EURUSD', 0.2)]])
    result = classify(observed, [], SUNDAY)
    assert result['posture'] == 'INSUFFICIENT_DATA' and result['regimes'] == [] and result['confidence'] is None
    assert {'SP500', 'DXY', 'VIX', 'BRENT'} <= set(result['missing_required'])
    assert all(r['state'] == 'UNAVAILABLE' for r in result['evaluated'])


def test_current_weekly_close_counts_whatever_its_age_but_an_old_close_needs_a_closed_market():
    tuesday = at(2026, 9, 29, 23)  # the EIA close of Tue 22 is still the latest expected publication
    assets = [close('WTI', 96.41, provider='eia', state='LIVE', stamp=at(2026, 9, 22, 20), change_pct=-0.58),
              close('BRENT', 114.89, provider='eia', status='STALE', state='FRIDAY CLOSE', stamp=at(2026, 9, 15, 20),
                    change_pct=1.2),
              close('US2Y', 3.5, change_bp=-6.0, state='LIVE', stamp=at(2026, 9, 29, 20))]
    result = evaluate(assets, [], tuesday, last_close=True)
    assert {'WTI', 'US2Y'} <= result['features'].keys() and 'BRENT' not in result['features']  # 14 days > max_age_days


# --- live basis sufficiency: only live features count (review finding: FRED credit / stale-bar correlation) ---------

SATURDAY = datetime(2026, 9, 26, 12, tzinfo=UTC)
BAA10Y = {'name': 'BAA10Y', 'value': 1.62, 'change_bp': 4.0, 'freshness_status': 'FRESH', 'as_of': '2026-09-24T00:00:00+00:00',
          'attribution': 'Source: FRED, Federal Reserve Bank of St. Louis'}


def fred_weekend_assets():
    """Saturday with a FRED key: BTC streams, the equity/vol closes are FRED's latest, Treasury's Friday close."""
    btc = {'symbol': 'BTC', 'session': {'state': 'LIVE'}, 'proxy': None,
           'quote': {'price': 65000, 'change_pct': 0.4, 'change_basis': 'rolling_24h', 'freshness_status': 'FRESH',
                     'data_class': 'LIVE', 'provider': 'binance', 'timestamp': SATURDAY.isoformat(), 'session': {'state': 'LIVE'}}}
    return [btc, close('SP500', 6500.0, provider='fred', stamp=THURSDAY_CLOSE, change_pct=-1.2),
            close('NASDAQ', 21500.0, provider='fred', stamp=THURSDAY_CLOSE, change_pct=-1.5),
            close('VIX', 22.0, provider='fred', stamp=THURSDAY_CLOSE, change_pct=12.0),
            close('US10Y', 4.1, change_bp=-6.0)]


def friday_bars():
    """Friday's one-minute BTC/NASDAQ bars (the Massive setup): aligned, but NASDAQ stopped updating at the close."""
    times = [(FRIDAY_CLOSE.replace(hour=19) + timedelta(minutes=m)).isoformat() for m in range(30)]
    return {'BTC': [{'time': t, 'close': 65000 + 10 * i + (i % 3)} for i, t in enumerate(times)],
            'NASDAQ': [{'time': t, 'close': 21500 + 5 * i + (i % 3)} for i, t in enumerate(times)]}


def test_daily_credit_spread_never_makes_the_live_basis_sufficient():
    result = evaluate(fred_weekend_assets(), [], SATURDAY, credit=[BAA10Y], last_close=True)
    assert result['basis'] == 'last_close' and result['basis_label'].startswith('last close')
    assert set(result['regimes']) == {'RISK_OFF', 'GROWTH_SCARE', 'VOLATILITY_EXPANSION'}
    assert result['posture'] == 'DEFENSIVE'
    credit = result['feature_sources']['CREDIT:BAA10Y']  # the spread is a daily close: kept, labelled as such
    assert credit['basis'] == 'last_close' and credit['provider'] == 'fred'
    assert result['features']['CREDIT:BAA10Y'] == {'value': 1.62, 'change_bp': 4.0}
    assert {s['basis'] for s in result['feature_sources'].values()} == {'last_close'}  # bases never mixed
    decisions = evaluate(fred_weekend_assets(), [], SATURDAY, credit=[BAA10Y])  # decision scoring: live basis only
    assert decisions['basis'] == 'live' and decisions['posture'] == 'INSUFFICIENT_DATA'


def test_correlation_of_bars_without_fresh_quotes_never_counts_as_live():
    assets = fred_weekend_assets()
    stale_nasdaq = {'price': 21520.0, 'change_pct': -1.5, 'freshness_status': 'STALE', 'data_class': 'LIVE',
                    'provider': 'massive', 'timestamp': FRIDAY_CLOSE.isoformat(), 'session': {'state': 'FRIDAY CLOSE'}}
    assets[2] = {'symbol': 'NASDAQ', 'quote': stale_nasdaq, 'session': {'state': 'FRIDAY CLOSE'}, 'proxy': None}
    result = evaluate(assets, [], SATURDAY, bars=friday_bars(), credit=[BAA10Y], last_close=True)
    assert result['basis'] == 'last_close' and result['posture'] == 'DEFENSIVE'
    assert not any(name.startswith('CORR:') for name in result['features'])  # live-only: never in the last-close basis
    live = evaluate(assets, [], SATURDAY, bars=friday_bars())
    assert 'CORR:BTC:NASDAQ' not in live['features']  # the NASDAQ leg is not FRESH


def test_live_correlation_counts_when_both_legs_are_fresh():
    stamp = FRIDAY_CLOSE.replace(hour=19, minute=30)
    fresh = [{'symbol': s, 'session': {'state': 'LIVE'}, 'proxy': None,
              'quote': {'price': p, 'change_pct': c, 'freshness_status': 'FRESH', 'data_class': 'LIVE', 'provider': 'massive',
                        'timestamp': stamp.isoformat(), 'session': {'state': 'LIVE'}}}
             for s, p, c in [('BTC', 65000, 0.4), ('NASDAQ', 21600, 0.2)]]
    result = evaluate(fresh, [], stamp, bars=friday_bars(), last_close=True)
    assert result['basis'] == 'live' and result['feature_sources']['CORR:BTC:NASDAQ']['basis'] == 'live'


def test_credit_row_older_than_the_close_window_is_not_used():
    old = dict(BAA10Y, as_of='2026-09-10T00:00:00+00:00')  # still flagged FRESH by the last successful poll
    result = evaluate(fred_weekend_assets(), [], SATURDAY, credit=[old], last_close=True)
    assert 'CREDIT:BAA10Y' not in result['features'] and result['basis'] == 'last_close'
