"""Event-aware calendar sources and calendar hygiene (docs/EVENT_AWARENESS.md §1). Fixtures mirror the live payloads
recorded on 2026-09-27 (see the calendars research notes): Fed calendar.json (with its UTF-8 BOM), fomccalendars.htm
panels with the SEP '*', BEA/BLS ICS (folded lines, escaped commas, TZID US-Eastern), the Census list-view table,
the Treasury refunding page and tentative auction XML, Finnhub /calendar/earnings and FRED release/dates. No network."""
import json
from datetime import UTC, date, datetime, time, timedelta

import httpx
import pytest

from backend import refresh
from backend import runtime as runtime_module
from backend.config import Settings
from backend.database import Store
from backend.providers import econ_calendars as econ
from backend.providers.calendars import (
    CalendarParseError,
    FOMCCalendar,
    auction_class,
    calendar_row,
    carry_lent,
    classify,
    dedupe,
    dedupe_key,
    fmp_earnings_rows,
    fmp_economic_rows,
    parse_fomc,
    parse_fomc_meetings,
    stale_ids,
)
from backend.providers.cb_calendars import BoJCalendar, ECBCalendar
from backend.providers.finnhub import FinnhubProvider, earnings_rows
from backend.providers.official_data import FredProvider
from backend.providers.registry import build_providers, by_capability
from backend.runtime import Runtime
from backend.service import HubService

FOMC_HTML = '''<div class="panel panel-default"><div class="panel-heading"><h4><a id="46788">2026 FOMC Meetings</a></h4></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>September</strong></div>
<div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">15-16*</div></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>October</strong></div>
<div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">27-28</div></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>December</strong></div>
<div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">8-9*</div></div>
<p>* Meeting associated with a Summary of Economic Projections.</p></div>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="1">2021 FOMC Meetings</a></h4></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>Apr/May</strong></div>
<div class="fomc-meeting__date col-xs-4">30-1*</div></div></div>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="2">2027 FOMC Meetings</a></h4></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>January</strong></div>
<div class="fomc-meeting__date col-xs-4">26-27</div></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>March</strong></div>
<div class="fomc-meeting__date col-xs-4">16-17*</div></div></div>'''

FED_JSON = '﻿' + json.dumps({'events': [
    {'title': 'FOMC Minutes', 'time': '2:00 p.m.', 'month': '2026-10', 'days': '7', 'type': 'FOMC',
     'description': 'Minutes of the Federal Open Market Committee, September 15-16, 2026', 'location': '', 'live': '',
     'link': '/monetarypolicy/fomcminutes20260916.htm'},
    {'title': 'FOMC Meeting', 'time': '2:00 p.m.', 'month': '2026-10', 'days': '28', 'type': 'FOMC', 'description': '',
     'location': '', 'live': '', 'link': ''},
    {'title': 'FOMC Press Conference', 'time': '2:30 p.m.', 'month': '2026-10', 'days': '28', 'type': 'FOMC',
     'description': '', 'location': '', 'live': '', 'link': ''},
    {'title': 'Beige Book', 'time': '2:00 p.m.', 'month': '2026-10', 'days': '14', 'type': 'Beige', 'description': '',
     'location': '', 'live': '', 'link': '/monetarypolicy/beige-book-default.htm'},
    {'title': 'Industrial Production and Capacity Utilization - G.17', 'time': '9:15 a.m.', 'month': '2026-10',
     'days': '16', 'type': 'Stat', 'description': '', 'location': '', 'live': '', 'link': '/releases/g17/current/'},
    {'title': 'Testimony - Chairman Kevin Warsh', 'time': '10:00 a.m.', 'month': '2026-07', 'days': '14, 15',
     'type': 'Testimony', 'description': 'Semiannual Monetary Policy Report to the Congress',
     'location': 'House Financial Services / Senate Banking', 'live': '', 'link': ''},
    {'title': 'Speech - Vice Chair for Supervision Jane Doe', 'time': '9:00 a.m.', 'month': '2026-10', 'days': '1',
     'type': 'Speeches', 'description': '', 'location': 'Washington, D.C.', 'live': '', 'link': ''},
    {'title': 'Speech - Governor John Roe', 'time': '9:00 a.m.', 'month': '2026-10', 'days': '1', 'type': 'Speeches',
     'description': '', 'location': '', 'live': '', 'link': ''},
    {'title': 'Holiday - Thanksgiving Day', 'time': '', 'month': '2026-11', 'days': '26', 'type': 'Other',
     'description': '', 'location': '', 'live': '', 'link': ''},
    {'title': 'Legacy event', 'time': '', 'month': '', 'days': '', 'type': 'events', 'description': '', 'location': '',
     'live': '', 'link': ''}], 'announcement': []})

BEA_ICS = '\r\n'.join([
    'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Bureau of Economic Analysis//Release Schedule//EN',
    'X-WR-TIMEZONE:America/New_York',
    'BEGIN:VEVENT', 'DTSTART;VALUE=DATE-TIME:20260930T123000Z', 'DURATION:PT30M',
    'SUMMARY:GDP (Third Estimate)\\, Industries\\, Corporate Profits\\, State GDP\\, and Personal Income by State\\, 2nd Qua',
    ' rter 2026', 'UID:bea-1', 'END:VEVENT',
    'BEGIN:VEVENT', 'DTSTART;VALUE=DATE-TIME:20260930T123000Z', 'DURATION:PT30M',
    'SUMMARY:Personal Income and Outlays\\, August 2026', 'UID:bea-2', 'END:VEVENT',
    'BEGIN:VEVENT', 'DTSTART;VALUE=DATE-TIME:20261029T123000Z', 'DURATION:PT30M',
    'SUMMARY:GDP (Advance Estima', ' te)\\, 3rd Quarter 2026', 'UID:bea-3', 'END:VEVENT',
    'BEGIN:VEVENT', 'DTSTART;VALUE=DATE-TIME:20261105T133000Z', 'DURATION:PT30M',
    'SUMMARY:U.S. International Trade in Goods and Services\\, September 2026', 'UID:bea-4', 'END:VEVENT',
    'END:VCALENDAR', ''])

BEA_JSON = {'Gross Domestic Product': {'release_dates': ['2026-09-30T12:30:00+00:00', '2026-10-29T12:30:00+00:00',
                                                         '2026-10-29T12:30:00+00:00']},
            'Personal Income and Outlays': {'release_dates': ['2026-09-30T12:30:00+00:00']},
            'U.S. International Trade in Goods and Services': {'release_dates': ['2026-11-05T13:30:00+00:00']},
            'file_last_updated': '2026-07-13T08:00:42'}

CENSUS_HTML = '''<table class="sortable"><thead><tr><th>Indicator</th><th>Release Date</th><th>Time</th><th>Reference Period</th></tr></thead><tbody>
<tr><td><a href="/retail/index.html">Advance Monthly Sales for Retail and Food Services</a></td><td sorttable_customkey="202610150830">October 15, 2026</td><td>8:30 AM</td><td>September 2026</td><td class="hiden">A202610150830</td><td class="hiden">A202610150830</td></tr>
<tr><td><a href="/manufacturing/m3/adv/index.html">Advance Report on Durable Goods Manufacturers&#39; Shipments, Inventories and Orders</a></td><td sorttable_customkey="202610270830">October 27, 2026</td><td>8:30 AM</td><td>September 2026</td><td class="hiden">A202610270830</td><td class="hiden">A202610270830</td></tr>
<tr style="background-color:#FFE954"><td><a href="/construction/nrc/index.html">New Residential Construction</a></td><td sorttable_customkey="202609170830">Suspended</td><td></td><td>August 2026</td><td class="hiden">A</td><td class="hiden">A</td></tr>
<tr><td><a href="/construction/nrc/index.html">New Residential Construction</a></td><td sorttable_customkey="202610200830">October 20, 2026</td><td>8:30 AM</td><td>August 2026</td><td class="hiden">A202610200830</td><td class="hiden">A202610200830</td></tr>
<tr><td><a href="/construction/nrc/index.html">New Residential Construction</a></td><td sorttable_customkey="202610200830">October 20, 2026</td><td>8:30 AM</td><td>September 2026</td><td class="hiden">A202610200830</td><td class="hiden">A202610200830</td></tr>
<tr><td><a href="/construction/nrs/index.html">New Residential Sales</a></td><td sorttable_customkey="202610271000">October 27, 2026</td><td>10:00 AM</td><td>September 2026</td><td class="hiden">A202604270830</td><td class="hiden">A202604270830</td></tr>
<tr><td><a href="/construction/c30/index.html">Construction Spending</a></td><td sorttable_customkey="202610011000">October 1, 2026</td><td>10:00 AM</td><td>August 2026</td><td class="hiden">A</td><td class="hiden">A</td></tr>
</tbody></table>'''

