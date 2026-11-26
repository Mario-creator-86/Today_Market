import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from backend.config import rules
from backend.providers.positioning_sources import (
    RESTRICTED,
    BinanceFuturesProvider,
    CFTCCOTProvider,
    CoinbaseIntxProvider,
    PositioningProvider,
    RateGuard,
    build_cot_records,
    cot_model,
    percentile_rank,
    primary_cot,
)

NOW = datetime(2026, 9, 26, 20, 30, tzinfo=UTC)
LATEST = date(2026, 9, 22)


def respond(monkeypatch, handler):
    """handler(method, url, kwargs) -> (status, body) or (status, body, headers). No network in tests."""
    calls = []

    async def request(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        status, body, *rest = handler(method, url, kwargs)
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return httpx.Response(status, content=content, headers=rest[0] if rest else None, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    return calls


def cot_row(report: str, code: str, name: str, day: date, net_spec: int, change: int = 0) -> dict:
    """A vendor-shaped row (numbers as strings, floating timestamp) for every field the report model needs."""
    groups = rules()['positioning_sources']['cftc_cot']['reports'][report]['groups']
    row = {'id': f'{day:%y%m%d}{code}F', 'report_date_as_yyyy_mm_dd': f'{day.isoformat()}T00:00:00.000',
           'cftc_contract_market_code': code, 'contract_market_name': name, 'market_and_exchange_names': f'{name} - CME',
           'contract_units': '(CONTRACTS OF 12,500,000 JAPANESE YEN)', 'open_interest_all': '400000',
           'change_in_open_interest_all': '1000'}
    for spec in groups.values():
        for key, field in spec.items():
            row[field] = '0' if key.startswith('change') else '10000'
    spec_group = {'tff': 'leveraged_money', 'disaggregated': 'managed_money', 'legacy': 'noncommercial'}[report]
    fields = groups[spec_group]
    row[fields['long']] = str(50000 + max(net_spec, 0))
    row[fields['short']] = str(50000 + max(-net_spec, 0))
    row[fields['change_long']] = str(change)
    row[fields['change_short']] = '0'
    return row


def test_config_fragment_loads_and_contract_codes_map_to_tracked_symbols():
    config = rules()['positioning_sources']
    tracked = {asset[0] for asset in rules()['assets']}
    contracts = config['cftc_cot']['contracts']
    assert len(contracts) == 20 and {c['symbol'] for c in contracts.values()} <= tracked
    assert all(c['primary'] in c['reports'] for c in contracts.values())
    assert config['binance_futures']['request_weight_limit_per_minute'] == 2400


def test_percentile_rank_and_rate_guard():
    assert percentile_rank(5, [1, 2, 3, 4, 5]) == 100 and percentile_rank(1, [1, 2, 3, 4]) == 25
    assert percentile_rank(1, []) is None
    guard = RateGuard(10, 60)
    assert guard.try_spend(6, now=0) and not guard.try_spend(5, now=1) and guard.try_spend(4, now=1)
    assert guard.try_spend(10, now=61)  # the window slid
    guard.block(30, now=61)
    assert not guard.try_spend(1, now=80) and guard.try_spend(0, now=121)


def test_cot_records_are_weekly_descriptive_with_net_change_and_percentile():
    model = cot_model('tff')
    raw = [cot_row('tff', '097741', 'JAPANESE YEN', LATEST - timedelta(weeks=i), net_spec=1000 * (60 - i),
                   change=1000 if i == 0 else 0) for i in range(60)]
    rows = [model.model_validate(r).model_dump(mode='json') for r in raw]
    record = build_cot_records('tff', rows, NOW)[0]
    assert record['symbol'] == 'USDJPY' and record['relation'] == 'inverse' and record['primary']
    assert record['as_of'] == '2026-09-22T20:00:00+00:00'  # Tuesday 16:00 New York (EDT) in UTC
    assert record['scheduled_release_at'] == '2026-09-25T19:30:00+00:00'  # Friday 15:30 ET
    assert record['delay'] == 'weekly, as of Tue 22 Sep' and record['live'] is False and not record['stale']
    lev = record['groups']['leveraged_money']
    assert lev['net'] == 60000 and lev['net_change_wow'] == 1000 and lev['net_percentile_3y'] == 100
    assert lev['percentile_weeks'] == 60 and lev['net_pct_oi'] == 15.0
    assert record['attribution'].startswith('Source: CFTC') and record['directional_score'] == 0
    assert record['label'] == 'descriptive'
    later = build_cot_records('tff', rows, NOW + timedelta(days=14))[0]
    assert later['stale'] is True
    short = build_cot_records('tff', rows[:10], NOW)[0]
    assert short['groups']['leveraged_money']['net_percentile_3y'] is None
    assert short['percentile_basis'].startswith('insufficient history')


@pytest.mark.asyncio
async def test_cftc_provider_queries_socrata_validates_rows_and_throttles(monkeypatch):
    def handler(method, url, kwargs):
        if 'gpe5-46if' in url:
            rows = [cot_row('tff', '097741', 'JAPANESE YEN', LATEST - timedelta(weeks=i), net_spec=-500 * i) for i in range(3)]
            rows.append(dict(rows[0], lev_money_positions_long='-5'))  # negative position: rejected
            return 200, rows
        if '72hh-3qpy' in url:
            return 200, [cot_row('disaggregated', '088691', 'GOLD', LATEST, net_spec=127389, change=-5727),
                         {'id': 'x', 'report_date_as_yyyy_mm_dd': 'garbage'}]
        return 200, [cot_row('legacy', '088691', 'GOLD', LATEST, net_spec=225000)]
    calls = respond(monkeypatch, handler)
    provider = CFTCCOTProvider()
    assert isinstance(provider, PositioningProvider)
    records = await provider.positioning(NOW)
    assert provider.health['status'] == 'ONLINE' and provider.health['rejected_rows'] == 2
    where = calls[0][2]['params']['$where']
    assert "'097741'" in where and "'13874A'" in where and 'report_date_as_yyyy_mm_dd >=' in where
    primary = primary_cot(records)
    assert set(primary) == {'GOLD', 'USDJPY'}
    gold = primary['GOLD']
    assert gold['report'] == 'disaggregated' and gold['groups']['managed_money']['net'] == 127389
    assert gold['groups']['managed_money']['net_change_wow'] == -5727
    assert [r['report'] for r in records if r['symbol'] == 'GOLD' and not r['primary']] == ['legacy']
    count = len(calls)
    assert await provider.positioning(NOW) == records and len(calls) == count  # min interval: cached, no new calls
    # Past the min interval but before the next scheduled release (Fri 2026-10-02 19:30Z): weekly data is kept.
    provider._refreshed -= 7200
    assert provider.next_release(LATEST) == datetime(2026, 10, 2, 19, 30, tzinfo=UTC)
    later = await provider.positioning(datetime(2026, 10, 2, 19, 0, tzinfo=UTC))
    assert len(calls) == count and later[0]['as_of_date'] == '2026-09-22'
    # Stale flag is recomputed for the reading time even on cached copies.
    assert all(not r['stale'] for r in later)
    assert all(r['stale'] for r in await provider.positioning(datetime(2026, 10, 4, tzinfo=UTC)))
    assert len(calls) == count + 3  # after the release time a refresh happens (one call per report)


@pytest.mark.asyncio
async def test_cftc_non_list_payload_and_request_cap(monkeypatch):
    respond(monkeypatch, lambda m, u, k: (200, {'error': True, 'message': 'query timeout'}))
    provider = CFTCCOTProvider()
    with pytest.raises(Exception, match='not a list'):
        await provider.positioning(NOW)
    calls = respond(monkeypatch, lambda m, u, k: (200, []))
    provider.guard = RateGuard(1, 3600)
    with pytest.raises(Exception, match='local request budget exhausted'):
        await provider.positioning(NOW)
    assert len(calls) == 1  # the cap stops the second report's call


PREMIUM = {'symbol': 'BTCUSDT', 'markPrice': '84000.0', 'indexPrice': '84042.0', 'estimatedSettlePrice': '84004.05',
           'lastFundingRate': '0.00010000', 'interestRate': '0.00010000', 'nextFundingTime': 1790467200000, 'time': 1790456008000}


def binance_handler(url, kwargs, weight='5'):
    symbol = kwargs.get('params', {}).get('symbol')
    headers = {'x-mbx-used-weight-1m': weight}
    if url.endswith('/premiumIndex'):
        if 'dapi' in url:
            return 200, [dict(PREMIUM, symbol='BTCUSD_PERP', pair='BTCUSD')], headers  # ETHUSD_PERP gets the wrong row
        return 200, dict(PREMIUM, symbol=symbol), headers
    if url.endswith('/openInterest'):
        return 200, {'symbol': 'BTCUSDT', 'openInterest': '94301.369', 'time': 1790456002952}, headers
    if url.endswith('/openInterestHist'):
        return 200, [{'symbol': 'BTCUSDT', 'sumOpenInterest': '100', 'sumOpenInterestValue': '8400000', 'timestamp': 1790449200000},
                     {'symbol': 'BTCUSDT', 'sumOpenInterest': '110', 'sumOpenInterestValue': '9240000', 'timestamp': 1790452800000}]
    if url.endswith('/globalLongShortAccountRatio'):
        return 200, [{'longAccount': '0.5629', 'longShortRatio': '1.2878', 'shortAccount': '0.4371', 'timestamp': 1790452800000},
                     {'longAccount': '2', 'longShortRatio': '1', 'shortAccount': '0.1', 'timestamp': 1790452800001}]
    return 404, {}


@pytest.mark.asyncio
async def test_binance_futures_funding_basis_open_interest_and_hourly_stats(monkeypatch):
    calls = respond(monkeypatch, lambda m, url, k: binance_handler(url, k))
    provider = BinanceFuturesProvider()
    records = await provider.positioning(NOW)
    btc = records[0]
    assert btc['status'] == 'ok' and btc['instrument'] == 'BTCUSDT' and btc['venue'] == 'Binance USDⓈ-M'
    assert btc['basis_bps'] == pytest.approx(-4.998, abs=0.01) and btc['funding_rate_bps'] == 1.0
    assert btc['as_of'] == '2026-09-26T20:53:28+00:00' and btc['open_interest'] == pytest.approx(94301.369)
    assert btc['next_funding_time'] == '2026-09-27T00:00:00+00:00' and 'not spot' in btc['delay']
    assert btc['open_interest_hist']['change_pct_vs_prev_bucket'] == 10.0
    assert btc['open_interest_hist']['delay'] == 'hourly bucket'
    assert btc['long_short_accounts']['long_short_ratio'] == pytest.approx(1.2878)
    assert provider.health['rejected_rows'] >= 1  # the impossible long/short row
    # ETHUSDT's openInterest mock returns the BTC symbol: mismatched rows are ignored, not mislabelled.
    assert records[1]['status'] == 'ok' and records[1]['open_interest'] is None
    assert records[3]['status'] == 'unavailable' and 'invalid premiumIndex' in records[3]['reason']
    coinm = records[2]
    assert coinm['venue'] == 'Binance COIN-M' and coinm['status'] == 'ok' and coinm['mark_price'] == 84000.0
    # COIN-M /futures/data was observed frozen live: it is not requested and the record says why.
    assert coinm['open_interest_hist'] is None and 'COIN-M hourly statistics not used' in coinm['stats_note']
    assert not any('dapi' in c[1] and '/futures/data/' in c[1] for c in calls)
    assert provider.health['used_weight_1m'] == 5 and all(r['directional_score'] == 0 for r in records)
    assert all(r['delay'] for r in records) and records[3]['as_of'] is None and records[3]['live'] is False
    # Returned records are deep copies: mutating nested data never corrupts the cache.
    records[0]['open_interest_hist']['sum_open_interest'] = -1
    assert (await provider.positioning(NOW))[0]['open_interest_hist']['sum_open_interest'] == 110
    # Hourly statistics are not refetched until a newer bucket can exist.
    provider._refreshed = None
    before = len(calls)
    await provider.positioning(NOW)
    assert not any('/futures/data/' in c[1] for c in calls[before:])


@pytest.mark.asyncio
async def test_binance_frozen_statistics_series_is_withheld_not_shown_as_hourly(monkeypatch):
    def handler(method, url, kwargs):
        if url.endswith('/openInterestHist'):
            return 200, [{'sumOpenInterest': '25758909', 'sumOpenInterestValue': str(30000 + i), 'timestamp': 1790445600000 + i * 3600000}
                         for i in range(3)]
        return binance_handler(url, kwargs)
    respond(monkeypatch, handler)
    btc = (await BinanceFuturesProvider().positioning(NOW))[0]
    assert btc['status'] == 'ok' and btc['open_interest_hist'] is None and btc['long_short_accounts'] is None
    assert 'open interest series unchanged' in btc['stats_note'] and btc['open_interest'] == pytest.approx(94301.369)


@pytest.mark.asyncio
async def test_binance_451_is_surfaced_as_restricted_location_and_backs_off(monkeypatch):
    calls = respond(monkeypatch, lambda m, u, k: (451, {'code': 0, 'msg': 'Service unavailable from a restricted location'}))
    provider = BinanceFuturesProvider()
    records = await provider.positioning(NOW)
    assert len(records) == 4 and all(r['status'] == 'unavailable' and r['reason'] == RESTRICTED for r in records)
    assert all(r['as_of'] is None and r['live'] is False for r in records)
    assert provider.health['status'] == 'DEGRADED' and provider.health['restricted'] is True
    assert RESTRICTED in provider.health['error'] and len(calls) == 1  # no retries on 451
    provider._refreshed = None
    await provider.positioning(NOW)
    assert len(calls) == 1  # still backing off: no request sent


@pytest.mark.asyncio
async def test_binance_request_weight_header_pauses_calls(monkeypatch):
    calls = respond(monkeypatch, lambda m, url, k: binance_handler(url, k, weight='2000'))
    provider = BinanceFuturesProvider()
    # First premiumIndex saw 2000 >= our 600 budget: every further call is refused locally and nothing is sent.
    with pytest.raises(Exception, match='local request budget exhausted'):
        await provider.positioning(NOW)
    assert len(calls) == 1 and provider.weight.blocked_until > 0


@pytest.mark.asyncio
async def test_binance_raises_when_every_instrument_fails(monkeypatch):
    respond(monkeypatch, lambda m, u, k: (400, {'code': -1121, 'msg': 'Invalid symbol.'}))
    with pytest.raises(Exception, match='HTTP 400'):
        await BinanceFuturesProvider().positioning(NOW)


INTX = {'symbol': 'BTC-PERP', 'type': 'PERP', 'base_asset_name': 'BTC', 'open_interest': '1074.26', 'funding_interval': '3600000000000',
        'quote': {'mark_price': '84005.1', 'index_price': '83984.3', 'predicted_funding': '0.000007',
                  'timestamp': '2026-09-26T20:53:55.201Z'}}


@pytest.mark.asyncio
async def test_coinbase_intx_cross_check_and_validation(monkeypatch):
    def handler(method, url, kwargs):
        if url.endswith('/funding'):
            return 200, {'results': [{'funding_rate': '0.000006', 'mark_price': '84018.6', 'event_time': '2026-09-26T20:00:00Z'},
                                     {'funding_rate': '0.000007', 'mark_price': '84006.5', 'event_time': '2026-09-26T19:00:00Z'}]}
        if 'ETH-PERP' in url:
            return 200, dict(INTX, symbol='ETH-PERP', quote=dict(INTX['quote'], timestamp='2026-09-26 20:53:55'))  # naive
        return 200, INTX
    respond(monkeypatch, handler)
    provider = CoinbaseIntxProvider()
    btc = await provider.instrument('BTC', 'BTC-PERP', NOW)
    assert btc['role'] == 'cross-check' and btc['as_of'] == '2026-09-26T20:53:55.201000+00:00'
    assert btc['basis_bps'] == pytest.approx(2.48, abs=0.01) and btc['funding_interval_hours'] == 1.0
    assert btc['last_funding_rate'] == 6e-06 and btc['last_funding_time'] == '2026-09-26T20:00:00+00:00'
    assert btc['open_interest_units'] == 'BTC' and btc['attribution'].startswith('Source: Coinbase International')
    with pytest.raises(Exception, match='invalid instrument payload'):
        await provider.instrument('ETH', 'ETH-PERP', NOW)
    assert provider.health['rejected_rows'] == 1


@pytest.mark.asyncio
async def test_coinbase_intx_partial_failure_and_missing_funding(monkeypatch):
    def handler(method, url, kwargs):
        if url.endswith('/funding'):
            return 200, {'unexpected': 'shape'}  # funding history is secondary: tolerated and counted
        if 'ETH-PERP' in url:
            return 500, {}
        return 200, INTX
    respond(monkeypatch, handler)
    provider = CoinbaseIntxProvider()
    btc, eth = await provider.positioning(NOW)
    assert btc['status'] == 'ok' and btc['last_funding_rate'] is None and btc['predicted_funding_rate'] == 7e-06
    assert provider.health['rejected_rows'] == 1
    assert eth['status'] == 'unavailable' and eth['as_of'] is None and eth['live'] is False and 'HTTP 500' in eth['reason']
    respond(monkeypatch, lambda m, u, k: (200, b'<html>maintenance</html>'))
    with pytest.raises(Exception, match='invalid JSON body'):
        await CoinbaseIntxProvider().positioning(NOW)
