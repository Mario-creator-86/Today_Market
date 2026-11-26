"""Background ingestion and analysis loops. Cadences come from config/rules.d/refresh.json (spec §10);
adaptive loops (central banks, release watch, SEC, Congress) re-plan their next run from the calendar."""
import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeGuard
from urllib.parse import urlsplit

import websockets
from pydantic import ValidationError
from sqlalchemy import delete, select

from backend import analytics, briefs, causal, macro, notifications, refresh, releases, workers
from backend.config import freshness_seconds, rules
from backend.database import RECORDS, Bar, Event, as_utc, uid, utcnow
from backend.engines import aware
from backend.providers import proxies
from backend.providers.base import ProviderUnavailable
from backend.providers.calendars import (
    calendar_key,
    carry_lent,
    dedupe,
    fmp_earnings_rows,
    fmp_economic_rows,
    local_date,
    stale_ids,
)
from backend.providers.licensed import LicensedNewsProvider
from backend.providers.market import TreasuryYieldProvider
from backend.providers.news import RSSProvider, SECProvider, x_source_for, x_watch_query
from backend.providers.registry import by_capability, is_stub
from backend.providers.stubs import LicensedNewsStub
from backend.schemas import DocumentInput, Quote
from backend.security import bound_queries
from backend.service import HubService

logger = logging.getLogger(__name__)
SHUTDOWN_TIMEOUT_SECONDS = 10
BLS_MIN_SECONDS = 7200  # keeps the keyless BLS API within its 20 queries/day
STREAM_RECHECK_SECONDS = 15  # a disabled Binance stream checks its switch again after this long
STREAM_ERROR_LOG_SECONDS = 60  # one log line per error type per minute from the stream
PROVIDER_ERRORS = (ProviderUnavailable, ValueError, KeyError, TypeError)
BINANCE_STREAM = 'wss://data-stream.binance.vision/stream?streams='
CRYPTO_PAIRS = {'BTCUSDT': 'BTC', 'ETHUSDT': 'ETH'}  # Binance pair -> tracked asset (live ticks)
DAY_SECONDS = 86400
CALENDAR_SCAN_ROWS = 5000  # stored calendar rows read for replacement and dedupe (the window holds a few hundred)
# The quote and daily-close polls whose first completed attempt is the 'first market data refresh' the morning brief
# waits for (AFP-05, briefs.morning_ready); the crypto stream is not a poll and never gates it.
REFRESH_KINDS = ('quotes', 'treasury_yields', 'equity_quotes', 'alpaca_quotes', 'eia_spot')
EVENT_ALERTED_MAX = 4000  # event ids whose EVENT alert was already dispatched (kept in memory, bounded)


def reject_reason(exc: Exception) -> str:
    """Short, value-free reason for a rejected row (field names and error types only, never payload values)."""
    if isinstance(exc, ValidationError):
        return '; '.join(f"{'.'.join(str(p) for p in error['loc']) or 'row'}: {error['type']}" for error in exc.errors())[:200]
    return str(exc)[:200] or type(exc).__name__


def binance_proxy_pairs() -> dict[str, str]:
    """Binance pair -> proxy symbol for the proxies Binance serves in coverage.json (e.g. PAXGUSDT -> PAXG)."""
    return {f'{symbol}USDT': symbol for symbol in proxies.specs('binance')}


def coinbase_proxy_products() -> dict[str, str]:
    return {f'{symbol}-USD': symbol for symbol in proxies.specs('coinbase')}


def equity_symbols() -> set[str]:
    return {symbol for symbol, _, kind in rules()['assets'] if kind == 'equity'}