BLS_ICS = '\r\n'.join([
    'BEGIN:VCALENDAR', 'PRODID:-//Department of Labor//Bureau of Labor Statistics//EN', 'VERSION:2.0',
    'X-WR-TIMEZONE:US-Eastern', 'BEGIN:VTIMEZONE', 'TZID:US-Eastern', 'BEGIN:STANDARD', 'DTSTART:19671029T020000',
    'TZOFFSETFROM:-0400', 'TZOFFSETTO:-0500', 'END:STANDARD', 'END:VTIMEZONE',
    'BEGIN:VEVENT', 'DTSTART;TZID=US-Eastern:20260929T100000', 'DURATION:PT0M',
    'SUMMARY:Job Openings and Labor Turnover Survey', 'CATEGORIES:Economic Releases', 'END:VEVENT',
    'BEGIN:VEVENT', 'DTSTART;TZID=US-Eastern:20261002T083000', 'DURATION:PT0M', 'SUMMARY:Employment Situation',
    'END:VEVENT',
    'BEGIN:VEVENT', 'DTSTART;TZID=US-Eastern:20261014T083000', 'DURATION:PT0M', 'SUMMARY:Consumer Price Index',
    'END:VEVENT',
    'BEGIN:VEVENT', 'DTSTART;TZID=US-Eastern:20261014T083000', 'DURATION:PT0M', 'SUMMARY:Real Earnings', 'END:VEVENT',
    'BEGIN:VEVENT', 'DTSTART;TZID=US-Eastern:20261015T083000', 'DURATION:PT0M', 'SUMMARY:Producer Price Index',
    'END:VEVENT',
    'BEGIN:VEVENT', 'DTSTART;TZID=US-Eastern:20261030T083000', 'DURATION:PT0M', 'SUMMARY:Employment Cost Index',
    'END:VEVENT',
    'BEGIN:VEVENT', 'DTSTART;TZID=US-Eastern:20261110T083000', 'DURATION:PT0M', 'SUMMARY:Consumer Price Index',
    'END:VEVENT', 'END:VCALENDAR', ''])

REFUNDING_HTML = '''<div class="field--item"><h2>Most Recent Quarterly Refunding Documents</h2>
<p><strong>DOCUMENTS RELEASED at 3:00 PM Monday, August 3, 2026</strong></p>
<ul><li><a href="/news/press-releases/jy2026">Financing Estimates</a></li><li><a href="/x">Sources and Uses Table</a></li></ul>
<p>(The next release is scheduled for November 2, 2026)</p>
<p><strong>DOCUMENTS RELEASED at 8:30 AM Wednesday, August 5, 2026</strong></p>
<ul><li><a href="/news/press-releases/jy2027">Policy Statement</a></li>
<li><a href="/system/files/221/TentativeAuctionScheduleQ32026.xml">Tentative Auction Schedule (XML)</a></li>
<li><a href="/system/files/221/TBAC-Minutes.pdf">Minutes of the Meeting of the Treasury Borrowing Advisory Committee</a></li></ul>
<p>(The next release is scheduled for November 4, 2026)</p>
<p><strong>Primary Dealer Meeting Agenda 12:00 PM Friday, July 17, 2026</strong> (next October 16, 2026)</p></div>'''


# The live page (2026-09-27) writes lower-case months and a space before the comma of the next date.
REFUNDING_HTML_LIVE = (REFUNDING_HTML.replace('Monday, August 3', 'Monday, august 3')
                       .replace('Wednesday, August 5', 'Wednesday, august 5')
                       .replace('November 4, 2026', 'November 4 , 2026'))


def _auction(term, kind, day, tips='N', frn='N', reopen='N'):
    return (f'<AuctionCalendarDate><SecurityTermWeekYear>{term}</SecurityTermWeekYear><SecurityType>{kind}</SecurityType>'
            f'<ReOpeningIndicator>{reopen}</ReOpeningIndicator><TIPS>{tips}</TIPS><FloatingRate>{frn}</FloatingRate>'
            f'<AnnouncementDate>2026-10-01</AnnouncementDate><AuctionDate>{day}</AuctionDate>'
            f'<SettlementDate>2026-10-31</SettlementDate></AuctionCalendarDate>')


TENTATIVE_XML = ('<?xml version="1.0" encoding="UTF-8"?><AuctionCalendar>'
                 '<AuctionCalendarName>Aug2026 Refunding Auction Calendar Official Ver 2</AuctionCalendarName>'
                 '<StartDate>2026-08-05</StartDate><EndDate>2027-01-30</EndDate>'
                 + _auction('3-Year', 'Note', '2026-10-06') + _auction('9-Year 10-Month', 'Note', '2026-10-07', reopen='Y')
                 + _auction('29-Year 10-Month', 'Bond', '2026-10-08', reopen='Y')
                 + _auction('19-Year 10-Month', 'Bond', '2026-10-21', reopen='Y')
                 + _auction('5-Year', 'Note', '2026-10-22', tips='Y') + _auction('2-Year', 'Note', '2026-10-26')
                 + _auction('2-Year', 'Note', '2026-10-28', frn='Y') + _auction('7-Year', 'Note', '2026-10-29')
                 + '<Holiday><HolidayName>Columbus Day</HolidayName><HolidayDate>2026-10-12</HolidayDate></Holiday>'
                 '</AuctionCalendar>')


def serve(monkeypatch, pages: dict, seen: list | None = None):
    """httpx.AsyncClient.request answering by URL fragment; values are (status, body) or a body (200)."""
    async def request(self, method, url, **kwargs):
        if seen is not None:
            seen.append({'url': url, 'params': kwargs.get('params'), 'user_agent': self.headers.get('user-agent')})
        for fragment, answer in pages.items():
            if fragment in url and (not isinstance(answer, dict) or 'symbol' not in answer
                                    or answer['symbol'] == (kwargs.get('params') or {}).get('symbol')):
                status, body = answer if isinstance(answer, tuple) else (200, answer)
                if callable(body):
                    body = body(kwargs.get('params') or {})
                content = body if isinstance(body, bytes) else body.encode() if isinstance(body, str) else json.dumps(body).encode()
                return httpx.Response(status, content=content, request=httpx.Request(method, url))
        return httpx.Response(404, content=b'', request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)


# --- Federal Reserve -----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fomc_rows_carry_sep_press_conference_sub_event_and_the_2027_panel(monkeypatch):
    assert [m['sep'] for m in parse_fomc_meetings(FOMC_HTML, 2026)] == [True, False, True]
    assert parse_fomc(FOMC_HTML, 2021) == [date(2021, 5, 1)]  # cross-month meeting, decision on the last day
    assert parse_fomc_meetings(FOMC_HTML, 2021)[0]['meeting_days'] == [date(2021, 4, 30), date(2021, 5, 1)]
    assert parse_fomc(FOMC_HTML, 2027) == [date(2027, 1, 27), date(2027, 3, 17)]  # panel at the bottom, after 2021
    serve(monkeypatch, {'fomccalendars': FOMC_HTML})
    rows = await FOMCCalendar().calendar('2026-10-01', '2027-01-31')
    by_key = {(r['scheduled_at'][:10], r['sub_event']): r for r in rows}
    october, press = by_key[('2026-10-28', 'DECISION')], by_key[('2026-10-28', 'PRESS_CONFERENCE')]
    assert october['scheduled_at'] == '2026-10-28T18:00:00+00:00' and october['sep'] is False  # 14:00 EDT
    assert press['scheduled_at'] == '2026-10-28T18:30:00+00:00' and press['parent_event_id'] == october['id']
    assert press['event_class'] == october['event_class'] == 'FOMC' and press['time_known'] and press['time_source'] == 'config'
    december = by_key[('2026-12-09', 'DECISION')]
    assert december['scheduled_at'] == '2026-12-09T19:00:00+00:00' and december['sep'] is True  # 14:00 EST, SEP meeting
    assert december['attribution'] == 'Source: Federal Reserve Board' and '2:00 p.m. ET' in december['time_basis']
    assert ('2027-01-27', 'DECISION') in by_key and october['meeting_days'] == ['2026-10-27', '2026-10-28']


