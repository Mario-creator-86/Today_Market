"""Celery tasks with a beat schedule built from config/rules.d/refresh.json. Celery owns scheduling only when
CELERY_ENABLED=true and REDIS_URL is set; otherwise the in-process asyncio Runtime stays the local default.
The Binance WebSocket stays with the single API ingestor. Every task holds a Redis lock so runs never overlap,
and adaptive cadences (watch modes, market hours) are re-checked inside the task against the last run."""
import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from celery import Celery
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend import config as app_config
from backend import refresh
from backend.config import ROOT, Settings, get_settings, rules
from backend.database import Store, retention_cutoffs, utcnow

logger = logging.getLogger(__name__)


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    celery_enabled: bool = False
    celery_broker_url: str = ''  # defaults to REDIS_URL


settings = get_settings()
worker_settings = WorkerSettings()
BROKER = worker_settings.celery_broker_url or settings.redis_url or 'redis://localhost:6379/0'

# Poll kind -> Runtime coroutine method. Kinds whose method the runtime does not provide report UNAVAILABLE.
POLLERS = {'derived': 'derived', 'derivatives': 'derivatives', 'global_sweep': 'poll_news',
           'breaking_news': 'poll_breaking', 'x_search': 'social', 'calendar': 'calendar',
           'model_discovery': 'discover', 'central_banks': 'poll_central_banks', 'sec_filings': 'poll_sec',
           'congress': 'poll_congress', 'hot_topics': 'research_hot_topics', 'release_watch': 'poll_release_sources'}
# Adaptive kinds are scheduled at their fastest cadence and skipped inside the task when not yet due.
FASTEST = {'central_banks': 'watch_seconds', 'congress': 'active_seconds', 'release_watch': 'tick_seconds',
           'sec_filings': 'seconds'}
SCHEDULED_TASKS = {'interpretation': 'hub.analyze', 'evaluation': 'hub.evaluate_outcomes',
                   'calibration': 'hub.calibrate', 'briefs': 'hub.publish_briefs', 'retention': 'hub.retention'}


def celery_owned() -> set[str]:
    """Cadence kinds the Runtime must not run itself because Celery beat schedules them."""
    if not (worker_settings.celery_enabled and settings.redis_url):
        return set()
    return set(POLLERS) | set(SCHEDULED_TASKS)


def beat_schedule(enabled: bool | None = None) -> dict[str, dict]:
    """Full schedule only when Celery owns scheduling; otherwise just the durable outcome evaluation, so a beat
    started next to the in-process Runtime never duplicates its polling and analysis."""
    if not (bool(worker_settings.celery_enabled and settings.redis_url) if enabled is None else enabled):
        return {'evaluation': {'task': 'hub.evaluate_outcomes', 'schedule': float(refresh.cadence('evaluation')['seconds'])}}
    schedule: dict[str, dict] = {}
    for kind in POLLERS:
        spec = refresh.cadence(kind)
        schedule[f'poll-{kind}'] = {'task': 'hub.poll', 'schedule': float(spec[FASTEST.get(kind, 'seconds')]),
                                    'args': [kind]}
    for kind, task in SCHEDULED_TASKS.items():
        schedule[kind] = {'task': task, 'schedule': float(refresh.cadence(kind)['seconds'])}
    return schedule


celery_app = Celery('market_hub', broker=BROKER)
celery_app.conf.update(timezone='UTC', enable_utc=True, task_serializer='json', accept_content=['json'],
                       result_serializer='json', broker_connection_retry_on_startup=True,
                       task_acks_late=True, worker_prefetch_multiplier=1, beat_schedule=beat_schedule())


def redis_client() -> Any:
    from redis import Redis
    return Redis.from_url(settings.redis_url or BROKER, decode_responses=True)


def _lock_timeout(kind: str) -> int:
    config = rules()['refresh']['locks']
    spec = refresh.cadence(kind) if kind in rules()['refresh']['cadences'] else {}
    return int(max(config['default_timeout_seconds'], spec.get('seconds', 0)))


@contextlib.contextmanager
def exclusive(client: Any, name: str, timeout: int) -> Iterator[bool]:
    lock = client.lock(f'hub:{name}:lock', timeout=timeout,
                       blocking_timeout=rules()['refresh']['locks']['blocking_timeout_seconds'])
    acquired = bool(lock.acquire(blocking=False))
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(Exception):  # expired lock: the next run already owns it
                lock.release()


def _last_run(client: Any, kind: str) -> str | None:
    value = client.get(f'hub:lastrun:{kind}')
    return value.decode() if isinstance(value, bytes) else value


def _with_hub(operation: Callable[[Any, Any], Any]) -> Any:
    """Fresh store/hub per task: Celery workers fork, and engines must not cross processes."""
    from backend.runtime import Runtime
    from backend.service import HubService
    store = Store(settings.database_url)
    try:
        # Provider settings are re-read per task, so edits saved from the UI reach workers on their next task.
        hub = HubService(store, Settings(_env_file=app_config.ENV_FILE))
        return operation(hub, Runtime(hub))
    finally:
        store.engine.dispose()


