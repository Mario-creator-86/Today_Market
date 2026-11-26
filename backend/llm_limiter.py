"""One process-wide limiter for the chat calls of the active LLM provider (Ollama or OpenAI).

Without it every loop (extraction, research planning, specialists, the interpreter) sent its chat calls at once: 104 in
one startup minute. Ollama cloud queued them (a trivial prompt took 37-119 s to its first byte), then answered 429, and
the provider circuit turned 8 real 429s into 260 refused calls. The limiter instead:

- lets at most `concurrency_limit` chat requests run at a time; the others wait in a priority queue at most the role's
  max_queue_wait_seconds (one wait for the whole call, its 429 retries included). Roles up to reserved_max_priority
  (the interpreter, the chief analyst, the Settings test) always go first and lower roles leave reserved_slots free for
  them; the lower roles are ordered by role_priority with aging (each level counts as priority_aging_seconds of
  waiting), so the extraction is overtaken only by calls that arrived shortly after it and never waits forever;
- on an HTTP 429 (or another backoff_statuses answer, e.g. 503 from an overloaded backend) halves the limit (not below
  min_concurrent), pauses new starts for the Retry-After (between the floor and the cap) or its own backoff, and sends
  the same call again, keeping its place in the queue, up to rate_limit_retries times;
- raises the limit by 1 after recover_after_successes consecutive successes with no 429 for recover_after_seconds, and
  for probe_after_seconds before it tries again the limit at which the provider last answered 429.

The subscription's real concurrency limit is not published: the limit adapts to what the provider accepts. Settings:
config/rules.d/ai_runtime.json `ai_runtime.limiter`. The request timeout starts only when the HTTP request is sent.
"""
import asyncio
import bisect
import itertools
import time
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeVar

from backend.config import rules_view
from backend.database import utcnow
from backend.providers.base import ProviderUnavailable

T = TypeVar('T')


class QueueWaitExceeded(ProviderUnavailable):
    """No slot within the role's max_queue_wait_seconds: nothing was sent to the provider."""


class QueueMeter:
    """Seconds the calls of one AI run spent waiting for a slot (so ai_runs.latency_ms is the provider time only)."""

    def __init__(self) -> None:
        self.seconds = 0.0


# Set by AIService around a chat call; the limiter adds each call's queue wait to it (a directly awaited coroutine runs
# in its caller's context, so the caller reads the total after the call).
QUEUE_METER: ContextVar[QueueMeter | None] = ContextVar('llm_queue_meter', default=None)


@dataclass(frozen=True)
class Ticket:
    """A call's place in the queue, kept across its 429 retries: the same order key and one overall deadline."""
    reserved: bool  # role_priority <= reserved_max_priority: may use the reserved slots and goes before every other role
    key: float  # time.monotonic() at the first enqueue + role_priority x priority_aging_seconds (lower starts sooner)
    seq: int  # FIFO among equal keys
    deadline: float  # time.monotonic() after which the call gives up waiting (QueueWaitExceeded)
    wait: float  # the role's max_queue_wait_seconds, for the error text


def limiter_settings() -> Mapping[str, Any]:
    return (rules_view().get('ai_runtime') or {}).get('limiter') or {}


def role_value(table: Mapping[str, Any], role: str | None) -> float:
    """A per-role number from a {role: value, 'default': value} table (the 'note' entries are text and ignored)."""
    value = table.get(role) if role else None
    return float(value if isinstance(value, (int, float)) and not isinstance(value, bool) else table['default'])