@pytest.mark.asyncio
async def test_fomc_page_without_meetings_keeps_stored_rows(monkeypatch):
    serve(monkeypatch, {'fomccalendars': '<html><h4>Redesigned</h4></html>'})
    provider = FOMCCalendar()
    with pytest.raises(CalendarParseError):
        await provider.calendar('2026-10-01', '2026-12-31')
    assert provider.health['status'] == 'DEGRADED' and provider.health['alert'] is True


@pytest.mark.asyncio
async def test_fed_calendar_json_minutes_testimony_speeches_and_holidays(monkeypatch):
    serve(monkeypatch, {'calendar.json': FED_JSON.encode('utf-8')})
    provider = econ.FedEventsCalendar()
    rows = await provider.calendar('2026-07-01', '2026-11-30')
    names = {(r['name'], r['scheduled_at']) for r in rows}
    assert ('FOMC minutes', '2026-10-07T18:00:00+00:00') in names  # 2:00 p.m. EDT
    minutes = next(r for r in rows if r['event_class'] == 'FOMC_MINUTES')
    assert minutes['sub_event'] == 'MINUTES' and minutes['parent_event_id'] is None  # never keeps a decision in progress
    assert minutes['source_url'] == 'https://www.federalreserve.gov/monetarypolicy/fomcminutes20260916.htm'
    # fomccalendars.htm stays the authority for decisions and press conferences.
    assert not any(r['event_class'] == 'FOMC' for r in rows)
    testimony = [r for r in rows if r['event_class'] == 'FED_TESTIMONY']
    assert [r['scheduled_at'] for r in testimony] == ['2026-07-14T14:00:00+00:00', '2026-07-15T14:00:00+00:00']
    assert testimony[0]['name'] == 'Testimony - Chairman Kevin Warsh' and testimony[0]['impact'] == 8  # speaker from source
    vice = next(r for r in rows if r['event_class'] == 'FED_SPEECH')
    assert vice['impact'] == 5 and 'Vice Chair' in vice['name']
    assert not any('Governor' in r['name'] for r in rows)
    production = next(r for r in rows if r['event_class'] == 'US_INDUSTRIAL_PRODUCTION')
    assert production['scheduled_at'] == '2026-10-16T13:15:00+00:00' and production['category'] == 'MACRO'
    assert all(r['time_known'] and r['attribution'] == 'Source: Federal Reserve Board' for r in rows)
    assert date(2026, 11, 26) in provider.holidays and provider.health['alert'] is False


FED_APPEARANCES = '﻿' + json.dumps({'events': [
    {'title': 'Testimony - Vice Chair for Supervision Michelle W. Bowman', 'time': '10:00 a.m.', 'month': '2026-10',
     'days': '6', 'type': 'Testimony', 'description': 'Bank capital', 'location': 'Senate Banking', 'live': '', 'link': ''},
    {'title': 'Testimony - Chair Kevin Warsh', 'time': '10:00 a.m.', 'month': '2026-10', 'days': '7', 'type': 'Testimony',
     'description': '', 'location': '', 'live': '', 'link': ''},
    {'title': 'Speech - Vice Chair Philip N. Jefferson', 'time': '9:00 a.m.', 'month': '2026-10', 'days': '8',
     'type': 'Speeches', 'description': '', 'location': '', 'live': '', 'link': ''},
    {'title': 'Speech - Vice Chair for Supervision Michelle W. Bowman', 'time': '2:00 p.m.', 'month': '2026-10', 'days': '8',
     'type': 'Speeches', 'description': '', 'location': '', 'live': '', 'link': ''}], 'announcement': []})


@pytest.mark.asyncio
async def test_vice_chair_testimony_is_not_chair_level_and_same_day_speeches_stay_apart(monkeypatch):
    """Review finding: a Vice Chair testimony was a Chair-level FED_TESTIMONY; two speeches of one class on one day merged."""
    serve(monkeypatch, {'calendar.json': FED_APPEARANCES.encode('utf-8')})
    rows = {r['title']: r for r in await econ.FedEventsCalendar().calendar('2026-10-01', '2026-10-31')}
    vice = rows['Testimony - Vice Chair for Supervision Michelle W. Bowman']
    assert (vice['event_class'], vice['kind'], vice['speaker_role'], vice['impact']) == ('FED_SPEECH', 'TESTIMONY', 'VICE_CHAIR', 5)
    assert vice['speaker'] == 'Vice Chair for Supervision Michelle W. Bowman' and vice['is_decision'] is False
    chair = rows['Testimony - Chair Kevin Warsh']
    assert (chair['event_class'], chair['kind'], chair['speaker_role'], chair['impact']) == ('FED_TESTIMONY', 'TESTIMONY', 'CHAIR', 8)
    speeches = [r for r in rows.values() if r['kind'] == 'SPEECH']
    assert {(r['event_class'], r['speaker_role']) for r in speeches} == {('FED_SPEECH', 'VICE_CHAIR')}
    # The speaker is part of the dedupe key: the two Vice Chairs speaking on 8 October are two releases, even when a
    # second source lists one of them.
    first, second = sorted(speeches, key=lambda r: r['scheduled_at'])
    assert dedupe_key(first) != dedupe_key(second)
    only_elsewhere = dict(second, provider='calendar', id='listed-elsewhere')
    assert len(dedupe([first, only_elsewhere])[0]) == 2  # a different speaker is never dropped as a duplicate
    relisted = dict(second, provider='calendar', id='vendor-copy')
    kept, dropped = dedupe([first, second, relisted])
    assert {r['id'] for r in kept} == {first['id'], second['id']} and [r['id'] for r in dropped] == ['vendor-copy']
    # Whatever rule matches, a title that names no Chair never gets a Chair-level class.
    assert econ.fed_class({'event_class': 'FED_SPEECH_CHAIR', 'impact': 7}, 'GOVERNOR') == ('FED_SPEECH', 5)
    assert econ.speaker_of('Speech - Governor John Roe') == ('GOVERNOR', 'Governor John Roe')


# --- BEA, Census, BLS, FRED fallback ---------------------------------------------------------------------------------------


