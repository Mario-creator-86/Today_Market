"""Event bus for quotes, events, decisions, alerts and provider status: Redis Streams when REDIS_URL is set,
an in-process bus otherwise. The WebSocket fan-out consumes either one through the same interface."""
import asyncio
import contextlib
import hashlib
import json
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Protocol

from backend.config import rules

logger = logging.getLogger(__name__)
Entry = tuple[str, str, dict]  # (stream name, entry id, payload)


def _config() -> dict:
    return rules()['refresh']['streams']


def check_stream(name: str) -> str:
    if name not in _config()['names']:
        raise ValueError(f'Unknown stream: {name}')
    return name


def _parse_id(entry_id: str) -> tuple[int, int]:
    millis, _, sequence = entry_id.partition('-')
    return int(millis), int(sequence or 0)


class EventBus(Protocol):
    backend: str

    async def publish(self, stream: str, payload: dict) -> str: ...
    def publish_nowait(self, stream: str, payload: dict) -> None: ...
    async def read(self, offsets: dict[str, str], block_ms: int | None = None, count: int | None = None) -> list[Entry]: ...
    async def latest_ids(self, streams: list[str]) -> dict[str, str]: ...
    async def close(self) -> None: ...


class MemoryBus:
    """Single-process fallback with Redis-like ids and MAXLEN trimming; nothing survives a restart."""
    backend = 'memory'

    def __init__(self, maxlen: int | None = None):
        self.maxlen = maxlen or _config()['maxlen']
        self.entries: dict[str, deque[tuple[tuple[int, int], dict]]] = {}
        self.last = (0, 0)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.changed: asyncio.Event | None = None
        self.lock = threading.Lock()  # publishers may run in worker threads

    def _next_id(self) -> tuple[int, int]:
        millis = int(time.time() * 1000)
        self.last = (millis, 0) if millis > self.last[0] else (self.last[0], self.last[1] + 1)
        return self.last

    def publish_nowait(self, stream: str, payload: dict) -> None:
        check_stream(stream)
        with self.lock:
            self.entries.setdefault(stream, deque(maxlen=self.maxlen)).append((self._next_id(), payload))
        self._wake()

    async def publish(self, stream: str, payload: dict) -> str:
        self._bind()
        self.publish_nowait(stream, payload)
        key = self.entries[stream][-1][0]
        return f'{key[0]}-{key[1]}'

    def _bind(self) -> asyncio.Event:
        loop = asyncio.get_running_loop()
        if self.loop is not loop or self.changed is None:
            self.loop, self.changed = loop, asyncio.Event()
        return self.changed

    def _wake(self) -> None:
        if self.loop is None or self.changed is None or self.loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.changed.set()
        else:  # publisher in a worker thread
            self.loop.call_soon_threadsafe(self.changed.set)

    def _collect(self, offsets: dict[str, str], count: int) -> list[Entry]:
        found: list[Entry] = []
        for stream, offset in offsets.items():
            after = _parse_id(offset)
            with self.lock:
                stored = list(self.entries.get(stream, ()))
            items = [(stream, f'{k[0]}-{k[1]}', p) for k, p in stored if k > after]
            found += items[:count]
        return found

    async def read(self, offsets: dict[str, str], block_ms: int | None = None, count: int | None = None) -> list[Entry]:
        changed = self._bind()
        count = count or _config()['read_count']
        changed.clear()  # cleared before collecting so a publish in between still wakes the wait
        found = self._collect(offsets, count)
        if found or not block_ms:
            return found
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(changed.wait(), block_ms / 1000)
        return self._collect(offsets, count)

    async def latest_ids(self, streams: list[str]) -> dict[str, str]:
        self._bind()
        latest = {}
        for stream in streams:
            items = self.entries.get(check_stream(stream))
            latest[stream] = f'{items[-1][0][0]}-{items[-1][0][1]}' if items else '0-0'
        return latest

    async def close(self) -> None:
        return None


