"""Official release schedules for the event-aware dashboard (docs/EVENT_AWARENESS.md §1). Keyless, schedule facts only.

- Federal Reserve calendar.json: FOMC minutes, Beige Book, Chair/Vice Chair testimony and speeches, G.17 industrial
  production (its federal-holiday rows are kept on the provider for cross-checks). Speeches and testimony carry
  speaker_role (CHAIR | VICE_CHAIR | GOVERNOR) and speaker from the title; only the Chair uses the Chair-level classes. FOMC decisions and press conferences
  come from fomccalendars.htm (FOMCCalendar), which stays the authority for decision dates.
- BEA ICS (JSON fallback): GDP and Personal Income & Outlays (PCE), exact UTC times, reference period from SUMMARY.
- Census economic-indicators calendar (HTML table, ET): Retail Sales, Durable Goods, Housing Starts, New Home Sales.
- BLS release ICS (CPI, Employment Situation, PPI, JOLTS, ECI): only with a contact User-Agent (BLS blocks anonymous
  robots); without one the FRED release-dates fallback (official_data.FredProvider.calendar) is used.
- Rules and schedule-only rows: US initial jobless claims (Thursday 08:30 ET, Wednesday when Thursday is a federal
  holiday), UMich (configured dates, 10:00 ET) and Conference Board (last Tuesday 10:00 ET). Their values, consensus and
  actuals are proprietary and never stored.
- Treasury quarterly refunding page (next financing estimates / refunding statement) and the tentative coupon auction
  schedule XML it links (dates only: time_known false until TreasuryDirect publishes a closing time).

Every row carries event_class, time_known, time_basis, time_source, reference_period, attribution and source_url. A page
that answers but parses to nothing raises CalendarParseError (DEGRADED + alert; the stored rows are kept)."""
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from defusedxml import DefusedXmlException, ElementTree

from backend.config import rules
from backend.database import utcnow
from backend.providers.base import HTTPProvider, ProviderUnavailable, identity, plain_text
from backend.providers.calendars import CalendarParseError, _clock, auction_class, calendar_row
from backend.providers.cb_calendars import _text
from backend.providers.discovery import sec_contact_configured

NEW_YORK = 'America/New_York'
PERIOD = re.compile(r'((?:January|February|March|April|May|June|July|August|September|October|November|December) \d{4}'
                    r'|(?:1st|2nd|3rd|4th|First|Second|Third|Fourth) Quarter \d{4})')


def _config(section: str) -> dict:
    return rules()['provider_routing']['econ_calendars'][section]


def _aliases() -> dict[str, str]:
    return rules()['provider_routing']['econ_calendars']['timezone_aliases']


def _in(day: date, start: str, end: str) -> bool:
    return start <= day.isoformat() <= end


def _raise_empty(provider: Any, what: str) -> None:
    """Alert-worthy: the source answered but nothing parsed (layout change); the stored rows are kept."""
    provider.health.update(status='DEGRADED', error=f'parser found no {what} (layout changed?)', alert=True)
    raise CalendarParseError(f'{provider.name}: parser found no {what}')


def _match(rules_list: list[dict], text: str) -> dict | None:
    return next((rule for rule in rules_list if re.search(rule['pattern'], text, re.IGNORECASE)), None)


# --- iCalendar (RFC 5545) ---------------------------------------------------------------------------------------------


def _unescape(value: str) -> str:
    return re.sub(r'\\([\\,;nN])', lambda m: '\n' if m.group(1) in 'nN' else m.group(1), value)


def parse_ics(text: str) -> list[dict[str, tuple[dict[str, str], str]]]:
    """VEVENTs as {PROPERTY: (parameters, value)}: folded lines are unfolded and TEXT values unescaped."""
    lines = re.sub(r'\r?\n[ \t]', '', text.lstrip('﻿')).splitlines()
    events: list[dict] = []
    current: dict | None = None
    for line in lines:
        if line.strip() == 'BEGIN:VEVENT':
            current = {}
        elif line.strip() == 'END:VEVENT':
            if current is not None:
                events.append(current)
            current = None
        elif current is not None and ':' in line:
            head, value = line.split(':', 1)
            name, *params = head.split(';')
            current[name.upper()] = ({k.upper(): v.strip('"') for k, v in (p.split('=', 1) for p in params if '=' in p)},
                                     _unescape(value.strip()))
    return events