def test_ics_unfolds_unescapes_and_maps_bls_timezone():
    events = econ.parse_ics(BEA_ICS)
    assert events[2]['SUMMARY'][1] == 'GDP (Advance Estimate), 3rd Quarter 2026'
    stamp, known = econ.ics_time(({'TZID': 'US-Eastern'}, '20261014T083000'), 'America/New_York')
    assert stamp == datetime(2026, 10, 14, 12, 30, tzinfo=UTC) and known
    day, known = econ.ics_time(({'VALUE': 'DATE'}, '20261014'), 'America/New_York')
    assert known is False and day == datetime(2026, 10, 14, 4, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_bea_ics_rows_with_reference_period_and_json_fallback(monkeypatch):
    serve(monkeypatch, {'online-calendar-subscription.ics': BEA_ICS})
    rows = await econ.BEAReleaseCalendar().calendar('2026-09-26', '2026-11-30')
    assert [(r['event_class'], r['scheduled_at'], r['reference_period']) for r in rows] == [
        ('US_GDP', '2026-09-30T12:30:00+00:00', '2nd Quarter 2026'),
        ('US_PCE', '2026-09-30T12:30:00+00:00', 'August 2026'),
        ('US_GDP', '2026-10-29T12:30:00+00:00', '3rd Quarter 2026')]
    assert rows[2]['name'] == 'US GDP (Advance estimate)' and rows[2]['impact'] == 9 and rows[0]['impact'] == 8
    assert all(r['time_known'] and r['time_source'] == 'source' and r['attribution'] == 'Source: U.S. Bureau of Economic Analysis'
               for r in rows)
    serve(monkeypatch, {'online-calendar-subscription.ics': (404, ''), 'release_dates.json': BEA_JSON})
    fallback = await econ.BEAReleaseCalendar().calendar('2026-09-26', '2026-11-30')
    assert sorted((r['event_class'], r['scheduled_at']) for r in fallback) == [
        ('US_GDP', '2026-09-30T12:30:00+00:00'), ('US_GDP', '2026-10-29T12:30:00+00:00'),
        ('US_PCE', '2026-09-30T12:30:00+00:00')]  # the file's duplicate entry is dropped
    assert all(r['reference_period'] is None and 'release_dates.json' in r['time_basis'] for r in fallback)
    # An ICS outage that opens the provider circuit (5xx on every attempt, or 429) still reaches the JSON host.
    for status in (503, 429):
        serve(monkeypatch, {'online-calendar-subscription.ics': (status, ''), 'release_dates.json': BEA_JSON})
        assert len(await econ.BEAReleaseCalendar().calendar('2026-09-26', '2026-11-30')) == 3


@pytest.mark.asyncio
async def test_census_rows_use_visible_cells_merge_periods_and_skip_suspended(monkeypatch):
    serve(monkeypatch, {'calendar-listview': CENSUS_HTML})
    provider = econ.CensusIndicatorCalendar()
    rows = {r['event_class']: r for r in await provider.calendar('2026-09-26', '2026-12-31')}
    assert set(rows) == {'US_RETAIL_SALES', 'US_DURABLE_GOODS', 'US_HOUSING_STARTS', 'US_NEW_HOME_SALES'}
    assert rows['US_RETAIL_SALES']['scheduled_at'] == '2026-10-15T12:30:00+00:00'
    assert rows['US_NEW_HOME_SALES']['scheduled_at'] == '2026-10-27T14:00:00+00:00'  # visible 10:00 AM, not the hidden key
    assert rows['US_HOUSING_STARTS']['reference_period'] == 'August 2026, September 2026'
    assert provider.health['suspended'] == 1
    serve(monkeypatch, {'calendar-listview': '<table><tr><td>Redesigned</td></tr></table>'})
    with pytest.raises(CalendarParseError):
        await econ.CensusIndicatorCalendar().calendar('2026-09-26', '2026-12-31')


@pytest.mark.asyncio
async def test_bls_ics_needs_a_contact_user_agent(monkeypatch):
    assert econ.BLSReleaseCalendar('MarketIntelligence contact@example.com').configured is False
    assert econ.BLSReleaseCalendar('').configured is False
    seen: list = []
    serve(monkeypatch, {'bls.ics': BLS_ICS}, seen)
    provider = econ.BLSReleaseCalendar('Research Desk ops@research-desk.test')
    rows = await provider.calendar('2026-09-26', '2026-10-31')
    assert 'ops@research-desk.test' in seen[0]['user_agent']
    by_class = {r['event_class']: r for r in rows}
    assert set(by_class) == {'US_JOLTS', 'US_NFP', 'US_CPI', 'US_PPI', 'US_ECI'}  # Real Earnings is not tracked
    assert by_class['US_CPI']['scheduled_at'] == '2026-10-14T12:30:00+00:00' and by_class['US_CPI']['impact'] == 10
    assert by_class['US_JOLTS']['scheduled_at'] == '2026-09-29T14:00:00+00:00'
    assert by_class['US_CPI']['time_basis'] == 'BLS release schedule (ICS)' and by_class['US_CPI']['time_known']
    serve(monkeypatch, {'bls.ics': (403, 'Access Denied')})
    blocked = econ.BLSReleaseCalendar('Research Desk ops@research-desk.test')
    with pytest.raises(Exception, match='HTTP 403'):
        await blocked.calendar('2026-09-26', '2026-10-31')
    assert blocked.health['status'] == 'AUTH_ERROR'


@pytest.mark.asyncio
async def test_fred_release_dates_fallback_uses_configured_agency_times(monkeypatch):
    def dates(params):
        rid = params['release_id']
        return {'realtime_start': '2026-09-26', 'realtime_end': '9999-12-31', 'order_by': 'release_date',
                'sort_order': 'desc', 'count': 3, 'offset': 0, 'limit': 60,
                'release_dates': [{'release_id': rid, 'date': '2026-11-10'}, {'release_id': rid, 'date': '2026-10-14'},
                                  {'release_id': rid, 'date': '2026-09-11'}]}
    seen: list = []
    serve(monkeypatch, {'/fred/release/dates': (200, dates)}, seen)
    rows = await FredProvider('fred-test-key').calendar('2026-09-26', '2026-10-31')
    cpi = [r for r in rows if r['event_class'] == 'US_CPI']
    assert [r['scheduled_at'] for r in cpi] == ['2026-10-14T12:30:00+00:00']  # out-of-window dates dropped
    assert cpi[0]['time_source'] == 'config' and 'BLS standard release time 08:30' in cpi[0]['time_basis']
    jolts = next(r for r in rows if r['event_class'] == 'US_JOLTS')
    assert jolts['scheduled_at'] == '2026-10-14T14:00:00+00:00'
    assert all(call['params']['include_release_dates_with_no_data'] == 'true' for call in seen)
    assert FredProvider.fallback_for == 'bls_release_calendar'


# --- rules and schedule-only rows ---------------------------------------------------------------------------------------


def test_us_federal_holidays_and_the_claims_rule():
    assert {date(2026, 11, 26), date(2026, 12, 25), date(2026, 7, 3), date(2026, 10, 12), date(2026, 11, 11)} <= \
        econ.us_federal_holidays(2026)
    assert date(2021, 12, 31) in econ.us_federal_holidays(2021)  # New Year 2022 fell on a Saturday
    holidays = econ.us_federal_holidays(2025) | econ.us_federal_holidays(2026)
    days = [day for day, _ in econ.claims_dates(date(2025, 6, 10), date(2026, 1, 10), holidays)]
    assert date(2025, 6, 18) in days and date(2025, 6, 19) not in days  # Juneteenth on a Thursday
    assert {date(2025, 11, 26), date(2025, 12, 24), date(2025, 12, 31)} <= set(days)
    late = dict(econ.claims_dates(date(2026, 11, 16), date(2026, 12, 31), econ.us_federal_holidays(2026)))
    assert date(2026, 11, 25) in late and date(2026, 11, 26) not in late  # Thanksgiving
    assert date(2026, 12, 24) in late and date(2026, 12, 31) in late  # Christmas/New Year on Friday: Thursday stays
    assert late[date(2026, 11, 25)] == date(2026, 11, 21)  # week ending the Saturday before


@pytest.mark.asyncio
async def test_claims_and_survey_rows_are_schedule_only():
    claims = await econ.JoblessClaimsCalendar().calendar('2026-11-20', '2026-11-30')
    assert [(r['scheduled_at'], r['time_source']) for r in claims] == [('2026-11-25T13:30:00+00:00', 'rule')]
    assert claims[0]['event_class'] == 'US_CLAIMS' and 'Thursday 08:30 ET' in claims[0]['time_basis']
    provider = econ.SurveyScheduleCalendar()
    rows = await provider.calendar('2026-09-26', '2027-01-31')
    umich = [r for r in rows if r['event_class'] == 'UMICH']
    assert umich[0]['scheduled_at'] == '2026-10-09T14:00:00+00:00' and umich[0]['reference_period'] == 'October 2026'
    assert umich[2]['scheduled_at'] == '2026-11-06T15:00:00+00:00'  # 10:00 EST after 1 November
    board = [r['scheduled_at'][:10] for r in rows if r['event_class'] == 'CONF_BOARD']
    assert board == ['2026-09-29', '2026-10-27', '2026-11-24', '2026-12-29', '2027-01-26']  # last Tuesdays
    assert all(r['schedule_only'] and r['consensus'] is None and r['actual'] is None for r in rows)
    assert 'configured through 2026-12-18' in provider.health['note']


# --- Treasury refunding and tentative auctions -----------------------------------------------------------------------------


def test_refunding_parser_accepts_live_page_spelling():
    parsed = {item['rule']['name']: item for item in econ.parse_refunding(REFUNDING_HTML_LIVE)}
    assert parsed['Treasury quarterly refunding statement']['day'] == date(2026, 11, 4)
    assert parsed['Treasury quarterly refunding statement']['time'] == time(8, 30)
    assert parsed['Treasury financing estimates (refunding)']['day'] == date(2026, 11, 2)


def test_auction_classes_normalise_reopenings_and_skip_tips_and_frn():
    assert auction_class('9-Year 10-Month', 'Note') == 'UST_AUCTION_10Y'
    assert auction_class('29-Year 10-Month', 'Bond') == 'UST_AUCTION_30Y'
    assert auction_class('2-Year', 'Note') == 'UST_AUCTION_2Y'
    assert auction_class('5-Year', 'Note', ['Y']) is None and auction_class('13-Week', 'Bill') is None


@pytest.mark.asyncio
async def test_refunding_page_and_tentative_auctions(monkeypatch):
    serve(monkeypatch, {'most-recent-quarterly-refunding-documents': REFUNDING_HTML,
                        'TentativeAuctionScheduleQ32026.xml': TENTATIVE_XML})
    rows = await econ.TreasuryRefundingCalendar().calendar('2026-09-26', '2026-11-30')
    refunding = {r['name']: r for r in rows if r['event_class'] == 'UST_REFUNDING'}
    assert refunding['Treasury quarterly refunding statement']['scheduled_at'] == '2026-11-04T13:30:00+00:00'  # 08:30 EST
    assert refunding['Treasury financing estimates (refunding)']['scheduled_at'] == '2026-11-02T20:00:00+00:00'
    assert all('previous release' in r['time_basis'] for r in refunding.values())
    auctions = {r['event_class']: r for r in rows if r['category'] == 'TREASURY_AUCTION'}
    assert set(auctions) == {'UST_AUCTION_3Y', 'UST_AUCTION_10Y', 'UST_AUCTION_30Y', 'UST_AUCTION_20Y', 'UST_AUCTION_2Y',
                             'UST_AUCTION_7Y'}  # TIPS and FRN have no class
    assert all(r['time_known'] is False and r['tentative'] for r in auctions.values())
    assert auctions['UST_AUCTION_10Y']['reopening'] is True


# --- Finnhub earnings ----------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finnhub_earnings_are_session_only_with_labelled_estimates(monkeypatch):
    calendars = {
        'AAPL': [{'date': '2026-10-28', 'epsActual': None, 'epsEstimate': 2.0219, 'hour': 'amc', 'quarter': 4,
                  'revenueActual': None, 'revenueEstimate': 115327453620, 'symbol': 'AAPL', 'year': 2026}],
        'MSFT': [{'date': '2026-10-27', 'epsActual': None, 'epsEstimate': 4.8231, 'hour': '', 'quarter': 1,
                  'revenueActual': None, 'revenueEstimate': 92452725366, 'symbol': 'MSFT', 'year': 2027}],
        'TSLA': [{'date': '2026-10-20', 'epsActual': None, 'epsEstimate': 0.4464, 'hour': 'amc', 'quarter': 3,
                  'revenueActual': None, 'revenueEstimate': 28179264130, 'symbol': 'TSLA', 'year': 2026},
                 {'date': '2026-10-21', 'hour': 'bmo', 'symbol': 'OTHER'}]}
    seen: list = []

    def answer(params):
        return {'earningsCalendar': calendars.get(params['symbol'], [])}
    serve(monkeypatch, {'/calendar/earnings': (200, answer)}, seen)
    provider = FinnhubProvider('test-key')
    status = provider.health['status']
    rows = {r['symbol']: r for r in await provider.calendar('2026-09-26', '2026-11-10')}
    assert len(seen) == 10 and all(call['params']['symbol'] for call in seen)  # one query per tracked equity
    assert provider.guard.remaining() == 50  # the shared 60/min guard paid for every call
    aapl = rows['AAPL']
    assert aapl['scheduled_at'] == '2026-10-28T20:00:00+00:00' and aapl['time_known'] is False  # after 16:00 EDT
    assert aapl['time_basis'].startswith('after the US close') and aapl['event_class'] == 'EARNINGS'
    assert aapl['consensus'] is None and aapl['estimates']['label'] == 'Finnhub estimate'
    assert aapl['estimates']['eps'] == 2.0219 and aapl['reference_period'] == 'Q4 FY2026'
    assert rows['MSFT']['scheduled_at'] == '2026-10-27T04:00:00+00:00' and rows['MSFT']['session'] is None
    assert 'OTHER' not in rows and 'BMNR' not in rows
    assert provider.health['status'] == status  # quote health untouched by the calendar


@pytest.mark.asyncio
async def test_finnhub_earnings_failures_never_touch_quotes_and_limit_replacement(monkeypatch):
    async def request(self, method, url, **kwargs):
        status = 404 if (kwargs.get('params') or {}).get('symbol') == 'COIN' else 200
        return httpx.Response(status, content=json.dumps({'earningsCalendar': []}).encode(), request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = FinnhubProvider('test-key')
    await provider.calendar('2026-09-26', '2026-11-10')
    assert provider.replace_scope({'symbol': 'AAPL'}) and not provider.replace_scope({'symbol': 'COIN'})
    assert provider.next_attempt == 0.0 and provider.failures == 0 and provider.health['earnings']['status'] == 'DEGRADED'
    assert 'HTTP 404' in provider.health['earnings']['failed']['COIN'] and 'error' not in provider.health['earnings']
    assert earnings_rows([{'symbol': 'AAPL', 'date': 'soon'}]) == []  # invalid vendor rows are dropped


# --- registry and calendar hygiene -------------------------------------------------------------------------------------------


def test_registry_adds_the_release_calendars():
    default = {p.name for p in by_capability(build_providers(Settings(_env_file=None)), 'calendar')}
    assert {'fed_events_calendar', 'bea_release_calendar', 'census_calendar', 'dol_claims_rule', 'survey_schedule',
            'treasury_refunding'} <= default
    assert 'bls_release_calendar' not in default and 'fred' not in default and 'finnhub' not in default
    keyed = {p.name for p in by_capability(build_providers(Settings(
        _env_file=None, sec_user_agent='Desk ops@desk.test', fred_api_key='k', finnhub_api_key='k')), 'calendar')}
    assert {'bls_release_calendar', 'fred', 'finnhub'} <= keyed


def row(name, provider, at, event_class=None, **extra):
    return calendar_row(name, extra.pop('country', 'US'), extra.pop('category', 'MACRO'), at, extra.pop('impact', 8),
                        provider, event_class=event_class, **extra)


T = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)


def test_dedupe_prefers_exact_time_then_official_and_lends_values():
    bls = row('US CPI', 'bls_release_calendar', T, 'US_CPI', time_known=True, time_source='source')
    fred = row('US CPI', 'fred', T, 'US_CPI', time_known=True, time_source='config')
    fmp = dict(fmp_economic_rows([{'date': '2026-10-14 12:30:00', 'event': 'CPI YoY', 'country': 'US', 'impact': 'High',
                                   'estimate': 3.0, 'previous': 2.9}], 'calendar')[0])
    assert fmp['event_class'] == 'US_CPI'
    kept, dropped = dedupe([bls, fred, fmp])
    assert [r['provider'] for r in kept] == ['bls_release_calendar']
    assert kept[0]['consensus'] == 3.0 and kept[0]['values_source'] == 'calendar' and kept[0]['duplicates'] == ['calendar', 'fred']
    assert {r['provider'] for r in dropped} == {'fred', 'calendar'}
    rule = row('US initial jobless claims', 'dol_claims_rule', T, 'US_CLAIMS', time_known=True, time_source='rule')
    vendor = row('Initial Jobless Claims', 'calendar', T, 'US_CLAIMS', time_known=True, time_source='source')
    assert [r['provider'] for r in dedupe([rule, vendor])[0]] == ['calendar']  # exact time beats the weekly rule
    # Different sub-events, symbols or days are different releases.
    decision = row('ECB decision', 'ecb_calendar', T, 'ECB', country='EU', sub_event='DECISION')
    conference = row('ECB press conference', 'ecb_calendar', T + timedelta(minutes=30), 'ECB', country='EU',
                     sub_event='PRESS_CONFERENCE')
    aapl = row('AAPL earnings', 'finnhub', T, 'EARNINGS', symbol='AAPL', time_known=False)
    amzn = row('AMZN earnings', 'finnhub', T, 'EARNINGS', symbol='AMZN', time_known=False)
    kept, dropped = dedupe([decision, conference, aapl, amzn])
    assert len(kept) == 4 and dropped == []
    # A date-only tentative auction loses to the announced auction with a closing time.
    tentative = row('9-Year 10-Month Note auction (tentative)', 'treasury_refunding', datetime(2026, 10, 14, 4, tzinfo=UTC),
                    'UST_AUCTION_10Y', time_known=False, category='TREASURY_AUCTION')
    announced = row('9-Year 10-Month Note auction', 'treasury_auctions', T + timedelta(minutes=30), 'UST_AUCTION_10Y',
                    time_known=True, time_source='source', category='TREASURY_AUCTION')
    assert [r['provider'] for r in dedupe([tentative, announced])[0]] == ['treasury_auctions']


def fmp_cpi(*metrics, day=14):
    """FMP economic-calendar rows of one CPI release: (event, estimate, actual)."""
    return fmp_economic_rows([{'date': f'2026-10-{day} 12:30:00', 'event': name, 'country': 'US', 'impact': 'High',
                               'estimate': estimate, 'actual': actual, 'previous': 0.2, 'unit': '%'}
                              for name, estimate, actual in metrics], 'calendar')


CPI_METRICS = (('CPI MoM', 0.3, 0.5), ('CPI YoY', 2.9, 3.1), ('Core CPI MoM', 0.3, 0.4))


def test_dedupe_keeps_every_metric_of_a_multi_metric_release():
    """Review finding: the official row won and every FMP metric row (with its consensus and actual) was dropped."""
    bls = row('US CPI', 'bls_release_calendar', T, 'US_CPI', time_known=True, time_source='source')
    vendor = fmp_cpi(*CPI_METRICS)
    assert {r['event_class'] for r in vendor} == {'US_CPI'}
    kept, dropped = dedupe([bls, *vendor])
    assert [r['provider'] for r in kept] == ['bls_release_calendar'] and len(dropped) == 3
    cpi_row = kept[0]
    # The configured headline (core CPI MoM) is lent as the scalars; every metric keeps its own values.
    assert (cpi_row['consensus'], cpi_row['actual'], cpi_row['unit']) == (0.3, 0.4, '%')
    assert cpi_row['metric'] == cpi_row['headline_metric'] == 'Core CPI MoM' and cpi_row['values_source'] == 'calendar'
    assert [(m['name'], m['consensus'], m['actual']) for m in cpi_row['metrics']] == [
        ('Core CPI MoM', 0.3, 0.4), ('CPI MoM', 0.3, 0.5), ('CPI YoY', 2.9, 3.1)]
    assert all(m['provider'] == 'calendar' for m in cpi_row['metrics'])
    # Dedupe again with the stored merged row and fresher vendor rows: values are replaced, never stacked or lost.
    fresher = fmp_cpi(('CPI MoM', 0.3, 0.6), ('CPI YoY', 2.9, 3.2), ('Core CPI MoM', 0.3, 0.5))
    again = dedupe([cpi_row, *fresher])[0][0]
    assert again['actual'] == 0.5 and [m['actual'] for m in again['metrics']] == [0.5, 0.6, 3.2]
    # Without an official row every vendor metric row is kept and each lists all metrics with the headline named.
    vendor_only = dedupe(fmp_cpi(*CPI_METRICS))[0]
    assert len(vendor_only) == 3 and all(len(r['metrics']) == 3 and r['headline_metric'] == 'Core CPI MoM'
                                         for r in vendor_only)
    # Several metrics and no configured headline: listed, never guessed into the scalars.
    durable = row('US durable goods orders (advance)', 'census_calendar', T, 'US_DURABLE_GOODS', time_known=True,
                  time_source='source')
    orders = [dict(row(name, 'calendar', T, 'US_DURABLE_GOODS', time_known=True, time_source='source'), consensus=c, actual=a)
              for name, c, a in (('Durable Goods Orders MoM', 1.0, 2.0), ('Durable Goods Orders ex Transp MoM', 0.2, 0.1))]
    merged = dedupe([durable, *orders])[0][0]
    assert merged['consensus'] is None and merged['actual'] is None and len(merged['metrics']) == 2
    assert 'values_source' not in merged and 'headline_metric' not in merged


def test_structured_estimates_are_kept_and_only_the_actual_is_lent():
    finnhub = earnings_rows([{'symbol': 'AAPL', 'date': '2026-10-28', 'hour': 'amc', 'epsEstimate': 2.02}])[0]
    fmp = row('AAPL earnings', 'calendar', datetime(2026, 10, 28, 4, tzinfo=UTC), 'EARNINGS', symbol='AAPL',
              time_known=False, category='EARNINGS', consensus=1.9, actual=2.1, unit='EPS')
    kept, dropped = dedupe([finnhub, fmp])
    aapl = kept[0]
    assert aapl['provider'] == 'finnhub' and dropped == [fmp]
    assert aapl['consensus'] is None and aapl['estimates']['label'] == 'Finnhub estimate'  # never relabelled
    assert aapl['actual'] == 2.1 and aapl['values_source'] == 'calendar' and aapl['consensus_source'] == 'finnhub'
    assert aapl['metrics'][0]['consensus'] == 1.9  # the vendor's own figure stays listed under its provider


def test_carry_lent_keeps_values_when_the_official_row_is_fetched_again():
    kept = dedupe([row('US CPI', 'bls_release_calendar', T, 'US_CPI', time_known=True, time_source='source'),
                   *fmp_cpi(*CPI_METRICS)])[0][0]
    fresh = row('US CPI', 'bls_release_calendar', T, 'US_CPI', time_known=True, time_source='source')
    carried = carry_lent(kept, fresh)
    assert carried['actual'] == 0.4 and carried['metrics'] == kept['metrics'] and carried['values_source'] == 'calendar'
    assert carry_lent(kept, dict(fresh, actual=9.9))['actual'] == 9.9  # a row's own value always wins
    assert carry_lent(None, fresh) is fresh


def test_stale_ids_only_inside_the_window_and_never_history():
    first, last = date(2026, 9, 26), date(2026, 11, 10)
    rows = [row('kept', 'p', datetime(2026, 10, 1, tzinfo=UTC)), row('gone', 'p', datetime(2026, 10, 2, tzinfo=UTC)),
            dict(row('released', 'p', datetime(2026, 10, 3, tzinfo=UTC)), actual=1.0),
            row('edge', 'p', datetime(2026, 9, 26, 23, tzinfo=UTC)), row('other', 'q', datetime(2026, 10, 2, tzinfo=UTC))]
    assert stale_ids(rows, 'p', {rows[0]['id']}, first, last) == [rows[1]['id']]
    assert stale_ids(rows, 'p', set(), first, last, scope=lambda r: r['name'] == 'kept') == [rows[0]['id']]


def test_watch_ignores_date_only_and_suspended_rows():
    now = datetime(2026, 9, 29, 3, 50, tzinfo=UTC)  # 23:50 ET the evening before a date-only auction day
    midnight = {'id': 'a', 'name': '9-Year 10-Month Note auction', 'impact': 7, 'time_known': False,
                'scheduled_at': '2026-09-29T04:00:00+00:00'}
    suspended = {'id': 's', 'name': 'US retail sales', 'impact': 8, 'status': 'SUSPENDED',
                 'scheduled_at': '2026-09-29T04:00:00+00:00'}
    state = refresh.watch_state(now, [midnight, suspended])
    assert state['mode'] == 'NORMAL' and state['next_event'] is None and state['next_watch_at'] is None
    assert refresh.watch_state(now, [dict(midnight, time_known=True)])['mode'] == 'RELEASE_WATCH'


@pytest.mark.asyncio
async def test_ecb_press_conference_is_a_sub_event_of_the_decision(monkeypatch):
    page = ('<dl><dt>28/10/2026</dt><dd>Governing Council of the ECB: monetary policy meeting in Frankfurt (Day 1)</dd>'
            '<dt>29/10/2026</dt><dd>Governing Council of the ECB: monetary policy meeting in Frankfurt (Day 2), followed '
            'by press conference</dd></dl>')
    serve(monkeypatch, {'ecb.europa.eu': page})
    decision, conference = await ECBCalendar().calendar('2026-10-01', '2026-10-31')
    assert decision['sub_event'] == 'DECISION' and conference['sub_event'] == 'PRESS_CONFERENCE'
    assert conference['parent_event_id'] == decision['id'] and decision['event_class'] == conference['event_class'] == 'ECB'
    assert '27 June 2022' in decision['time_basis'] and '14:45 CET' in conference['time_basis']
    # Vendor rows of the same meeting join the official ones (official first): the FMP press conference is no longer
    # a second, unclassified event, and the vendor's rate figures are kept on the official decision.
    vendor = fmp_economic_rows([
        {'date': '2026-10-29 13:45:00', 'event': 'ECB Press Conference', 'country': 'EU', 'impact': 'High'},
        {'date': '2026-10-29 13:15:00', 'event': 'ECB Interest Rate Decision', 'country': 'EU', 'impact': 'High',
         'estimate': 2.15, 'actual': 2.15, 'unit': '%'},
        {'date': '2026-10-29 13:15:00', 'event': 'Deposit Facility Rate', 'country': 'EU', 'impact': 'High',
         'estimate': 2.0, 'actual': 2.0, 'unit': '%'}], 'calendar')
    assert [(r['event_class'], r['sub_event']) for r in vendor] == [('ECB', 'PRESS_CONFERENCE'), ('ECB', 'DECISION'),
                                                                    ('ECB', 'DECISION')]
    kept, dropped = dedupe([decision, conference, *vendor])
    assert {r['provider'] for r in kept} == {'ecb_calendar'} and len(dropped) == 3
    official = next(r for r in kept if r['sub_event'] == 'DECISION')
    assert official['metric'] == 'Deposit Facility Rate' and official['actual'] == 2.0 and len(official['metrics']) == 2
    assert classify('FOMC Economic Projections', 'US') == {'event_class': 'FOMC', 'sub_event': 'PROJECTIONS'}
    assert classify('BoJ Press Conference', 'JP') == {'event_class': 'BOJ', 'sub_event': 'PRESS_CONFERENCE'}


@pytest.mark.asyncio
async def test_rows_carry_kind_is_decision_and_session_anchors(monkeypatch):
    """docs/EVENT_AWARENESS.md §8: kind + is_decision on every row; date-only rows name the session they are anchored to."""
    serve(monkeypatch, {'fomccalendars': FOMC_HTML, 'calendar.json': FED_JSON.encode('utf-8'),
                        'bls.ics': BLS_ICS, 'most-recent-quarterly-refunding-documents': REFUNDING_HTML,
                        'TentativeAuctionScheduleQ32026.xml': TENTATIVE_XML})
    fomc = await FOMCCalendar().calendar('2026-10-01', '2026-10-31')
    assert {(r['sub_event'], r['kind'], r['is_decision']) for r in fomc} == {('DECISION', 'DECISION', True),
                                                                              ('PRESS_CONFERENCE', 'PRESS_CONFERENCE', False)}
    fed = {r['event_class']: r for r in await econ.FedEventsCalendar().calendar('2026-07-01', '2026-11-30')}
    assert (fed['FOMC_MINUTES']['kind'], fed['FED_BEIGE_BOOK']['kind'], fed['US_INDUSTRIAL_PRODUCTION']['kind']) == \
        ('MINUTES', 'RELEASE', 'RELEASE')
    assert not any(r['is_decision'] for r in fed.values()) and fed['FOMC_MINUTES']['speaker_role'] is None
    bls = await econ.BLSReleaseCalendar('Desk ops@desk.test').calendar('2026-09-26', '2026-10-31')
    assert {(r['kind'], r['is_decision']) for r in bls} == {('RELEASE', False)}
    treasury = await econ.TreasuryRefundingCalendar().calendar('2026-09-26', '2026-11-30')
    assert {r['name']: r['kind'] for r in treasury if r['event_class'] == 'UST_REFUNDING'} == {
        'Treasury quarterly refunding statement': 'STATEMENT', 'Treasury financing estimates (refunding)': 'RELEASE'}
    assert {r['kind'] for r in treasury if r['category'] == 'TREASURY_AUCTION'} == {'AUCTION'}
    boj = BoJCalendar().rows([{'decision_day': '2026-10-30', 'meeting_days': ['2026-10-29', '2026-10-30'],
                               'provisional': False, 'report': True, 'press_conference': False}], '2026-10-01', '2026-10-31', 'now')
    assert [(r['kind'], r['is_decision'], r['session'], r['time_known']) for r in boj] == [('DECISION', True, 'TOKYO_DAY', False)]
    earnings = {r['symbol']: r for r in earnings_rows([
        {'symbol': 'AAPL', 'date': '2026-10-28', 'hour': 'amc', 'epsEstimate': 2.02, 'revenueEstimate': 1.2e11},
        {'symbol': 'TSLA', 'date': '2026-10-21', 'hour': 'bmo', 'epsEstimate': 0.45},
        {'symbol': 'MSFT', 'date': '2026-10-27', 'hour': 'dmh'}, {'symbol': 'NVDA', 'date': '2026-11-18', 'hour': ''}])}
    assert {s: r['session'] for s, r in earnings.items()} == {'AAPL': 'AMC', 'TSLA': 'BMO', 'MSFT': None, 'NVDA': None}
    assert all(r['kind'] == 'EARNINGS' and r['is_decision'] is False for r in earnings.values())
    estimate = earnings['AAPL']['estimates']
    assert estimate['label'] == 'Finnhub estimate' and 'consensus' not in estimate['label'].casefold()
    assert estimate['values'] == [{'name': 'EPS', 'value': 2.02, 'unit': 'USD/share'},
                                  {'name': 'Revenue', 'value': 1.2e11, 'unit': 'USD'}]
    assert earnings['AAPL']['consensus'] is None and earnings['AAPL']['consensus_source'] == 'finnhub'
    assert earnings['MSFT']['estimates'] is None  # no estimate published: nothing is invented
    fmp = fmp_earnings_rows([{'symbol': 'AAPL', 'date': '2026-10-28', 'epsEstimated': 1.9}], {'AAPL'})[0]
    assert (fmp['kind'], fmp['session']) == ('EARNINGS', None)
    expiry = row('BTC options expiry', 'crypto_expiries', T, 'CRYPTO_EXPIRY', category='OPTION_EXPIRY', country='CRYPTO')
    legislation = row('HR1: bill', 'congress_api', T, None, category='LEGISLATION')
    unknown = row('Monetary policy meeting', 'configured', T, None, category='CENTRAL_BANK')
    assert (expiry['kind'], legislation['kind'], unknown['kind'], unknown['is_decision']) == ('EXPIRY', 'OTHER', None, None)


# --- runtime: replacement, dedupe, fallback, throttling -------------------------------------------------------------------------


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


class Source:
    configured = True

    def __init__(self, name, rows, fallback_for=None):
        self.name, self.rows, self.calls, self.fail = name, rows, 0, False
        self.health = {'name': name, 'status': 'ONLINE'}
        if fallback_for:
            self.fallback_for = fallback_for

    async def calendar(self, start, end):
        self.calls += 1
        if self.fail:
            from backend.providers.base import ProviderUnavailable
            raise ProviderUnavailable(f'{self.name}: HTTP 403', 403)
        return [dict(r) for r in self.rows]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, 'utcnow', lambda: NOW)
    store = Store(f'sqlite:///{tmp_path}/calendar.db')
    store.initialize()
    hub = HubService(store, Settings(_env_file=None, enable_ingestion=False))
    hub.initialize()
    return Runtime(hub)


