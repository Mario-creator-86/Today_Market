"""Session calendars per asset class/venue (config market_sessions). Unknown venues are UNKNOWN, never guessed."""
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from backend.config import rules_view
from backend.engines import aware

STATES = ('LIVE', 'LAST CLOSE', 'FRIDAY CLOSE', 'AFTER HOURS', 'PREMARKET', 'UNKNOWN')
STATE_LABELS = {'LIVE': 'Market open', 'LAST CLOSE': 'Market closed · last close', 'FRIDAY CLOSE': 'Market closed · Friday close',
                'AFTER HOURS': 'After hours', 'PREMARKET': 'Pre-market', 'UNKNOWN': 'Session unknown'}
FRIDAY = 4
CALENDAR_ERRORS = (ValueError, KeyError, IndexError)


def ruleset() -> dict:
    """Shared read-only view of rules() (config.rules_view), refreshed when a config file changes.

    rules() deep-copies the merged config on every call; hot evaluation loops must not pay that per row.
    Callers must never mutate the returned dict.
    """
    return rules_view()


@lru_cache
def _calendar(name: str) -> Any:
    return xcals.get_calendar(name)


def venue(asset_class: str, symbol: str | None = None) -> dict | None:
    config = ruleset()['market_sessions']
    name = config['symbols'].get(symbol or '') or config['asset_classes'].get(asset_class)
    return dict(config['venues'][name], name=name) if name in config['venues'] else None


def _clock(value: str) -> time:
    hours, minutes = value.split(':')
    return time(int(hours), int(minutes))


def _fx_open(day: date, spec: dict) -> bool:
    return day.weekday() < 5 and day.strftime('%m-%d') not in set(spec.get('holidays', []))


def _fx_close(day: date, spec: dict) -> datetime:
    return datetime.combine(day, _clock(spec['close']), ZoneInfo(spec['timezone'])).astimezone(UTC)


def _fx_day(at: datetime, spec: dict) -> date:
    """FX trading date: a session runs from the previous day's close to this day's close (New York)."""
    local = at.astimezone(ZoneInfo(spec['timezone']))
    return local.date() + timedelta(days=1) if local.time() >= _clock(spec['close']) else local.date()


def _fx_next_open_day(day: date, spec: dict) -> date:
    while not _fx_open(day, spec):
        day += timedelta(days=1)
    return day


def _session_label(spec: dict, at: datetime) -> Any:
    """Session containing `at`, or the next session when `at` falls between sessions."""
    return _calendar(spec['calendar']).minute_to_session(pd.Timestamp(at).floor('min'), direction='next')


def session_close(asset_class: str, at: datetime, symbol: str | None = None) -> datetime | None:
    """Close of the session containing `at`; between sessions, the close of the next one."""
    spec, at = venue(asset_class, symbol), aware(at)
    if spec is None:
        return None
    if spec['kind'] == 'continuous':
        cutoff = datetime.combine(at.date(), _clock(spec['close_utc']), UTC)
        return cutoff if at < cutoff else cutoff + timedelta(days=1)
    if spec['kind'] == 'fx':
        return _fx_close(_fx_next_open_day(_fx_day(at, spec), spec), spec)
    try:
        return _calendar(spec['calendar']).session_close(_session_label(spec, at)).to_pydatetime()
    except CALENDAR_ERRORS:
        return None


def next_session_close(asset_class: str, at: datetime, symbol: str | None = None) -> datetime | None:
    spec, current = venue(asset_class, symbol), session_close(asset_class, at, symbol)
    if spec is None or current is None:
        return None
    if spec['kind'] == 'continuous':
        return current + timedelta(days=1)
    if spec['kind'] == 'fx':
        day = _fx_next_open_day(_fx_day(aware(at), spec), spec)
        return _fx_close(_fx_next_open_day(day + timedelta(days=1), spec), spec)
    try:
        calendar = _calendar(spec['calendar'])
        label = calendar.next_session(_session_label(spec, aware(at)))
        return calendar.session_close(label).to_pydatetime()
    except CALENDAR_ERRORS:
        return None