def ics_time(prop: tuple[dict[str, str], str] | None, default_zone: str,
             aliases: dict[str, str] | None = None) -> tuple[datetime | None, bool]:
    """(aware UTC datetime, time_known). UTC ('Z'), TZID (aliases such as BLS 'US-Eastern' mapped to IANA) or
    floating times in the calendar's zone; VALUE=DATE is a date only (stamped 00:00 local, time_known False)."""
    if prop is None:
        return None, False
    params, value = prop
    aliases = _aliases() if aliases is None else aliases
    try:
        zone = ZoneInfo(aliases.get(params.get('TZID') or default_zone, params.get('TZID') or default_zone))
        if params.get('VALUE') == 'DATE' or re.fullmatch(r'\d{8}', value):
            return datetime.combine(datetime.strptime(value[:8], '%Y%m%d').date(), time(0), zone).astimezone(UTC), False
        stamp = datetime.strptime(value.rstrip('Z')[:15], '%Y%m%dT%H%M%S')
        if value.endswith('Z'):
            return stamp.replace(tzinfo=UTC), True
        return stamp.replace(tzinfo=zone).astimezone(UTC), True
    except (ValueError, ZoneInfoNotFoundError):
        return None, False


# --- Federal Reserve calendar.json -----------------------------------------------------------------------------------


def fed_clock(value: str | None) -> time | None:
    """'2:00 p.m.' / '10:00 a.m.' -> time; anything else (blank, 'TBA') -> None."""
    match = re.fullmatch(r'\s*(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?\s*', str(value or ''), re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == 'p' else 0)
    return time(hour, int(match.group(2)))


def fed_days(item: dict) -> list[date]:
    """'month' YYYY-MM plus 'days' '16' or '1, 8, 15' -> dates; legacy rows without a month are skipped."""
    month = re.fullmatch(r'(\d{4})-(\d{2})', str(item.get('month') or ''))
    if not month:
        return []
    result = []
    for number in re.findall(r'\d+', str(item.get('days') or '')):
        try:
            result.append(date(int(month.group(1)), int(month.group(2)), int(number)))
        except ValueError:
            continue
    return result


def speaker_of(title: str, config: dict | None = None) -> tuple[str | None, str | None]:
    """(speaker_role CHAIR | VICE_CHAIR | GOVERNOR | None, speaker) from a Fed calendar title such as
    'Testimony - Vice Chair for Supervision Michelle W. Bowman'; the speaker is the text after the separator."""
    config = config or _config('fed_events')
    role = next((r['role'] for r in config['speaker_roles'] if re.search(r['pattern'], title)), None)
    parts = re.split(config['speaker_separator'], title, maxsplit=1)
    return role, (parts[-1].strip()[:120] or None)


def fed_class(rule: dict, role: str | None, config: dict | None = None) -> tuple[str, int]:
    """(event_class, impact): only the Chair uses the Chair-level classes; any other speaker's testimony or speech is the
    configured official class, whatever rule matched."""
    config = config or _config('fed_events')
    if rule['event_class'] in config['chair_classes'] and role != 'CHAIR':
        return config['official_class'], min(int(rule['impact']), int(config['official_impact']))
    return rule['event_class'], int(rule['impact'])


def parse_fed_events(payload: Any) -> tuple[list[dict], set[date]]:
    """calendar.json -> (matched items {title, day, time|None, rule, link, location}, federal holidays). Items are
    matched by type and title pattern; speakers come from the source title, never from a template."""
    config = _config('fed_events')
    items: list[dict] = []
    holidays: set[date] = set()
    events = payload.get('events') if isinstance(payload, dict) else None
    for item in events if isinstance(events, list) else []:
        if not isinstance(item, dict):
            continue
        # The Fed escapes its text ('Q&amp;A'): titles and locations are decoded to plain text.
        title, kind = plain_text(str(item.get('title') or '')), str(item.get('type') or '')
        days = fed_days(item)
        if not title or not days:
            continue
        if kind == config['holiday_type'] and re.search(config['holiday_pattern'], title):
            holidays.update(days)
            continue
        rule = next((r for r in config['rules'] if r['type'] == kind and re.search(r['pattern'], title)), None)
        if rule is None:
            continue
        for day in days:
            items.append({'title': title, 'day': day, 'time': fed_clock(item.get('time')), 'rule': rule,
                          'link': item.get('link'), 'location': item.get('location')})
    return items, holidays