def cpi(provider, day=14, **extra):
    return row('US CPI', provider, datetime(2026, 10, day, 12, 30, tzinfo=UTC), 'US_CPI', time_known=True,
               time_source='config' if provider == 'fred' else 'source', **extra)


@pytest.mark.asyncio
async def test_refresh_replaces_rescheduled_rows_and_keeps_rows_after_a_failure(runtime):
    source = Source('bls_release_calendar', [cpi('bls_release_calendar'), cpi('bls_release_calendar', day=20)])
    runtime.hub.providers = {'bls_release_calendar': source}
    await runtime.refresh_calendar()
    assert len(runtime.hub.store.list('macro_calendar')) == 2
    source.rows = [cpi('bls_release_calendar', day=15)]  # rescheduled: the 14th and the 20th disappear
    runtime.calendar_fetched.clear()  # past calendar_sources.min_seconds
    await runtime.refresh_calendar()
    assert [r['scheduled_at'][:10] for r in runtime.hub.store.list('macro_calendar')] == ['2026-10-15']
    source.fail = True
    runtime.calendar_fetched.clear()
    await runtime.refresh_calendar()
    assert source.calls == 3 and [r['scheduled_at'][:10] for r in runtime.hub.store.list('macro_calendar')] == ['2026-10-15']


