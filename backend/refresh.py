"""Refresh cadences (spec section 10) as configuration, event/central-bank watch modes and immediate
recalculation triggers. Pure functions of (now, rows); only RecalcRequests holds state.

Date-only calendar rows (time_known false) and suspended/cancelled rows never open a watch window. The event-aware
triggers (docs/EVENT_AWARENESS.md §5) compare event_risk.trigger_state snapshots (phases, 60/15-minute windows, critical
ids, regime posture), published decision scores and market sessions; press conferences are detected from their
calendar rows. The market interpretation has its own cadence and trigger list (refresh.interpretation)."""
import asyncio
import contextlib
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from backend.config import rules_view
from backend.engines import aware

TRIGGERS = ('BREAKING_EVENT', 'MACRO_RELEASE', 'CENTRAL_BANK_STATEMENT', 'LARGE_MOVE', 'TECHNICAL_BREAKOUT',
            'CROSS_ASSET_DIVERGENCE', 'FACT_STATUS_CHANGE', 'EVENT_CRITICAL_NEW', 'EVENT_WINDOW_60', 'EVENT_WINDOW_15',
            'EVENT_RELEASED', 'PRESS_CONFERENCE_START', 'REGIME_CHANGE', 'LARGE_SCORE_CHANGE', 'MARKET_OPEN_CLOSE')
BREAKOUT_STATES = {'ABOVE_RESISTANCE', 'BELOW_SUPPORT'}
PRE_PHASES = {'SCHEDULED', 'APPROACHING', 'IMMINENT'}
RELEASED_PHASES = {'RELEASED', 'IN_PROGRESS', 'POST_EVENT'}


def _config() -> dict:
    return rules_view()['refresh']  # shared read-only view: read on every trigger check


def cadence(kind: str) -> dict:
    return _config()['cadences'][kind]


def cadence_table() -> list[dict]:
    """Every default refresh rule, for display and adjustment (config/rules.d/refresh.json)."""
    return [dict(spec, kind=kind) for kind, spec in _config()['cadences'].items()]


# --- watch modes ----------------------------------------------------------------------------

def _scheduled(row: dict) -> datetime | None:
    value = row.get('scheduled_at') or row.get('scheduled_utc')
    try:
        return aware(value) if value else None
    except ValueError:
        return None


def is_central_bank(row: dict) -> bool:
    name = str(row.get('name') or '').lower()
    return any(pattern in name for pattern in _config()['watch']['central_bank_patterns'])


def _timed(row: dict, inactive: set[str]) -> bool:
    """Exact clock time known and still scheduled (a date-only row stamped at midnight is not a release time)."""
    return row.get('time_known') is not False and str(row.get('status') or 'SCHEDULED').upper() not in inactive


def _major(rows: Iterable[dict], minimum: float) -> list[tuple[datetime, dict]]:
    inactive = set(_config()['watch']['inactive_statuses'])
    found = [(_scheduled(r), r) for r in rows if (r.get('impact') or 0) >= minimum and _timed(r, inactive)]
    return sorted(((t, r) for t, r in found if t is not None), key=lambda item: item[0])


def watch_state(now: datetime, calendar_rows: Iterable[dict]) -> dict:
    """RELEASE_WATCH from lead minutes before a major release until trail minutes after it (or its actual);
    the central-bank window is +-N minutes around a major decision."""
    config, now = _config()['watch'], aware(now)
    rows = list(calendar_rows)
    lead, trail = timedelta(minutes=config['lead_minutes']), timedelta(minutes=config['trail_minutes'])
    events = []
    for at, row in _major(rows, config['min_impact']):
        if at - lead <= now < at:
            phase = 'PRE_RELEASE'
        elif at <= now <= at + trail:
            phase = 'RELEASED' if row.get('actual') is not None else 'AWAITING_ACTUAL'
        else:
            continue
        events.append({'id': row.get('id'), 'name': row.get('name'), 'scheduled_at': at.isoformat(), 'phase': phase,
                       'seconds_to_release': round((at - now).total_seconds(), 1)})
    window = timedelta(minutes=config['central_bank_window_minutes'])
    banks = [(t, r) for t, r in _major(rows, config['central_bank_min_impact']) if is_central_bank(r)]
    upcoming = [(t, r) for t, r in _major(rows, config['min_impact']) if t > now]
    next_watch = [t - lead for t, _ in upcoming if t - lead > now]
    return {'mode': 'RELEASE_WATCH' if events else 'NORMAL', 'active': bool(events), 'events': events,
            'central_bank_window': any(abs((t - now).total_seconds()) <= window.total_seconds() for t, _ in banks),
            'next_event': {'id': upcoming[0][1].get('id'), 'name': upcoming[0][1].get('name'),
                           'scheduled_at': upcoming[0][0].isoformat()} if upcoming else None,
            'next_watch_at': min(next_watch).isoformat() if next_watch else None}