class FedEventsCalendar(HTTPProvider):
    """federalreserve.gov/json/calendar.json (keyless; ET wall-clock times)."""

    def __init__(self):
        config = _config('fed_events')
        super().__init__(config['provider'], config['base_url'])
        self.holidays: set[date] = set()

    async def calendar(self, start: str, end: str) -> list[dict]:
        config = _config('fed_events')
        # httpx parses the bytes, so the UTF-8 BOM the Fed serves is accepted (json.loads(text) would reject it).
        items, self.holidays = parse_fed_events((await self.request('GET', config['path'])).json())
        if not items and not self.holidays:
            _raise_empty(self, 'calendar events')
        zone = ZoneInfo(config['timezone'])
        rows = []
        speech_kinds = set(rules()['provider_routing']['calendars']['dedupe']['speech_kinds'])
        for item in items:
            if not _in(item['day'], start, end):
                continue
            rule, known = item['rule'], item['time'] is not None
            link = str(item.get('link') or '')
            spoken = rule.get('kind') in speech_kinds
            role, speaker = speaker_of(item['title'], config) if spoken else (None, None)
            event_class, impact = fed_class(rule, role, config) if spoken else (rule['event_class'], rule['impact'])
            rows.append(calendar_row(rule.get('name') or item['title'], 'US', rule['category'],
                                     datetime.combine(item['day'], item['time'] or time(0), zone), impact, self.name,
                                     config['site_url'] + link if link.startswith('/') else link or config['page_url'],
                                     event_class=event_class, sub_event=rule.get('sub_event'), kind=rule.get('kind'),
                                     speaker_role=role, speaker=speaker, bank='fed',
                                     time_known=known, time_source='source' if known else 'none',
                                     time_basis=config['time_basis'] if known else 'date only (no time in the Fed calendar)',
                                     attribution=config['attribution'], title=item['title'],
                                     location=plain_text(str(item.get('location') or ''))[:120] or None))
        self.health.update(alert=False, holidays=len(self.holidays))
        return rows


# --- BEA ---------------------------------------------------------------------------------------------------------------


def bea_rows_from_ics(text: str) -> list[dict]:
    """(release rule, UTC time, summary, reference period, vintage) for allow-listed BEA ICS events."""
    config, aliases = _config('bea'), _aliases()
    result = []
    for event in parse_ics(text):
        summary = event.get('SUMMARY', ({}, ''))[1]
        rule = _match(config['releases'], summary)
        stamp, known = ics_time(event.get('DTSTART'), NEW_YORK, aliases)
        if rule is None or stamp is None or not known:
            continue
        periods = PERIOD.findall(summary)
        vintage = re.search(r'\((Advance|Second|Third)[^)]*\)', summary)
        result.append({'rule': rule, 'at': stamp, 'summary': summary, 'reference_period': periods[-1] if periods else None,
                       'vintage': vintage.group(1) if vintage else None, 'basis': config['time_basis']})
    return result


def bea_rows_from_json(payload: Any) -> list[dict]:
    """release_dates.json fallback: UTC times per release name; no reference period (the JSON has none)."""
    config = _config('bea')
    result = []
    seen: set[tuple[str, str]] = set()
    for name, value in (payload.items() if isinstance(payload, dict) else []):
        rule = _match(config['releases'], str(name))
        if rule is None or not isinstance(value, dict) or not isinstance(value.get('release_dates'), list):
            continue
        for stamp in value['release_dates']:
            try:
                at = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
            except ValueError:
                continue
            if at.tzinfo is None or (rule['event_class'], at.isoformat()) in seen:
                continue  # the file repeats some entries
            seen.add((rule['event_class'], at.isoformat()))
            result.append({'rule': rule, 'at': at.astimezone(UTC), 'summary': str(name), 'reference_period': None,
                           'vintage': None, 'basis': config['json_time_basis']})
    return result