@pytest.mark.asyncio
async def test_fred_fallback_runs_only_while_bls_is_unusable(runtime):
    bls = Source('bls_release_calendar', [cpi('bls_release_calendar')])
    fred = Source('fred', [cpi('fred'), cpi('fred', day=21)], fallback_for='bls_release_calendar')
    runtime.hub.providers = {'bls_release_calendar': bls, 'fred': fred}
    bls.fail = True
    await runtime.refresh_calendar()
    assert fred.calls == 1 and {r['provider'] for r in runtime.hub.store.list('macro_calendar')} == {'fred'}
    bls.fail = False
    await runtime.refresh_calendar()
    stored = runtime.hub.store.list('macro_calendar')
    # BLS answered: the fallback is not called and its rows are replaced; the same CPI is kept once, from BLS.
    assert fred.calls == 1 and [(r['provider'], r['scheduled_at'][:10]) for r in stored] == [('bls_release_calendar', '2026-10-14')]
    assert stored[0]['time_source'] == 'source'  # same name and time: one id, now carrying the BLS row


@pytest.mark.asyncio
async def test_dedupe_across_stored_rows_and_source_throttling(runtime, monkeypatch):
    rule = Source('dol_claims_rule', [row('US initial jobless claims', 'dol_claims_rule', datetime(2026, 10, 1, 12, 30, tzinfo=UTC),
                                          'US_CLAIMS', time_known=True, time_source='rule')])
    vendor = Source('calendar', [dict(row('Initial Jobless Claims', 'calendar', datetime(2026, 10, 1, 12, 30, tzinfo=UTC),
                                          'US_CLAIMS', time_known=True, time_source='source'), consensus=220.0)])
    runtime.hub.providers = {'dol_claims_rule': rule}
    await runtime.refresh_calendar()
    runtime.hub.providers = {'dol_claims_rule': rule, 'calendar': vendor}
    await runtime.refresh_calendar()
    stored = runtime.hub.store.list('macro_calendar')
    assert [(r['provider'], r['consensus']) for r in stored] == [('calendar', 220.0)]
    assert rule.calls == 1  # dol_claims_rule is fetched at most every calendar_sources.min_seconds