def previous_close(asset_class: str, at: datetime, symbol: str | None = None) -> datetime | None:
    """Close of the latest session that ended at or before `at`. None for a continuous venue (it never closes), an
    unknown venue or a calendar that cannot tell."""
    spec, at = venue(asset_class, symbol), aware(at)
    if spec is None or spec['kind'] == 'continuous':
        return None
    if spec['kind'] == 'fx':
        day = _fx_day(at, spec)
        for _ in range(10):
            close = _fx_close(day, spec)
            if _fx_open(day, spec) and close <= at:
                return close
            day -= timedelta(days=1)
        return None
    try:
        calendar, minute = _calendar(spec['calendar']), pd.Timestamp(at).floor('min')
        label = calendar.minute_to_session(minute, direction='previous')
        close = calendar.session_close(label).to_pydatetime()
        if close > at:  # `at` is inside that session: the one before it is the latest that ended
            close = calendar.session_close(calendar.previous_session(label)).to_pydatetime()
        return close
    except CALENDAR_ERRORS:
        return None


def day_minutes(asset_class: str, day: datetime, symbol: str | None = None) -> int | None:
    """One-minute bars a full UTC day of the venue holds: 1440 for a continuous venue, the session length for an
    exchange calendar (0 on a day without a session). None when it cannot be told (FX, unknown venues, calendar
    errors): the caller then makes no coverage check."""
    spec = venue(asset_class, symbol)
    if spec is None or spec['kind'] == 'fx':
        return None
    if spec['kind'] == 'continuous':
        return 24 * 60
    try:
        calendar, label = _calendar(spec['calendar']), pd.Timestamp(aware(day).date())
        if not calendar.is_session(label):
            return 0
        return int((calendar.session_close(label) - calendar.session_open(label)).total_seconds() // 60)
    except CALENDAR_ERRORS:
        return None


def _in_maintenance(spec: dict, at: datetime) -> bool:
    window = spec.get('maintenance')
    if not window:
        return False
    local = at.astimezone(ZoneInfo(window['timezone'])).time()
    return _clock(window['start']) <= local < _clock(window['end'])


def _calendar_state(spec: dict, at: datetime) -> str:
    calendar, minute = _calendar(spec['calendar']), pd.Timestamp(at).floor('min')
    if calendar.is_open_on_minute(minute):
        return 'LAST CLOSE' if _in_maintenance(spec, at) else 'LIVE'
    upcoming = calendar.minute_to_session(minute, direction='next')
    previous = calendar.minute_to_session(minute, direction='previous')
    opens, closed = calendar.session_open(upcoming), calendar.session_close(previous)
    if spec.get('premarket_minutes') and opens - timedelta(minutes=spec['premarket_minutes']) <= minute < opens:
        return 'PREMARKET'
    if spec.get('after_hours_minutes') and closed <= minute < closed + timedelta(minutes=spec['after_hours_minutes']):
        return 'AFTER HOURS'
    return 'FRIDAY CLOSE' if previous.weekday() == FRIDAY else 'LAST CLOSE'


def market_state(asset_class: str, at: datetime, symbol: str | None = None) -> str:
    """LIVE | LAST CLOSE | FRIDAY CLOSE | AFTER HOURS | PREMARKET | UNKNOWN. Only LIVE may gate publication."""
    spec, at = venue(asset_class, symbol), aware(at)
    if spec is None:
        return 'UNKNOWN'
    if spec['kind'] == 'continuous':
        return 'LIVE'
    if spec['kind'] == 'fx':
        day = _fx_day(at, spec)
        if _fx_open(day, spec):
            return 'LIVE'
        previous = day - timedelta(days=1)
        while not _fx_open(previous, spec):
            previous -= timedelta(days=1)
        return 'FRIDAY CLOSE' if previous.weekday() == FRIDAY else 'LAST CLOSE'
    try:
        return _calendar_state(spec, at)
    except CALENDAR_ERRORS:
        return 'UNKNOWN'


def session_info(asset_class: str, at: datetime, symbol: str | None = None) -> dict:
    """Display payload: state plus the venue and its basis so the UI never implies unmodelled precision. `venue` is
    the machine venue id (e.g. XNYS_INDEX); `venue_label` and `basis` are the plain-English text to show."""
    spec = venue(asset_class, symbol)
    close, following = session_close(asset_class, at, symbol), next_session_close(asset_class, at, symbol)
    state = market_state(asset_class, at, symbol)
    return {'state': state, 'label': STATE_LABELS[state], 'venue': spec['name'] if spec else None,
            'venue_label': (spec.get('label') or None) if spec else None,
            'basis': spec['basis'] if spec else 'No venue metadata configured',
            'session_close': close.isoformat() if close else None,
            'next_session_close': following.isoformat() if following else None}


# --- daily-close publication calendars (config daily_closes) ---------------------------------------------------------

def close_schedule(provider: str, symbol: str | None = None) -> dict | None:
    """Publication schedule of a daily-close source; '<provider>:<symbol>' overrides '<provider>'."""
    schedules = ruleset()['daily_closes']['schedules']
    return schedules.get(f'{provider}:{symbol}') or schedules.get(provider)


@lru_cache(maxsize=8192)
def _business(calendar: str, day: date, extra: tuple[str, ...]) -> bool:
    if day.isoformat() in extra:
        return False
    if calendar == 'weekdays':
        return day.weekday() < 5
    return bool(_calendar(calendar).is_session(pd.Timestamp(day)))


def _previous_business(schedule: dict, day: date, limit: int = 30) -> date | None:
    """`day` itself when it is a business day, else the latest business day before it."""
    extra = tuple(schedule.get('extra_holidays', []))
    for _ in range(limit):
        if _business(schedule['calendar'], day, extra):
            return day
        day -= timedelta(days=1)
    return None


def _add_business(schedule: dict, day: date, count: int, limit: int = 30) -> date | None:
    extra = tuple(schedule.get('extra_holidays', []))
    for _ in range(limit):
        if count <= 0:
            return day
        day += timedelta(days=1)
        count -= _business(schedule['calendar'], day, extra)
    return None


def expected_close_date(schedule: dict, at: datetime, search_days: int = 30) -> date | None:
    """Observation date of the latest close that should already be published at `at`; None when the calendar cannot
    tell (out of range, unknown calendar). Never guessed."""
    zone = ZoneInfo(schedule['timezone'])
    local, publish = aware(at).astimezone(zone), _clock(schedule['publish'])
    grace = timedelta(hours=schedule.get('grace_hours', 0))
    try:
        weekly = schedule.get('weekly')
        if weekly:
            for back in range(search_days):
                release = local.date() - timedelta(days=back)
                if release.weekday() == weekly['release_weekday'] and \
                        datetime.combine(release, publish, zone) + grace <= local:
                    covers = release - timedelta(days=(weekly['release_weekday'] - weekly['covers_through_weekday']) % 7)
                    return _previous_business(schedule, covers)
            return None
        day = _previous_business(schedule, local.date())
        for _ in range(search_days):
            if day is None:
                return None
            published = _add_business(schedule, day, int(schedule.get('lag_days', 0)))
            if published is not None and datetime.combine(published, publish, zone) + grace <= local:
                return day
            day = _previous_business(schedule, day - timedelta(days=1))
    except CALENDAR_ERRORS:
        return None
    return None


def close_status(provider: str, symbol: str, observed_at: datetime, at: datetime) -> dict:
    """{'status': CURRENT | STALE | None, 'observed': date, 'expected': date}: CURRENT when the close is the latest
    publication the source's calendar and lag lead us to expect (or newer), STALE when it is older, None when the
    source has no configured schedule or its calendar cannot tell."""
    schedule = close_schedule(provider, symbol)
    if schedule is None:
        return {'status': None, 'observed': None, 'expected': None}
    observed = aware(observed_at).astimezone(ZoneInfo(schedule['timezone'])).date()
    expected = expected_close_date(schedule, at)
    status = None if expected is None else 'CURRENT' if observed >= expected else 'STALE'
    return {'status': status, 'observed': observed.isoformat(), 'expected': expected.isoformat() if expected else None}