class BEAReleaseCalendar(HTTPProvider):
    """BEA release schedule: ICS first (reference period and estimate vintage in SUMMARY), the JSON as fallback."""

    def __init__(self):
        super().__init__(_config('bea')['provider'], '')  # two official hosts: full URLs as paths

    async def calendar(self, start: str, end: str) -> list[dict]:
        config = _config('bea')
        releases: list[dict] = []
        try:
            releases = bea_rows_from_ics((await self.request('GET', config['ics_url'])).text)
        except ProviderUnavailable as exc:  # the JSON file on the second official host is the fallback
            self.health['ics_error'] = str(exc)[:120]
            self.next_attempt = 0.0  # a failure of the ICS host must not refuse the JSON host through the shared circuit
        if not releases:
            releases = bea_rows_from_json((await self.request('GET', config['json_url'])).json())
        if not releases:
            _raise_empty(self, 'BEA releases')
        rows = []
        for item in releases:
            if not _in(item['at'].astimezone(ZoneInfo(NEW_YORK)).date(), start, end):
                continue
            rule = item['rule']
            name = rule['name'] + (f" ({item['vintage']} estimate)" if item['vintage'] else '')
            impact = rule.get('advance_impact', rule['impact']) if item['vintage'] == 'Advance' else rule['impact']
            rows.append(calendar_row(name, 'US', 'MACRO', item['at'], impact, self.name, config['page_url'],
                                     event_class=rule['event_class'], reference_period=item['reference_period'],
                                     time_known=True, time_source='source', time_basis=item['basis'],
                                     attribution=config['attribution'], release=item['summary'][:200]))
        self.health.update(alert=False)
        return rows


# --- Census -------------------------------------------------------------------------------------------------------------


def parse_census(page: str) -> tuple[list[dict], int]:
    """(releases, suspended count). Date and time come from the visible cells (ET), never the hidden sort keys; one
    slot listing several reference periods (catch-up releases) becomes one release."""
    config = _config('census')
    merged: dict[tuple, dict] = {}
    suspended = 0
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', page, re.S | re.I):
        cells = [_text(cell) for cell in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)]
        if len(cells) < 4:
            continue
        rule = _match(config['indicators'], cells[0])
        if rule is None:
            continue
        if 'suspended' in cells[1].lower():
            suspended += 1  # no scheduled time: never a countdown, never a row
            continue
        try:
            day = datetime.strptime(cells[1], '%B %d, %Y').date()
            at = datetime.strptime(cells[2].upper().replace('.', ''), '%I:%M %p').time()
        except ValueError:
            continue
        key = (rule['event_class'], day, at)
        item = merged.setdefault(key, {'rule': rule, 'day': day, 'time': at, 'periods': [], 'indicator': cells[0]})
        if cells[3] and cells[3] not in item['periods']:
            item['periods'].append(cells[3])
    return list(merged.values()), suspended


class CensusIndicatorCalendar(HTTPProvider):
    def __init__(self):
        config = _config('census')
        super().__init__(config['provider'], config['base_url'])

    async def calendar(self, start: str, end: str) -> list[dict]:
        config = _config('census')
        releases, suspended = parse_census((await self.request('GET', config['path'], headers={'Accept': 'text/html'})).text)
        if not releases:
            _raise_empty(self, 'indicator releases')
        zone = ZoneInfo(config['timezone'])
        rows = [calendar_row(item['rule']['name'], 'US', 'MACRO', datetime.combine(item['day'], item['time'], zone),
                             item['rule']['impact'], self.name, config['base_url'] + config['path'],
                             event_class=item['rule']['event_class'], reference_period=', '.join(item['periods']) or None,
                             time_known=True, time_source='source', time_basis=config['time_basis'],
                             attribution=config['attribution'], release=item['indicator'][:200])
                for item in releases if _in(item['day'], start, end)]
        self.health.update(alert=False, suspended=suspended)
        return rows


# --- BLS ------------------------------------------------------------------------------------------------------------------