def run_locked(kind: str, operation: Callable[[], Any], client: Any = None, check_due: bool = False) -> dict:
    client = client or redis_client()
    try:
        with exclusive(client, kind, _lock_timeout(kind)) as acquired:
            if not acquired:
                return {'kind': kind, 'status': 'already_running'}
            now = utcnow()
            if check_due and not _due(kind, now, _last_run(client, kind)):
                return {'kind': kind, 'status': 'not_due'}
            result = operation()
            client.set(f'hub:lastrun:{kind}', now.isoformat())
            return {'kind': kind, 'status': 'ok', 'result': result}
    finally:
        with contextlib.suppress(Exception):
            client.close()


def _due(kind: str, now: datetime, last_run: str | None) -> bool:
    if kind not in FASTEST:
        return True
    calendar = _with_hub(lambda hub, _runtime: hub.calendar()) if kind in {'central_banks', 'release_watch'} else []
    congress = _with_hub(lambda hub, _runtime: refresh.congress_active(hub.events(), now)) if kind == 'congress' else False
    return refresh.due(kind, now, last_run, calendar, congress_is_active=congress)


def poll_once(kind: str) -> Any:
    method = POLLERS[kind]

    def operation(_hub: Any, runtime: Any) -> Any:
        target = getattr(runtime, method, None)
        if target is None:
            logger.warning('poller_unavailable', extra={'kind': kind, 'operation': method})
            return {'status': 'UNAVAILABLE', 'reason': f'Runtime has no {method}()'}
        return asyncio.run(target())
    return _with_hub(operation)


@celery_app.task(name='hub.poll')
def poll(kind: str) -> dict:
    if kind not in POLLERS:
        raise ValueError(f'Unknown poll kind: {kind}')
    return run_locked(kind, lambda: poll_once(kind), check_due=True)


@celery_app.task(name='hub.discover_models')
def discover_models() -> dict:
    return run_locked('model_discovery', lambda: poll_once('model_discovery'))


@celery_app.task(name='hub.analyze')
def analyze() -> dict:
    def operation() -> Any:
        return _with_hub(lambda hub, _runtime: len(asyncio.run(hub.recalculate())))
    return run_locked('interpretation', operation)


@celery_app.task(name='hub.evaluate_outcomes')
def evaluate_outcomes() -> dict:
    from backend.evaluation import evaluate_pending

    def operation() -> Any:
        store = Store(settings.database_url)
        try:
            return evaluate_pending(store)
        finally:
            store.engine.dispose()
    return run_locked('evaluation', operation)


def calibrate_all(store: Store, now: datetime) -> dict:
    """Candidates stay in shadow; promotion remains an explicit, validated step (calibration.promote)."""
    from backend import calibration
    results: dict[str, Any] = {}
    for symbol, _, _ in rules()['assets']:
        try:
            results[symbol] = calibration.create_candidate(store, symbol).get('state')
        except (ValueError, KeyError, ZeroDivisionError) as exc:
            results[symbol] = f'skipped:{type(exc).__name__}'
    monitor = getattr(calibration, 'monitor', None)
    return {'candidates': results, 'rollbacks': monitor(store, now) if monitor else None}


@celery_app.task(name='hub.calibrate')
def calibrate() -> dict:
    def operation() -> Any:
        store = Store(settings.database_url)
        try:
            return calibrate_all(store, utcnow())
        finally:
            store.engine.dispose()
    return run_locked('calibration', operation)


@celery_app.task(name='hub.publish_briefs')
def publish_briefs() -> dict:
    return run_locked('briefs', lambda: _with_hub(lambda hub, _runtime: [b['kind'] for b in hub.publish_briefs(utcnow())]))


def run_retention(store: Store, now: datetime, symbols: list[str] | None = None) -> dict:
    """Aggregates the last closed hours into 1s/5s/1m/5m/1h bars, then prunes raw ticks and old bars."""
    config = rules()['refresh']['storage']
    intervals = [int(i) for i in config['bar_intervals_seconds']]
    largest = max(intervals)
    lookback = refresh.cadence('retention')['seconds'] + largest
    since = datetime.fromtimestamp(int((now - timedelta(seconds=lookback)).timestamp()) // largest * largest, UTC)
    written = {s: store.aggregate_bars(s, intervals, since, now) for s in symbols or [a[0] for a in rules()['assets']]}
    tick_cutoff, bar_cutoffs = retention_cutoffs(now, config['raw_tick_retention_hours'], config['bar_retention_days'])
    # Never prune ticks newer than what was just aggregated.
    return {'bars_written': written, 'pruned': store.prune(min(tick_cutoff, since), bar_cutoffs)}


@celery_app.task(name='hub.retention')
def retention() -> dict:
    def operation() -> Any:
        store = Store(settings.database_url)
        try:
            return run_retention(store, utcnow())
        finally:
            store.engine.dispose()
    return run_locked('retention', operation)
