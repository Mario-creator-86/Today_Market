import json
from datetime import date

import httpx
import pytest

from backend.analytics import relative_policy
from backend.config import rules
from backend.providers.base import ProviderUnavailable
from backend.providers.official_data import (
    BLSProvider,
    EcbDataProvider,
    EIAWeeklyPetroleumCalendar,
    EIAWeeklyPetroleumProvider,
    EurostatProvider,
    FredProvider,
    RateGuard,
    TreasuryFiscalProvider,
    build_central_bank_state,
    central_bank_state,
    fx_crosses,
    jsonstat_cells,
    parse_wpsr_schedule,
)
from backend.schemas import Quote

EXR_HEADER = 'KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS,TITLE\n'


def exr(day: str, rates: dict[str, float]) -> str:
    return ''.join(f'EXR.D.{c}.EUR.SP00.A,D,{c},EUR,SP00,A,{day},{v},A,"{c}/Euro, 2.15 pm (C.E.T.)"\n' for c, v in rates.items())


FULL = {'USD': 1.1403, 'GBP': 0.86045, 'JPY': 179.70, 'AUD': 1.6220, 'CAD': 1.6127, 'CHF': 0.9445, 'NZD': 2.0108,
        'SEK': 11.2900}
# 24 Sep 2026 fix (documented ECB rates for the DXY currencies; AUD and NZD are test values, not ECB data).
PREVIOUS = {'USD': 1.1367, 'GBP': 0.85986, 'JPY': 180.57, 'AUD': 1.6220, 'CAD': 1.6047, 'CHF': 0.9409, 'NZD': 2.0108,
            'SEK': 11.2645}
FM = ('KEY,FREQ,REF_AREA,CURRENCY,PROVIDER_FM,INSTRUMENT_FM,PROVIDER_FM_ID,DATA_TYPE_FM,TIME_PERIOD,OBS_VALUE\n'
      'FM.B.U2.EUR.4F.KR.DFR.LEV,B,U2,EUR,4F,KR,DFR,LEV,2026-09-16,2.5\n'
      'FM.B.U2.EUR.4F.KR.MRR_FR.LEV,B,U2,EUR,4F,KR,MRR_FR,LEV,2026-09-16,2.65\n')
YC = ('KEY,FREQ,REF_AREA,CURRENCY,PROVIDER_FM,INSTRUMENT_FM,PROVIDER_FM_ID,DATA_TYPE_FM,TIME_PERIOD,OBS_VALUE\n'
      'YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y,B,U2,EUR,4F,G_N_A,SV_C_YM,SR_2Y,2026-09-24,3.2293870\n'
      'YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y,B,U2,EUR,4F,G_N_A,SV_C_YM,SR_10Y,2026-09-24,3.5661681763\n')
HICP = ('KEY,FREQ,TIME_PERIOD,OBS_VALUE,ACTION,VALID_FROM,VALID_TO\n'
        'HICP.M.U2.N.000000.4D0.ANR,M,2026-07,2.9,Replace,2026-08-19T11:17:24.000+02:00,\n'
        'HICP.M.U2.N.000000.4D0.ANR,M,2026-08,3.2,Replace,2026-09-17T11:15:45.000+02:00,\n'
        'HICP.M.U2.N.000000.4D0.ANR,M,1996-12,,Delete,,2026-08-31T10:12:57.000+02:00\n'
        'HICP.M.U2.N.000000.4D0.ANR,M,2026-05,not-a-number,Replace,2026-06-17T11:00:00.000+02:00,\n')


def respond(monkeypatch, handler, calls: list | None = None):
    async def request(self, method, url, **kwargs):
        if calls is not None:
            calls.append((method, str(url), kwargs))
        status, body, *headers = handler(method, str(url), kwargs)
        content = body if isinstance(body, bytes) else body.encode() if isinstance(body, str) else json.dumps(body).encode()
        final = kwargs.pop('_final_url', None) or url
        return httpx.Response(status, content=content, headers=headers[0] if headers else None,
                              request=httpx.Request(method, final))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)


