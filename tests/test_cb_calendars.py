import json

import httpx
import pytest

from backend.providers.base import ProviderUnavailable
from backend.providers.cb_calendars import (
    BoECalendar,
    BoJCalendar,
    CalendarParseError,
    ECBCalendar,
    central_bank_calendars,
    parse_boe,
    parse_boj,
    parse_ecb,
)

# Trimmed copies of the live page structures (fetched 2026-09-26).
ECB_HTML = '''<main><div class="definition-list -zebra"><dl>
<dt> 30/09/2026 </dt><dd> Governing Council of the ECB: non-monetary policy meeting (in Frankfurt)<br> </dd>
<dt> 28/10/2026 </dt><dd> Governing Council of the ECB: monetary policy meeting in Frankfurt (Day 1)<br> </dd>
<dt> 29/10/2026 </dt><dd> Governing Council of the ECB: monetary policy meeting in Frankfurt (Day 2), followed by press conference<br> </dd>
<dt> 26/11/2026 </dt><dd> General Council meeting of the ECB in Frankfurt<br> </dd>
<dt> 11/10/2028 </dt><dd> Governing Council of the ECB: monetary policy meeting in Frankfurt<br> </dd>
<dt> 12/10/2028 </dt><dd> Governing Council of the ECB: monetary policy meeting in Frankfurt (Day 2), followed by press conference<br> </dd>
<dt> 31/02/2029 </dt><dd> Governing Council of the ECB: monetary policy meeting (Day 2), followed by press conference<br> </dd>
</dl></div></main>'''

BOE_HTML = '''<h2 class="cookie-title exclude-navigation">Our use of cookies</h2>
<h2>2026 confirmed dates</h2><table><tbody>
<tr><td>Thursday 18 June</td><td><a href="/x">June MPC Summary and minutes</a></td></tr>
<tr><td>Thursday 5 November</td><td><a href="/y">November MPC Summary and minutes</a> and <a href="/z">November Monetary Policy Report</a></td></tr>
<tr><td>Thursday&nbsp;17 December</td><td><a href="/w">December MPC Summary and minutes</a></td></tr>
</tbody></table>
<h2>2027 provisional dates</h2><table><tbody>
<tr><td>Thursday&nbsp;4 February</td><td><span><a href="/a">February Monetary Policy Report</a>&nbsp;</span>&nbsp;</td></tr>
</tbody></table>'''

BOJ_HTML = '''<h2 id="p2026">2026</h2><div class="tbl-box"><table><caption class="non-caption">Table : 2026</caption>
<thead><tr><th rowspan="2" scope="col">Date of MPM</th><th colspan="3" scope="col">Release Schedule</th></tr>
<tr><th scope="col">Outlook Report<br>(The Bank's View)</th><th>Summary of Opinions</th><th>MPM Minutes</th></tr></thead><tbody>
<tr><td><a href="/en/mopo/mpmdeci/mpr_2026/k260428a.pdf">Apr. 30 (Thurs.), May 1 (Fri.) [PDF 92KB]</a></td>
<td><a href="/en/mopo/outlook/gor2604b.pdf">May 1 (Fri.) [PDF 1,407KB]</a></td><td>May 12 (Tues.)</td><td>June 19 (Fri.)</td></tr>
<tr><td>Oct. 29 (Thurs.), 30 (Fri.)</td><td>Oct. 30 (Fri.)</td><td>Nov. 10 (Tues.)</td><td>Dec. 23 (Wed.)</td></tr>
<tr><td>Dec. 17 (Thurs.), 18 (Fri.)</td><td>-</td><td>Dec. 28 (Mon.)</td><td>Jan. 27 (Wed.), 2027</td></tr>
</tbody></table></div>
<h2 id="p2027">2027</h2><div class="tbl-box"><table><caption class="non-caption">Table : 2027</caption><thead><tr><th>Date of MPM</th></tr></thead>
<tbody><tr>
  <td>Sept. 21
  (Tues.), 22 (Wed.)</td>
  <td>-</td><td>Oct. 1 (Fri.)</td><td>To be announced</td></tr>
<tr><td>Dec. 31 (Fri.), Jan. 1 (Sat.)</td><td>-</td><td>-</td><td>-</td></tr>
</tbody></table></div>
<h2 id="p01"><a href="/en/mopo/mpmsche_minu/past.htm">Past Monetary Policy Meetings</a></h2>'''


def serve(monkeypatch, pages: dict[str, str], calls: list | None = None):
    async def request(self, method, url, **kwargs):
        if calls is not None:
            calls.append(url)
        for fragment, body in pages.items():
            if fragment in url:
                return httpx.Response(200, content=body.encode(), request=httpx.Request(method, url))
        return httpx.Response(404, content=b'', request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)


def test_ecb_parser_groups_meeting_days_and_skips_non_monetary_and_invalid_dates():
    meetings = parse_ecb(ECB_HTML)
    assert [m['decision_day'] for m in meetings] == ['2026-10-29', '2028-10-12']
    assert meetings[0]['meeting_days'] == ['2026-10-28', '2026-10-29'] and meetings[0]['press_conference']
    # A Day-1 entry without the '(Day 1)' marker still groups with its Day-2 press-conference day.
    assert meetings[1]['meeting_days'] == ['2028-10-11', '2028-10-12']