def us_market_open(now: datetime) -> bool:
    try:
        from backend.sessions import market_state
        return market_state('equity', now) == 'LIVE'
    except ImportError:  # sessions module optional during migration; engines keeps the XNYS check
        from backend.engines import market_status
        return market_status('equity', now) == 'LIVE'


def congress_active(events: Iterable[dict], now: datetime) -> bool:
    """An important regulation/legislation event updated within the active horizon keeps the 5-minute cadence."""
    config, now = _config()['congress'], aware(now)
    horizon = timedelta(hours=config['active_hours'])
    for event in events:
        importance = event.get('importance')
        stamp = event.get('last_updated_utc') or event.get('first_seen_utc')
        if event.get('event_type') in config['active_event_types'] and isinstance(importance, (int, float)) \
                and importance >= config['active_min_importance'] and stamp and now - aware(stamp) <= horizon:
            return True
    return False


def next_interval(kind: str, now: datetime, calendar_rows: Iterable[dict] = (), *, us_open: bool | None = None,
                  congress_is_active: bool = False, markets_active: bool = True) -> float:
    """Seconds until the next poll of `kind`. Release watch returns the exact time to a release so the
    authoritative source is polled at the scheduled second."""
    spec, now = cadence(kind), aware(now)
    rows = list(calendar_rows)
    if kind == 'central_banks':
        state = watch_state(now, rows)
        if state['central_bank_window']:
            return float(spec['watch_seconds'])
        window = timedelta(minutes=_config()['watch']['central_bank_window_minutes'])
        starts = [(t - window - now).total_seconds() for t, r in _major(rows, _config()['watch']['central_bank_min_impact'])
                  if is_central_bank(r) and t - window > now]
        return float(min([spec['seconds'], *starts]))
    if kind in {'calendar', 'release_watch'}:
        state = watch_state(now, rows)
        idle = cadence('calendar')['seconds']
        if state['next_watch_at']:
            idle = min(idle, (aware(state['next_watch_at']) - now).total_seconds())
        if kind == 'calendar' or not state['active']:
            return float(max(0.0, idle))
        pending = [e['seconds_to_release'] for e in state['events'] if e['phase'] == 'PRE_RELEASE']
        return float(max(0.0, min([spec['seconds'], *pending])))
    if kind == 'sec_filings':
        return float(spec['seconds'] if (us_market_open(now) if us_open is None else us_open) else spec['off_hours_seconds'])
    if kind == 'congress':
        return float(spec['active_seconds'] if congress_is_active else spec['seconds'])
    if kind == 'interpretation':
        return float(spec['seconds'] if markets_active else spec['inactive_seconds'])
    return float(spec['seconds'])


def due(kind: str, now: datetime, last_run: datetime | str | None, calendar_rows: Iterable[dict] = (), **options) -> bool:
    """Elapsed-time check; release watch is also due once a major release time passed since the last run, so a
    scheduler ticking at a fixed rate still polls the authoritative source right at the release."""
    if last_run is None:
        return True
    now, last, rows = aware(now), aware(last_run), list(calendar_rows)
    if kind == 'release_watch' and any(last < at <= now for at, _ in _major(rows, _config()['watch']['min_impact'])):
        return True
    return (now - last).total_seconds() >= next_interval(kind, now, rows, **options)