def test_fragment_merges_without_redefining_keys():
    config = rules()['official_data']
    assert {'ecb', 'eurostat', 'bls', 'treasury_fiscal', 'eia_wpsr', 'fred', 'central_bank_state'} <= config.keys()
    assert rules()['official_data_version']


def test_fx_crosses_convert_ecb_quotation_to_tracked_pairs():
    crosses = fx_crosses(FULL, ['EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'USDCHF', 'NZDUSD'])
    assert crosses['EURUSD'] == 1.1403
    assert crosses['GBPUSD'] == pytest.approx(1.1403 / 0.86045, abs=1e-5)
    assert crosses['USDJPY'] == pytest.approx(179.70 / 1.1403, abs=1e-3)
    assert crosses['AUDUSD'] == pytest.approx(1.1403 / 1.6220, abs=1e-5)
    assert crosses['USDCAD'] == pytest.approx(1.6127 / 1.1403, abs=1e-5)
    assert crosses['USDCHF'] == pytest.approx(0.9445 / 1.1403, abs=1e-5)
    assert crosses['NZDUSD'] == pytest.approx(1.1403 / 2.0108, abs=1e-5)
    assert 'GBPUSD' not in fx_crosses({'USD': 1.1}, ['GBPUSD'])


@pytest.mark.asyncio
async def test_ecb_fx_is_a_daily_fix_last_close_never_live(monkeypatch):
    partial = {'USD': 1.15, 'GBP': 0.87}  # newest day incomplete: must not mix fixing dates
    calls: list = []
    respond(monkeypatch, lambda m, u, k: (200, EXR_HEADER + exr('2026-09-24', PREVIOUS) + exr('2026-09-25', FULL)
                                          + exr('2026-09-28', partial)), calls)
    provider = EcbDataProvider()
    records = await provider.fx_reference()
    # 7 pairs plus the USD index replica, all from the latest complete fix
    assert {r['period'] for r in records} == {'2026-09-25'} and len(records) == 8
    eurusd = next(r for r in records if r['symbol'] == 'EURUSD')
    assert eurusd['as_of'] == '2026-09-25T12:15:00Z'  # 14:15 CEST
    assert all(r['live'] is False and r['daily_close'] and r['label'] == 'LAST CLOSE' and r['delay'] == 'daily fix'
               for r in records)
    assert eurusd['derived_via_eur'] is False and next(r for r in records if r['symbol'] == 'USDJPY')['derived_via_eur']
    assert calls[0][1].endswith('/service/data/EXR/D.USD+GBP+JPY+AUD+CAD+CHF+NZD+SEK.EUR.SP00.A')
    assert calls[0][2]['params']['format'] == 'csvdata'
    # change vs the previous complete fix, never vs an incomplete newer day
    assert eurusd['previous_close'] == 1.1367 and eurusd['previous_close_at'] == '2026-09-24T12:15:00Z'
    assert eurusd['change_pct'] == pytest.approx((1.1403 / 1.1367 - 1) * 100, abs=1e-4)
    assert eurusd['change_basis'] == 'previous_fix'
    quotes = {q['symbol']: q for q in await provider.quotes()}
    assert set(quotes) == {'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'USDCHF', 'NZDUSD', 'DXY'}
    for quote in quotes.values():
        Quote.model_validate(quote)  # the stored shape is a plain Quote with its provenance
        assert quote['daily_close'] is True and quote['previous_close'] and quote['as_of_label']
    assert quotes['EURUSD']['provider'] == 'ecb' and quotes['EURUSD']['data_class'] == 'DAILY_CLOSE'
    assert quotes['EURUSD']['attribution'] == 'Source: ECB' and quotes['EURUSD']['label'] == 'ECB reference rate'
    assert quotes['USDJPY']['label'] == 'ECB reference rate (cross via EUR)' and quotes['USDJPY']['derived'] is False
    dxy = quotes['DXY']
    assert dxy['provider'] == 'ecb-derived' and dxy['derived'] is True and dxy['data_class'] == 'DERIVED_DAILY'
    assert dxy['label'].startswith('USD index replica (ICE DXY formula)') and 'derived by this app' in dxy['label']
    assert dxy['attribution'] == 'Computed from ECB reference rates'
    assert dxy['price'] == pytest.approx(100.984, abs=0.001) and dxy['previous_close'] == pytest.approx(101.293, abs=0.001)
    assert dxy['change_pct'] == pytest.approx(-0.305, abs=0.002) and dxy['change_basis'] == 'previous_fix'
    respond(monkeypatch, lambda m, u, k: (200, EXR_HEADER + exr('2026-12-01', FULL)))
    winter = await EcbDataProvider().fx_reference()
    assert winter[0]['as_of'] == '2026-12-01T13:15:00Z'  # 14:15 CET
    assert 'previous_close' not in winter[0]  # a single fix has no change, never a made-up one


@pytest.mark.asyncio
async def test_ecb_rates_yields_and_hicp_vintages(monkeypatch):
    bodies = {'/FM/': FM, '/YC/': YC, '/HICP/': HICP}
    respond(monkeypatch, lambda m, u, k: (200, next(b for key, b in bodies.items() if key in u)))
    provider = EcbDataProvider()
    rates = {r['series']: r for r in await provider.policy_rates()}
    assert rates['DFR']['value'] == 2.5 and rates['MRR_FR']['value'] == 2.65
    assert rates['DFR']['label'] == 'POLICY RATE' and rates['DFR']['as_of'] == '2026-09-15T22:00:00Z'
    yields = {r['series']: r for r in await provider.yields()}
    assert yields['EU2Y']['value'] == pytest.approx(3.2294) and yields['EU10Y']['delay'] == 'T+1'
    hicp = await provider.hicp()
    assert [r['period'] for r in hicp] == ['2026-07', '2026-08']
    assert hicp[-1]['known_from'] == '2026-09-17T09:15:45Z' and hicp[-1]['as_of'] == hicp[-1]['known_from']
    assert provider.health['rejected_rows'] == 1  # the non-numeric row; the Delete action is not an observation


def test_central_bank_state_never_guesses_and_feeds_relative_policy():
    ecb_rates = [{'series': 'DFR', 'value': 2.5, 'period': '2026-09-16', 'as_of': '2026-09-15T22:00:00Z', 'attribution': 'Source: ECB'}]
    ecb_yields = [{'series': 'EU2Y', 'value': 3.23, 'period': '2026-09-24', 'as_of': '2026-09-24T15:30:00Z', 'attribution': 'Source: ECB'}]
    state = central_bank_state(ecb_rates, ecb_yields, None)
    assert 'rate' not in state['USD'] and 'policy_rate' in state['USD']['missing']
    assert state['EUR']['rate'] == 2.5 and state['EUR']['yield_2y'] == 3.23 and state['EUR']['verified']
    assert 'expected_next_rate' not in state['EUR'] and 'expected_next_rate' in state['EUR']['missing']
    assert relative_policy(state['USD'], state['EUR'])['bias'] == 'WAIT'
    fred = [{'series': 'DFEDTARU', 'value': 5.5, 'period': '2026-09-25', 'as_of': '2026-09-25T00:00:00Z', 'attribution': 'Source: FRED'},
            {'series': 'DFEDTARL', 'value': 5.25, 'period': '2026-09-25', 'as_of': '2026-09-25T00:00:00Z', 'attribution': 'Source: FRED'},
            {'series': 'DGS2', 'value': 4.81, 'period': '2026-09-24', 'as_of': '2026-09-24T00:00:00Z', 'attribution': 'Source: FRED'}]
    state = central_bank_state(ecb_rates, ecb_yields, fred)
    assert state['USD']['rate'] == 5.375 and state['USD']['policy_rate_range'] == [5.25, 5.5]
    assert state['USD']['yield_2y'] == 4.81 and state['USD']['verified']
    # No official source publishes the expected next rate, so the relative policy engine still waits.
    assert relative_policy(state['USD'], state['EUR'])['bias'] == 'WAIT'
    treasury = {'symbol': 'US2Y', 'price': 4.8, 'daily_close': True, 'timestamp': '2026-09-25T20:00:00Z'}
    assert central_bank_state(ecb_rates, ecb_yields, None, treasury)['USD']['yield_2y'] == 4.8


@pytest.mark.asyncio
async def test_build_state_survives_offline_sources_and_skips_unconfigured_fred(monkeypatch):
    calls: list = []
    respond(monkeypatch, lambda m, u, k: (503, 'down'), calls)
    monkeypatch.setattr('asyncio.sleep', _no_sleep)
    state = await build_central_bank_state(EcbDataProvider(), FredProvider(key=''))
    assert 'rate' not in state['EUR'] and 'rate' not in state['USD']
    assert not any('stlouisfed' in url for _, url, _ in calls)


async def _no_sleep(_seconds):
    return None


EUROSTAT = {
    'version': '2.0', 'class': 'dataset', 'updated': '2026-09-17T11:00:00+0200',
    'id': ['freq', 'unit', 'coicop18', 'release', 'geo', 'time'], 'size': [1, 1, 1, 2, 1, 3],
    'dimension': {'freq': {'category': {'index': {'M': 0}}}, 'unit': {'category': {'index': {'RCH_A': 0}}},
                  'coicop18': {'category': {'index': {'TOTAL': 0}}}, 'release': {'category': {'index': {'FIN': 0, 'FLS': 1}}},
                  'geo': {'category': {'index': {'EA': 0}}},
                  'time': {'category': {'index': {'2026-06': 0, '2026-07': 1, '2026-08': 2}}}},
    'value': {'0': 2.8, '1': 2.9, '3': 2.8, '4': 2.9, '5': 3.3}, 'status': {'3': 'e', '4': 'e', '5': 'e'}}


def test_jsonstat_cells_decode_row_major_positions():
    cells = {(c['release'], c['time']): (v, s) for c, v, s in jsonstat_cells(EUROSTAT)}
    assert cells[('FLS', '2026-08')] == (3.3, 'e') and cells[('FIN', '2026-07')] == (2.9, None)
    assert ('FIN', '2026-08') not in cells  # final not yet published: missing, not zero


@pytest.mark.asyncio
async def test_eurostat_hicp_flash_vs_final(monkeypatch):
    calls: list = []
    respond(monkeypatch, lambda m, u, k: (200, EUROSTAT), calls)
    rows = await EurostatProvider().hicp_flash()
    aug = [r for r in rows if r['period'] == '2026-08']
    assert [(r['release'], r['value']) for r in aug] == [('FLASH', 3.3)]
    assert aug[0]['delay'] == 'monthly flash release' and aug[0]['as_of'] == '2026-09-17T09:00:00Z'
    assert aug[0]['attribution'] == 'Source: Eurostat' and aug[0]['estimated']
    assert calls[0][1].endswith('/statistics/1.0/data/prc_hicp_fpd') and calls[0][2]['params']['geo'] == 'EA'


BLS_BODY = {'status': 'REQUEST_SUCCEEDED', 'message': [], 'Results': {'series': [
    {'seriesID': 'CES0000000001', 'data': [
        {'year': '2026', 'period': 'M08', 'periodName': 'August', 'latest': 'true', 'value': '159075',
         'footnotes': [{'code': 'P', 'text': 'preliminary'}]},
        {'year': '2026', 'period': 'M07', 'value': '158913', 'footnotes': [{}]},
        {'year': '2025', 'period': 'M13', 'value': '158000', 'footnotes': [{}]}]},
    {'seriesID': 'LNS14000000', 'data': [{'year': '2026', 'period': 'M08', 'latest': 'true', 'value': '4.1', 'footnotes': [{}]}]}]}}


@pytest.mark.asyncio
async def test_bls_batches_one_query_and_flags_preliminary(monkeypatch):
    monkeypatch.delenv('BLS_API_KEY', raising=False)
    calls: list = []
    respond(monkeypatch, lambda m, u, k: (200, BLS_BODY), calls)
    provider = BLSProvider()
    rows = await provider.series(2025, 2026)
    assert len(calls) == 1 and calls[0][0] == 'POST'
    payload = calls[0][2]['json']
    assert payload['seriesid'] == list(rules()['official_data']['bls']['series']) and 'registrationkey' not in payload
    nfp = next(r for r in rows if r['series'] == 'CES0000000001' and r['latest'])
    assert nfp['preliminary'] and nfp['value'] == 159075 and nfp['period'] == '2026-08'
    assert 'retrieved' in nfp['attribution'] and nfp['as_of_basis'].startswith('retrieved')
    assert provider.health['rejected_rows'] == 1  # M13 annual average
    respond(monkeypatch, lambda m, u, k: (200, {'status': 'REQUEST_NOT_PROCESSED', 'message': ['threshold']}))
    with pytest.raises(ProviderUnavailable):
        await provider.series(2025, 2026)
    assert provider.health['status'] == 'DEGRADED' and provider.health['error_type'] == 'REQUEST_NOT_PROCESSED'
    assert provider.health['error'] == 'BLS did not process the request'  # plain words; the vendor status kept apart


@pytest.mark.asyncio
async def test_bls_daily_budget_guard_refuses_before_calling(monkeypatch):
    monkeypatch.delenv('BLS_API_KEY', raising=False)
    calls: list = []
    respond(monkeypatch, lambda m, u, k: (200, BLS_BODY), calls)
    provider = BLSProvider()
    daily = rules()['official_data']['bls']['rate_limits'][0]['max_calls']
    provider.guard.calls.extend([provider.guard.clock() - 3600] * daily)
    with pytest.raises(ProviderUnavailable, match='local rate guard'):
        await provider.series(2026, 2026)
    assert calls == []


def test_rate_guard_windows_expire():
    now = [0.0]
    guard = RateGuard('x', [{'max_calls': 2, 'per_seconds': 10}], lambda: now[0])
    guard.acquire()
    guard.acquire()
    with pytest.raises(ProviderUnavailable):
        guard.acquire()
    now[0] = 10.5
    guard.acquire()
    assert guard.usage() == [{'max_calls': 2, 'per_seconds': 10.0, 'used': 1}]


DTS = {'data': [
    {'record_date': '2026-09-24', 'account_type': 'Treasury General Account (TGA) Closing Balance',
     'close_today_bal': 'null', 'open_today_bal': '924627'},
    {'record_date': '2026-09-24', 'account_type': 'Total TGA Deposits (Table II)', 'close_today_bal': 'null', 'open_today_bal': '278110'},
    {'record_date': '2026-09-24', 'account_type': 'Something else', 'close_today_bal': '1', 'open_today_bal': '1'},
    {'record_date': '2026-09-23', 'account_type': 'Treasury General Account (TGA) Closing Balance',
     'close_today_bal': 'null', 'open_today_bal': 'null'},
    {'record_date': 'Record Date', 'account_type': 'Type of Account'}]}


@pytest.mark.asyncio
async def test_treasury_tga_is_t_plus_one_and_reads_the_closing_line(monkeypatch):
    calls: list = []
    respond(monkeypatch, lambda m, u, k: (200, DTS), calls)
    provider = TreasuryFiscalProvider()
    rows = await provider.tga(date(2026, 9, 20))
    by_series = {r['series']: r for r in rows}
    assert by_series['TGA_CLOSE']['value'] == 924627 and by_series['TGA_CLOSE']['unit'] == 'USD mn'
    assert by_series['TGA_CLOSE']['label'] == 'T+1' and by_series['TGA_CLOSE']['as_of'] == '2026-09-24T20:00:00Z'
    assert by_series['TGA_DEPOSITS']['value'] == 278110 and len(rows) == 2  # blank 09-23 line is missing, not zero
    assert provider.health['rejected_rows'] == 1
    assert calls[0][2]['params']['filter'] == 'record_date:gte:2026-09-20'


TABLE1 = (b'"STUB_1","9/18/26","9/11/26","Difference","Percent Change","9/19/25","Difference","Percent Change"\n'
          b'"Crude Oil","710.950","708.386","2.564","0.400","820.712","-109.762","-13.400"\n'
          b'"Commercial (Excluding SPR)","426.398","423.429","2.969","0.700","414.754","11.644","2.800"\n'
          b'"Strategic Petroleum Reserve (SPR)","284.552","284.957","-0.405","-0.100","405.958","-121.406","-29.900"\n'
          b'"Total Motor Gasoline","206.046","207.732","-1.686","-0.800","216.569","-10.524","-4.900"\n'
          b'"Distillate Fuel Oil","107.431","","-0.428","-0.400","122.999","-15.568","-12.700"\n'
          b'"STUB_1","STUB_2","9/18/26"\n"Crude Oil Supply ","(1) Domestic Production","13,939"\n')


@pytest.mark.asyncio
async def test_eia_inventories_follow_same_host_redirect_only(monkeypatch):
    calls: list = []

    def handler(method, url, kwargs):
        kwargs['_final_url'] = 'https://ir.eia.gov/secure/wpsr/table1.csv?Policy=x'
        return 200, TABLE1, {'Last-Modified': 'Wed, 23 Sep 2026 13:37:23 GMT'}
    respond(monkeypatch, handler, calls)
    provider = EIAWeeklyPetroleumProvider()
    rows = {r['series']: r for r in await provider.inventories()}
    assert calls[0][2]['follow_redirects'] is True
    crude = rows['WPSR_CRUDE_COMMERCIAL']
    assert crude['value'] == 426.398 and crude['change'] == 2.969 and crude['week_ending'] == '2026-09-18'
    assert crude['delay'] == 'weekly, as of Fri 18 Sep' and crude['label'] == 'WEEKLY' and 'EIA' in crude['attribution']
    assert crude['file_last_modified'] == '2026-09-23T13:37:23+00:00'
    assert 'WPSR_DISTILLATE' not in rows and provider.health['rejected_rows'] == 1  # blank prior week: dropped

    def elsewhere(method, url, kwargs):
        kwargs['_final_url'] = 'https://evil.example/table1.csv'
        return 200, TABLE1
    respond(monkeypatch, elsewhere)
    with pytest.raises(ProviderUnavailable):
        await EIAWeeklyPetroleumProvider().inventories()


SCHEDULE = '''<table><tbody>
<tr class="past-holiday"><th scope="row">September 4, 2026</th><td>September 10, 2026</td><td>Thursday</td><td>12:00 p.m.</td><td>Labor Day</td></tr>
<tr><th scope="row">October 9, 2026</th>
<td>October 15, 2026</td>
<td>Thursday</td>
<td>12:00 p.m.</td><td>Columbus Day</td></tr>
<tr><th scope="row">December 18, 2026</th><td class="message-updated">December 28, 2026</td><td>Monday</td><td>5:00 p.m.</td><td>Christmas</td></tr>
</tbody></table>'''


def test_wpsr_schedule_holiday_alternates():
    exceptions = parse_wpsr_schedule(SCHEDULE)
    assert exceptions[date(2026, 10, 9)].isoformat() == '2026-10-15T16:00:00+00:00'  # 12:00 EDT
    assert exceptions[date(2026, 12, 18)].isoformat() == '2026-12-28T22:00:00+00:00'  # 17:00 EST


@pytest.mark.asyncio
async def test_wpsr_calendar_uses_standard_wednesday_and_exceptions(monkeypatch):
    respond(monkeypatch, lambda m, u, k: (200, SCHEDULE))
    rows = await EIAWeeklyPetroleumCalendar().calendar('2026-10-05', '2026-11-06')
    times = [(r['week_ending'], r['scheduled_at']) for r in rows]
    assert times[:2] == [('2026-10-02', '2026-10-07T14:30:00+00:00'), ('2026-10-09', '2026-10-15T16:00:00+00:00')]
    assert ('2026-10-30', '2026-11-04T15:30:00+00:00') in times  # after the US DST change
    assert all(r['category'] == 'MACRO' and r['provider'] == 'eia_wpsr_calendar' and r['id'] for r in rows)


FRED_BODY = {'observations': [
    {'realtime_start': '2026-09-12', 'realtime_end': '2026-09-12', 'date': '2026-08-01', 'value': '334.131'},
    {'realtime_start': '2026-09-12', 'realtime_end': '2026-09-12', 'date': '2026-07-01', 'value': '.'},
    {'realtime_start': 'bad', 'realtime_end': '2026-09-12', 'date': '2026-06-01', 'value': '332.5'}]}


@pytest.mark.asyncio
async def test_fred_not_configured_without_key(monkeypatch):
    monkeypatch.delenv('FRED_API_KEY', raising=False)
    calls: list = []
    respond(monkeypatch, lambda m, u, k: (200, FRED_BODY), calls)
    provider = FredProvider()
    assert provider.configured is False and provider.health['status'] == 'NOT CONFIGURED'
    with pytest.raises(ProviderUnavailable, match='NOT CONFIGURED'):
        await provider.latest(['DGS2'])
    assert calls == [] and len(provider.guard.calls) == 0


@pytest.mark.asyncio
async def test_fred_vintage_snapshot_is_point_in_time(monkeypatch):
    monkeypatch.setenv('FRED_API_KEY', 'test-key-not-real')
    calls: list = []
    respond(monkeypatch, lambda m, u, k: (200, FRED_BODY), calls)
    provider = FredProvider()
    rows = await provider.as_known_on('CPIAUCSL', '2026-09-12', observation_start='2026-06-01')
    params = calls[0][2]['params']
    assert params['realtime_start'] == params['realtime_end'] == '2026-09-12' and params['api_key'] == 'test-key-not-real'
    assert [r['period'] for r in rows] == ['2026-08-01']  # '.' is missing, the malformed row is rejected
    assert rows[0]['known_from'] == '2026-09-12T00:00:00Z' and rows[0]['label'] == 'OFFICIAL RELEASE'
    assert rows[0]['attribution'].startswith('Source: FRED') and provider.health['rejected_rows'] == 1
    await provider.first_releases('PAYEMS', '2026-01-01')
    assert calls[1][2]['params']['output_type'] == 4
    with pytest.raises(ValueError):
        await provider.observations('BAMLH0A0HYM2')  # ICE series are not configured (internal-use terms)
    respond(monkeypatch, lambda m, u, k: (400, {'error_message': 'Bad Request. api_key test-key-not-real'}))
    with pytest.raises(ProviderUnavailable):
        await provider.latest(['DGS10'])
    assert 'test-key-not-real' not in json.dumps(provider.health)


@pytest.mark.asyncio
async def test_fred_daily_series_are_last_close(monkeypatch):
    monkeypatch.setenv('FRED_API_KEY', 'test-key-not-real')
    respond(monkeypatch, lambda m, u, k: (200, {'observations': [
        {'realtime_start': '2026-09-25', 'realtime_end': '9999-12-31', 'date': '2026-09-24', 'value': '5.18'}]}))
    row = (await FredProvider().latest(['DGS10']))[0]
    assert row['label'] == 'LAST CLOSE' and row['delay'] == 'EOD' and row['as_of'] == '2026-09-24T00:00:00Z'
    assert row['live'] is False


# --- review additions ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bls_official_gaps_are_missing_not_rejected(monkeypatch):
    monkeypatch.delenv('BLS_API_KEY', raising=False)
    body = {'status': 'REQUEST_SUCCEEDED', 'Results': {'series': [{'seriesID': 'CUSR0000SA0', 'data': [
        {'year': '2025', 'period': 'M10', 'value': '-',
         'footnotes': [{'code': 'X', 'text': 'Data unavailable due to the 2025 lapse in appropriations'}]},
        {'year': '2025', 'period': 'M09', 'value': '324.5', 'footnotes': [{}]}]}]}}
    respond(monkeypatch, lambda m, u, k: (200, body))
    provider = BLSProvider()
    rows = await provider.series(2025, 2025)
    assert [r['period'] for r in rows] == ['2025-09']  # the gap is not turned into a value
    assert provider.health['missing_values'] == 1 and provider.health['rejected_rows'] == 0


@pytest.mark.asyncio
async def test_open_circuit_does_not_charge_the_daily_budget(monkeypatch):
    monkeypatch.delenv('BLS_API_KEY', raising=False)
    calls: list = []
    respond(monkeypatch, lambda m, u, k: (200, BLS_BODY), calls)
    provider = BLSProvider()
    provider.next_attempt = float('inf')
    with pytest.raises(ProviderUnavailable, match='circuit open'):
        await provider.series(2026, 2026)
    assert calls == [] and len(provider.guard.calls) == 0


@pytest.mark.asyncio
async def test_fred_current_vintage_has_no_fake_release_time(monkeypatch):
    monkeypatch.setenv('FRED_API_KEY', 'test-key-not-real')
    # Current-vintage answers stamp realtime_start with the request date, not the release date.
    respond(monkeypatch, lambda m, u, k: (200, {'observations': [
        {'realtime_start': '2026-09-26', 'realtime_end': '2026-09-26', 'date': '2026-08-01', 'value': '334.131'}]}))
    row = (await FredProvider().latest(['CPIAUCSL']))[0]
    assert row['known_from'] is None and row['as_of_basis'].startswith('retrieved')
    assert row['as_of'] == row['retrieved_at'] and row['label'] == 'OFFICIAL RELEASE'
    respond(monkeypatch, lambda m, u, k: (200, {'observations': [
        {'realtime_start': '2026-09-11', 'realtime_end': '9999-12-31', 'date': '2026-08-01', 'value': '334.2'}]}))
    first = (await FredProvider().first_releases('CPIAUCSL', '2026-08-01'))[0]
    assert first['known_from'] == '2026-09-11T00:00:00Z' and first['as_of'] == first['known_from']


@pytest.mark.asyncio
async def test_build_state_tolerates_malformed_fred_payload(monkeypatch):
    monkeypatch.setenv('FRED_API_KEY', 'test-key-not-real')

    def handler(method, url, kwargs):
        if 'stlouisfed' in url:
            return 200, {'unexpected': True}  # no observations list
        return 200, next(b for key, b in {'/FM/': FM, '/YC/': YC}.items() if key in url)
    respond(monkeypatch, handler)
    state = await build_central_bank_state(EcbDataProvider(), FredProvider())
    assert state['EUR']['rate'] == 2.5 and 'rate' not in state['USD']


@pytest.mark.asyncio
async def test_wpsr_calendar_flags_unparsed_schedule(monkeypatch):
    respond(monkeypatch, lambda m, u, k: (200, '<html>redesigned page</html>'))
    provider = EIAWeeklyPetroleumCalendar()
    rows = await provider.calendar('2026-10-05', '2026-10-20')
    assert rows and provider.health['status'] == 'DEGRADED' and provider.health['schedule_exceptions'] == 0
