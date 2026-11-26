"""Outcome of a scheduled release from the official series the app already stores (EVR-4).

The calendar knows when the US employment report is due; the official-data loop stores the BLS series (payrolls,
unemployment) and nothing joined them. Here a class maps to its series (config event_risk.release_documents.classes
[...].series): when the newest stored period of a series is the release's reference period, the numbers become the
release outcome ('payrolls +29k (prev +133k), unemployment 4.2% (prev 4.1%)') and are attached to the calendar row as
actual / previous / metrics / values_source / actual_ingested_at, stored in macro_releases and handed to the alert and
refresh triggers by the runtime. Every number is read from stored observations; a consensus is never invented, so a
release with an outcome and no consensus records no surprise. actual_ingested_at is when the app first stored that
period (the row's created_at), never a later retrieval: a replay at time T sees only what was known at T."""
import re
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend import macro, sessions
from backend.database import RECORDS, Store
from backend.engines import aware

SeriesLoader = Callable[[str], list[dict]]
SHOWS = ('change', 'level', 'pct_change', 'yoy')
LENT = ('actual', 'previous', 'unit', 'actual_ingested_at', 'release_outcome')  # calendar fields the outcome lends
MONTHS = {m: i for i, m in enumerate(('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                                      'september', 'october', 'november', 'december'), 1)}
MONTH_TEXT = re.compile(r'\b(' + '|'.join(MONTHS) + r')\b\s+(\d{4})', re.IGNORECASE)
DATE_TEXT = re.compile(r'\b(\d{4}-\d{2}-\d{2})\b')


def _config() -> dict:
    return sessions.ruleset()['event_risk']['release_documents']


def class_series(event_class: str | None) -> list[dict]:
    """The configured series of a class, [] for a class without an outcome mapping."""
    return list((_config()['classes'].get(str(event_class or '')) or {}).get('series') or [])


def mapped(row: dict) -> bool:
    """Whether a calendar row's class has an outcome mapping (timed rows only: a date-only row has no release second)."""
    return row.get('time_known') is not False and bool(class_series(row.get('event_class')))


def providers_for(classes: Iterable[str]) -> list[tuple[str, str]]:
    """(provider name, method) pairs that fetch the series of these classes (official_data.release_poll.providers),
    in configuration order, each once."""
    mapping = sessions.ruleset()['official_data']['release_poll']['providers']
    prefixes = {str(spec[key]).split(':', 1)[0] for cls in classes for spec in class_series(cls)
                for key in ('id', 'fallback') if spec.get(key)}
    return [(str(name), str(method)) for prefix, (name, method) in mapping.items() if prefix in prefixes]


def series_rows(store: Store, key: str, limit: int = 16) -> list[dict]:
    """Stored official_series rows of one series ('bls:CES0000000001'), newest period first, each with its id and
    ingested_at (when the app first stored that period)."""
    table: Any = RECORDS['official_series']
    try:
        with store.session() as session:
            rows: list[Any] = list(session.scalars(select(table).where(table.id.like(f'{key}:%'))
                                                   .order_by(table.id.desc()).limit(limit)))
            return [dict(row.payload, id=row.id, ingested_at=aware(row.created_at).isoformat()) for row in rows]
    except (KeyError, SQLAlchemyError):
        return []


def loader_for(store: Store) -> SeriesLoader:
    return lambda key: series_rows(store, key)


# --- reference period ----------------------------------------------------------------------------------------------------

def _previous_friday(day: date) -> date:
    while day.weekday() != 4:
        day -= timedelta(days=1)
    return day


def expected_period(scheduled_at: datetime, row: dict | None = None, rule: str | None = None) -> str:
    """Reference period of a release as a period prefix ('2026-09' for a monthly series, '2026-09-25' for a weekly
    one): the row's own reference_period ('September 2026', 'week ending 2026-09-25'), else the class period rule on the
    scheduled day in series_zone (previous_month, previous_week: the week ending the last Friday at least three days
    before)."""
    text = str((row or {}).get('reference_period') or '')
    found = DATE_TEXT.search(text)
    if found:
        return found[1]
    month = MONTH_TEXT.search(text)
    if month:
        return f'{int(month[2]):04d}-{MONTHS[month[1].lower()]:02d}'
    local = aware(scheduled_at).astimezone(ZoneInfo(_config()['series_zone'])).date()
    if rule == 'previous_week':
        return _previous_friday(local - timedelta(days=3)).isoformat()
    first = local.replace(day=1) - timedelta(days=1)
    return f'{first.year:04d}-{first.month:02d}'


def _is_period(period: Any, expected: str) -> bool:
    return str(period or '')[:len(expected)] == expected


# --- metrics ---------------------------------------------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _level(rows: list[dict], index: int) -> float | None:
    """The stored value `index` periods back, else the previous value the newer row carries (FRED previous_value,
    EIA previous)."""
    if index < len(rows):
        return _num(rows[index].get('value'))
    if index >= 1 and index - 1 < len(rows):
        newer = rows[index - 1]
        return _num(newer.get('previous_value') if newer.get('previous_value') is not None else newer.get('previous'))
    return None


def shown_value(spec: dict, rows: list[dict], index: int = 0) -> float | None:
    """The number a series spec shows for the period `index` back (0 = newest): the level, the change on the period
    before, the percent change on it, or the percent change on `periods` before (yoy). None when not computable."""
    value = _level(rows, index)
    if value is None:
        return None
    show = spec.get('show', 'level')
    if show == 'level':
        return value
    back = int(spec.get('periods') or 1) if show == 'yoy' else 1
    base = _level(rows, index + back)
    if base is None:
        return None
    if show == 'change':
        return value - base
    return 100 * (value / base - 1) if base else None


def format_value(spec: dict, value: float, *, suffix: bool = True) -> str:
    decimals = int(spec.get('decimals', 1))
    signed = spec.get('show', 'level') != 'level'
    number = f'{value:+,.{decimals}f}' if signed else f'{value:,.{decimals}f}'
    tail = (spec.get('suffix') or '') if suffix else ''
    return f"{number}{spec.get('unit') or ''}{tail}"


def metric(spec: dict, rows: list[dict]) -> dict | None:
    """One outcome metric from the newest stored period: {name, series, show, period, value, previous, unit, text,
    provider, ingested_at, headline}; None when its value cannot be computed from what is stored."""
    value = shown_value(spec, rows, 0)
    if value is None or not rows:
        return None
    previous = shown_value(spec, rows, 1)
    text = f"{spec['name']} {format_value(spec, value)}"
    if previous is not None:
        text += f' (prev {format_value(spec, previous, suffix=False)})'
    newest = rows[0]
    return {'name': str(spec['name']), 'series': str(newest.get('id') or spec['id']).rsplit(':', 1)[0], 'show': spec.get('show', 'level'),
            'period': newest.get('period'), 'value': round(value, 4), 'previous': round(previous, 4) if previous is not None else None,
            'unit': spec.get('unit') or None, 'text': text, 'provider': newest.get('provider'),
            'ingested_at': newest.get('ingested_at'), 'headline': bool(spec.get('headline'))}


def _rows(spec: dict, loader: SeriesLoader, expected: str) -> list[dict]:
    """The stored rows of the spec's series (else its fallback) when their newest period is the expected one."""
    for key in (spec.get('id'), spec.get('fallback')):
        if not key:
            continue
        rows = sorted((r for r in loader(str(key)) if r.get('period') is not None), key=lambda r: str(r['period']), reverse=True)
        if rows and _is_period(rows[0]['period'], expected):
            return rows
    return []


def outcome(event_class: str, scheduled_at: datetime, loader: SeriesLoader, row: dict | None = None) -> dict | None:
    """The outcome of a scheduled release of `event_class` from the stored official series: None until the series hold
    the release's reference period. {event_class, period, headline, actual, previous, unit, metrics, text,
    values_source, actual_ingested_at, series}. `text` is what the opening screen shows."""
    specs = class_series(event_class)
    if not specs:
        return None
    rule = str(_config()['classes'][event_class].get('period_rule') or 'previous_month')
    expected = expected_period(scheduled_at, row, rule)
    metrics = [m for spec in specs if (m := metric(spec, _rows(spec, loader, expected)))]
    if not metrics:
        return None
    headline = next((m for m in metrics if m['headline']), metrics[0])
    stamps = [aware(m['ingested_at']) for m in metrics if m.get('ingested_at')]
    return {'event_class': event_class, 'period': headline['period'], 'headline': headline['name'],
            'actual': headline['value'], 'previous': headline['previous'], 'unit': headline['unit'],
            'metrics': [{k: m[k] for k in ('name', 'series', 'show', 'period', 'value', 'previous', 'unit', 'text', 'provider')}
                        for m in metrics],
            'text': ', '.join(m['text'] for m in metrics), 'values_source': headline.get('provider'),
            'actual_ingested_at': max(stamps).isoformat() if stamps else None,
            'series': list(dict.fromkeys(m['series'] for m in metrics))}


# --- attaching to the calendar --------------------------------------------------------------------------------------------

def _due(row: dict, now: datetime) -> datetime | None:
    """The scheduled time of a mapped row whose outcome may be attached now (attach_before_minutes before it until
    attach_window_hours after), else None."""
    if not mapped(row) or not row.get('scheduled_at'):
        return None
    config = _config()
    try:
        scheduled = aware(row['scheduled_at'])
    except (TypeError, ValueError):
        return None
    before, after = timedelta(minutes=config['attach_before_minutes']), timedelta(hours=config['attach_window_hours'])
    return scheduled if scheduled - before <= aware(now) <= scheduled + after else None


def attached_row(row: dict, found: dict) -> dict:
    """The calendar row with the outcome attached: the headline's actual / previous / unit, every metric, the series
    provider as values_source, the first-stored time as actual_ingested_at and the outcome block. The fields are lent
    (lent_fields), so a calendar re-fetch keeps them (providers.calendars.carry_lent) and never alerts them again."""
    metrics = [{'name': m['name'], 'provider': m.get('provider'), 'row_id': None, 'actual': m['value'],
                'previous': m['previous'], 'unit': m['unit'], 'series': m['series'], 'text': m['text']}
               for m in found['metrics']]
    lent = sorted(set(row.get('lent_fields') or []) | set(LENT))
    return dict(row, actual=found['actual'], previous=found['previous'], unit=found['unit'], metrics=metrics,
                headline_metric=found['headline'], metric=found['headline'], values_source=found['values_source'],
                actual_ingested_at=found['actual_ingested_at'], release_outcome=found, lent_fields=lent)


def attach(store: Store, rows: Iterable[dict], now: datetime, loader: SeriesLoader | None = None) -> list[dict]:
    """Attaches the outcome to every mapped calendar row due now whose series hold the release (idempotent: a row that
    already carries the outcome of that period is left alone; a vendor actual is never replaced). Each attached row is
    stored (macro_calendar) and recorded in macro_releases with the first-stored time of the numbers. Returns the rows
    attached by this call, for the MACRO_RELEASE alert and the refresh trigger."""
    loader = loader or loader_for(store)
    now = aware(now)
    attached: list[dict] = []
    for row in rows:
        scheduled = _due(row, now)
        if scheduled is None:
            continue
        held = row.get('release_outcome') if isinstance(row.get('release_outcome'), dict) else None
        if row.get('actual') is not None and held is None:
            continue  # a vendor actual (with its consensus) stands; the official series are not needed
        found = outcome(str(row.get('event_class')), scheduled, loader, row)
        if found is None or (held is not None and held.get('period') == found['period']):
            continue
        if not found.get('actual_ingested_at'):
            found['actual_ingested_at'] = now.isoformat()
        updated = attached_row(row, found)
        store.put('macro_calendar', str(updated['id']), updated)
        macro.record_release(store, updated, now, known_at=found['actual_ingested_at'])
        attached.append(updated)
    return attached


def event_outcome(row: dict) -> dict | None:
    """The outcome block an EventObject carries for a calendar row with an attached outcome: {text, period, headline,
    actual, previous, unit, metrics: [{name, value, previous, unit, text}], source, ingested_at}."""
    found = row.get('release_outcome')
    if not isinstance(found, dict) or not found.get('text'):
        return None
    return {'text': str(found['text']), 'period': found.get('period'), 'headline': found.get('headline'),
            'actual': found.get('actual'), 'previous': found.get('previous'), 'unit': found.get('unit'),
            'metrics': [{k: m.get(k) for k in ('name', 'value', 'previous', 'unit', 'text')} for m in found.get('metrics') or []
                        if isinstance(m, dict)],
            'source': found.get('values_source'), 'ingested_at': found.get('actual_ingested_at')}


def latest_outcomes(store: Store, limit: int = 20) -> list[dict]:
    """Stored macro_releases rows that carry an official outcome, newest scheduled first (the /api/releases view)."""
    rows = [r for r in store.list('macro_releases', 5000) if isinstance(r.get('outcome'), dict)]
    rows.sort(key=lambda r: str(r.get('scheduled_at') or ''), reverse=True)
    return rows[:limit]