# --- market interpretation cadence ---------------------------------------------------------------

def interpretation_config() -> dict:
    return _config()['interpretation']


def divergence_config() -> dict:
    """Post-event MARKET DIVERGENCE research (docs/EVENT_AWARENESS.md §3): search-budget priority, retry interval while
    the planner or the budget is unavailable, bookkeeping bound and summary length."""
    return _config()['divergence_research']


def market_sessions(now: datetime, classes: Iterable[str] | None = None) -> dict[str, bool | None]:
    """Open (True), closed (False) or unknown (None) per session class (backend.sessions), e.g. equity, bond, fx."""
    from backend.sessions import market_state
    result: dict[str, bool | None] = {}
    for kind in classes if classes is not None else interpretation_config()['session_classes']:
        state = market_state(kind, now)
        result[kind] = None if state == 'UNKNOWN' else state == 'LIVE'
    return result


def traditional_markets_open(now: datetime) -> bool:
    return any(market_sessions(now, interpretation_config()['traditional_classes']).values())


def interpretation_interval(now: datetime, markets_open: bool | None = None) -> float:
    """active_seconds while any traditional market (equities, rates, FX, commodities) is open, else idle_seconds;
    crypto alone keeps the idle cadence (its moves still arrive as immediate triggers)."""
    config = interpretation_config()
    is_open = traditional_markets_open(now) if markets_open is None else markets_open
    return float(config['active_seconds'] if is_open else config['idle_seconds'])


# --- immediate recalculation triggers --------------------------------------------------------

def _importance(event: dict) -> float | None:
    if isinstance(event.get('importance'), (int, float)):
        return float(event['importance'])
    extracted = (event.get('extraction') or {}).get('potential_importance')
    return float(extracted) / 100 if isinstance(extracted, (int, float)) else None


def _event_state(state: dict | None) -> dict | None:
    """The comparable part of event_risk.trigger_state(); anything unusable is treated as absent."""
    if not isinstance(state, dict):
        return None
    return {'phases': dict(state.get('phases') or {}), 'windows': dict(state.get('windows') or {}),
            'critical_ids': sorted(str(i) for i in state.get('critical_ids') or []), 'risk_mode': bool(state.get('risk_mode')),
            'dominant_event_id': state.get('dominant_event_id'), 'posture': state.get('posture'),
            'divergences': sorted(str(i) for i in state.get('divergences') or [])}


def trigger_state(events: Iterable[dict], calendar_rows: Iterable[dict], moves: dict[str, dict] | None = None,
                  levels: dict[str, str] | None = None, divergences: Iterable[str] = (), *, now: datetime | None = None,
                  event_state: dict | None = None, scores: dict[str, float] | None = None,
                  sessions: dict[str, bool | None] | None = None) -> dict:
    """Comparable snapshot: event statuses, released calendar ids, windowed moves, level states, divergences, plus
    (event-aware) the observation time, timed calendar rows (for crossings), the event_risk trigger state, published
    decision scores and market sessions."""
    rows = list(calendar_rows)
    minimum = _config()['watch']['min_impact']
    inactive = set(_config()['watch']['inactive_statuses'])
    scheduled = []
    for row in rows:
        at = _scheduled(row)
        if row.get('id') and at is not None and _timed(row, inactive) and (row.get('impact') or 0) >= minimum:
            scheduled.append({'id': str(row['id']), 'at': at.isoformat(), 'sub_event': row.get('sub_event'),
                              'name': row.get('name')})
    return {'events': {e['id']: {'status': e.get('verification_status'), 'type': e.get('event_type'),
                                 'importance': _importance(e), 'sources': list(e.get('sources') or [])} for e in events},
            'released': sorted(str(r['id']) for r in rows
                               if r.get('id') and r.get('actual') is not None and (r.get('impact') or 0) >= minimum),
            'moves': {s: {'change': m['change'], 'unit': m['unit']} for s, m in (moves or {}).items()},
            'levels': dict(levels or {}), 'divergences': sorted(set(divergences)),
            'at': aware(now).isoformat() if now is not None else None, 'scheduled': scheduled,
            'event_risk': _event_state(event_state), 'scores': dict(scores or {}), 'sessions': dict(sessions or {})}