class BLSReleaseCalendar(HTTPProvider):
    """BLS release schedule ICS. BLS blocks robots without contact information, so the adapter is configured only when
    SEC_USER_AGENT carries a real contact e-mail (example.com placeholders do not count)."""

    def __init__(self, contact: str):
        config = _config('bls')
        contact = (contact or '').strip()
        configured = sec_contact_configured(contact)
        super().__init__(config['provider'], config['base_url'], configured=configured,
                         headers={'User-Agent': config['user_agent_format'].format(contact=contact),
                                  'Accept': 'text/calendar, text/plain;q=0.9, */*;q=0.8'})
        if not configured:
            self.health['reason'] = 'needs a contact e-mail in SEC_USER_AGENT (BLS blocks anonymous robots)'

    async def calendar(self, start: str, end: str) -> list[dict]:
        config, aliases = _config('bls'), _aliases()
        rows = []
        parsed = 0
        for event in parse_ics((await self.request('GET', config['path'])).text):
            summary = event.get('SUMMARY', ({}, ''))[1]
            rule = _match(config['releases'], summary)
            stamp, known = ics_time(event.get('DTSTART'), NEW_YORK, aliases)
            if rule is None or stamp is None:
                continue
            parsed += 1
            if not _in(stamp.astimezone(ZoneInfo(NEW_YORK)).date(), start, end):
                continue
            rows.append(calendar_row(rule['name'], 'US', 'MACRO', stamp, rule['impact'], self.name, config['page_url'],
                                     event_class=rule['event_class'], time_known=known,
                                     time_source='source' if known else 'none', time_basis=config['time_basis'],
                                     attribution=config['attribution'], release=summary[:200]))
        if not parsed:
            _raise_empty(self, 'BLS releases')
        self.health.update(alert=False)
        return rows


# --- rules and schedule-only rows ---------------------------------------------------------------------------------------


