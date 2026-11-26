"""Central-bank meeting calendars parsed from the official schedule pages (ECB, Bank of England, Bank of Japan).

Each page lists meeting dates only; the announcement time comes from configuration (per bank, local timezone) and is
converted to UTC. Rows are date/event facts with the bank's attribution and source link; page text is never stored.
A parse that finds no meetings at all is an alert-worthy failure (layout change), not an empty calendar.
A bank without a fixed announcement time (BoJ) is date-only and names its session (TOKYO_DAY): the event engine keeps
the decision live for the Tokyo business day instead of a clock time (docs/EVENT_AWARENESS.md §8)."""
import html as html_lib
import re
import time as clock
from datetime import date, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from pydantic import Field

from backend.config import rules
from backend.database import utcnow
from backend.providers.base import HTTPProvider
from backend.providers.calendars import MONTHS, CalendarParseError, _clock, calendar_row
from backend.providers.models import ProviderRow, validate_rows


class MeetingRow(ProviderRow):
    """One parsed policy meeting. decision_day is the announcement day (last meeting day unless marked otherwise)."""
    decision_day: date
    meeting_days: list[date] = Field(min_length=1, max_length=3)
    press_conference: bool = False
    report: bool = False
    provisional: bool = False


def _meeting(decision_day: date, meeting_days: list[date], *, press_conference: bool = False, report: bool = False,
             provisional: bool = False) -> dict:
    return {'decision_day': decision_day.isoformat(), 'meeting_days': [d.isoformat() for d in meeting_days],
            'press_conference': press_conference, 'report': report, 'provisional': provisional}


def _text(fragment: str) -> str:
    """Tag-free, entity-decoded, whitespace-normalised cell text (&nbsp; becomes a plain space)."""
    return ' '.join(html_lib.unescape(re.sub(r'<[^>]+>', ' ', fragment)).replace('\xa0', ' ').split())