def observe_state(store: Any, assets: list[dict], events: list[dict], calendar_rows: list[dict], now: datetime,
                  divergences: Iterable[str] = (), event_state: dict | None = None) -> dict:
    """trigger_state from live data: windowed moves from stored ticks, level states and scores from published
    decisions, market sessions now, and the event_risk trigger state computed by the caller."""
    from backend.briefs import level_state
    from backend.notifications import window_changes
    moves = window_changes(store, [a['symbol'] for a in assets], _config()['triggers']['move_window_seconds'], now)
    scores = {a['symbol']: float(a['decision']['score']) for a in assets
              if isinstance((a.get('decision') or {}).get('score'), (int, float))}
    return trigger_state(events, calendar_rows, moves, {a['symbol']: level_state(a) for a in assets}, divergences,
                         now=now, event_state=event_state, scores=scores, sessions=market_sessions(now))


def _large(move: dict | None) -> bool:
    if not move:
        return False
    return abs(move['change']) >= _config()['triggers']['large_move'].get(move['unit'], float('inf'))


def detect_triggers(previous: dict | None, current: dict) -> list[dict]:
    """Reasons to recalculate now instead of waiting for the regular cadence. The first state is a baseline."""
    if previous is None:
        return []
    config = _config()['triggers']
    found: list[dict] = []
    before = previous.get('events', {})
    for event_id, event in current['events'].items():
        old = before.get(event_id)
        if event['status'] == 'STALE' and (old is None or old.get('status') != 'STALE'):
            continue  # a backfilled old release, or a fact ageing out, is not news
        if old is None:
            bank = event['type'] in config['central_bank_event_types'] or \
                bool(set(event['sources']) & set(config['central_bank_sources']))
            breaking = event['type'] in config['breaking_event_types'] or \
                (event['importance'] is not None and event['importance'] >= config['breaking_min_importance'])
            if bank:
                found.append({'trigger': 'CENTRAL_BANK_STATEMENT', 'id': event_id})
            elif breaking:
                found.append({'trigger': 'BREAKING_EVENT', 'id': event_id})
        elif old.get('status') != event['status']:
            found.append({'trigger': 'FACT_STATUS_CHANGE', 'id': event_id, 'from': old.get('status'), 'to': event['status']})
    for release in sorted(set(current['released']) - set(previous.get('released', []))):
        found.append({'trigger': 'MACRO_RELEASE', 'id': release})
    for symbol, move in current['moves'].items():
        if _large(move) and not _large(previous.get('moves', {}).get(symbol)):
            found.append({'trigger': 'LARGE_MOVE', 'id': symbol, 'change': move['change'], 'unit': move['unit']})
    for symbol, state in current['levels'].items():
        if state in BREAKOUT_STATES and previous.get('levels', {}).get(symbol) != state:
            found.append({'trigger': 'TECHNICAL_BREAKOUT', 'id': symbol, 'state': state})
    for divergence in sorted(set(current['divergences']) - set(previous.get('divergences', []))):
        found.append({'trigger': 'CROSS_ASSET_DIVERGENCE', 'id': divergence})
    return found + _event_triggers(previous, current)