class RedisBus:
    """Redis Streams with approximate MAXLEN trimming; payloads are one JSON field so types survive."""
    backend = 'redis'

    def __init__(self, url: str = '', client: Any = None):
        if client is None:
            from redis.asyncio import Redis
            client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
        self.client = client
        self.prefix = _config()['prefix']
        self.pending: set[asyncio.Future] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def key(self, stream: str) -> str:
        return self.prefix + check_stream(stream)

    async def publish(self, stream: str, payload: dict) -> str:
        self.loop = asyncio.get_running_loop()
        entry = await self.client.xadd(self.key(stream), {'data': json.dumps(payload, default=str)},
                                       maxlen=_config()['maxlen'], approximate=True)
        return entry.decode() if isinstance(entry, bytes) else str(entry)

    def publish_nowait(self, stream: str, payload: dict) -> None:
        """Fire-and-forget from sync code; a Redis outage is logged, never raised into ingestion."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        coroutine: Coroutine[Any, Any, str] = self.publish(stream, payload)
        if loop is not None:
            future: asyncio.Future = loop.create_task(coroutine)
        elif self.loop is not None and not self.loop.is_closed():
            future = asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, self.loop), loop=self.loop)
        else:
            coroutine.close()
            logger.debug('stream_publish_dropped', extra={'stream': stream})
            return
        self.pending.add(future)
        future.add_done_callback(self._done)

    def _done(self, future: asyncio.Future) -> None:
        self.pending.discard(future)
        if not future.cancelled() and future.exception() is not None:
            logger.warning('stream_publish_failed', extra={'error_type': type(future.exception()).__name__})

    async def read(self, offsets: dict[str, str], block_ms: int | None = None, count: int | None = None) -> list[Entry]:
        response = await self.client.xread({self.key(s): i for s, i in offsets.items()},
                                           count=count or _config()['read_count'], block=block_ms or None)
        return [(_text(key).removeprefix(self.prefix), _text(entry_id), _decode(fields))
                for key, items in response or [] for entry_id, fields in items]

    async def latest_ids(self, streams: list[str]) -> dict[str, str]:
        latest = {}
        for stream in streams:
            items = await self.client.xrevrange(self.key(stream), count=1)
            latest[stream] = _text(items[0][0]) if items else '0-0'
        return latest

    async def ensure_group(self, stream: str, group: str) -> None:
        try:
            await self.client.xgroup_create(self.key(stream), group, id='$', mkstream=True)
        except Exception as exc:  # BUSYGROUP: the group already exists
            if 'BUSYGROUP' not in str(exc):
                raise

    async def read_group(self, group: str, consumer: str, streams: list[str], block_ms: int | None = None,
                         count: int | None = None) -> list[Entry]:
        """Consumer-group read for Celery/worker consumers; acknowledge with ack() after processing."""
        response = await self.client.xreadgroup(group, consumer, {self.key(s): '>' for s in streams},
                                                count=count or _config()['read_count'], block=block_ms or None)
        return [(_text(key).removeprefix(self.prefix), _text(entry_id), _decode(fields))
                for key, items in response or [] for entry_id, fields in items]

    async def ack(self, stream: str, group: str, *entry_ids: str) -> int:
        return int(await self.client.xack(self.key(stream), group, *entry_ids))

    async def close(self) -> None:
        for future in list(self.pending):
            with contextlib.suppress(Exception):
                await future
        await self.client.aclose()


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _decode(fields: dict) -> dict:
    raw = fields.get('data', fields.get(b'data'))
    try:
        return json.loads(_text(raw)) if raw is not None else {_text(k): _text(v) for k, v in fields.items()}
    except ValueError:
        return {'raw': _text(raw)}


def create_bus(redis_url: str) -> EventBus:
    return RedisBus(redis_url) if redis_url else MemoryBus()


# Restamped on every build (as_of, generated_at, countdowns and ages) or on every provider poll (a health row's
# latency_ms and last_success, the rate-limit counters a provider reads from each response, and the LLM limiter's
# in_flight/queued calls on the chat provider's row): never on their own a reason to push a snapshot. They stay in the
# payload that is sent.
VOLATILE_KEYS = frozenset({'as_of', 'generated_at', 'age_seconds', 'minutes_until', 'minutes_until_event', 'seconds_until',
                           'seconds_to_release', 'latency_ms', 'last_success', 'x_ratelimit_reset',
                           'x_ratelimit_remaining', 'news_lag_seconds', 'in_flight', 'queued'})
VOLATILE_PREFIXES = ('used_weight_',)  # Binance used_weight_1m and any other per-window weight counter


# Values recomputed on every read with continuous decay, compared at the precision the UI shows (decimals): fact
# confidence decays with its half-life, so at full precision some event would change on every build.
DISPLAY_PRECISION = {'fact_confidence': 1, 'dominance_score': 0, 'relevance_score': 0}

# Text and values derived from live prices outside assets[].quote/proxy (market-view reason facts, regime feature values
# and driver inputs): they move with every price, so they never on their own re-send the full snapshot. They stay in the
# payload; the client also re-reads the dashboard over HTTP every 30 s while the stream is live, which refreshes them.
PRICE_DERIVED_KEYS = frozenset({'reason_facts', 'features', 'inputs', 'input'})

# The per-asset live parts of a snapshot (assets[].quote and assets[].proxy): sent on their own in an 'asset_quotes'
# frame when nothing else changed, so a moving price never re-sends the whole dashboard.
LIVE_PARTS = {'quote': 'quotes', 'proxy': 'proxies'}


def _shown(value: Any, decimals: int) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return value
    return math.floor(value * 10 ** decimals + 1e-9) / 10 ** decimals


def _volatile(key: str) -> bool:
    return key in VOLATILE_KEYS or key.startswith(VOLATILE_PREFIXES)


def _stable(value: Any, skip: frozenset[str] = frozenset()) -> Any:
    """The snapshot without its time-only and per-poll fields (VOLATILE_KEYS and VOLATILE_PREFIXES, at any depth) and
    the `skip` keys, with decaying values at their displayed precision (DISPLAY_PRECISION)."""
    if isinstance(value, dict):
        return {k: _shown(v, DISPLAY_PRECISION[k]) if k in DISPLAY_PRECISION else _stable(v, skip)
                for k, v in value.items() if not _volatile(str(k)) and k not in skip}
    if isinstance(value, list):
        return [_stable(v, skip) for v in value]
    return value


def _digest(value: Any, skip: frozenset[str] = frozenset()) -> str:
    encoded = json.dumps(_stable(value, skip), sort_keys=True, default=str, separators=(',', ':')).encode()
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


def fingerprint(snapshot: dict) -> str:
    """Content hash of a snapshot that ignores time-only fields, so an unchanged market state is not re-sent."""
    return _digest(snapshot)


def split_live(snapshot: dict) -> tuple[dict, dict[str, dict[str, Any]]]:
    """(the snapshot without assets[].quote / assets[].proxy, {'quotes': {symbol: quote}, 'proxies': {symbol: proxy}})."""
    live: dict[str, dict[str, Any]] = {frame: {} for frame in LIVE_PARTS.values()}
    assets = snapshot.get('assets')
    if not isinstance(assets, list):
        return snapshot, live
    rest = []
    for asset in assets:
        if isinstance(asset, dict) and asset.get('symbol'):
            for part, frame in LIVE_PARTS.items():
                live[frame][str(asset['symbol'])] = asset.get(part)
            asset = {k: v for k, v in asset.items() if k not in LIVE_PARTS}
        rest.append(asset)
    return dict(snapshot, assets=rest), live


def _live_digests(live: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    return {frame: {symbol: _digest(value) for symbol, value in parts.items()} for frame, parts in live.items()}


def fingerprints(snapshot: dict) -> tuple[str, dict[str, dict[str, str]]]:
    """Content hash of everything but the live parts, and one hash per symbol and live part ({'quotes': {symbol: hash},
    'proxies': {...}}), with the same volatile-field and display-precision rules as fingerprint()."""
    rest, live = split_live(snapshot)
    return _digest(rest, PRICE_DERIVED_KEYS), _live_digests(live)


class Fanout:
    """One consumer for every WebSocket client. Quotes coalesce per symbol and flush at the UI quote rate;
    snapshots are built once (in a worker thread) and shared, so clients never trigger per-socket recomputation.
    A snapshot is built at most every ui_snapshot seconds while clients are connected. When anything but the per-asset
    live parts (assets[].quote / assets[].proxy) changed, the full snapshot is broadcast, at most every ui_snapshot_full
    seconds (a change seen earlier goes out when that interval allows); when only live parts changed, an 'asset_quotes'
    frame {as_of, quotes: {symbol: quote|null}, proxies: {symbol: proxy|null}} carries just the symbols whose part
    changed. A client that connects in between gets the latest full snapshot at once, or a fresh build sent to it alone."""

    def __init__(self, bus: EventBus, snapshot: Callable[[], dict] | None = None, streams: list[str] | None = None,
                 queue_size: int = 32):
        cadences = rules()['refresh']['cadences']
        self.bus, self.snapshot = bus, snapshot
        self.streams = streams or list(_config()['names'])
        self.quote_seconds = cadences['ui_quotes']['seconds']
        self.snapshot_seconds = cadences['ui_snapshot']['seconds']
        self.full_seconds = cadences['ui_snapshot_full']['seconds']
        self.queue_size = queue_size
        self.subscribers: set[asyncio.Queue] = set()
        self.pending: set[asyncio.Queue] = set()  # connected since the last build, without a current snapshot yet
        # Clients that must get the next full build: a build failed while they waited, or their queue overflowed and
        # lost frames (a price-only frame means nothing without the snapshot it updates).
        self.resync: set[asyncio.Queue] = set()
        self.tasks: list[asyncio.Task] = []
        self.refresh = asyncio.Event()
        self.latest_snapshot: dict | None = None
        self.snapshot_at = 0.0  # monotonic time of the latest build
        self.built_at: float | None = None  # monotonic time of the latest cadence build (the one every client gets)
        self.sent_fingerprint: str | None = None  # everything but the live parts, as every client last received it
        self.sent_live: dict[str, dict[str, str]] = {}  # per live part and symbol, as every client last received it
        self.full_sent_at: float | None = None  # monotonic time of the latest full broadcast

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(self.queue_size)
        # Only a snapshot from the current cadence is replayed: an older one would show stale quotes as fresh.
        if self.latest_snapshot is not None and time.monotonic() - self.snapshot_at <= self.snapshot_seconds:
            queue.put_nowait({'type': 'snapshot', 'data': self.latest_snapshot})
        else:
            self.pending.add(queue)
            self.refresh.set()
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)
        self.pending.discard(queue)
        self.resync.discard(queue)

    def _put(self, queue: asyncio.Queue, message: dict) -> None:
        if queue.full():  # a slow client drops its backlog instead of stalling everyone, and gets the next full build
            with contextlib.suppress(asyncio.QueueEmpty):
                while True:
                    queue.get_nowait()
            if message.get('type') != 'snapshot':
                self.resync.add(queue)
        queue.put_nowait(message)

    def broadcast(self, message: dict) -> None:
        for queue in list(self.subscribers):
            self._put(queue, message)

    def start(self) -> None:
        if not self.tasks:
            self.tasks = [asyncio.create_task(self._consume(), name='fanout-consume')]
            if self.snapshot is not None:
                self.tasks.append(asyncio.create_task(self._snapshots(), name='fanout-snapshots'))

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.tasks = []

    async def _consume(self) -> None:
        offsets = await _retry(lambda: self.bus.latest_ids(self.streams))
        quotes: dict[str, dict] = {}
        flushed = time.monotonic()
        while True:
            try:
                entries = await self.bus.read(offsets, block_ms=int(self.quote_seconds * 1000))
            except Exception as exc:
                logger.warning('stream_read_failed', extra={'error_type': type(exc).__name__})
                await asyncio.sleep(self.quote_seconds)
                continue
            for stream, entry_id, payload in entries:
                offsets[stream] = entry_id
                if stream == 'quotes':
                    quotes[str(payload.get('symbol'))] = payload
                else:
                    # Decisions, briefs and recalculations reach the snapshot at its next cadence build.
                    self.broadcast({'type': stream, 'data': payload})
            if quotes and time.monotonic() - flushed >= self.quote_seconds:
                self.broadcast({'type': 'quotes', 'data': list(quotes.values())})
                quotes, flushed = {}, time.monotonic()

    def _build(self) -> tuple[dict, str, dict[str, dict[str, Any]], dict[str, dict[str, str]]]:
        """(snapshot, hash of everything but the live parts, the live parts, their hashes per part and symbol)."""
        assert self.snapshot is not None
        snapshot = self.snapshot()
        rest, parts = split_live(snapshot)
        return snapshot, _digest(rest, PRICE_DERIVED_KEYS), parts, _live_digests(parts)

    def _live_frame(self, as_of: Any, parts: dict[str, dict[str, Any]], live: dict[str, dict[str, str]]) -> dict | None:
        """'asset_quotes' frame of the live parts that changed since every client last received them, None when none."""
        changed = {frame: {symbol: parts[frame][symbol] for symbol, digest in hashes.items()
                           if self.sent_live.get(frame, {}).get(symbol) != digest}
                   for frame, hashes in live.items()}
        if not any(changed.values()):
            return None
        return {'type': 'asset_quotes', 'data': {'as_of': as_of, **changed}}

    async def _snapshot_round(self, due: bool) -> None:
        """One build. On a cadence build (`due`) a change outside the live parts broadcasts the full snapshot once
        ui_snapshot_full seconds passed since the last one; otherwise changed live parts go out as 'asset_quotes'.
        Clients waiting for their first snapshot always get the full build, alone when the others need nothing new."""
        try:
            snapshot, content, parts, live = await asyncio.to_thread(self._build)
        except Exception as exc:
            logger.error('snapshot_failed', extra={'error_type': type(exc).__name__})
            # No retry loop: waiting clients get the next cadence build in full (never a price-only frame first).
            self.resync |= self.pending
            self.pending.clear()
            self.built_at = time.monotonic()
            return
        self.latest_snapshot, self.snapshot_at = snapshot, time.monotonic()
        message = {'type': 'snapshot', 'data': snapshot}
        if due:
            self.built_at = self.snapshot_at
            full_allowed = self.full_sent_at is None or self.snapshot_at - self.full_sent_at >= self.full_seconds
            if content != self.sent_fingerprint and full_allowed:
                self.sent_fingerprint, self.sent_live, self.full_sent_at = content, live, self.snapshot_at
                self.pending.clear()
                self.resync.clear()
                self.broadcast(message)
                return
            frame = self._live_frame(snapshot.get('as_of'), parts, live)
            if frame is not None:
                self.sent_live = live
                for queue in list(self.subscribers - self.pending - self.resync):  # the others get the full build below
                    self._put(queue, frame)
        for queue in list(self.pending | self.resync):
            if queue in self.subscribers:
                self._put(queue, message)
        self.pending.clear()
        self.resync.clear()

    async def _snapshots(self) -> None:
        assert self.snapshot is not None
        while True:
            elapsed = None if self.built_at is None else time.monotonic() - self.built_at
            due = bool(self.subscribers) and (elapsed is None or elapsed >= self.snapshot_seconds)
            if due or self.pending:
                await self._snapshot_round(due)
            self.refresh.clear()
            if self.pending:  # a client connected during the build
                continue
            wait = None
            if self.subscribers and self.built_at is not None:
                wait = max(0.0, self.snapshot_seconds - (time.monotonic() - self.built_at))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.refresh.wait(), wait)


async def _retry(operation: Callable[[], Awaitable[dict[str, str]]]) -> dict[str, str]:
    delay = 1.0
    while True:
        try:
            return await operation()
        except Exception as exc:
            logger.warning('stream_unavailable', extra={'error_type': type(exc).__name__})
            await asyncio.sleep(delay)
            delay = min(30.0, delay * 2)
