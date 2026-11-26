"""Daily AI request budget (config/rules.d/ai_runtime.json `budget`, docs/AI_AGENTS.md).

The Ollama cloud subscription has a usage quota that must last the whole day, while the loops that ask the AI
(research planning, news extraction, the market interpretation, specialists) together sent 700-1000 requests a day.
The LLM limiter (backend/llm_limiter.py) only smooths bursts; this budget caps the volume. A chat request is sent only
when, for the current Europe/Bucharest day,

1. the role's count is below its role_daily cap (0 means the role never calls the AI),
2. the day's total is below daily_requests, and the roles outside reserve.roles together stay below
   (1 - reserve.share) of it, so the interpretation, the chief analyst and the auditor always have room, and
3. the requests of the last spread.burst_hours hours stay below daily_requests x spread.hourly_share_of_daily x
   burst_hours (the non-reserve roles below (1 - reserve.share) of that), so a morning burst cannot eat the day, and
4. with spread.role_pace, the role has used at most ceil(cap x (hours since midnight + burst_hours) / hours of the
   day) today, so each role's cap is spread over the day too (without it the caps ran out by mid-afternoon).

Counts come from the stored ai_runs rows (their `requests` field: HTTP chat requests that reached the provider), so a
restart never resets them; requests admitted but not stored yet are counted in memory. Rows written before the budget
existed have no `requests` field: they appear in the usage history only and never count against a day's budget (the
upgrade day already held 1432 of them, which would have refused every role until midnight). A refused call sends nothing:
AIService stores it in ai_runs with success false, `budget_refusal` ('daily' | 'role' | 'pace') and a plain error, and
the caller's own fallback runs. Refusals are logged once per role per hour, and stored as at most one ai_runs row per
role per hour: the first refusal writes the row, every further one within REFUSAL_ROW_SECONDS (and the same budget
day) only raises its `suppressed` count (141-254 refusal rows a day were stored before, one per refused attempt). The
usage record counts a row's refusals as 1 + suppressed.
"""
import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from backend.config import rules_view
from backend.database import RECORDS, Store, as_utc, utcnow

logger = logging.getLogger(__name__)

DEFAULT_ZONE = 'Europe/Bucharest'
REFUSAL_MESSAGES = {'daily': 'AI daily budget reached', 'role': 'AI daily budget reached', 'pace': 'AI hourly pace reached'}
REFUSAL_LOG_SECONDS = 3600  # one log line per role per hour, never one per refused call
REFUSAL_ROW_SECONDS = 3600  # one stored ai_runs refusal row per role per hour; later refusals are counted in it
HISTORY_DAYS = 7
# Plain names for the Settings usage table (the same words as the frontend's fallback labels).
ROLE_LABELS = {
    'market_interpreter': 'Market interpretation', 'chief_analyst': 'Chief analyst', 'auditor': 'Auditor',
    'macro': 'Macro specialist', 'fx': 'FX specialist', 'crypto': 'Crypto specialist',
    'equity_credit': 'Equity and credit specialist', 'geopolitical_energy': 'Geopolitics and energy specialist',
    'fact_checker': 'Fact checker', 'research_planner': 'Research planner', 'extractor': 'News extraction',
}


def budget_settings() -> Mapping[str, Any]:
    return rules_view().get('budget') or {}


def zone(config: Mapping[str, Any]) -> ZoneInfo:
    """The day's time zone: the first word of `reset` ('Europe/Bucharest midnight'), Europe/Bucharest otherwise."""
    text = str(config.get('reset') or '').strip()
    name = text.split()[0] if text else DEFAULT_ZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_ZONE)


def day_start(day: date, tz: ZoneInfo) -> datetime:
    """Local midnight of `day` in UTC (DST-aware)."""
    return as_utc(datetime.combine(day, clock_time(0), tzinfo=tz))


def budget_day(now: datetime | None = None) -> str:
    """The current budget day ('YYYY-MM-DD' in the reset time zone)."""
    return as_utc(now or utcnow()).astimezone(zone(budget_settings())).date().isoformat()