def _event_triggers(previous: dict, current: dict) -> list[dict]:
    """Event-aware triggers (edge-triggered: an unchanged state never re-fires)."""
    config = _config()['triggers']
    found: list[dict] = []
    before, after = previous.get('event_risk'), current.get('event_risk')
    # Without an event_risk state, the scheduled times of timed calendar rows crossed since the previous observation
    # are the release / press-conference triggers; with one, its phases are the authority (no double firing).
    if after is None and previous.get('at') and current.get('at'):
        start, end = aware(previous['at']), aware(current['at'])
        for row in current.get('scheduled', []):
            if start < aware(row['at']) <= end:
                kind = 'PRESS_CONFERENCE_START' if row.get('sub_event') == 'PRESS_CONFERENCE' else 'EVENT_RELEASED'
                found.append({'trigger': kind, 'id': row['id'], 'name': row.get('name')})
    if before is not None and after is not None:
        for event_id in sorted(set(after['critical_ids']) - set(before['critical_ids'])):
            found.append({'trigger': 'EVENT_CRITICAL_NEW', 'id': event_id})
        for event_id, window in sorted(after['windows'].items()):
            old = before['windows'].get(event_id)
            if window == 'W15' and old != 'W15':
                found.append({'trigger': 'EVENT_WINDOW_15', 'id': event_id})
            elif window == 'W60' and old not in {'W60', 'W15'}:
                found.append({'trigger': 'EVENT_WINDOW_60', 'id': event_id})
        # Sub-event phases are keyed '<event_id>:<SUB_EVENT>'; a press conference starting is its own trigger, other
        # sub-events are covered by their parent's release.
        for event_id, phase in sorted(after['phases'].items()):
            if phase not in RELEASED_PHASES or before['phases'].get(event_id) not in PRE_PHASES:
                continue
            parent, _, sub_event = event_id.partition(':')
            if sub_event == 'PRESS_CONFERENCE':
                found.append({'trigger': 'PRESS_CONFERENCE_START', 'id': event_id, 'event_id': parent})
            elif not sub_event:
                found.append({'trigger': 'EVENT_RELEASED', 'id': event_id, 'phase': phase})
        for event_id in sorted(set(after['divergences']) - set(before['divergences'])):
            found.append({'trigger': 'CROSS_ASSET_DIVERGENCE', 'id': f'MARKET_DIVERGENCE:{event_id}'})
        if before['posture'] != after['posture']:
            found.append({'trigger': 'REGIME_CHANGE', 'id': str(after['posture']), 'from': before['posture'],
                          'to': after['posture']})
    old_scores = previous.get('scores') or {}
    for symbol, score in sorted((current.get('scores') or {}).items()):
        old = old_scores.get(symbol)
        if isinstance(old, (int, float)) and abs(score - old) >= config['large_score_change']:
            found.append({'trigger': 'LARGE_SCORE_CHANGE', 'id': symbol, 'from': old, 'to': score})
    old_sessions = previous.get('sessions') or {}
    for kind, is_open in sorted((current.get('sessions') or {}).items()):
        old_open = old_sessions.get(kind)
        if is_open is not None and old_open is not None and is_open != old_open:
            found.append({'trigger': 'MARKET_OPEN_CLOSE', 'id': kind, 'state': 'OPEN' if is_open else 'CLOSED'})
    return found


class RecalcRequests:
    """Debounced immediate-recalculation requests. The analysis loop awaits next(regular interval); hooks call
    request(triggers). Runs never come closer together than min_interval (triggers.min_interval_seconds by default).
    `accepted` lists the trigger names this consumer reacts to (triggers.analysis_triggers by default)."""

    def __init__(self, min_interval: float | None = None, clock: Callable[[], float] = time.monotonic,
                 accepted: Iterable[str] | None = None):
        self.min_interval = _config()['triggers']['min_interval_seconds'] if min_interval is None else min_interval
        names = _config()['triggers']['analysis_triggers'] if accepted is None else accepted
        self.accepted = {name for name in names if name in TRIGGERS}
        self.clock = clock
        self.pending: list[dict] = []
        self.event = asyncio.Event()
        self.last_run: float | None = None

    def request(self, triggers: Iterable[dict]) -> None:
        added = [t for t in triggers if t.get('trigger') in self.accepted]
        if added:
            self.pending += added
            self.event.set()

    async def next(self, timeout: float) -> list[dict]:
        """Returns the triggers that caused this run ([] for the regular cadence)."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.event.wait(), timeout)
        if self.last_run is not None:
            wait = self.min_interval - (self.clock() - self.last_run)
            if wait > 0:
                await asyncio.sleep(wait)
        reasons, self.pending = self.pending, []
        self.event.clear()
        self.last_run = self.clock()
        return reasons