@pytest.mark.asyncio
async def test_refresh_keeps_vendor_metric_values_on_the_official_row(runtime, monkeypatch):
    """Review finding: every FMP CPI metric row was deleted by the dedupe, so the release had no consensus or actual
    (no MACRO_RELEASE, no surprise grading). The values now live on the official row and survive later refreshes."""
    raw = [{'date': '2026-10-14 12:30:00', 'event': name, 'country': 'US', 'impact': 'High', 'estimate': estimate,
            'actual': actual, 'previous': 0.2, 'unit': '%'} for name, estimate, actual in CPI_METRICS]
    bls = Source('bls_release_calendar', [cpi('bls_release_calendar')])
    vendor = Source('calendar', raw)  # FMP answers raw vendor rows; the runtime converts them
    runtime.hub.providers = {'bls_release_calendar': bls, 'calendar': vendor}
    before = refresh.trigger_state([], runtime.hub.store.list('macro_calendar'))
    await runtime.refresh_calendar()
    stored = runtime.hub.store.list('macro_calendar')
    assert [(r['provider'], r['actual'], r['consensus'], r['metric']) for r in stored] == [
        ('bls_release_calendar', 0.4, 0.3, 'Core CPI MoM')]
    assert [m['actual'] for m in stored[0]['metrics']] == [0.4, 0.5, 3.1] and stored[0]['actual_ingested_at']
    after = refresh.trigger_state([], stored)
    assert [t['trigger'] for t in refresh.detect_triggers(before, after)] == ['MACRO_RELEASE']
    # Release alerts name the published metric, never the official row carrying a lent value.
    titles = sorted(a['title'] for a in runtime.hub.store.list('alerts') if a['kind'] == 'MACRO_RELEASE')
    assert titles == ['Released: CPI MoM', 'Released: CPI YoY', 'Released: Core CPI MoM']
    # Later refreshes: the first-known time of the actual never moves (no-lookahead replays), whether the vendor rows
    # are fetched again or the vendor is down and only BLS answers (the official row keeps the values lent to it).
    monkeypatch.setattr(runtime_module, 'utcnow', lambda: NOW + timedelta(hours=1))
    runtime.calendar_fetched.clear()
    await runtime.refresh_calendar()
    vendor.fail = True
    runtime.calendar_fetched.clear()
    await runtime.refresh_calendar()
    again = runtime.hub.store.list('macro_calendar')
    assert bls.calls == 3 and [(r['actual'], len(r['metrics'])) for r in again] == [(0.4, 3)]
    assert again[0]['actual_ingested_at'] == stored[0]['actual_ingested_at'] == NOW.isoformat()
    assert {m['actual_ingested_at'] for m in again[0]['metrics']} == {NOW.isoformat()}
    # Release history stays under the vendor's own metric names (lent values are never re-recorded as 'US CPI').
    assert {r['name'] for r in runtime.hub.store.list('macro_releases')} == {'CPI MoM', 'CPI YoY', 'Core CPI MoM'}