def test_boe_parser_normalises_nbsp_and_flags_reports_and_provisional_years():
    meetings = parse_boe(BOE_HTML)
    assert [m['decision_day'] for m in meetings] == ['2026-06-18', '2026-11-05', '2026-12-17', '2027-02-04']
    assert [m['report'] for m in meetings] == [False, True, False, True]
    assert [m['provisional'] for m in meetings] == [False, False, False, True]


def test_boj_parser_handles_cross_month_line_breaks_and_year_end():
    meetings = parse_boj(BOJ_HTML)
    assert [m['decision_day'] for m in meetings] == ['2026-05-01', '2026-10-30', '2026-12-18', '2027-09-22', '2028-01-01']
    assert meetings[0]['meeting_days'] == ['2026-04-30', '2026-05-01'] and meetings[0]['report']
    assert meetings[2]['report'] is False and meetings[3]['meeting_days'] == ['2027-09-21', '2027-09-22']


@pytest.mark.asyncio
async def test_rows_use_configured_local_decision_time_converted_to_utc_with_attribution(monkeypatch):
    serve(monkeypatch, {'ecb.europa.eu': ECB_HTML, 'bankofengland': BOE_HTML, 'boj.or.jp': BOJ_HTML})
    ecb = await ECBCalendar().calendar('2026-10-01', '2026-12-31')
    decision, conference = ecb
    # 29 Oct 2026 is after the end of CEST (25 Oct): 14:15 CET = 13:15 UTC; press conference 14:45 CET.
    assert decision['scheduled_at'] == '2026-10-29T13:15:00+00:00' and decision['category'] == 'CENTRAL_BANK'
    assert conference['scheduled_at'] == '2026-10-29T13:45:00+00:00' and conference['name'] != decision['name']
    assert decision['attribution'] == 'Source: ECB' and decision['source_url'].startswith('https://www.ecb.europa.eu/')
    assert decision['provider'] == 'ecb_calendar' and decision['country'] == 'EU' and decision['time_known'] is True
    assert decision['as_of'].endswith('+00:00') and 'static schedule' in decision['delay']
    boe = await BoECalendar().calendar('2026-01-01', '2027-12-31')
    stamps = {row['scheduled_at'] for row in boe}
    assert '2026-06-18T11:00:00+00:00' in stamps  # 12:00 BST
    assert '2026-11-05T12:00:00+00:00' in stamps  # 12:00 GMT
    assert all(row['attribution'] == 'Source: Bank of England' and row['country'] == 'GB' for row in boe)
    assert [row['provisional'] for row in boe if row['scheduled_at'].startswith('2027-02-04')] == [True]
    boj = await BoJCalendar().calendar('2026-10-01', '2026-10-31')
    assert [row['scheduled_at'] for row in boj] == ['2026-10-30T03:00:00+00:00']  # 12:00 JST placeholder
    assert boj[0]['time_known'] is False and 'approximate' in boj[0]['time_basis']
    assert boj[0]['attribution'] == 'Source: Bank of Japan' and boj[0]['meeting_days'] == ['2026-10-29', '2026-10-30']
    assert len({row['id'] for row in ecb + boe + boj}) == len(ecb + boe + boj)


@pytest.mark.asyncio
async def test_empty_parse_is_an_alert_worthy_failure(monkeypatch):
    serve(monkeypatch, {'bankofengland': '<html><h2>Redesigned page</h2><p>No table</p></html>'})
    provider = BoECalendar()
    with pytest.raises(CalendarParseError):
        await provider.calendar('2026-01-01', '2027-12-31')
    assert provider.health['status'] == 'DEGRADED' and provider.health['alert'] is True
    assert 'no meetings' in provider.health['error']


@pytest.mark.asyncio
async def test_no_meetings_in_range_is_not_a_failure_and_pages_are_cached(monkeypatch):
    calls: list = []
    serve(monkeypatch, {'boj.or.jp': BOJ_HTML}, calls)
    provider = BoJCalendar()
    assert await provider.calendar('2026-01-01', '2026-01-31') == []
    assert provider.health['status'] == 'ONLINE' and provider.health['alert'] is False
    await provider.calendar('2026-10-01', '2026-12-31')
    assert len(calls) == 1  # min_refresh_seconds guard: the official page is fetched at most hourly


@pytest.mark.asyncio
async def test_http_failure_propagates_as_provider_unavailable(monkeypatch):
    serve(monkeypatch, {})
    provider = ECBCalendar()
    with pytest.raises(ProviderUnavailable) as info:
        await provider.calendar('2026-01-01', '2026-12-31')
    assert not isinstance(info.value, CalendarParseError) and provider.health['status'] == 'DEGRADED'


def test_factory_builds_three_keyless_configured_providers():
    providers = central_bank_calendars()
    assert [p.name for p in providers] == ['ecb_calendar', 'boe_mpc_calendar', 'boj_mpm_calendar']
    assert all(p.configured and hasattr(p, 'calendar') for p in providers)
    assert json.dumps([p.health for p in providers])