class GeneratedCalendar:
    """Rows from a published rule or configured schedule: no request, always configured."""

    def __init__(self, name: str):
        self.name, self.configured = name, True
        self.health: dict = {**identity(name), 'status': 'PENDING', 'latency_ms': None, 'last_success': None, 'error': None,
                             'error_type': None, 'source': 'rule / configured schedule (no request)'}

    def _done(self, **extra: Any) -> None:
        self.health.update(status='ONLINE', last_success=utcnow().isoformat(), error=None, error_type=None, **extra)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = (date(year + month // 12, month % 12 + 1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    return day - timedelta(days=1) if day.weekday() == 5 else day + timedelta(days=1) if day.weekday() == 6 else day


def us_federal_holidays(year: int) -> set[date]:
    """Observed US federal holidays (5 U.S.C. 6103) for a year: fixed dates move to Friday/Monday when they fall on a
    weekend (the observed New Year's Day of year+1 may fall on 31 December)."""
    result: set[date] = set()
    for y in (year, year + 1):
        fixed = [date(y, 1, 1), date(y, 6, 19), date(y, 7, 4), date(y, 11, 11), date(y, 12, 25)]
        result |= {_observed(d) for d in fixed}
        result |= {_nth_weekday(y, 1, 0, 3), _nth_weekday(y, 2, 0, 3), _last_weekday(y, 5, 0), _nth_weekday(y, 9, 0, 1),
                   _nth_weekday(y, 10, 0, 2), _nth_weekday(y, 11, 3, 4)}
    return {d for d in result if d.year == year}


def claims_dates(start: date, end: date, holidays: set[date]) -> list[tuple[date, date]]:
    """(release day, week-ending Saturday): Thursdays, or the Wednesday before when Thursday is a federal holiday."""
    result = []
    thursday = start - timedelta(days=(start.weekday() - 3) % 7)
    while thursday <= end + timedelta(days=1):
        release = thursday - timedelta(days=1) if thursday in holidays else thursday
        if start <= release <= end:
            result.append((release, thursday - timedelta(days=5)))
        thursday += timedelta(days=7)
    return result


class JoblessClaimsCalendar(GeneratedCalendar):
    """US initial jobless claims by the DOL weekly rule (no official machine-readable schedule exists)."""

    def __init__(self):
        super().__init__(_config('claims')['provider'])

    async def calendar(self, start: str, end: str) -> list[dict]:
        config = _config('claims')
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        holidays = set().union(*(us_federal_holidays(y) for y in range(first.year, last.year + 1)))
        zone = ZoneInfo(config['timezone'])
        rows = [calendar_row(config['name'], 'US', 'MACRO', datetime.combine(day, _clock(config['time']), zone),
                             config['impact'], self.name, config['source_url'], event_class=config['event_class'],
                             reference_period=f'week ending {week.isoformat()}', time_known=True, time_source='rule',
                             time_basis=config['time_basis'], attribution=config['attribution'])
                for day, week in claims_dates(first, last, holidays)]
        self._done()
        return rows


class SurveyScheduleCalendar(GeneratedCalendar):
    """Schedule-only rows for proprietary surveys (UMich configured dates, Conference Board last-Tuesday rule).
    Their values, consensus and actuals are never fetched or stored."""

    def __init__(self):
        super().__init__(_config('surveys')['provider'])

    async def calendar(self, start: str, end: str) -> list[dict]:
        config = _config('surveys')
        rows = []
        umich = config['umich']
        zone = ZoneInfo(umich['timezone'])
        covered = max((item['date'] for item in umich['dates']), default=None)
        for item in umich['dates']:
            day = date.fromisoformat(item['date'])
            if _in(day, start, end):
                rows.append(calendar_row(f"{umich['name']} ({item['kind']})", 'US', 'MACRO',
                                         datetime.combine(day, _clock(umich['time']), zone), umich['impact'], self.name,
                                         umich['source_url'], event_class=umich['event_class'],
                                         reference_period=item.get('reference_period'), time_known=True, time_source='config',
                                         time_basis=umich['time_basis'], attribution=umich['attribution'],
                                         schedule_only=True))
        board = config['conference_board']
        zone = ZoneInfo(board['timezone'])
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        month = date(first.year, first.month, 1)
        while month <= last:
            key = month.strftime('%Y-%m')
            day = date.fromisoformat(board['exceptions'][key]) if key in board['exceptions'] else \
                _last_weekday(month.year, month.month, 1)
            if _in(day, start, end):
                rows.append(calendar_row(board['name'], 'US', 'MACRO', datetime.combine(day, _clock(board['time']), zone),
                                         board['impact'], self.name, board['source_url'], event_class=board['event_class'],
                                         time_known=True, time_source='rule', time_basis=board['time_basis'],
                                         attribution=board['attribution'], schedule_only=True))
            month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
        # Past the configured UMich dates the schedule is unknown, not empty: say so in health.
        self._done(umich_dates_through=covered, note=None if covered is None or covered >= end else
                   f'UMich release dates configured through {covered}; later dates are not listed')
        return rows


# --- Treasury quarterly refunding and tentative auctions ------------------------------------------------------------------


REFUNDING_DATE = r'([A-Za-z]+ \d{1,2}\s*,\s*\d{4})'
REFUNDING_BLOCK = re.compile(r'DOCUMENTS RELEASED at (\d{1,2}:\d{2}\s*[AP]\.?M\.?) [A-Za-z]+, ' + REFUNDING_DATE
                             + r'(.{0,3000}?)\(The next release is scheduled for ' + REFUNDING_DATE + r'\)', re.IGNORECASE)


def _refunding_day(text: str) -> date:
    """'November 4 , 2026' / 'august 3, 2026' (the live page writes both) -> date."""
    return datetime.strptime(re.sub(r'\s*,\s*', ', ', text).title(), '%B %d, %Y').date()


def parse_refunding(page: str) -> list[dict]:
    """'DOCUMENTS RELEASED at 3:00 PM Monday, August 3, 2026 Financing Estimates ... (The next release is scheduled for
    November 2, 2026)' -> {rule, next_day, time (of the previous release)} per configured document label. Month case
    and the spacing around the comma vary on the live page."""
    config = _config('treasury_refunding')
    result = []
    for at, _previous, body, following in REFUNDING_BLOCK.findall(_text(page)):
        rule = next((r for r in config['documents'] if r['label'].lower() in body.lower()), None)
        if rule is None:
            continue
        try:
            clock = re.sub(r'[\s.]', '', at).upper()
            result.append({'rule': rule, 'day': _refunding_day(following),
                           'time': datetime.strptime(clock, '%I:%M%p').time()})
        except ValueError:
            continue
    return result


def _local_name(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _xml_date(value: str | None) -> date | None:
    text = (value or '').strip()
    for fmt, size in (('%Y-%m-%d', 10), ('%m/%d/%Y', None)):
        try:
            return datetime.strptime(text[:size] if size else text, fmt).date()
        except ValueError:
            continue
    return None


def parse_tentative_auctions(xml: str | bytes) -> tuple[list[dict], set[date]]:
    """Tentative auction schedule XML -> (auctions {term, type, auction_day, announcement_day, reopening, flags},
    holidays). Any element with an AuctionDate child is an auction; any with a HolidayDate child a holiday."""
    root = ElementTree.fromstring(xml)
    auctions, holidays = [], set()
    for element in root.iter():
        children = {_local_name(child.tag): (child.text or '').strip() for child in element}
        if 'HolidayDate' in children:
            day = _xml_date(children['HolidayDate'])
            if day:
                holidays.add(day)
        if 'AuctionDate' not in children:
            continue
        day = _xml_date(children['AuctionDate'])
        if day is None:
            continue
        auctions.append({'term': children.get('SecurityTermWeekYear', ''), 'type': children.get('SecurityType', ''),
                         'auction_day': day, 'announcement_day': _xml_date(children.get('AnnouncementDate')),
                         'reopening': children.get('ReOpeningIndicator', '').upper() in {'Y', 'YES', 'TRUE'},
                         'flags': [children.get('TIPS', ''), children.get('FloatingRate', '')]})
    return auctions, holidays


class TreasuryRefundingCalendar(HTTPProvider):
    """'Most recent quarterly refunding documents' page: the next financing estimates and refunding statement (time of
    the previous release, stated in time_basis) plus the tentative coupon auction schedule XML linked there (dates
    only, time_known false; TreasuryDirect's announced auctions with a closing time win the dedupe)."""

    def __init__(self):
        config = _config('treasury_refunding')
        super().__init__(config['provider'], config['base_url'])

    async def calendar(self, start: str, end: str) -> list[dict]:
        config = _config('treasury_refunding')
        page = (await self.request('GET', config['path'], headers={'Accept': 'text/html'})).text
        documents = parse_refunding(page)
        if not documents:
            _raise_empty(self, 'refunding documents')
        zone = ZoneInfo(config['timezone'])
        page_url = config['base_url'] + config['path']
        rows = [calendar_row(item['rule']['name'], 'US', 'TREASURY', datetime.combine(item['day'], item['time'], zone),
                             item['rule']['impact'], self.name, page_url, event_class=config['event_class'],
                             kind=item['rule'].get('kind'), time_known=config['assume_previous_release_time'],
                             time_source='rule' if config['assume_previous_release_time'] else 'none',
                             time_basis=config['time_basis'], attribution=config['attribution'])
                for item in documents if _in(item['day'], start, end)]
        link = re.search(r'href="([^"]*TentativeAuctionSchedule[^"]*\.xml)"', page, re.IGNORECASE)
        path = link.group(1).removeprefix(config['base_url']) if link else None
        auctions: list[dict] = []
        if path and path.startswith('/'):
            try:
                auctions, _holidays = parse_tentative_auctions((await self.request('GET', path)).content)
            except (ElementTree.ParseError, DefusedXmlException, ValueError) as exc:
                self.health.update(status='DEGRADED', error='tentative auction XML unreadable', error_type=type(exc).__name__)
        else:
            self.health['tentative_schedule'] = 'link not found on the refunding page'
        impacts = rules()['provider_routing']['calendars']['auction_impact']
        for auction in auctions:
            event_class = auction_class(auction['term'], auction['type'], auction['flags'])
            if event_class is None or not _in(auction['auction_day'], start, end):
                continue
            rows.append(calendar_row(f"{auction['term']} {auction['type']} auction (tentative)", 'US', 'TREASURY_AUCTION',
                                     datetime.combine(auction['auction_day'], time(0), zone), impacts.get(auction['type'], 3),
                                     self.name, config['base_url'] + (path or ''), event_class=event_class, tentative=True,
                                     reopening=auction['reopening'], time_known=False, time_source='none',
                                     time_basis=config['auction_time_basis'], attribution=config['attribution'],
                                     announcement_date=auction['announcement_day'].isoformat() if auction['announcement_day'] else None))
        self.health.update(alert=False, tentative_auctions=len(auctions))
        return rows


def release_calendars(sec_user_agent: str) -> list[Any]:
    """Every provider of this module (keyless; BLS configured only with a contact User-Agent)."""
    return [FedEventsCalendar(), BEAReleaseCalendar(), CensusIndicatorCalendar(), BLSReleaseCalendar(sec_user_agent),
            JoblessClaimsCalendar(), SurveyScheduleCalendar(), TreasuryRefundingCalendar()]