def raise_if_cancelling() -> None:
    """httpx/anyio can absorb a task cancellation and return normally; loops must still stop."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


def usable(provider: Any) -> TypeGuard[Any]:
    """Present and not switched off in Settings -> Data providers. Disabled providers are skipped silently."""
    return provider is not None and getattr(provider, 'enabled', True) is not False


class Runtime:
    def __init__(self, hub: HubService, bus: Any = None):
        self.hub = hub
        self.bus = bus if bus is not None else hub.bus
        self.tasks: list[asyncio.Task] = []
        self.last_stream_tick = 0.0
        self.proxy_stream_tick = 0.0  # last proxy (PAXG) message on the Binance stream
        self.proxy_written: dict[str, float] = {}  # proxy_quotes key -> monotonic time of the last stream write
        self.proxy_trades: dict[str, tuple[str | None, float]] = {}  # proxy key -> (last trade id, price) seen
        self.proxy_pending: dict[str, dict] = {}  # proxy key -> newest streamed trade not yet stored
        self.stream_errors: dict[str, float] = {}  # error type -> monotonic time it was last logged
        self.finnhub_missing: set[str] = set()  # tracked equities Finnhub could not serve in its last round
        self.daily_bars_loaded = False
        self.last_run: dict[str, float] = {}
        self.recalc = refresh.RecalcRequests()
        interpretation = refresh.interpretation_config()
        self.interpret = refresh.RecalcRequests(min_interval=interpretation['min_interval_seconds'],
                                                accepted=interpretation['triggers'])
        self.analysis_started = asyncio.Event()  # set after the first analysis run (the interpretation waits for it)
        self.trigger_state: dict | None = None
        self.event_state: dict | None = None  # last event_risk.trigger_state (kept while a new one cannot be built)
        self.event_state_at: float | None = None
        self.event_state_task: asyncio.Task | None = None
        self.event_state_errors: dict[str, float] = {}
        # Post-event MARKET DIVERGENCE (docs/EVENT_AWARENESS.md §3): the diverging post_event entries of the last
        # snapshot, the events a research-planner request was started for (once each) and the last attempt per event.
        self.divergence_events: dict[str, dict] = {}
        self.divergence_researched: dict[str, float] = {}
        self.divergence_attempts: dict[str, float] = {}
        self.divergence_task: asyncio.Task | None = None
        self.divergences: list[str] = []  # latest DIVERGENCE alert keys (derived loop), for CROSS_ASSET_DIVERGENCE
        self.previous_health: dict[str, dict] = {}
        # AFP-05: when this process started and when every scheduled quote / daily-close poll finished its first attempt
        # (None until then); the start of the analysis cycle whose decisions a brief would describe. briefs.morning_ready
        # composes the morning brief only from a cycle that started after that refresh.
        self.refresh_state: dict[str, str | None] = {'started_at': utcnow().isoformat(), 'refreshed_at': None}
        self.first_done: set[str] = set()  # loop kinds that completed their first attempt (success or failure)
        self.scheduled_kinds: set[str] = set()  # loop kinds this process runs (Celery-owned ones excluded)
        self.cycle_started_at: datetime | None = None
        self.event_alerted: set[str] = set()  # AFP-04: event ids whose EVENT alert was dispatched (once per event)
        self.release_polls: dict[str, list[float]] = {}  # EVR-4: calendar row id -> monotonic times of targeted polls
        self.calendar_fetched: dict[str, float] = {}  # calendar source -> monotonic time of its last successful fetch
        self.calendar_ok: dict[str, bool] = {}  # calendar source -> last fetch outcome (fallback sources follow it)
        self.dispatcher = notifications.Dispatcher(hub.store, notifications.build_channels(hub.store))
        hub.settings_listeners.append(self.settings_changed)

    def settings_changed(self, _settings: Any) -> None:
        """Alert channels follow the saved settings; the Dispatcher (and its hourly rate-limit history) is kept."""
        self.dispatcher.channels = notifications.build_channels(self.hub.store)

    # --- scheduling ------------------------------------------------------------------------------

    async def start(self):
        owned = workers.celery_owned()
        fixed: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
            ('quotes', self.poll_market), ('global_sweep', self.poll_news), ('breaking_news', self.poll_breaking),
            ('model_discovery', self.discover), ('x_search', self.social), ('derived', self.derived),
            ('derivatives', self.derivatives), ('hot_topics', self.research_hot_topics), ('retention', self.retention),
            ('briefs', self.morning_brief), ('positioning', self.poll_positioning), ('official_data', self.poll_official),
            ('discovery', self.poll_discovery), ('equity_quotes', self.poll_equities), ('calibration', self.calibrate),
            ('eia_spot', self.poll_eia_spot), ('treasury_yields', self.poll_treasury_yields),
            ('alpaca_quotes', self.poll_alpaca_quotes),
            ('alpaca_bars', self.poll_alpaca_bars), ('alpaca_news', self.poll_alpaca_news)]
        adaptive = [('calendar', self.calendar), ('central_banks', self.poll_central_banks), ('sec_filings', self.poll_sec),
                    ('congress', self.poll_congress), ('release_watch', self.poll_release_sources)]
        self.scheduled_kinds = {kind for kind, _ in fixed + adaptive if kind not in owned}
        briefs.set_gate(self.morning_gate)
        self.tasks = [asyncio.create_task(self.crypto_stream(), name='crypto_stream')]
        if 'interpretation' not in owned:
            self.tasks.append(asyncio.create_task(self.analysis_loop(), name='interpretation'))
        # The market interpretation runs in-process only (docs/EVENT_AWARENESS.md §5; no Celery task yet).
        self.tasks.append(asyncio.create_task(self.interpretation_loop(), name='market_interpretation'))
        for kind, operation in fixed + adaptive:
            if kind not in owned:
                self.tasks.append(asyncio.create_task(self.cycle(kind, operation), name=kind))

    async def stop(self):
        if briefs.gate() is self.morning_gate:
            briefs.set_gate(None)
        for task in (self.event_state_task, self.divergence_task):
            if task is not None:
                task.cancel()
        for task in self.tasks:
            task.cancel()
        if not self.tasks:
            return
        _, pending = await asyncio.wait(self.tasks, timeout=SHUTDOWN_TIMEOUT_SECONDS)
        if pending:
            # Never block application shutdown on a task that ignores cancellation.
            logger.error('background_tasks_not_stopped', extra={'error_type': ','.join(t.get_name() for t in pending)})

    def interval(self, kind: str) -> float:
        now = utcnow()
        if kind in {'calendar', 'central_banks', 'release_watch', 'sec_filings', 'congress'}:
            return max(1.0, refresh.next_interval(kind, now, self.hub.calendar(window=True),
                                                  congress_is_active=refresh.congress_active(self.hub.events(), now)))
        return float(refresh.cadence(kind)['seconds'])

    def pause(self, kind: str, elapsed: float) -> float:
        """Seconds to sleep before the next round of `kind`: its interval, or for a cadence marked fixed_rate
        (refresh.json: the REST-polled equity quotes) the interval minus the time the round took, never below 0, so a
        round starts every `seconds` and the quote age the freshness limit is sized for holds."""
        interval = self.interval(kind)
        try:
            fixed = bool(refresh.cadence(kind).get('fixed_rate'))
        except KeyError:
            fixed = False
        return max(0.0, interval - max(0.0, elapsed)) if fixed else interval

    async def cycle(self, kind: str, operation: Callable[[], Awaitable[Any]]):
        while True:
            started = time.monotonic()
            try:
                await operation()
                self.last_run[kind] = time.monotonic()
            except Exception as exc:
                logger.error('background_operation_failed', extra={'operation': kind, 'error_type': type(exc).__name__})
            self.mark_first(kind)
            raise_if_cancelling()
            await asyncio.sleep(self.pause(kind, time.monotonic() - started))

    def mark_first(self, kind: str) -> None:
        """Records the first completed attempt of a loop (a failing provider counts: it must not hold the morning
        brief for the day). Once every scheduled market-data poll (REFRESH_KINDS) has one, the first market data
        refresh since start is complete (refresh_state.refreshed_at)."""
        if kind in self.first_done:
            return
        self.first_done.add(kind)
        wanted = set(REFRESH_KINDS) & self.scheduled_kinds
        if self.refresh_state.get('refreshed_at') is None and wanted <= self.first_done:
            self.refresh_state['refreshed_at'] = utcnow().isoformat()

    def morning_gate(self, now: datetime) -> tuple[bool, str | None]:
        """briefs.morning_ready for this process (AFP-05): the morning brief waits for the first market data refresh
        after start and for an analysis cycle started after it (cycle_started_at: the running or last cycle)."""
        return briefs.morning_ready(now, self.refresh_state, self.cycle_started_at)

    async def repeat(self, operation, seconds: int):
        """Fixed-interval loop kept for callers that schedule an operation directly."""
        while True:
            try:
                await operation()
            except Exception as exc:
                logger.error('background_operation_failed', extra={'operation': operation.__name__, 'error_type': type(exc).__name__})
            raise_if_cancelling()
            await asyncio.sleep(seconds)

    def publish(self, stream: str, payload: dict) -> None:
        if self.bus is not None:
            self.bus.publish_nowait(stream, payload)

    async def save_health(self):
        rows = self.hub.health()
        self.hub.store.put_many('provider_health', {item['name']: item for item in rows})
        alerts = [notifications.provider_alert(self.previous_health.get(item['name']), item, utcnow()) for item in rows]
        for item in rows:
            if self.previous_health.get(item['name'], {}).get('status') != item.get('status'):
                self.publish('providers', item)
        self.previous_health = {item['name']: dict(item) for item in rows}
        await self.alert(alerts)

    async def alert(self, alerts: list[dict | None]) -> None:
        for result in await self.dispatcher.dispatch_many(alerts):
            if result.get('status') == 'NEW':
                self.publish('alerts', self.hub.store.get('alerts', result['alert_id']) or {'id': result['alert_id']})

    def plain_assets(self) -> list[dict]:
        """Published assets without the event overlay (the loops need decisions, quotes and levels only; the overlay
        reads news and the calendar again for every call). Falls back to hub.assets() for a hub without the flag."""
        try:
            return self.hub.assets(event_overlay=False)
        except TypeError:
            return self.hub.assets()

    def observe(self) -> None:
        """Immediate recalculation when an event status, a release, a large move or a level break appears (§10), and
        an immediate market interpretation on the event-aware triggers (event windows, releases, press conferences,
        regime or score changes, market open/close; docs/EVENT_AWARENESS.md §5). A post-event MARKET DIVERGENCE also
        starts one research-planner request per event (research_divergences)."""
        now = utcnow()
        state = refresh.observe_state(self.hub.store, self.plain_assets(), self.hub.events(), self.hub.calendar(window=True),
                                      now, self.divergences, event_state=self.event_trigger_state(now))
        triggers = refresh.detect_triggers(self.trigger_state, state)
        self.trigger_state = state
        self.recalc.request(triggers)
        self.interpret.request(triggers)
        self.schedule_divergence_research()

    def event_trigger_state(self, now: datetime) -> dict | None:
        """The latest event_risk.trigger_state(event_risk.snapshot(hub)), rebuilt at most every
        interpretation.event_state_seconds. Inside the event loop the rebuild runs in a worker thread (the snapshot reads
        assets, regime and calendar) and the previous state is returned meanwhile; the next observation compares the
        new one. Until backend.event_risk exists, or while it fails, the last good state is kept: no window entry is
        lost and nothing fires from a missing state."""
        seconds = refresh.interpretation_config()['event_state_seconds']
        if self.event_state_at is not None and time.monotonic() - self.event_state_at < seconds:
            return self.event_state
        self.event_state_at = time.monotonic()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # called outside the event loop (tests, scripts): build it now
            self.build_event_state(now)
            return self.event_state
        if self.event_state_task is None or self.event_state_task.done():
            self.event_state_task = loop.create_task(asyncio.to_thread(self.build_event_state, now), name='event_state')
        return self.event_state

    def build_event_state(self, now: datetime) -> None:
        try:
            from backend import event_risk
            snapshot, trigger_state = event_risk.snapshot, event_risk.trigger_state
        except (ImportError, AttributeError):
            return  # the event-risk engine is not installed: the calendar-based triggers still work
        try:
            built = snapshot(self.hub, now)
            self.event_state = trigger_state(built)
            self.divergence_events = {str(p['event_id']): p for p in (built.get('post_event') or [])
                                      if isinstance(p, dict) and p.get('event_id')
                                      and (p.get('divergence') or p.get('research_recommended'))}
        except Exception as exc:  # an engine failure never stops the loop that observed it; logged once a minute per type
            name, clock = type(exc).__name__, time.monotonic()
            if clock - self.event_state_errors.get(name, -1e9) >= STREAM_ERROR_LOG_SECONDS:
                self.event_state_errors[name] = clock
                logger.error('event_state_failed', extra={'error_type': name})

    def pending_divergences(self) -> list[str]:
        """Diverging events (event_risk trigger_state divergences plus post-event entries with research_recommended) that
        have no research-planner request yet and were not attempted within divergence_research.retry_seconds."""
        config = refresh.divergence_config()
        ids = [str(i) for i in (self.event_state or {}).get('divergences') or []] + list(self.divergence_events)
        clock = time.monotonic()
        return [i for i in dict.fromkeys(ids) if i not in self.divergence_researched
                and clock - self.divergence_attempts.get(i, -1e9) >= config['retry_seconds']]

    def schedule_divergence_research(self) -> None:
        """Starts research_divergences in the background (observe() is synchronous and called by every loop)."""
        if not self.pending_divergences():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # outside the event loop (scripts, sync tests): the next observation inside the loop starts it
        if self.divergence_task is None or self.divergence_task.done():
            self.divergence_task = loop.create_task(self.research_divergences(), name='divergence_research')

    async def research_divergences(self) -> list[str]:
        """Post-event MARKET DIVERGENCE (docs/EVENT_AWARENESS.md §3): one research-planner request per diverging event
        through the existing corroborate path, so the planner-availability guard and the search budget apply. An event
        whose request could not start (no planner model, budget spent) is retried after retry_seconds while it still
        diverges; once started it is never requested again (a research_jobs marker keeps that across restarts and
        worker processes). Returns the event ids started now."""
        config = refresh.divergence_config()
        started: list[str] = []
        for event_id in self.pending_divergences():
            marker = f'divergence:{event_id}'
            if self.hub.store.get('research_jobs', marker):
                self.divergence_researched[event_id] = time.monotonic()
                continue
            self.divergence_attempts[event_id] = time.monotonic()
            entry = self.divergence_events.get(event_id) or {}
            facts = '; '.join(str(f) for f in entry.get('summary_facts') or [])
            summary = f"MARKET DIVERGENCE after {entry.get('name') or event_id}" + (f': {facts}' if facts else '')
            event: dict[str, Any] = {'id': marker, 'summary_fact': summary[:config['max_summary_chars']],
                                     'event_type': 'MARKET_DIVERGENCE', 'verification_status': 'UNCONFIRMED',
                                     'sources': [], 'topic': entry.get('event_class') or event_id}
            if await self.corroborate(event, priority=config['priority']):
                self.divergence_researched[event_id] = time.monotonic()
                self.hub.store.put('research_jobs', marker, {'kind': 'DIVERGENCE_RESEARCH', 'event_id': event_id,
                                                             'started_at': utcnow().isoformat()})
                started.append(event_id)
        for tracked in (self.divergence_researched, self.divergence_attempts):
            for old in sorted(tracked, key=tracked.__getitem__)[:max(0, len(tracked) - config['max_tracked'])]:
                del tracked[old]
        return started

    # --- analysis --------------------------------------------------------------------------------

    async def analysis_loop(self):
        first = True
        while True:
            try:
                # The first full analysis runs at startup; later ones on the cadence or an immediate trigger.
                reasons = [] if first else await self.recalc.next(refresh.next_interval('interpretation', utcnow()))
                first = False
                await self.analyze(reasons)
            except Exception as exc:
                logger.error('background_operation_failed', extra={'operation': 'interpretation', 'error_type': type(exc).__name__})
                await asyncio.sleep(refresh.cadence('derived')['seconds'])
            finally:
                self.analysis_started.set()
            raise_if_cancelling()

    async def interpretation_loop(self):
        """Market interpretation (backend.interpretation.refresh): after the first analysis (or startup_wait_seconds),
        then every interpretation.active_seconds while a traditional market is open, else idle_seconds, and at once on
        its triggers (debounced by min_interval_seconds). The interpretation skips unchanged inputs itself."""
        config = refresh.interpretation_config()
        try:
            await asyncio.wait_for(self.analysis_started.wait(), config['startup_wait_seconds'])
        except TimeoutError:
            pass
        first = True
        while True:
            try:
                if first:
                    # The startup run counts for the trigger debounce too: a trigger right after it (or after its
                    # failure) waits min_interval_seconds instead of sending another AI request at once.
                    reasons, self.interpret.last_run = [], self.interpret.clock()
                else:
                    reasons = await self.interpret.next(refresh.interpretation_interval(utcnow()))
                first = False
                await self.interpret_market(reasons)
            except Exception as exc:
                logger.error('background_operation_failed', extra={'operation': 'market_interpretation',
                                                                   'error_type': type(exc).__name__})
                await asyncio.sleep(refresh.cadence('derived')['seconds'])
            raise_if_cancelling()

    async def interpret_market(self, reasons: list[dict] | None = None) -> dict | None:
        """One interpretation refresh; None while backend.interpretation is not installed."""
        try:
            from backend import interpretation
            refresh_interpretation = interpretation.refresh
        except (ImportError, AttributeError):
            return None
        result = await refresh_interpretation(self.hub, list(reasons or []), utcnow())
        if reasons:
            self.publish('recalculations', {'reasons': reasons, 'at': utcnow().isoformat(), 'scope': 'interpretation'})
        return result

    async def analyze(self, reasons: list[dict] | None = None):
        self.cycle_started_at = utcnow()
        decisions = await self.hub.recalculate()
        now = utcnow()
        if reasons:
            self.hub.store.put('recalc_triggers', uid(), {'reasons': reasons, 'at': now.isoformat()})
            self.publish('recalculations', {'reasons': reasons, 'at': now.isoformat()})
        await self.alert([notifications.setup_alert(d, now) for d in decisions])
        from backend.evaluation import evaluate_pending
        async with self.hub.analysis_lock:  # never advance outcome cursors while decisions are being written
            await asyncio.to_thread(evaluate_pending, self.hub.store)

    async def morning_brief(self):
        now = utcnow()
        due = briefs.morning_due(now)
        if due and not self.hub.store.get('briefs', f'morning:{due}'):
            async with self.hub.analysis_lock:
                self.hub.publish_briefs(now)

    async def retention(self):
        await asyncio.to_thread(workers.run_retention, self.hub.store, utcnow())

    async def calibrate(self):
        """Daily shadow calibration candidates and the champion degradation monitor (same job as Celery's hub.calibrate).
        The last run is persisted, so restarts do not create an extra candidate set each time."""
        now = utcnow()
        last = (self.hub.store.get('recalc_triggers', 'calibration:last_run') or {}).get('at')
        if last and (now - aware(last)).total_seconds() < refresh.cadence('calibration')['seconds']:
            return
        await asyncio.to_thread(workers.calibrate_all, self.hub.store, now)
        self.hub.store.put('recalc_triggers', 'calibration:last_run', {'at': now.isoformat()})

    # --- market data -----------------------------------------------------------------------------

    def ingest_quotes(self, rows: list[dict]) -> int:
        """Validated quotes -> ticks; the stored payload is the whole Quote (source, as-of, basis and labels kept).
        A rejected row is logged with its symbol, provider and reason, never dropped silently."""
        count = 0
        for data in rows:
            try:
                quote = Quote.model_validate(data)
                if self.hub.ingest_quote(quote):
                    count += 1
                    self.publish('quotes', quote.model_dump(mode='json'))
            except ValueError as exc:
                row = data if isinstance(data, dict) else {}
                logger.warning('quote_rejected', extra={'symbol': str(row.get('symbol'))[:32],
                                                        'provider': str(row.get('provider'))[:32], 'reason': reject_reason(exc)})
        return count

    def store_proxy(self, source: str, row: dict) -> bool:
        """A proxy observation -> proxy_quotes '<source>:<symbol>' (docs/MARKET_COVERAGE.md), never a tick of the asset it
        stands in for. Only a later observation replaces the stored one; an invalid row is logged and dropped."""
        try:
            key, payload = proxies.payload(source, row)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning('proxy_quote_rejected', extra={'symbol': str(row.get('symbol'))[:32], 'provider': source,
                                                          'reason': reject_reason(exc)})
            return False
        if not proxies.newer(self.hub.store.get('proxy_quotes', key), aware(payload['timestamp'])):
            return False
        self.hub.store.put('proxy_quotes', key, payload)
        return True

    async def poll_market(self):
        if time.monotonic() - self.last_stream_tick > freshness_seconds('binance', self.hub.settings.quote_stale_seconds):
            for name in ['binance', 'coinbase']:
                fallback = self.hub.providers.get(name)
                if not usable(fallback):
                    continue
                try:
                    self.ingest_quotes(await fallback.quotes())
                    break
                except PROVIDER_ERRORS:
                    continue
        for provider in [p for p in self.hub.providers.values() if getattr(p, 'name', '') in {'massive', 'massive-stub'}]:
            if not usable(provider) or not provider.configured:
                continue
            for method in ('quotes', 'indices', 'fx_quotes'):
                try:
                    self.ingest_quotes(await getattr(provider, method)())
                except PROVIDER_ERRORS:
                    continue
        await self.poll_proxy_fallback()
        await self.save_health()

    async def poll_treasury_yields(self):
        """Official Treasury daily closes on their own cycle (refresh 'treasury_yields', hourly: the file changes once per
        business day), so a slow or failing Treasury server never stalls the 5 s quotes loop. The attempt is recorded
        before the call and the cadence applies to attempts; a failed or unreadable answer is logged and waits."""
        yields = self.hub.providers.get('treasury_yields')
        if not isinstance(yields, TreasuryYieldProvider) or not usable(yields):
            return
        self.last_run['treasury_yields_attempt'] = time.monotonic()
        self.ingest_quotes(await self._call(yields.quotes))
        await self.save_health()

    async def poll_proxy_fallback(self):
        """REST fallback for the streamed proxies (PAXG for gold): Binance, then Coinbase when Binance fails, only while
        the stream has not delivered one recently and at most once per proxy_rest_fallback seconds."""
        now = time.monotonic()
        if (now - self.proxy_stream_tick <= freshness_seconds('binance', self.hub.settings.quote_stale_seconds)
                or now - self.last_run.get('proxy_rest_fallback', -1e9) < refresh.cadence('proxy_rest_fallback')['seconds']):
            return
        self.last_run['proxy_rest_fallback'] = now
        for name, symbols in (('binance', binance_proxy_pairs()), ('coinbase', coinbase_proxy_products())):
            provider = self.hub.providers.get(name)
            if not symbols or not usable(provider) or not callable(getattr(provider, 'proxy_quotes', None)):
                continue
            try:
                rows = await provider.proxy_quotes(symbols)
            except PROVIDER_ERRORS as exc:
                logger.warning('provider_call_failed', extra={'operation': f'{name}.proxy_quotes', 'error_type': type(exc).__name__})
                continue
            for row in rows:
                self.store_proxy(name, row)
            if rows:
                return

    @staticmethod
    def stream_uri() -> str:
        pairs = dict.fromkeys([*CRYPTO_PAIRS, *binance_proxy_pairs()])
        return BINANCE_STREAM + '/'.join(f'{pair.lower()}@ticker' for pair in pairs)

    def handle_binance_message(self, message: str | bytes, binance: Any) -> dict | None:
        """One combined-stream ticker message. BTC/ETH become live quotes; any other stream is ignored. A configured proxy
        pair (PAXGUSDT) never becomes a tick of the asset it stands in for: it returns a proxy_quotes row for
        write_stream_proxy (the caller runs it off the event loop), at most once per proxy_stream seconds.
        The ticker also pushes when only the best bid/ask changes, so a proxy row exists only when the message shows a
        new trade (its last trade id or last price changed) and is stamped with that message's event time; later
        messages without a trade never refresh it. The first message of a pair after a (re)connect only sets that
        baseline (its last trade may be minutes old); until the stream shows a trade, the REST fallback, which carries
        the true last-trade time, stays in charge. Raises ValueError/KeyError/TypeError for a malformed message; the
        caller counts it and keeps the socket."""
        envelope = json.loads(message)
        data = envelope.get('data') if isinstance(envelope, dict) else None
        if not isinstance(data, dict) or 's' not in data:
            return None  # subscription replies and control frames
        pair = str(data['s'])
        stamp = datetime.fromtimestamp(data['E'] / 1000, UTC)
        symbol = CRYPTO_PAIRS.get(pair)
        if symbol is not None:
            quote = Quote(symbol=symbol, price=float(data['c']), timestamp=stamp, provider='binance', bid=float(data['b']),
                          ask=float(data['a']), change_pct=float(data['P']), change_basis='rolling_24h',
                          volume=float(data['v']), attribution='Source: Binance',
                          latency_ms=max(0, (utcnow() - stamp).total_seconds() * 1000))
            if self.hub.ingest_quote(quote):
                self.publish('quotes', quote.model_dump(mode='json'))
            self.last_stream_tick = time.monotonic()
            binance.health.update(status='ONLINE', error=None, last_success=utcnow().isoformat(), latency_ms=quote.latency_ms)
            return None
        proxy = binance_proxy_pairs().get(pair)
        if proxy is None:
            return None  # a stream the app did not ask for: ignored, never an error
        key = proxies.key('binance', proxy)
        price, change = float(data['c']), float(data['P'])
        trade = data.get('L')
        marker = (str(trade) if trade is not None else None, price)
        previous = self.proxy_trades.get(key)
        self.proxy_trades[key] = marker
        now = time.monotonic()
        if previous is not None and previous != marker:
            self.proxy_stream_tick = now
            self.proxy_pending[key] = {'symbol': proxy, 'price': price, 'timestamp': stamp.isoformat(), 'change_pct': change,
                                       'change_basis': 'rolling_24h',
                                       'delay_label': 'real-time (Binance WebSocket; the token trades 24/7)',
                                       'attribution': 'Source: Binance'}
        if key not in self.proxy_pending or now - self.proxy_written.get(key, -1e9) < refresh.cadence('proxy_stream')['seconds']:
            return None
        return self.proxy_pending.pop(key)

    def write_stream_proxy(self, row: dict) -> bool:
        """Stores a proxy row returned by handle_binance_message (blocking database work: run it in a thread)."""
        if not self.store_proxy('binance', row):
            return False
        self.proxy_written[proxies.key('binance', row['symbol'])] = time.monotonic()
        return True

    def stream_failure(self, exc: Exception) -> None:
        """A failure that is not the message's fault (a locked database, a bug): logged by type at most once per
        STREAM_ERROR_LOG_SECONDS per type; the socket and the task stay up."""
        name, now = type(exc).__name__, time.monotonic()
        if now - self.stream_errors.get(name, -1e9) >= STREAM_ERROR_LOG_SECONDS:
            self.stream_errors[name] = now
            logger.error('stream_message_failed', extra={'provider': 'binance', 'error_type': name})

    async def crypto_stream(self):
        """Binance combined stream for BTC/ETH and the proxy pairs. Runs for the life of the process: a bad message is
        counted, any other failure is logged, and a lost connection is retried with backoff (up to 60 s)."""
        backoff = 1
        while True:
            if not usable(self.hub.providers.get('binance')):  # switched off: no connection, no health writes
                await asyncio.sleep(STREAM_RECHECK_SECONDS)
                continue
            try:
                async with websockets.connect(self.stream_uri(), ping_interval=20, ping_timeout=20, open_timeout=15,
                                              max_size=100000) as ws:
                    backoff = 1
                    self.proxy_trades.clear()  # trades may have happened while disconnected: take a new baseline
                    async for message in ws:
                        binance = self.hub.providers.get('binance')
                        if not usable(binance):
                            break  # switched off while streaming: close the connection
                        try:
                            row = self.handle_binance_message(message, binance)
                            if row is not None:
                                await asyncio.to_thread(self.write_stream_proxy, row)
                        except (ValueError, KeyError, TypeError, OverflowError) as exc:
                            # One bad message never drops the socket: BTC/ETH keep streaming.
                            binance.health['rejected_rows'] = binance.health.get('rejected_rows', 0) + 1
                            logger.warning('stream_message_rejected', extra={'provider': 'binance', 'reason': reject_reason(exc)})
                        except Exception as exc:  # e.g. a locked database: never the end of the stream
                            self.stream_failure(exc)
            except Exception as exc:  # cancellation (BaseException) still stops the task
                binance = self.hub.providers.get('binance')
                if not usable(binance):
                    continue  # switched off meanwhile: the next pass waits quietly
                logger.warning('crypto_stream_reconnecting', extra={'error_type': type(exc).__name__})
                binance.health.update(status='DEGRADED', error='WebSocket disconnected; REST fallback active')
                raise_if_cancelling()
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)

    async def derivatives(self):
        provider = self.hub.providers.get('deribit')
        if not usable(provider):
            return
        for currency in rules()['provider_routing']['deribit']['currencies']:
            stamp = utcnow().isoformat()
            rows = await provider.metrics(currency) + await provider.futures(currency)
            # ~1,000 option instruments per currency: one transaction, not one commit per row.
            self.hub.store.put_many('derivative_metrics', {
                m['instrument_name']: dict(m, currency=currency, provider='deribit', ingested_at=stamp)
                for m in rows if m.get('instrument_name')})
        await self.save_health()

    async def poll_equities(self):
        """Keyed real-time equity quotes (Finnhub /quote, consolidated). ETF proxies (SPY/QQQ/IWM) keep their own tickers
        and go to proxy_quotes as 'finnhub:<SYM>', never onto index symbols. Equities Finnhub could not serve (not
        configured, switched off, failed) are left to the Alpaca IEX fallback; symbols merely deferred by the rate guard
        are not (they are served first next round)."""
        from backend.providers.finnhub import quote_fields
        equities = equity_symbols()
        provider = self.hub.providers.get('finnhub')
        if not usable(provider) or not provider.configured:
            self.finnhub_missing = equities
            return
        etfs = proxies.specs('finnhub')
        try:
            records = await provider.quotes(only=equities | set(etfs))
        except PROVIDER_ERRORS:
            self.finnhub_missing = equities
            raise
        failed = provider.health.get('status') in {'AUTH_ERROR', 'RATE_LIMITED', 'OFFLINE', 'DEGRADED'}
        deferred = set() if failed else set(provider.health.get('rate_guard_skipped') or [])
        self.finnhub_missing = equities - {r.get('symbol') for r in records} - deferred
        self.ingest_quotes([quote_fields(r) for r in records if r.get('symbol') in equities])
        for record in records:
            if record.get('symbol') in etfs:
                self.store_proxy('finnhub', dict(record, delay_label=record.get('delay')))

    async def poll_alpaca_quotes(self):
        """Alpaca IEX snapshots, one call per round: the ETF proxies coverage.json assigns to Alpaca (proxy_quotes
        'alpaca:<SYM>') and, only as a fallback, the tracked equities Finnhub could not serve (labelled IEX, feed iex)."""
        from backend.providers.finnhub import quote_fields
        alpaca = self.hub.providers.get('alpaca')
        if not usable(alpaca) or not alpaca.configured:
            return
        etfs = proxies.specs('alpaca')
        finnhub = self.hub.providers.get('finnhub')
        equities = equity_symbols() if not usable(finnhub) or not finnhub.configured else self.finnhub_missing & equity_symbols()
        try:
            records = await alpaca.snapshots(sorted(equities | set(etfs)))
        except PROVIDER_ERRORS as exc:
            logger.warning('provider_call_failed', extra={'operation': 'alpaca.snapshots', 'error_type': type(exc).__name__})
            records = []
        self.ingest_quotes([quote_fields(r) for r in records if r.get('symbol') in equities])
        for record in records:
            if record.get('symbol') in etfs:
                self.store_proxy('alpaca', record)
        await self.save_health()

    async def poll_alpaca_bars(self):
        """Daily bars (IEX feed, split-adjusted) for the tracked equities and the ETF proxies, stored as Bar rows with
        interval 86400 starting at the New York session date 00:00 (history for previous day/week and SMA20/50). The
        first successful run loads daily_bars.days of history; later runs refresh the last refresh_days."""
        alpaca = self.hub.providers.get('alpaca')
        if not usable(alpaca) or not alpaca.configured:
            return
        config = rules()['provider_routing']['alpaca']['daily_bars']
        days = config['refresh_days'] if self.daily_bars_loaded else config['days']
        symbols = sorted(equity_symbols() | set(config['extra_symbols']))
        try:
            rows = await alpaca.daily_bars(symbols, (utcnow() - timedelta(days=days)).date())
        except PROVIDER_ERRORS as exc:
            logger.warning('provider_call_failed', extra={'operation': 'alpaca.daily_bars', 'error_type': type(exc).__name__})
            rows = []
        if rows:
            await asyncio.to_thread(self.store_daily_bars, rows)
            self.daily_bars_loaded = True
        await self.save_health()

    def store_daily_bars(self, rows: list[dict]) -> int:
        """Upsert daily Bar rows (interval 86400) in one transaction; the natural key is (symbol, interval, start)."""
        by_symbol: dict[str, dict[datetime, dict]] = {}
        for row in rows:
            if row.get('interval_seconds') == DAY_SECONDS:
                by_symbol.setdefault(row['symbol'], {})[as_utc(row['start'])] = row
        written = 0
        with self.hub.store.session() as session:
            for symbol, items in by_symbol.items():
                existing = {as_utc(bar.start): bar for bar in session.scalars(select(Bar).where(
                    Bar.symbol == symbol, Bar.interval_seconds == DAY_SECONDS, Bar.start.in_(list(items))))}
                for start, item in items.items():
                    values = {k: item[k] for k in ('open', 'high', 'low', 'close', 'volume', 'ticks', 'provider')}
                    bar = existing.get(start)
                    if bar is None:
                        session.add(Bar(symbol=symbol, interval_seconds=DAY_SECONDS, start=start, **values))
                    else:
                        for key, value in values.items():
                            setattr(bar, key, value)
                    written += 1
            session.commit()
        return written

    async def poll_alpaca_news(self):
        """Benzinga headlines and summaries via the Alpaca News API, through the normal document pipeline (source
        'benzinga', tier 4 specialist). Never the full article body."""
        alpaca = self.hub.providers.get('alpaca')
        if not usable(alpaca) or not alpaca.configured:
            return
        await self.ingest_from(alpaca)
        self.observe()

    async def poll_eia_spot(self):
        """EIA daily spot closes for WTI and Brent (keyless). The page changes weekly; the provider sends
        If-Modified-Since, so an unchanged page costs a 304."""
        provider = self.hub.providers.get('eia_spot')
        if not usable(provider):
            return
        self.ingest_quotes(await self._call(provider.quotes))
        await self.save_health()

    async def poll_positioning(self):
        """Descriptive futures positioning: venue funding/basis/OI (real time) and CFTC COT (weekly, cached)."""
        from backend.providers.registry import by_method
        batch: dict[str, dict] = {}
        for provider in by_method(self.hub.providers, 'positioning'):
            try:
                records = await provider.positioning()
            except PROVIDER_ERRORS:
                continue
            for record in records:
                key = ':'.join(str(record.get(k)) for k in ('provider', 'contract_code', 'report', 'instrument', 'as_of_date', 'as_of')
                               if record.get(k) is not None)
                batch[key] = record
        self.hub.store.put_many('positioning_reports', batch)
        await self.save_health()

    async def poll_official(self):
        """Official statistics and central-bank state. Everything is labelled with its as-of and delay; nothing is live."""
        from backend.providers.official_data import build_central_bank_state, fred_quotes
        providers = self.hub.providers
        ecb = providers.get('ecb_data') if usable(providers.get('ecb_data')) else None
        fred = providers.get('fred') if usable(providers.get('fred')) else None
        records: list[dict] = []
        calls: list[Callable[[], Awaitable[Any]]] = []
        if ecb is not None:
            self.ingest_quotes(await self._call(ecb.quotes))  # ECB reference rates: daily fix, labelled LAST CLOSE
            calls += [ecb.policy_rates, ecb.yields, ecb.hicp]
        for name, method in (('eurostat', 'hicp_flash'), ('treasury_fiscal', 'tga'), ('eia_wpsr', 'inventories')):
            if usable(providers.get(name)):
                calls.append(getattr(providers[name], method))
        # Unregistered BLS allows 20 queries/day: one batched call at most every BLS_MIN_SECONDS.
        if usable(providers.get('bls_data')) and time.monotonic() - self.last_run.get('bls_data', -1e9) >= BLS_MIN_SECONDS:
            calls.append(providers['bls_data'].series)
            self.last_run['bls_data'] = time.monotonic()
        for call in calls:
            records += await self._call(call)
        if fred is not None and fred.configured:
            latest = await self._call(fred.latest)
            records += latest
            # SP500 / NASDAQ / VIX / WTI / BRENT daily closes (fred.quote_symbols), labelled with the owner's notice.
            self.ingest_quotes(fred_quotes(latest))
            credit = await self._call(lambda: fred.observations('BAA10Y', observation_start=(utcnow() - timedelta(days=10)).date().isoformat()))
            if len(credit) >= 2:
                last, previous = credit[-1], credit[-2]
                self.hub.store.put('credit_spreads', 'BAA10Y', {
                    'name': 'BAA10Y', 'value': last['value'], 'change_bp': round((last['value'] - previous['value']) * 100, 2),
                    'as_of': last['as_of'], 'delay': last.get('delay'), 'attribution': last.get('attribution'),
                    # Daily EOD series: fresh while it is the latest published observation.
                    'freshness_status': 'FRESH' if (utcnow() - aware(last['as_of'])).days <= 4 else 'STALE'})
        self.store_series(records)
        await self.attach_releases()
        state = await build_central_bank_state(ecb, fred, self.hub.quote('US2Y'))
        if 'rate' not in state.get('USD', {}) and usable(providers.get('nyfed_rates')):
            # Without FRED, the official target range comes from the New York Fed (public, T+1).
            nyfed = await self._call(providers['nyfed_rates'].policy_rate, default={})
            if nyfed:
                missing = [m for m in state.get('USD', {}).get('missing', []) if m not in {'policy_rate', 'rate', 'as_of', 'verified'}]
                state['USD'] = dict(state.get('USD', {}), **nyfed, missing=missing)
                state['USD'].pop('reason', None)
        banks = rules()['decision']['fx_banks']
        # 'bank' is the fx_banks id the readers key on; the source's display name ('Fed', 'ECB') is kept as bank_name.
        self.hub.store.put_many('central_bank_state', {banks[ccy]: dict(row, bank=banks[ccy], bank_name=row.get('bank'),
                                                                        currency=ccy)
                                                       for ccy, row in state.items() if ccy in banks})
        await self.save_health()

    def store_series(self, records: list[dict]) -> int:
        """official_series rows keyed '<provider>:<series>:<period>'. An existing period keeps its created_at (the time
        the app first stored it), which is the no-lookahead 'known since' of a release outcome."""
        items = {f"{r['provider']}:{r['series']}:{r.get('period')}": r for r in records if r.get('series')}
        self.hub.store.put_many('official_series', items)
        return len(items)

    async def attach_releases(self) -> list[dict]:
        """EVR-4: attaches the outcome read from the stored official series to the calendar rows of mapped releases
        (backend/releases.attach), then alerts (MACRO_RELEASE) and observes the rows attached now, so the decisions and
        the interpretation refresh on the MACRO_RELEASE trigger. Consensus stays as the calendar has it: none is invented."""
        now = utcnow()
        rows = self.hub.calendar()  # the whole stored calendar: releases.attach keeps the rows inside its window
        attached = await asyncio.to_thread(releases.attach, self.hub.store, rows, now)
        if attached:
            logger.info('release_outcome_attached', extra={'count': len(attached),
                                                           'classes': ','.join(str(r.get('event_class')) for r in attached)})
            await self.alert([notifications.macro_release_alert(row, now) for row in attached])
            self.observe()
        return attached

    async def poll_release_outcomes(self, watched: list[dict]) -> list[dict]:
        """Targeted official-series poll right after a mapped release (official_data.release_poll): for each watched
        calendar row past its scheduled time without an actual, the providers serving its class series are polled at
        most max_polls times, poll_seconds apart, then the outcome is attached. A targeted BLS call moves the regular
        BLS poll back by BLS_MIN_SECONDS (one daily budget)."""
        config = rules()['official_data']['release_poll']
        rows = {str(r.get('id')): r for r in self.hub.calendar(window=True)}
        due: list[dict] = []
        for item in watched:
            row = rows.get(str(item.get('id')))
            if not row or item.get('phase') != 'AWAITING_ACTUAL' or not releases.mapped(row) \
                    or row.get('actual') is not None or row.get('release_outcome'):
                continue
            attempts = self.release_polls.setdefault(str(row['id']), [])
            if len(attempts) >= config['max_polls'] or (attempts and time.monotonic() - attempts[-1] < config['poll_seconds']):
                continue
            attempts.append(time.monotonic())
            due.append(row)
        if not due:
            return []
        records: list[dict] = []
        for name, method in releases.providers_for([str(r.get('event_class')) for r in due]):
            provider = self.hub.providers.get(name)
            if not usable(provider) or not getattr(provider, 'configured', True):
                continue
            records += await self._call(getattr(provider, method))
            if name == 'bls_data':
                self.last_run['bls_data'] = time.monotonic()
        self.store_series(records)
        return await self.attach_releases()

    async def poll_discovery(self):
        """Discovery-only candidates (GDELT) are stored apart; official sanctions/filings become Tier-1 documents."""
        gdelt = self.hub.providers.get('gdelt_events')
        batch = await self._call(gdelt.batch, default={}) if usable(gdelt) else {}
        if batch.get('status') in {'OK', 'OK_FALLBACK'}:
            self.hub.store.put_many('discovery_items', {c['candidate_id']: c for c in batch.get('candidates', [])})
            self.hub.store.put('discovery_items', f"gdelt-intensity:{batch['batch_id']}",
                               {'kind': 'GDELT_INTENSITY', 'as_of': batch.get('as_of'), 'intensity': batch.get('intensity', {}),
                                'attribution': batch.get('attribution'), 'delay': batch.get('delay')})
        for name in ('ofac_actions', 'ofac_sls', 'sec_fulltext'):
            provider = self.hub.providers.get(name)
            if usable(provider) and provider.configured:
                await self.ingest_from(provider)
        await self.save_health()
        self.observe()

    async def _call(self, call: Callable[[], Awaitable[Any]], default: Any = None) -> Any:
        owner = getattr(call, '__self__', None)
        if owner is not None and not usable(owner):  # a bound method of a switched-off provider
            return [] if default is None else default
        try:
            return await call()
        except PROVIDER_ERRORS as exc:
            logger.warning('provider_call_failed', extra={'operation': getattr(call, '__name__', 'call'), 'error_type': type(exc).__name__})
            return [] if default is None else default

    # --- news, official releases, social ---------------------------------------------------------

    async def ingest_from(self, provider: Any) -> list[dict]:
        """Validated documents -> events (deduplicated), enrichment, stream publication. Returns touched events."""
        touched: list[dict] = []
        if not usable(provider):
            return touched
        try:
            documents = await provider.documents()
        except PROVIDER_ERRORS as exc:
            logger.warning('news_ingestion_failed', extra={'provider': provider.name, 'error_type': type(exc).__name__})
            return touched
        for data in documents:
            try:
                document = DocumentInput.model_validate(data)
                # Deduplication and verification are CPU/DB work: keep the event loop free for quotes and the API.
                event = await asyncio.to_thread(self.hub.ingest_document, document, trusted_transport=not document.discovery_only)
            except ValueError:
                continue
            await self.hub.enrich_event(event['id'])
            touched.append(event)
            self.publish('events', {'id': event['id'], 'status': event['verification_status'], 'summary': event['summary_fact']})
        return touched

    def _feeds(self, kinds: set[str] | None = None, exclude: set[str] | None = None) -> list[Any]:
        sources = {s['id']: s for s in rules()['sources']}
        result = []
        for provider in by_capability(self.hub.providers, 'news'):
            if not isinstance(provider, RSSProvider):
                continue
            kind = sources.get(provider.source['id'], {}).get('kind')
            if (kinds is None or kind in kinds) and (exclude is None or kind not in exclude):
                result.append(provider)
        return result

    async def poll_news(self):
        """Global sweep of official feeds except central banks (their own adaptive loop)."""
        for provider in self._feeds(exclude={'central_bank'}):
            await self.ingest_from(provider)
        await self.save_health()
        self.observe()

    async def poll_central_banks(self):
        for provider in self._feeds(kinds={'central_bank'}):
            await self.ingest_from(provider)
        self.observe()

    async def poll_breaking(self):
        for provider in self.hub.providers.values():
            if isinstance(provider, (LicensedNewsProvider, LicensedNewsStub)) and usable(provider) and provider.configured:
                await self.ingest_from(provider)
        self.observe()

    async def poll_sec(self):
        provider = self.hub.providers.get('sec_edgar')
        if isinstance(provider, SECProvider) and usable(provider) and provider.configured:
            await self.ingest_from(provider)

    async def poll_congress(self):
        congress = self.hub.providers.get('congress')
        if usable(congress):
            await self.ingest_from(congress)
        await self.refresh_calendar(names={'congress_api', 'congress_api-stub'})

    async def poll_release_sources(self):
        """Release watch: poll the authoritative statistical sources and refresh actuals around major releases, and
        right after a mapped release the official series that publish its outcome (poll_release_outcomes)."""
        state = refresh.watch_state(utcnow(), self.hub.calendar(window=True))
        if not state['active']:
            return
        for provider in self._feeds(kinds={'official_statistics'}):
            await self.ingest_from(provider)
        await self.refresh_calendar(names={'calendar', 'calendar-stub'})
        await self.poll_release_outcomes(state['events'])
        self.observe()

    async def social(self):
        provider = self.hub.providers.get('x') or self.hub.providers.get('x-stub')
        if not usable(provider) or not provider.configured:
            return
        for post in await provider.search(x_watch_query()):
            source_id = x_source_for(post.get('author_username'))
            official = next((s.get('official') for s in rules()['sources'] if s['id'] == source_id), False)
            url = f"https://x.com/{post.get('author_username') or 'i'}/status/{post['id']}"
            document = DocumentInput(source_id=source_id if not post.get('stub') else 'dev-stub',
                                     title=post['text'][:1000] if len(post['text']) >= 5 else f"X post {post['id']}",
                                     content=post['text'], url=url if not post.get('stub') else f"https://example.invalid/x/{post['id']}",
                                     published_at=post['created_at'], event_type='SOCIAL_DISCOVERY',
                                     # Unknown accounts are discovery only; official accounts are official statements.
                                     discovery_only=not official, direct=True)
            try:
                event = self.hub.ingest_document(document, trusted_transport=bool(official))
            except ValueError:
                continue
            self.hub.store.put('social_posts', post['id'], dict(post, source_id=source_id, official=bool(official),
                                                                 event_id=event['id'], url=None if post.get('stub') else url))
            if not official and event['verification_status'] != 'CONFIRMED':
                await self.research_event(event, priority=1)
        await self.save_health()
        self.observe()

    # --- research attempts (one research = one research-planner AI request; docs/AI_AGENTS.md) ---------------------

    def research_due(self, key: str, evidence: int | None = None, *, anomaly: bool = False) -> tuple[bool, bool]:
        """(due, fresh_evidence) for research on `key` (an event id, or 'anomaly:<symbol>:<window>'), from the stored
        attempts (research_jobs 'attempts:<key>', so restarts and worker processes share them). An event is researched
        at most refresh.hot_topics.max_attempts_per_event times, and not again within recheck_hours unless it received
        new evidence since the last attempt (fresh_evidence, which also waives the search-budget topic cooldown); an
        anomaly at most once per anomaly_recheck_hours."""
        config = rules()['refresh']['hot_topics']
        state = self.hub.store.get('research_jobs', f'attempts:{key}')
        if not state or not state.get('last_attempt_at'):
            return True, False
        hours = (utcnow() - aware(state['last_attempt_at'])).total_seconds() / 3600
        if anomaly:
            return hours >= config['anomaly_recheck_hours'], False
        if int(state.get('attempts') or 0) >= config['max_attempts_per_event']:
            return False, False
        fresh = evidence is not None and evidence > int(state.get('evidence') or 0)
        return hours >= config['recheck_hours'] or fresh, fresh and hours < config['recheck_hours']

    def record_research(self, key: str, evidence: int | None = None) -> None:
        state = self.hub.store.get('research_jobs', f'attempts:{key}') or {}
        now = utcnow().isoformat()
        self.hub.store.put('research_jobs', f'attempts:{key}', {
            'kind': 'RESEARCH_ATTEMPTS', 'key': key, 'attempts': int(state.get('attempts') or 0) + 1,
            'evidence': evidence if evidence is not None else state.get('evidence'),
            'first_attempt_at': state.get('first_attempt_at') or now, 'last_attempt_at': now})

    async def research_event(self, event: dict, priority: int) -> bool:
        """corroborate() for a news or X event within its attempt limits (research_due); a started request is
        recorded. Returns whether it started."""
        evidence = len(event.get('evidence') or [])
        due, fresh = self.research_due(event['id'], evidence)
        if not due:
            return False
        if fresh:
            started = await self.corroborate(event, priority=priority, fresh_evidence=True)
        else:
            started = await self.corroborate(event, priority=priority)
        if started:
            self.record_research(event['id'], evidence)
        return bool(started)

    async def corroborate(self, event: dict, priority: int, *, fresh_evidence: bool = False) -> bool:
        """Unverified claim -> research plan -> bounded searches -> poll the registered sources the results point to.
        Search hits never confirm anything; only a document retrieved from a registered source can (§8, §9).
        Returns whether the request started (planner available, within the daily AI budget, and a search-budget slot
        acquired). fresh_evidence waives the topic cooldown (new evidence since the last research)."""
        if (not usable(self.hub.ollama) or not getattr(self.hub.ollama, 'configured', False)
                or not self.hub.ai.allowed('research_planner')):
            # No planner model, Ollama switched off or the planner's daily budget spent: a search-budget slot (and
            # one of the event's research attempts) would buy nothing.
            return False
        if fresh_evidence:
            acquired = self.hub.search_budget.acquire(event['id'], priority, fresh_evidence=True)
        else:
            acquired = self.hub.search_budget.acquire(event['id'], priority)
        if not acquired:
            return False
        recent = [{'summary': e['summary_fact'], 'status': e['verification_status']} for e in self.hub.events(10)]
        moves = {a['symbol']: a['quote'].get('change_pct') for a in self.plain_assets() if a.get('quote')}
        plan, run_id = await self.hub.ai.run_with_id('research_planner', {
            'event': {k: event.get(k) for k in ('id', 'summary_fact', 'event_type', 'verification_status', 'sources', 'topic')},
            'asset_moves': moves, 'recent_events': recent, 'monitored_topics': rules()['x_monitoring']['keywords'],
            'evidence_ids': []})
        if not plan:
            return True
        domains = {s['domain']: s['id'] for s in rules()['sources'] if s.get('tier', 5) <= 2 and s['domain'] != 'x.com'}
        leads = set()
        for query in bound_queries(plan['queries']):
            try:
                results = await self.hub.ollama.search(query)
            except PROVIDER_ERRORS:
                continue
            self.hub.store.put('research_jobs', uid(), {'event_id': event['id'], 'query': query, 'results': results,
                                                         'discovery_only': True, 'plan_run_id': run_id, 'created_at': utcnow().isoformat()})
            for result in results:
                host = urlsplit(result['url']).hostname or ''
                leads |= {sid for domain, sid in domains.items() if host == domain or host.endswith('.' + domain)}
        with self.hub.store.session() as session:
            row = session.get(Event, event['id'])
            if row:
                row.payload = dict(row.payload, corroboration_leads=sorted(leads), corroboration_checked_at=utcnow().isoformat())
                session.commit()
        for provider in self._feeds():
            if provider.source['id'] in leads:
                await self.ingest_from(provider)
        for name in leads & {'reuters', 'ap'}:
            provider = self.hub.providers.get(name)
            if usable(provider) and provider.configured:
                await self.ingest_from(provider)
        return True

    async def research_hot_topics(self):
        """Unresolved important events are researched on the hot-topic cycle within their attempt limits
        (research_event: at most max_attempts_per_event times, recheck_hours apart unless new evidence arrived) and
        the search budget."""
        config = rules()
        minimum = config['brief']['min_event_importance'] / 100
        urgent = config['refresh']['hot_topics']['urgent_min_importance']
        horizon = utcnow() - timedelta(hours=config['decision']['reaction_event_age_hours'])
        candidates = [event for event in self.hub.events(50)
                      if event['verification_status'] in {'UNCONFIRMED', 'PARTIALLY_CONFIRMED'}
                      and (event.get('importance') or 0) >= minimum and aware(event['first_seen_utc']) >= horizon]
        # The most important first: the planner's daily cap and pace go to them, not to the order of arrival.
        for event in sorted(candidates, key=lambda e: e.get('importance') or 0, reverse=True):
            await self.research_event(event, priority=0 if (event.get('importance') or 0) >= urgent else 1)

    async def discover(self):
        if usable(self.hub.ai.provider):  # a switched-off LLM vendor's catalog is not read either
            try:
                await self.hub.ai.discover()
            except (ProviderUnavailable, ValueError):
                pass
        await self.save_health()

    # --- calendar and macro releases -------------------------------------------------------------

    def calendar_due(self, provider: Any) -> bool:
        """Sources listed in refresh.calendar_sources.min_seconds are fetched at most that often (daily schedules);
        every other source on each calendar run."""
        minimum = rules()['refresh']['calendar_sources']['min_seconds'].get(provider.name)
        last = self.calendar_fetched.get(provider.name)
        return minimum is None or last is None or time.monotonic() - last >= minimum

    def primary_usable(self, name: str) -> bool:
        """A fallback source runs only while its primary is missing, switched off, not configured or failing."""
        primary = self.hub.providers.get(name)
        return usable(primary) and bool(getattr(primary, 'configured', True)) and self.calendar_ok.get(name, True)

    async def fetch_calendars(self, start: str, end: str, names: set[str] | None) -> dict[str, tuple[Any, list[dict]]]:
        """provider name -> (provider, rows) for every source that answered this run. A superseded fallback answers
        with no rows, so its stored rows are replaced once its primary is back."""
        providers = [p for p in by_capability(self.hub.providers, 'calendar') if names is None or p.name in names]
        results: dict[str, tuple[Any, list[dict]]] = {}
        for provider in sorted(providers, key=lambda p: bool(getattr(p, 'fallback_for', None))):
            primary = getattr(provider, 'fallback_for', None)
            if primary and self.primary_usable(primary):
                if primary in results:
                    results[provider.name] = (provider, [])
                continue
            if not self.calendar_due(provider):
                continue
            try:
                fetched = await provider.calendar(start, end)
                rows = [r if 'category' in r else next(iter(fmp_economic_rows([r], provider.name))) for r in fetched]
                if hasattr(provider, 'earnings'):
                    watch = {symbol for symbol, _, kind in rules()['assets'] if kind == 'equity'}
                    earnings = await provider.earnings(start, end)
                    rows += earnings if is_stub(provider) else fmp_earnings_rows(earnings, watch, provider.name)
            except PROVIDER_ERRORS as exc:
                self.calendar_ok[provider.name] = False
                logger.warning('calendar_failed', extra={'provider': provider.name, 'error_type': type(exc).__name__})
                continue
            self.calendar_ok[provider.name] = True
            self.calendar_fetched[provider.name] = time.monotonic()
            results[provider.name] = (provider, rows)
        return results

    def delete_calendar_rows(self, ids: list[str]) -> None:
        if not ids:
            return
        table = RECORDS['macro_calendar']
        with self.hub.store.session() as session:
            session.execute(delete(table).where(table.id.in_(ids)))
            session.commit()

    async def refresh_calendar(self, names: set[str] | None = None) -> int:
        """Fetch the calendar sources, keep first-seen/first-actual times, record macro surprises, then apply the
        calendar hygiene (docs/EVENT_AWARENESS.md §1): a source that answered replaces its stored rows inside the fetch
        window (rescheduled or cancelled releases disappear), and the same release from two sources is kept once."""
        today = utcnow().date()
        first, last = today - timedelta(days=1), today + timedelta(days=rules()['provider_routing']['calendars']['fetch_days'])
        start, end = first.isoformat(), last.isoformat()
        results = await self.fetch_calendars(start, end, names)
        now = utcnow()
        stored = {str(r['id']): r for r in self.hub.store.list('macro_calendar', CALENDAR_SCAN_ROWS)}
        # Vendor metric rows merged into an official row are not stored themselves: their first-actual times live on
        # in the kept row's metrics.
        merged_metrics = {str(m['row_id']): m for r in stored.values() for m in r.get('metrics') or []
                          if isinstance(m, dict) and m.get('row_id')}
        batch: dict[str, dict] = {}
        for _provider, rows in results.values():
            for row in rows:
                key = row.get('id') or calendar_key(row)
                previous = stored.get(key) or merged_metrics.get(key) or {}
                # First-seen and first-actual times are never overwritten (no-lookahead replays, spec §38). A row
                # re-fetched without values keeps what an earlier dedupe lent to it (its vendor rows are gone).
                merged = dict(carry_lent(previous, row), id=key, ingested_at=previous.get('ingested_at', now.isoformat()))
                if merged.get('actual') is not None:
                    merged['actual_ingested_at'] = previous.get('actual_ingested_at') or now.isoformat()
                    # Lent values were recorded under the vendor's own metric names when their rows were fetched.
                    if merged.get('category') == 'MACRO' and not merged.get('stub') and not merged.get('values_source'):
                        macro.record_release(self.hub.store, merged, now)
                        result = macro.release_surprise(self.hub.store, merged, now)
                        if result:
                            merged.update(raw_surprise=result['raw_surprise'], z_score=result['z_score'],
                                          surprise_z_score=result['z_score'], surprise_observations=result['observations'])
                            merged['trigger'] = (causal.macro_trigger(merged) or {}).get('trigger')
                if key in batch:  # the same id from two sources (same name and time): the better row is kept
                    kept, _ = dedupe([batch[key], merged])
                    merged = kept[0] if len(kept) == 1 else merged
                batch[key] = merged
        # Release alerts name the metric that was published: a vendor row merged into the official one below still
        # alerts under its own name ('Core CPI MoM'), and values lent to the official row never alert twice.
        released = [row for row in batch.values() if not row.get('values_source')]
        # Replacement: a source that answered owns its window; rows it no longer lists are removed (unless another
        # source lists the same id this run).
        removed: set[str] = set()
        for name, (provider, rows) in results.items():
            ids = {str(r.get('id') or calendar_key(r)) for r in rows}
            scope = getattr(provider, 'replace_scope', None)
            removed |= set(stale_ids(stored.values(), name, ids, first, last, scope if callable(scope) else None))
        removed -= set(batch)
        # Dedupe across sources over the window (stored rows of sources not fetched this run included).
        window = {key: row for key, row in stored.items() if key not in removed}
        window.update(batch)
        config = rules()['provider_routing']['calendars']
        kept, dropped = dedupe(row for row in window.values() if first <= (local_date(row, config) or first) <= last)
        removed |= {str(r['id']) for r in dropped}
        for row in kept:
            key = str(row['id'])
            if key in batch or row != stored.get(key):
                batch[key] = row
        batch = {key: row for key, row in batch.items() if key not in removed}
        self.delete_calendar_rows(sorted(removed))
        self.hub.store.put_many('macro_calendar', batch)
        await self.alert([notifications.macro_release_alert(r, now) for r in released])
        return len(batch)

    async def calendar(self):
        await self.refresh_calendar()
        await self.save_health()
        self.observe()

    # --- derived metrics -------------------------------------------------------------------------

    async def derived(self):
        now = utcnow()
        events = self.hub.events()
        context = causal.build_context(None, events, now)
        for event in events:
            event['causal_paths'] = causal.event_paths(event, now, context)
        await asyncio.to_thread(analytics.capture_reactions, self.hub.store, events, self.hub.settings.quote_stale_seconds)
        series = {}
        for symbol, _, _ in rules()['assets']:
            anomaly_rules = analytics.anomaly_rules(symbol)
            if anomaly_rules:
                lookback = max(r['window_seconds'] for r in anomaly_rules)
                series[symbol] = analytics.recent_prices(self.hub.store, symbol, now, lookback)
        found = analytics.scan_anomalies(series, now, events=events, releases=self.hub.calendar(window=True))
        assets = self.plain_assets()
        alerts: list[dict | None] = [notifications.anomaly_alert(item['symbol'], item, now) for item in found if item['triggered']]
        market = notifications.market_alerts(self.hub.store, assets, now)
        # Kept between cycles: every loop's observe() compares against the latest divergence set, not an empty one.
        self.divergences = sorted(a['dedupe_key'] for a in market if a['kind'] == 'DIVERGENCE')
        alerts += market
        # AFP-04: EVENT alerts for newly confirmed events with an assessed importance and an identified asset impact,
        # once per event (notifications.event_alerts; the ids already dispatched are not rebuilt every cycle).
        fresh_events = notifications.event_alerts([e for e in events if str(e.get('id')) not in self.event_alerted], now)
        if len(self.event_alerted) >= EVENT_ALERTED_MAX:
            self.event_alerted.clear()
        self.event_alerted.update(str(a['event_id']) for a in fresh_events)
        alerts += fresh_events
        await self.alert(alerts)
        for item in found:
            if item['triggered'] and item.get('research_recommended'):
                # At most once per symbol and window per refresh.hot_topics.anomaly_recheck_hours (stored attempts).
                key = f"anomaly:{item['symbol']}:{item['window_seconds']}"
                if not self.research_due(key, anomaly=True)[0]:
                    continue
                if await self.corroborate({'id': key, 'summary_fact': f"Unexplained move in {item['symbol']}",
                                           'event_type': 'PRICE_ANOMALY', 'verification_status': 'UNCONFIRMED',
                                           'sources': [], 'topic': item['symbol']}, priority=0):
                    self.record_research(key)
        self.observe()