@pytest.mark.asyncio
async def test_a_new_sec_contact_rebuilds_the_bls_release_calendar(runtime):
    assert runtime.hub.providers['bls_release_calendar'].configured is False  # no contact e-mail in the test settings
    runtime.hub.apply_settings(Settings(_env_file=None, enable_ingestion=False, sec_user_agent='Desk ops@desk.test'),
                               ['sec_user_agent'])
    assert runtime.hub.providers['bls_release_calendar'].configured is True


@pytest.mark.asyncio
async def test_finnhub_scope_keeps_rows_of_symbols_not_queried(runtime):
    class Earnings(Source):
        def replace_scope(self, stored_row):
            return stored_row.get('symbol') in self.scope
    aapl = row('AAPL earnings', 'finnhub', datetime(2026, 10, 28, 20, tzinfo=UTC), 'EARNINGS', symbol='AAPL', time_known=False)
    coin = row('COIN earnings', 'finnhub', datetime(2026, 10, 28, 20, tzinfo=UTC), 'EARNINGS', symbol='COIN', time_known=False)
    earnings = Earnings('finnhub', [aapl, coin])
    earnings.scope = {'AAPL', 'COIN'}
    runtime.hub.providers = {'finnhub': earnings}
    await runtime.refresh_calendar()
    runtime.calendar_fetched.clear()
    earnings.rows, earnings.scope = [], {'AAPL'}  # COIN was not queried this round (guard full): its row stays
    await runtime.refresh_calendar()
    assert [r['symbol'] for r in runtime.hub.store.list('macro_calendar')] == ['COIN']