def _date(year: int, month: int, day: int) -> date | None:
    """A real calendar date or None: a malformed cell is skipped, never coerced into a plausible date."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _month(name: str) -> int | None:
    return MONTHS.get(name.strip('.').casefold()[:3])


def parse_ecb(page: str) -> list[dict]:
    """ECB Governing Council schedule: <dt>dd/mm/yyyy</dt><dd>description</dd>. Only monetary policy meetings count;
    consecutive meeting days form one meeting whose decision day is the press-conference day (else the last day)."""
    days: list[tuple[date, bool]] = []
    for stamp, description in re.findall(r'<dt>\s*(\d{2}/\d{2}/\d{4})\s*</dt>\s*<dd>(.*?)</dd>', page, re.S):
        text = _text(description).casefold()
        if 'monetary policy meeting' not in text or 'non-monetary' in text:
            continue
        day = _date(*(int(part) for part in reversed(stamp.split('/'))))
        if day:
            days.append((day, 'press conference' in text))
    days.sort()
    groups: list[list[tuple[date, bool]]] = []
    for item in days:
        if groups and (item[0] - groups[-1][-1][0]).days == 1:
            groups[-1].append(item)
        else:
            groups.append([item])
    meetings = []
    for group in groups:
        conference = [day for day, flag in group if flag]
        meetings.append(_meeting(conference[-1] if conference else group[-1][0], [day for day, _ in group],
                                 press_conference=bool(conference)))
    return meetings


def _year_sections(page: str) -> list[tuple[int, str, str]]:
    """(year, heading text, table html) for every '<h2>YYYY ...</h2> ... </table>' section."""
    return [(int(year), _text(heading), body) for year, heading, body in
            re.findall(r'<h2[^>]*>\s*((?:19|20)\d{2})\b([^<]*)</h2>(.*?)</table>', page, re.S)]


def parse_boe(page: str) -> list[dict]:
    """Bank of England upcoming MPC dates: one table per '<YYYY> confirmed dates' heading, first cell 'Thursday 5 November'.
    The second cell marks Monetary Policy Report meetings; non-confirmed sections are flagged provisional."""
    meetings = []
    for year, heading, table in _year_sections(page):
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', table, re.S):
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
            if not cells:
                continue
            match = re.search(r'\b(\d{1,2})\s+([A-Za-z]+)\b', _text(cells[0]))
            month = _month(match.group(2)) if match else None
            day = _date(year, month, int(match.group(1))) if match and month else None
            if day is None:
                continue
            report = any('monetary policy report' in _text(cell).casefold() for cell in cells[1:])
            meetings.append(_meeting(day, [day], report=report, provisional='confirmed' not in heading.casefold()))
    return meetings


def _boj_days(cell: str, year: int) -> list[date]:
    """'Apr. 27 (Mon.), 28 (Tues.)', 'Apr. 30 (Thurs.), May 1 (Fri.)' or 'Dec. 30 (Wed.), 2027' -> dates."""
    text = re.sub(r'\[[^\]]*\]|\([^)]*\)', ' ', _text(cell))
    days: list[date] = []
    month: int | None = None
    for part in (p.strip() for p in text.split(',')):
        if re.fullmatch(r'(19|20)\d{2}', part):
            year = int(part)
            days = [moved for d in days if (moved := _date(year, d.month, d.day))]
            continue
        named = re.fullmatch(r'([A-Za-z]+)\.?\s+(\d{1,2})', part)
        if named and _month(named.group(1)):
            month, day_number = _month(named.group(1)), int(named.group(2))
        elif re.fullmatch(r'\d{1,2}', part) and month:
            day_number = int(part)
        else:
            continue
        candidate = _date(year, month, day_number) if month else None
        if candidate is None:
            continue
        if days and candidate < days[-1]:  # meeting spanning the year end
            candidate = _date(candidate.year + 1, candidate.month, candidate.day)
        if candidate:
            days.append(candidate)
    return days


def parse_boj(page: str) -> list[dict]:
    """Bank of Japan MPM schedule: one table per year heading; the first cell of each row holds the meeting days,
    the second the Outlook Report release ('-' when none). Decision day is the last meeting day."""
    meetings = []
    for year, _heading, table in _year_sections(page):
        body = table.split('</thead>')[-1]
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', body, re.S):
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
            if not cells:
                continue
            days = _boj_days(cells[0], year)
            if not days:
                continue
            report = len(cells) > 1 and _text(cells[1]) not in {'', '-'}
            meetings.append(_meeting(days[-1], days, report=report))
    return meetings


class CentralBankCalendar(HTTPProvider):
    """Official central-bank schedule page -> CENTRAL_BANK calendar rows (capability: calendar)."""
    bank: ClassVar[str] = ''

    def __init__(self):
        config = self.config
        url = config['url']
        scheme, _, rest = url.partition('://')
        host, _, path = rest.partition('/')
        super().__init__(config['provider'], f'{scheme}://{host}')
        self.path = '/' + path
        self._cache: tuple[float, list[dict], str] | None = None

    @property
    def config(self) -> dict:
        return rules()['central_bank_calendars']['banks'][self.bank]

    @staticmethod
    def parse(page: str) -> list[dict]:
        raise NotImplementedError

    async def meetings(self) -> tuple[list[dict], str]:
        """Validated meetings plus the UTC fetch time; re-fetched at most once per min_refresh_seconds."""
        minimum = rules()['central_bank_calendars']['min_refresh_seconds']
        if self._cache and clock.monotonic() - self._cache[0] < minimum:
            return self._cache[1], self._cache[2]
        page = (await self.request('GET', self.path)).text
        fetched = utcnow().isoformat()
        parsed = validate_rows(MeetingRow, self.parse(page), self.health)
        if not parsed:
            # Alert-worthy: the page answered but the layout no longer matches the parser.
            self.health.update(status='DEGRADED', error='parser found no meetings (page layout changed?)', alert=True)
            raise CalendarParseError(f'{self.name}: parser found no meetings')
        self.health.update(alert=False, meetings_parsed=len(parsed))
        self._cache = (clock.monotonic(), parsed, fetched)
        return parsed, fetched

    def rows(self, meetings: list[dict], start: str, end: str, as_of: str) -> list[dict]:
        config = self.config
        zone = ZoneInfo(config['timezone'])
        delay = rules()['central_bank_calendars']['delay_label']
        common: dict[str, Any] = {'bank': self.bank, 'attribution': config['attribution'], 'as_of': as_of, 'delay': delay,
                                  'time_known': config['time_known'], 'time_basis': config['time_basis'],
                                  'time_source': 'config' if config['time_known'] else 'none', 'date_source': 'official page',
                                  'event_class': config['event_class'], 'session': config.get('session')}
        rows = []
        for meeting in meetings:
            day = date.fromisoformat(str(meeting['decision_day']))
            if not start <= day.isoformat() <= end:
                continue
            facts = {'meeting_days': [str(d) for d in meeting['meeting_days']], 'provisional': meeting['provisional'],
                     'report': meeting['report'], 'press_conference': meeting['press_conference']}
            decision = calendar_row(config['decision_name'], config['country'], 'CENTRAL_BANK',
                                    datetime.combine(day, _clock(config['decision_time']), zone), config['impact'],
                                    self.name, config['url'], sub_event='DECISION', **common, **facts)
            rows.append(decision)
            if meeting['press_conference'] and config.get('press_conference_time'):
                # Sub-event of the decision: the event stays in progress until the press conference is over.
                rows.append(calendar_row(config['press_conference_name'], config['country'], 'CENTRAL_BANK',
                                         datetime.combine(day, _clock(config['press_conference_time']), zone),
                                         config.get('press_conference_impact', config['impact']), self.name, config['url'],
                                         sub_event='PRESS_CONFERENCE', parent_event_id=decision['id'],
                                         **dict(common, time_basis=config.get('press_conference_time_basis', config['time_basis'])),
                                         **facts))
        return rows

    async def calendar(self, start: str, end: str) -> list[dict]:
        meetings, as_of = await self.meetings()
        return self.rows(meetings, start, end, as_of)


class ECBCalendar(CentralBankCalendar):
    bank = 'ecb'
    parse = staticmethod(parse_ecb)


class BoECalendar(CentralBankCalendar):
    bank = 'boe'
    parse = staticmethod(parse_boe)


class BoJCalendar(CentralBankCalendar):
    bank = 'boj'
    parse = staticmethod(parse_boj)


def central_bank_calendars() -> list[CentralBankCalendar]:
    """All three official-page calendars (free, keyless, always configured)."""
    return [ECBCalendar(), BoECalendar(), BoJCalendar()]