class LLMLimiter:
    def __init__(self, settings: Callable[[], Mapping[str, Any]] = limiter_settings):
        self.settings = settings
        self.reset()

    def reset(self) -> None:
        """Start state (process start, and between tests)."""
        config = self.settings()
        self.limit = int(config.get('start_concurrent', config['max_concurrent']))
        self.in_flight = 0
        self.rate_limited_total = 0  # HTTP 429 answers since start
        self.rate_limited_at: datetime | None = None  # UTC time of the last HTTP 429 (GET /api/ai/usage)
        self.successes = 0  # consecutive successes since the last failure or limit increase
        self.strikes = 0  # consecutive backoff pauses (grows the backoff); a success resets it
        self.last_backoff: float | None = None  # time.monotonic() of the last 429 (or other backoff status)
        self.refused_at: int | None = None  # the limit at which the provider last answered 429 (before halving)
        self.paused_until = 0.0  # time.monotonic() before which no new call starts
        self.paused_until_at: datetime | None = None  # the same moment in UTC, for the health row
        self._waiters: list[tuple[int, float, int, asyncio.Future]] = []  # sorted: (0 reserved | 1, key, seq, future)
        self._order = itertools.count()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._health: dict | None = None

    # --- state ---------------------------------------------------------------------------------------------------

    def capacity(self, config: Mapping[str, Any] | None = None) -> int:
        """The current limit inside [min_concurrent, max_concurrent] (an edited config applies at once)."""
        config = config or self.settings()
        return max(int(config['min_concurrent']), min(self.limit, int(config['max_concurrent'])))

    def shared_capacity(self, config: Mapping[str, Any], capacity: int) -> int:
        """Slots the non-reserved roles may use: all but reserved_slots, unless that would leave them none."""
        reserved = int(config['reserved_slots'])
        return capacity - reserved if capacity > reserved else capacity

    def paused(self) -> bool:
        return time.monotonic() < self.paused_until

    def queued(self) -> int:
        return sum(1 for *_, future in self._waiters if not future.done())

    def snapshot(self) -> dict:
        """Limiter fields of the chat provider's health row. in_flight and queued change constantly: they are
        streams.VOLATILE_KEYS, so on their own they never push a WebSocket snapshot."""
        return {'concurrency_limit': self.capacity(), 'in_flight': self.in_flight, 'queued': self.queued(),
                'paused_until': self.paused_until_at.isoformat() if self.paused() and self.paused_until_at else None,
                'rate_limited_total': self.rate_limited_total}

    def bind(self, health: dict) -> None:
        """Shows the limiter on this health row (the active chat provider's; a rebuilt provider binds its new row)."""
        if self._health is not health:
            self._health = health
            self._publish()

    def _publish(self) -> None:
        if self._health is not None:
            self._health.update(self.snapshot())

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # A new event loop (a worker's asyncio.run, a test): slots and waiters of the old one can never resume.
            self._loop, self.in_flight, self._waiters, self._timer = loop, 0, [], None
        return loop

    # --- slots ---------------------------------------------------------------------------------------------------

    def ticket(self, role: str | None) -> Ticket:
        """The role's queue place and wait limit, read before anything is queued (a config error raises here, never
        while a slot is held)."""
        config = self.settings()
        priority = role_value(config['role_priority'], role)
        wait = role_value(config['max_queue_wait_seconds'], role)
        now = time.monotonic()
        return Ticket(reserved=priority <= float(config['reserved_max_priority']),
                      key=now + priority * float(config['priority_aging_seconds']), seq=next(self._order),
                      deadline=now + wait, wait=wait)

    async def acquire(self, role: str | None, ticket: Ticket | None = None) -> None:
        """Waits for a slot until the ticket's deadline, then raises QueueWaitExceeded. A cancelled or timed out
        waiter never keeps a slot: one granted as it left is handed on at once."""
        loop = self._bind_loop()
        ticket = ticket or self.ticket(role)
        started = time.monotonic()
        future: asyncio.Future = loop.create_future()
        bisect.insort(self._waiters, (0 if ticket.reserved else 1, ticket.key, ticket.seq, future))
        try:
            self._dispatch()
            if not future.done():
                async with asyncio.timeout(max(0.0, ticket.deadline - time.monotonic())):
                    await future
        except BaseException as exc:
            if future.done() and not future.cancelled():
                self.release()
            else:
                future.cancel()
                self._publish()
            if isinstance(exc, TimeoutError):
                raise QueueWaitExceeded(f'LLM queue wait exceeded {ticket.wait:g} s') from None
            raise
        finally:
            meter = QUEUE_METER.get()
            if meter is not None:
                meter.seconds += time.monotonic() - started

    def release(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)
        self._dispatch()

    def _dispatch(self) -> None:
        """Starts waiters in queue order while there is capacity and no pause. A non-reserved waiter never takes the
        reserved slots; a reserved one behind it may."""
        if self.paused():
            self._arm()
        else:
            config = self.settings()
            capacity = self.capacity(config)
            shared = self.shared_capacity(config, capacity)
            waiting = []
            for entry in self._waiters:
                future = entry[-1]
                if future.done():  # cancelled or timed out while queued
                    continue
                if self.in_flight < (capacity if entry[0] == 0 else shared):
                    self.in_flight += 1
                    future.set_result(None)
                else:
                    waiting.append(entry)
            self._waiters = waiting
        self._publish()

    def _arm(self) -> None:
        """Wakes the queue (and clears paused_until on the health row) when the pause ends."""
        if self._timer is None and self._loop is not None:
            self._timer = self._loop.call_later(max(0.0, self.paused_until - time.monotonic()), self._resume)

    def _resume(self) -> None:
        self._timer = None
        self._dispatch()

    # --- outcomes ------------------------------------------------------------------------------------------------

    def succeeded(self) -> None:
        """A success resets the backoff growth. After recover_after_successes consecutive successes the limit rises by 1,
        once no 429 came for recover_after_seconds (probe_after_seconds to reach again the limit that got the last
        429), so a provider whose real limit is below max_concurrent is not probed every few calls."""
        config = self.settings()
        self.strikes = 0
        self.successes += 1
        capacity = self.capacity(config)
        target = capacity + 1
        if self.successes < int(config['recover_after_successes']) or target > int(config['max_concurrent']):
            return
        quiet = float(config['probe_after_seconds'] if self.refused_at is not None and target >= self.refused_at
                      else config['recover_after_seconds'])
        if self.last_backoff is not None and time.monotonic() - self.last_backoff < quiet:
            return  # the streak is kept: the limit rises with the first success after the quiet time
        self.successes = 0
        self.limit = target
        self._dispatch()

    def failed(self) -> None:
        self.successes = 0

    def rate_limited(self, retry_after: float | None, status_code: int = 429) -> None:
        """An HTTP 429 (or another backoff status): halve the limit and pause new starts. An answer that arrives during
        a pause replies to a request sent before it, so it is counted but does not halve or extend again (one step per
        pause)."""
        config = self.settings()
        if status_code == 429:
            self.rate_limited_total += 1
            self.rate_limited_at = utcnow()
        self.successes = 0
        self.last_backoff = time.monotonic()
        if self.paused():
            self._publish()
            return
        self.strikes += 1
        capacity = self.capacity(config)
        self.refused_at = capacity
        self.limit = max(int(config['min_concurrent']), capacity // 2)
        own = float(config['rate_limit_backoff_seconds']) * float(config['rate_limit_backoff_growth']) ** (self.strikes - 1)
        backoff = min(float(config['rate_limit_backoff_max_seconds']),
                      max(float(config['rate_limit_backoff_min_seconds']), own if retry_after is None else retry_after))
        self.paused_until = time.monotonic() + backoff
        self.paused_until_at = utcnow() + timedelta(seconds=backoff)
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._dispatch()

    async def run(self, role: str | None, send: Callable[[], Awaitable[T]], health: dict | None = None) -> T:
        """send() inside a slot. After an HTTP 429 (or another backoff_statuses answer) the slot is released, the
        limiter backs off and the same call is sent again with a new slot, keeping its place in the queue and its one
        overall queue deadline, up to rate_limit_retries times; then that error ('Rate limited (HTTP 429)') is raised.
        A 429 whose Retry-After is above rate_limit_backoff_max_seconds (an exhausted quota) is not retried. Any other
        error is raised at once (transport retries stay inside send)."""
        if health is not None:
            self.bind(health)
        config = self.settings()
        retries = int(config['rate_limit_retries'])
        backoff_statuses = {int(code) for code in config['backoff_statuses']}
        ceiling = float(config['rate_limit_backoff_max_seconds'])
        ticket = self.ticket(role)
        attempt = 0
        while True:
            await self.acquire(role, ticket)
            try:
                result = await send()
            except ProviderUnavailable as exc:
                if exc.status_code not in backoff_statuses:
                    self.failed()
                    raise
                self.rate_limited(exc.retry_after if exc.status_code == 429 else None, exc.status_code)
                if exc.retry_after is not None and exc.retry_after > ceiling:
                    raise ProviderUnavailable(f'{exc}: retry after {exc.retry_after:.0f} s', exc.status_code,
                                              exc.retry_after) from None
                if attempt >= retries:
                    raise
                attempt += 1
            except Exception:
                self.failed()
                raise
            else:
                self.succeeded()
                return result
            finally:
                self.release()


# The one instance every chat call shares (tests replace it or call reset()).
LIMITER = LLMLimiter()