def whole(value: Any, default: int = 0) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def number(value: Any, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


@dataclass(frozen=True)
class Limits:
    daily: int
    caps: dict[str, int]
    reserve: frozenset[str]
    share: float  # reserve share of the day and of the pace window
    window_hours: float
    pace: int  # requests allowed in the last window_hours
    role_pace: bool = False  # spread.role_pace: each role's cap is spread over the day as well

    @property
    def shared_daily(self) -> int:
        """Requests the non-reserve roles may use together today."""
        return math.floor(self.daily * (1 - self.share))

    @property
    def shared_pace(self) -> int:
        return math.floor(self.pace * (1 - self.share))


def limits(config: Mapping[str, Any]) -> Limits:
    daily = max(0, whole(config.get('daily_requests')))
    spread = config.get('spread') or {}
    reserve = config.get('reserve') or {}
    window = max(1.0, number(spread.get('burst_hours'), 2))
    raw = daily * number(spread.get('hourly_share_of_daily'), 1 / 24) * window
    pace = max(1, math.floor(raw + 1e-9)) if daily else 0  # never below one request while the day allows any
    caps = {str(role): max(0, whole(cap)) for role, cap in (config.get('role_daily') or {}).items()
            if isinstance(cap, (int, float)) and not isinstance(cap, bool)}
    share = min(max(number(reserve.get('share'), 0), 0), 0.9)
    return Limits(daily=daily, caps=caps, reserve=frozenset(str(r) for r in reserve.get('roles') or []), share=share,
                  window_hours=window, pace=pace, role_pace=spread.get('role_pace') is True)


def role_allowance(cap: int, window_hours: float, elapsed_hours: float, day_hours: float = 24.0) -> int:
    """Requests a role may have used so far today under spread.role_pace: its cap in proportion to the hours since
    midnight plus one pace window (so the first requests of the day are never refused), rounded up, at most the cap."""
    if cap <= 0:
        return 0
    share = min(1.0, max(0.0, elapsed_hours + window_hours) / max(day_hours, 1.0))
    return min(cap, math.ceil(cap * share - 1e-9))


def sent(row: Mapping[str, Any]) -> int:
    """Chat requests of one stored ai_runs row that count against the budget: its `requests` field, written by the
    budget-aware code. A row written before the budget has none and counts 0 here (see recorded())."""
    requests = row.get('requests')
    if isinstance(requests, int) and not isinstance(requests, bool):
        return max(0, requests)
    return 0


def recorded(row: Mapping[str, Any]) -> int:
    """Requests of a row for the usage history: sent(), or one request for a row written before the budget (unless it
    was refused or never got a limiter slot). Only the history shows these older rows."""
    requests = row.get('requests')
    if isinstance(requests, int) and not isinstance(requests, bool):
        return max(0, requests)
    return 0 if row.get('budget_refusal') or row.get('queue_wait_exceeded') else 1


def refusals(row: Mapping[str, Any]) -> int:
    """Refused attempts one stored row stands for: 0 for a row the budget did not refuse, else 1 plus the attempts
    counted in it instead of being stored (`suppressed`)."""
    if not row.get('budget_refusal'):
        return 0
    return 1 + max(0, whole(row.get('suppressed')))


def row_tokens(row: Mapping[str, Any]) -> int | None:
    """Prompt + completion tokens the provider reported for the row (both stored), else None."""
    prompt, completion = row.get('prompt_tokens'), row.get('completion_tokens')
    if isinstance(prompt, int) and isinstance(completion, int):
        return prompt + completion
    return None


def _sum_tokens(rows: Iterable[Mapping[str, Any]]) -> int | None:
    values = [t for t in (row_tokens(r) for r in rows) if t is not None]
    return sum(values) if values else None


class AIBudget:
    def __init__(self, store: Store, settings: Callable[[], Mapping[str, Any]] = budget_settings,
                 clock: Callable[[], datetime] = utcnow):
        self.store, self.settings, self.clock = store, settings, clock
        self.pending: dict[str, int] = {}  # role -> requests admitted whose ai_runs row is not stored yet
        self.logged: dict[str, float] = {}  # role -> time.monotonic() of its last refusal log line
        self.refused: dict[str, tuple[str, datetime]] = {}  # role -> (ai_runs id, time) of its current refusal row

    # --- counts --------------------------------------------------------------------------------------------------

    def rows(self, since: datetime) -> list[dict]:
        """The counting fields of the ai_runs rows stored since `since` (JSON fields only: a row's raw output can be
        40 kB and is never read here)."""
        cls = RECORDS['ai_runs']
        payload = cls.payload
        query = select(cls.created_at, payload['agent'].as_string(), payload['requests'].as_integer(),
                       payload['budget_refusal'].as_string(), payload['queue_wait_exceeded'].as_boolean(),
                       payload['success'].as_boolean(), payload['prompt_tokens'].as_integer(),
                       payload['completion_tokens'].as_integer(),
                       payload['http_status'].as_integer(),
                       payload['suppressed'].as_integer()).where(cls.created_at >= as_utc(since))
        with self.store.session() as session:
            result = session.execute(query).all()
        keys = ('at', 'agent', 'requests', 'budget_refusal', 'queue_wait_exceeded', 'success', 'prompt_tokens',
                'completion_tokens', 'http_status', 'suppressed')
        rows = []
        for values in result:
            row = {key: value for key, value in zip(keys, values, strict=True) if value is not None}
            row['at'] = as_utc(values[0])
            rows.append(row)
        return rows

    def refusal(self, role: str, now: datetime | None = None) -> str | None:
        """'role' | 'daily' | 'pace' when a chat request of `role` may not be sent now, else None. No budget section
        in the configuration means no limit."""
        config = self.settings()
        if not config:
            return None
        bounds = limits(config)
        cap = bounds.caps.get(role)
        if cap == 0 or bounds.daily == 0:
            return 'role' if cap == 0 else 'daily'
        now = as_utc(now or self.clock())
        tz = zone(config)
        today_date = now.astimezone(tz).date()
        start = day_start(today_date, tz)
        window_start = now - timedelta(hours=bounds.window_hours)
        rows = self.rows(min(start, window_start))
        reserved = role in bounds.reserve

        def used(items: list[dict], roles: Callable[[str], bool]) -> int:
            """Stored requests of the matching roles plus those admitted and not stored yet."""
            return (sum(sent(r) for r in items if roles(str(r.get('agent') or '')))
                    + sum(count for name, count in self.pending.items() if roles(name)))

        def anyone(name: str) -> bool:
            return True

        def shared(name: str) -> bool:
            return name not in bounds.reserve

        today = [r for r in rows if r['at'] >= start]
        recent = [r for r in rows if r['at'] >= window_start]
        mine = used(today, lambda name: name == role)
        if cap is not None and mine >= cap:
            return 'role'
        if used(today, anyone) >= bounds.daily or (not reserved and used(today, shared) >= bounds.shared_daily):
            return 'daily'
        if cap is not None and bounds.role_pace:
            day_hours = (day_start(today_date + timedelta(days=1), tz) - start).total_seconds() / 3600
            elapsed = (now - start).total_seconds() / 3600
            if mine >= role_allowance(cap, bounds.window_hours, elapsed, day_hours):
                return 'pace'
        if used(recent, anyone) >= bounds.pace or (not reserved and used(recent, shared) >= bounds.shared_pace):
            return 'pace'
        return None

    def role_used(self, role: str, now: datetime | None = None) -> tuple[int, int | None]:
        """(requests the role used today, its role_daily cap or None). The extraction gate reads it to become stricter
        once part of the extractor's cap is used."""
        config = self.settings()
        cap = limits(config).caps.get(role) if config else None
        now = as_utc(now or self.clock())
        tz = zone(config)
        cls = RECORDS['ai_runs']
        query = select(cls.payload['requests'].as_integer()).where(
            cls.created_at >= day_start(now.astimezone(tz).date(), tz), cls.payload['agent'].as_string() == role)
        with self.store.session() as session:
            values: list[Any] = list(session.scalars(query))
        stored = sum(max(0, value) for value in values if isinstance(value, int) and not isinstance(value, bool))
        return stored + self.pending.get(role, 0), cap

    def disabled(self, role: str) -> bool:
        """The configuration switches the role off (its cap or the daily budget is 0): it never calls the AI."""
        config = self.settings()
        if not config:
            return False
        bounds = limits(config)
        return bounds.daily == 0 or bounds.caps.get(role) == 0

    # --- admission -----------------------------------------------------------------------------------------------

    def admit(self, role: str) -> str | None:
        """Reserves one request for `role` (None) or returns the refusal kind; a refusal is logged once per role per
        hour. Every admitted request is given back with settle() once its ai_runs row is stored (or the call ended
        without one)."""
        kind = self.refusal(role)
        if kind is None:
            self.pending[role] = self.pending.get(role, 0) + 1
            return None
        clock = time.monotonic()
        if clock - self.logged.get(role, -1e18) >= REFUSAL_LOG_SECONDS:
            self.logged[role] = clock
            logger.warning('ai_budget_refused', extra={'role': role, 'reason': kind, 'error': REFUSAL_MESSAGES[kind]})
        return kind

    def _refusal_row(self, role: str, now: datetime) -> tuple[str, datetime] | None:
        """(id, time) of the role's refusal row of the last REFUSAL_ROW_SECONDS and of the current budget day: the one
        remembered in memory, else the newest stored one (so a restart keeps counting in the same row). Only a row that
        sent nothing qualifies."""
        known = self.refused.get(role)
        if known is None:
            cls = RECORDS['ai_runs']
            query = (select(cls.id, cls.payload['refused_at'].as_string())
                     .where(cls.payload['agent'].as_string() == role, cls.payload['budget_refusal'].as_string().is_not(None),
                            cls.payload['requests'].as_integer() == 0)
                     .order_by(cls.created_at.desc()).limit(1))
            with self.store.session() as session:
                found = session.execute(query).first()
            if found is not None and found[1]:
                try:
                    known = (str(found[0]), as_utc(datetime.fromisoformat(found[1])))
                except ValueError:
                    known = None
        if known is None or not 0 <= (now - known[1]).total_seconds() < REFUSAL_ROW_SECONDS:
            return None
        tz = zone(self.settings())
        if known[1].astimezone(tz).date() != now.astimezone(tz).date():
            return None  # a new budget day starts its own row, so each day's usage counts its own refusals
        return known

    def record_refusal(self, role: str, kind: str, row: Mapping[str, Any], run_id: str) -> str:
        """Stores a refused call that sent nothing and returns the id of the ai_runs row that stands for it: `row`
        under `run_id` for the role's first refusal of the hour, else the existing refusal row with its `suppressed`
        count raised by one (and the latest refusal kind and error, which the caller reads back)."""
        now = as_utc(self.clock())
        known = self._refusal_row(role, now)
        stored = self.store.get('ai_runs', known[0]) if known else None
        if known is not None and stored is not None and stored.get('budget_refusal') and not sent(stored):
            self.store.put('ai_runs', known[0], dict(stored, budget_refusal=kind, error=REFUSAL_MESSAGES[kind],
                                                    suppressed=max(0, whole(stored.get('suppressed'))) + 1,
                                                    last_refused_at=now.isoformat()))
            self.refused[role] = known
            return known[0]
        self.store.put('ai_runs', run_id, dict(row, budget_refusal=kind, error=REFUSAL_MESSAGES[kind], suppressed=0,
                                               refused_at=now.isoformat(), last_refused_at=now.isoformat()))
        self.refused[role] = (run_id, now)
        return run_id

    def settle(self, role: str, admitted: int) -> None:
        if admitted <= 0:
            return
        left = self.pending.get(role, 0) - admitted
        if left > 0:
            self.pending[role] = left
        else:
            self.pending.pop(role, None)

    # --- usage (GET /api/ai/usage) -------------------------------------------------------------------------------

    def usage(self, now: datetime | None = None, limiter: Any = None) -> dict:
        """GET /api/ai/usage. `used` counts the stored requests of budget-aware rows plus those admitted and not stored
        yet (the figure the budget checks); a role's calls split into succeeded, failed (sent and failed) and refused
        (the budget stopped the run, possibly after its first request was sent) without overlap. With no budget section
        there is no limit: daily_requests, remaining and the pace limits are null."""
        config = self.settings()
        bounds = limits(config)
        budgeted = bool(config)
        tz = zone(config)
        now = as_utc(now or self.clock())
        today = now.astimezone(tz).date()
        start, resets = day_start(today, tz), day_start(today + timedelta(days=1), tz)
        first = day_start(today - timedelta(days=HISTORY_DAYS - 1), tz)
        window_start = now - timedelta(hours=bounds.window_hours)
        rows = self.rows(min(first, window_start))
        # Today's figures come from budget-aware rows only (a `requests` field); older rows show in the history.
        current = [r for r in rows if r['at'] >= start and 'requests' in r]
        pending = dict(self.pending)
        used = sum(sent(r) for r in current) + sum(pending.values())
        names = list(bounds.caps) + sorted({str(r['agent']) for r in current if r.get('agent')} - set(bounds.caps))
        roles = []
        for role in names:
            mine = [r for r in current if r.get('agent') == role]
            roles.append({'role': role, 'label': ROLE_LABELS.get(role, role.replace('_', ' ').capitalize()),
                          'used': sum(sent(r) for r in mine) + pending.get(role, 0), 'cap': bounds.caps.get(role),
                          'tokens': _sum_tokens(mine),
                          'refused': sum(refusals(r) for r in mine),
                          'succeeded': sum(1 for r in mine if r.get('success')),
                          'failed': sum(1 for r in mine if not r.get('success') and not r.get('budget_refusal')
                                        and recorded(r) > 0)})
        history = []
        for offset in range(HISTORY_DAYS - 1, -1, -1):
            day = today - timedelta(days=offset)
            low, high = day_start(day, tz), day_start(day + timedelta(days=1), tz)
            items = [r for r in rows if low <= r['at'] < high]
            history.append({'day': day.isoformat(), 'used': sum(recorded(r) for r in items), 'tokens': _sum_tokens(items)})
        recent = [r for r in rows if r['at'] >= window_start]
        shared_used = (sum(sent(r) for r in recent if str(r.get('agent') or '') not in bounds.reserve)
                       + sum(count for name, count in pending.items() if name not in bounds.reserve))
        limited = [r['at'] for r in rows if r.get('http_status') == 429]
        at = getattr(limiter, 'rate_limited_at', None)
        if isinstance(at, datetime):
            limited.append(as_utc(at))
        snapshot = limiter.snapshot() if limiter is not None and callable(getattr(limiter, 'snapshot', None)) else None
        return {'day': today.isoformat(), 'resets_at': resets.isoformat(),
                'daily_requests': bounds.daily if budgeted else None, 'used': used,
                'remaining': max(0, bounds.daily - used) if budgeted else None, 'reserve_share': bounds.share,
                'pace': {'window_hours': bounds.window_hours, 'limit': bounds.pace if budgeted else None,
                         'used': sum(sent(r) for r in recent) + sum(pending.values()),
                         # The roles outside reserve.roles are refused once shared_used reaches shared_limit.
                         'shared_limit': bounds.shared_pace if budgeted else None, 'shared_used': shared_used},
                'roles': roles, 'last_rate_limited_at': max(limited).isoformat() if limited else None,
                'history': history,
                'limiter': {key: snapshot.get(key) for key in ('concurrency_limit', 'in_flight', 'queued', 'paused_until')}
                if isinstance(snapshot, dict) else None}


EDITABLE_KEYS = {'daily_requests': None, 'role_daily': None, 'reserve': {'share', 'roles'},
                 'spread': {'burst_hours', 'hourly_share_of_daily', 'role_pace'}}


def change_errors(changes: Mapping[str, Any], known_roles: Iterable[str]) -> list[str]:
    """Keys an edit (PUT /api/admin/config/ai_budget) may not set: anything but the values of EDITABLE_KEYS (never a
    note, never an unknown role). The values themselves are checked by validate() on the merged section."""
    known = set(known_roles)
    errors = [f'{key}: not editable here' for key in changes if key not in EDITABLE_KEYS]
    for key, allowed in EDITABLE_KEYS.items():
        value = changes.get(key)
        if key == 'role_daily' and value is not None:
            if not isinstance(value, dict):
                errors.append('role_daily: expected an object')
            else:
                errors += [f'role_daily.{role}: unknown AI role' for role in value if role not in known]
        elif allowed is not None and value is not None:
            if not isinstance(value, dict):
                errors.append(f'{key}: expected an object')
            else:
                errors += [f'{key}.{name}: not editable here' for name in value if name not in allowed]
    return errors


def validate(section: Any, known_roles: Iterable[str]) -> list[str]:
    """Errors of a complete budget section (the configuration file after an edit), in plain words."""
    known = set(known_roles)
    errors: list[str] = []
    if not isinstance(section, dict):
        return ['budget: expected an object']
    daily = section.get('daily_requests')
    if not isinstance(daily, int) or isinstance(daily, bool) or not 0 <= daily <= 10000:
        errors.append('daily_requests: a whole number from 0 to 10000')
    caps = section.get('role_daily')
    if not isinstance(caps, dict):
        errors.append('role_daily: expected an object')
    else:
        for role, cap in caps.items():
            if role not in known:
                errors.append(f'role_daily.{role}: unknown AI role')
            elif not isinstance(cap, int) or isinstance(cap, bool) or not 0 <= cap <= 5000:
                errors.append(f'role_daily.{role}: a whole number from 0 to 5000')
    reserve = section.get('reserve')
    if not isinstance(reserve, dict):
        errors.append('reserve: expected an object')
    else:
        share = reserve.get('share')
        if not isinstance(share, (int, float)) or isinstance(share, bool) or not 0 <= share <= 0.9:
            errors.append('reserve.share: a number from 0 to 0.9')
        roles = reserve.get('roles')
        if not isinstance(roles, list) or any(role not in known for role in roles):
            errors.append('reserve.roles: a list of known AI roles')
    spread = section.get('spread')
    if not isinstance(spread, dict):
        errors.append('spread: expected an object')
    else:
        hours = spread.get('burst_hours')
        if not isinstance(hours, (int, float)) or isinstance(hours, bool) or not 1 <= hours <= 24:
            errors.append('spread.burst_hours: a number from 1 to 24')
        share = spread.get('hourly_share_of_daily')
        if not isinstance(share, (int, float)) or isinstance(share, bool) or not 0 < share <= 1:
            errors.append('spread.hourly_share_of_daily: a number above 0 and at most 1')
        if 'role_pace' in spread and not isinstance(spread['role_pace'], bool):
            errors.append('spread.role_pace: true or false')
    return errors
