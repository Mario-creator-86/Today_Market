import asyncio
import hashlib
import importlib
import logging
import math
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from backend import (
    analytics,
    briefs,
    causal,
    decision,
    event_risk,
    notifications,
    positioning,
    sessions,
    technicals,
)
from backend.ai import AIService, OllamaProvider, OpenAIProvider, SearchBudgetManager
from backend.ai_budget import budget_day, budget_settings, limits
from backend.analytics import unit_for
from backend.config import Settings, rules, rules_view
from backend.database import (
    RECORDS,
    Asset,
    Decision,
    Document,
    Event,
    Evidence,
    Store,
    Tick,
    daily_close_providers,
    uid,
    utcnow,
)
from backend.engines import aware, category_side, page_key
from backend.ingestion import Ingestion
from backend.llm_limiter import LIMITER
from backend.provider_settings import affected
from backend.providers.base import provider_label
from backend.providers.registry import build_providers, is_stub
from backend.regime import evaluate as evaluate_regime
from backend.schemas import ROLE_SCHEMAS, iso
from backend.security import accept_extraction
from backend.technicals import DAILY_CLASSES

# Coverage source names (config coverage.json) served by a differently named adapter.
PROVIDER_ALIASES = {'ecb': 'ecb_data', 'ecb-derived': 'ecb_data', 'treasury': 'treasury_yields'}
CLOSE_LOOKBACK_ROWS = 20  # earlier ticks scanned for the previous daily close (live ticks in between are skipped)
REAL_TICK_ROWS = 5  # latest non-stub ticks scanned when the newest tick is a development stub
PROXY_ROWS_LIMIT = 1000
ANALYSIS_STATUS_KEY = 'analysis-status'  # asset_scores record of the full analysis cycle (last success / attempt / error)
MINUTE_BAR_SECONDS, DAILY_BAR_SECONDS = 60, 86400
logger = logging.getLogger(__name__)


def event_risk_error(operation: str, exc: Exception) -> str:
    """Logs an event-risk failure and returns the short, secret-free text served beside `event_risk: null`: the
    dashboard and asset endpoints keep working without the event layer (docs/EVENT_AWARENESS.md §8)."""
    logger.error('event_risk_failed', extra={'operation': operation, 'error_type': type(exc).__name__})
    return f'event risk unavailable ({type(exc).__name__})'


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _stub_tick(tick: Tick) -> bool:
    """A development-stub tick: flagged stub, or from a '<name>-stub' provider (config market_data)."""
    return bool((tick.payload or {}).get('stub')) or tick.provider.endswith(
        sessions.ruleset()['market_data']['exclude_provider_suffix'])


def data_class(payload: dict) -> str:
    """Preserve an explicit class; only legacy rows fall back on the daily_close flag or LIVE."""
    value = payload.get('data_class')
    if value is not None and value != 'LIVE':
        return str(value)
    return 'DAILY_CLOSE' if payload.get('daily_close') else 'LIVE'


def entry_keys(source: dict) -> list[str]:
    """Env settings one coverage source needs: every one of 'keys' (Alpaca: key ID and secret), else 'key'
    ([] = keyless)."""
    keys = source.get('keys') or ([source['key']] if source.get('key') else [])
    return [str(key) for key in keys if key]


def source_keys(config: dict) -> dict[str, list[str]]:
    """Coverage source -> env keys that enable it ([] = keyless), from the level sources of every asset."""
    keys: dict[str, list[str]] = {}
    for spec in config['coverage']['assets'].values():
        for source in spec.get('level', []):
            keys.setdefault(source['provider'], entry_keys(source))
    return keys


def notices(providers: Iterable[str | None], config: dict) -> list[dict]:
    """Notices the data owners' terms require (config source_notices) for the providers of what is displayed, e.g.
    the FRED® API notice whenever a FRED value is shown: [{id, text, providers: [displayed providers it covers]}]."""
    shown = {p for p in providers if p}
    result = []
    for notice in config.get('source_notices', {}).get('notices', []):
        covered = [p for p in notice['providers'] if p in shown]
        if covered:
            result.append({'id': notice['id'], 'text': notice['text'], 'providers': covered})
    return result


def displayed_providers(assets: Iterable[dict], regime: dict | None = None) -> list[str]:
    """Providers of the quotes and proxies shown for these assets, plus the regime's inputs (e.g. FRED's BAA10Y)."""
    found = [item.get('provider') for asset in assets for item in (asset.get('quote'), asset.get('proxy')) if item]
    found += [source.get('provider') or source.get('source') for source in (regime or {}).get('feature_sources', {}).values()]
    return [str(p) for p in dict.fromkeys(found) if p]


def interpretation_module() -> Any:
    """backend.interpretation (market_interpreter role) when installed, else None."""
    try:
        return importlib.import_module('backend.interpretation')
    except ImportError:
        return None


def disabled_health(health: dict) -> dict:
    """The quiet health row of a provider switched off in Settings. Its descriptive fields (display label, category)
    are kept, so Settings still names and groups it (UIs show label ?? name); the error text and type are cleared."""
    return {'name': health.get('name'), 'label': health.get('label'), 'category': health.get('category'),
            'status': 'DISABLED', 'latency_ms': None, 'last_success': health.get('last_success'), 'error': None,
            'error_type': None}


def importance_fields(payload: dict) -> dict:
    """Event importance (0..1): source-tier weight times extractor importance, or unknown until assessed.
    Events enriched before `tier_weight` was stored
    still carry the tier weight as their importance (the former max() could never lower it), so that value is the tier
    weight. {} when the event has no tier weight at all (nothing is guessed)."""
    raw = payload.get('tier_weight', payload.get('importance'))
    if not isinstance(raw, (int, float)) or not _finite(raw):
        return {}
    tier = float(raw)
    potential = (payload.get('extraction') or {}).get('potential_importance')
    if isinstance(potential, (int, float)) and _finite(potential):
        return {'importance': round(tier * float(potential) / 100, 4),
                'importance_basis': 'source tier x extractor importance', 'tier_weight': tier}
    return {'importance': None, 'importance_basis': 'not assessed', 'tier_weight': tier}


EXTRACTION_GATE_CACHE = 5000  # events remembered as "nothing to extract" (their evidence count), then forgotten


def extraction_marker(url: str) -> str:
    """research_jobs key of a page's extraction record: every http/https or www. spelling of a page is one key."""
    return 'extracted:' + hashlib.sha256(page_key(url).encode()).hexdigest()


def _word(text: str, phrase: str) -> bool:
    return re.search(rf'(?<!\w){re.escape(phrase)}(?!\w)', text) is not None


def worth_extracting(document: dict, config: dict | None = None, *, strict: bool = False) -> bool:
    """Whether one evidence document is worth an AI extraction (config ai_runtime `extraction`): a source of tier
    <= max_llm_tier or an official one, else a title or text that names a tracked asset (the symbol as a capitalised
    whole word, the name as a whole word in any case) or matches a causal trigger rule (causal.derive_trigger, every
    keyword group of one rule). Everything else stays on the rule-based path, with no AI request. strict (the
    extractor used strict_after_cap_share of its daily cap): the tier alone is not enough, every page needs the asset
    or causal match."""
    settings = config if config is not None else (rules_view().get('extraction') or {})
    tier = document.get('tier')
    if not strict and (document.get('official')
                       or (isinstance(tier, (int, float)) and tier <= settings.get('max_llm_tier', 2))):
        return True
    title, text = str(document.get('title') or ''), str(document.get('excerpt') or document.get('content') or '')
    combined = f'{title}\n{text}'
    if settings.get('match_assets', True):
        excluded = {str(name).casefold() for name in settings.get('asset_name_exclusions') or []}
        folded = combined.casefold()
        for symbol, name, _kind in rules_view()['assets']:
            if _word(combined, str(symbol)) or (str(name).casefold() not in excluded and _word(folded, str(name).casefold())):
                return True
    if settings.get('match_causal_rules', True):
        from backend import causal
        try:
            return causal.derive_trigger(title, text, document.get('source_id'))['trigger'] is not None
        except Exception:  # causal rules unavailable: the tier and asset rules decided
            return False
    return False


def disable(provider: Any) -> None:
    """Switched off in Settings -> Data providers: kept in hub.providers (some loops index it by name), never called
    (configured=False), with a quiet DISABLED health that raises no PROVIDER alert."""
    provider.enabled, provider.configured = False, False
    provider.health = disabled_health(provider.health)


class HubService(Ingestion):
    def __init__(self, store: Store, settings: Settings):
        super().__init__(store, settings)
        self.settings_listeners: list[Callable[[Settings], Any]] = []  # called after settings or switches change
        self._correlation_bars: tuple[datetime, tuple[str, ...], dict[str, list[dict]]] | None = None
        self._page_bars: dict[str, tuple[datetime, list[dict], list[dict]]] = {}  # asset page: symbol -> (read at, all, live)
        self.ollama = OllamaProvider(settings.ollama_base_url, settings.ollama_api_key.get_secret_value())
        self.openai = OpenAIProvider(settings.openai_base_url, settings.openai_api_key.get_secret_value())
        llm: Any = self.openai if settings.llm_provider == 'openai' else self.ollama
        self.ai = AIService(store, llm)
        LIMITER.bind(llm.health)  # the chat provider's health row shows the shared LLM limiter
        self.providers = build_providers(settings, llm=self.ollama)
        if settings.llm_provider == 'openai':
            self.providers[self.openai.name] = self.openai
        self.search_budget = SearchBudgetManager(settings.search_hourly_budget, settings.search_topic_cooldown, store)
        self._nothing_to_extract: dict[str, tuple[int, str]] = {}  # event id -> (evidence count, budget day) with no page
        self.decision_ai_reuse: dict[str, dict] = {}  # symbol -> the last setup explanation and audit (decision.reusable)
        self.analysis_lock = asyncio.Lock()
        self.bus: Any = None  # set by the application: streams.EventBus
        self.apply_enabled()

    # --- provider settings and switches (docs/PROVIDER_SETTINGS.md) -------------------------------------------------

    def enabled_flags(self) -> dict[str, bool]:
        """Provider switches from the 'providers' records; a missing row means enabled."""
        try:
            rows = self.store.list('providers', 1000)
        except SQLAlchemyError:  # the service is built before its tables exist; initialize() applies them again
            return {}
        return {row['id']: row.get('enabled') is not False for row in rows}

    def apply_enabled(self, providers: dict[str, Any] | None = None) -> None:
        flags = self.enabled_flags()
        known = providers if providers is not None else {'ollama': self.ollama, 'openai': self.openai, **self.providers}
        for name, provider in known.items():
            if flags.get(name) is False and getattr(provider, 'enabled', True):
                disable(provider)

    def _rebuild(self, names: set[str]) -> None:
        """Fresh instances for the named providers (and their stub/real counterparts) from self.settings.
        hub.providers is rebound to a new dict: loops iterate it across awaits, so its keys never change in place."""
        settings = self.settings
        names = names | {f"{name.removesuffix('-stub')}{suffix}" for name in names for suffix in ('', '-stub')}
        fresh: dict[str, Any] = {}
        if names & {'ollama', 'openai'}:
            self.ollama = OllamaProvider(settings.ollama_base_url, settings.ollama_api_key.get_secret_value())
            self.openai = OpenAIProvider(settings.openai_base_url, settings.openai_api_key.get_secret_value())
            names |= {'ollama', 'openai'}
            fresh['ollama'] = self.ollama
            if settings.llm_provider == 'openai':
                fresh['openai'] = self.openai
            self.apply_enabled({'ollama': self.ollama, 'openai': self.openai})
            self.ai.provider = self.openai if settings.llm_provider == 'openai' else self.ollama
            LIMITER.bind(self.ai.provider.health)
        if names - {'ollama', 'openai'}:
            built = build_providers(settings, llm=self.ollama)
            fresh.update({name: p for name, p in built.items() if name in names and name not in {'ollama', 'openai'}})
        self.apply_enabled(fresh)
        providers = {name: p for name, p in self.providers.items() if name not in names}
        providers.update(fresh)
        self.providers = providers

    def _notify(self) -> None:
        for listener in list(self.settings_listeners):
            listener(self.settings)

    def apply_settings(self, settings: Settings, changed: Iterable[str]) -> None:
        """Hot-applies saved provider settings: only the instances of the changed groups are rebuilt (every
        stub-capable adapter for provider_mode, both LLM vendors for ollama_*/openai_*/llm_provider)."""
        names, llm = affected(changed)
        self.settings = settings
        self._rebuild(names | ({'ollama'} if llm else set()))
        self._notify()

    def set_enabled(self, name: str, enabled: bool, reason: str | None = None) -> None:
        """Persists a provider switch. Disabling marks the running instance (its last success is kept);
        enabling rebuilds it from the current settings."""
        self.store.put('providers', name, {'name': name, 'enabled': enabled, 'reason': reason,
                                           'updated_at': utcnow().isoformat()})
        if enabled:
            self._rebuild({name})
        else:
            self.apply_enabled()
        self._notify()

    def publish(self, stream: str, payload: dict) -> None:
        if self.bus is not None:
            self.bus.publish_nowait(stream, payload)

    def initialize(self):
        config = rules()
        with self.store.session() as session:
            for symbol, name, kind in config['assets']:
                row = session.get(Asset, symbol)
                if not row:
                    session.add(Asset(id=symbol, symbol=symbol, name=name, asset_class=kind))
                else:
                    row.name, row.asset_class = name, kind
            session.commit()
        # The registry follows configuration (tiers/kinds change with sources_version); admin overrides live apart.
        self.store.put_many('sources', {s['id']: s for s in config['sources']})
        self.run_event_repair()
        for role in ROLE_SCHEMAS:
            if not self.store.get('ai_role_assignments', role):
                self.store.put('ai_role_assignments', role, {'role': role, 'primary': None, 'backup': None, 'second_opinion': None})
        interpretation = interpretation_module()
        if interpretation is not None:  # an unassigned market_interpreter inherits chief_analyst (audited role_inherited)
            interpretation.ensure_role(self.store)
        self.record_configuration(config)
        self.apply_enabled()  # the records exist now (the service may have been built before the tables)

    def run_event_repair(self) -> None:
        """Startup repair of stored events (merged or classified under earlier rules), when the ingestion layer offers
        one: Ingestion.repair_stored_events (config dedupe repair_note), or a repair_events of an earlier layout. It
        runs after the sources are stored (a repair reads source tiers). A failure is logged and never stops the
        start."""
        target = getattr(self, 'ingestion', self)
        repair = next((method for name in ('repair_events', 'repair_stored_events')
                       if callable(method := getattr(target, name, None))), None)
        if repair is None:
            return
        try:
            result = repair()
            logger.info('events_repaired', extra={'operation': getattr(repair, '__name__', 'repair'),
                                                  'result': str(result)[:200] if result is not None else None})
        except Exception as exc:  # a repair problem must never keep the app from starting
            logger.error('events_repair_failed', extra={'operation': getattr(repair, '__name__', 'repair'),
                                                        'error_type': type(exc).__name__})

    def record_configuration(self, config: dict) -> str:
        """Immutable, content-addressed configuration history: an edit without a version bump is still recorded.
        Called at startup and every analysis cycle, so hand-edited config files are recorded before any decision uses them."""
        key = f"{config['version']}:{config['content_hash'][:16]}"
        if not self.store.get('configuration_versions', key):
            self.store.put('configuration_versions', key, dict(config, stored_at=utcnow().isoformat()))
        return key

    def health(self) -> list[dict]:
        # A call in flight when its provider was switched off may still write its health: it stays DISABLED.
        return [dict(p.health) if getattr(p, 'enabled', True) else disabled_health(p.health) for p in self.providers.values()]

    def quote(self, symbol: str, kind: str | None = None, now: datetime | None = None) -> dict | None:
        """Latest observed quote with freshness, market session and the development-stub flag recomputed now.
        Daily closes (data_class DAILY_CLOSE / DERIVED_DAILY) also get close_status against their publication calendar
        and, when the provider gave no change, the change vs the previous stored close. They are never FRESH, and an
        old fix never reports a LIVE market (docs/MARKET_COVERAGE.md).
        When the newest tick is a development stub (stamped 'now' every cycle), the latest real tick is returned
        instead while it is inside its source's expected window (_real_tick); otherwise the stub, still flagged stub."""
        now = now or utcnow()
        kind = kind or next((c for s, _, c in rules()['assets'] if s == symbol), 'unknown')
        with self.store.session() as session:
            tick = session.scalar(select(Tick).where(Tick.symbol == symbol).order_by(Tick.timestamp.desc()).limit(1))
            if not tick:
                return None
            if _stub_tick(tick):
                tick = self._real_tick(session, symbol, now) or tick
            payload = dict(tick.payload or {})
            klass = data_class(payload)
            daily = klass in DAILY_CLASSES
            change = self._close_change(session, tick) if daily and not any(
                _finite(payload.get(k)) for k in ('change_pct', 'change_bp')) else {}
        verdict = technicals.quote_freshness(tick.provider, tick.timestamp, now, self.settings.quote_stale_seconds,
                                             self.settings.max_future_seconds)
        live = technicals.is_live(klass, payload.get('daily_close'))
        fresh = verdict['fresh'] and live
        info = sessions.session_info(kind, now, symbol)
        close = sessions.close_status(tick.provider, symbol, tick.timestamp, now) if daily else {}
        market = 'LAST CLOSE' if daily and info['state'] == 'LIVE' and close.get('status') != 'CURRENT' else info['state']
        labels = {} if fresh or not live else self.stale_labels(payload, tick.timestamp, verdict['threshold_seconds'])
        return dict(payload, **change, **labels, price=tick.price, timestamp=iso(tick.timestamp), provider=tick.provider,
                    data_class=klass, close_status=close.get('status'), close_date=close.get('observed'),
                    close_expected_date=close.get('expected'),
                    market_status=market, market_state=info['state'], session=info,
                    freshness_status='FRESH' if fresh else 'STALE',
                    age_seconds=verdict['age_seconds'], freshness_threshold_seconds=verdict['threshold_seconds'],
                    stub=_stub_tick(tick),
                    delay_label=labels.get('delay_label') or payload.get('delay_label') or ('daily close' if daily else None))

    @staticmethod
    def stale_labels(payload: dict, stamp: datetime, limit: float) -> dict:
        """A live quote judged STALE never keeps a stored label that calls it real-time (the label was written when the
        tick was fetched): as_of_label / delay_label are replaced by market_data.freshness.stale_label, and the stored
        text is kept as as_of_label_at_ingestion. Labels that make no real-time claim are left alone."""
        template = (rules_view()['market_data'].get('freshness') or {}).get('stale_label')
        changed = {key: payload[key] for key in ('as_of_label', 'delay_label')
                   if isinstance(payload.get(key), str) and 'real-time' in payload[key].lower()}
        if not template or not changed:
            return {}
        text = template.format(as_of=f'{aware(stamp).astimezone(UTC):%H:%M:%S} UTC', limit=f'{limit:g}')
        return {**dict.fromkeys(changed, text),
                'as_of_label_at_ingestion': payload.get('as_of_label') or payload.get('delay_label')}

    def _real_tick(self, session: Any, symbol: str, now: datetime) -> Tick | None:
        """Latest non-stub tick of `symbol` while its source's expected window holds: for a source with a publication
        schedule (daily closes, and equity last trades dated by their session close) while its close is still the
        CURRENT one; for any other source while the tick is FRESH. Never an older real value than that."""
        suffix = sessions.ruleset()['market_data']['exclude_provider_suffix']
        rows = session.scalars(select(Tick).where(Tick.symbol == symbol, ~Tick.provider.endswith(suffix, autoescape=True))
                               .order_by(Tick.timestamp.desc()).limit(REAL_TICK_ROWS))
        real = next((row for row in rows if not _stub_tick(row)), None)
        if real is None:
            return None
        status = sessions.close_status(real.provider, symbol, real.timestamp, now)['status']
        if status is not None:
            return real if status == 'CURRENT' else None
        return real if technicals.quote_freshness(real.provider, real.timestamp, now, self.settings.quote_stale_seconds,
                                                  self.settings.max_future_seconds)['fresh'] else None

    @staticmethod
    def _close_change(session: Any, tick: Tick) -> dict:
        """Change vs the latest earlier-dated daily close of the same symbol (the observation date in the source's
        timezone), for a daily close whose provider reported none. Live ticks are never used as a previous close."""
        schedule = sessions.close_schedule(tick.provider, tick.symbol)
        zone = ZoneInfo(schedule['timezone']) if schedule else UTC
        day = aware(tick.timestamp).astimezone(zone).date()
        boundary = datetime.combine(day, time(0), zone)
        rows = session.scalars(select(Tick).where(Tick.symbol == tick.symbol, Tick.timestamp < boundary)
                               .order_by(Tick.timestamp.desc()).limit(CLOSE_LOOKBACK_ROWS))
        previous = next((row for row in rows if data_class(row.payload or {}) != 'LIVE' and not (row.payload or {}).get('stub')
                         and row.price > 0), None)
        if previous is None:
            return {}
        change = {'previous_close': previous.price, 'previous_close_at': iso(previous.timestamp),
                  'change_pct': round((tick.price / previous.price - 1) * 100, 4), 'change_basis': 'previous_close'}
        if unit_for(tick.symbol) == 'bp':
            change['change_bp'] = round((tick.price - previous.price) * rules()['market_units']['bp_per_unit'], 2)
        return change

    # --- coverage and proxies (docs/MARKET_COVERAGE.md) ------------------------------------------------------------

    def _hub_provider(self, source: str, providers: dict[str, Any]) -> Any:
        """Running adapter behind a coverage source name ('ecb-derived' is served by ecb_data, 'eia' by the EIA spot
        provider, never the weekly petroleum report)."""
        if source == 'eia':  # 'eia' alone is the EIA news feed; eia_wpsr* are the weekly inventory report
            names = sorted(n for n in providers if n.startswith('eia') and 'spot' in n) or sorted(
                n for n in providers if n.startswith('eia_') and not n.startswith('eia_wpsr'))
            return providers[names[0]] if names else None
        return providers.get(PROVIDER_ALIASES.get(source, source))

    def _key_set(self, key: str) -> bool:
        value = getattr(self.settings, key.lower(), None)
        return bool(value.get_secret_value() if isinstance(value, SecretStr) else value)

    def _source_states(self, config: dict) -> dict[str, str]:
        """Coverage source -> READY (running, enabled, configured, not a stub) | DISABLED (switched off) |
        MISSING_KEY (one of its keys is not set) | UNAVAILABLE."""
        providers, keys = self.providers, source_keys(config)
        names = {s['provider'] for spec in config['coverage']['assets'].values() for s in spec.get('level', [])}
        names |= {p['source'] for entries in config['coverage']['proxies'].values() for p in entries}
        states = {}
        for name in names:
            provider = self._hub_provider(name, providers)
            if provider is not None and getattr(provider, 'enabled', True) is False:
                states[name] = 'DISABLED'
            elif provider is not None and getattr(provider, 'configured', True) and not is_stub(provider):
                states[name] = 'READY'
            elif any(not self._key_set(key) for key in keys.get(name, [])):
                states[name] = 'MISSING_KEY'
            else:
                states[name] = 'UNAVAILABLE'
        return states

    def _unset_keys(self, config: dict) -> set[str]:
        """Env keys of coverage sources that are not set (only the names, never the values)."""
        return {key for keys in source_keys(config).values() for key in keys if not self._key_set(key)}

    def _source_health(self, states: dict[str, str]) -> dict[str, dict]:
        """Coverage source -> the health row of the adapter behind it (a switched-off one reads DISABLED)."""
        rows = {}
        for name in states:
            provider = self._hub_provider(name, self.providers)
            health = getattr(provider, 'health', None) if provider is not None else None
            if isinstance(health, dict):
                rows[name] = dict(health) if getattr(provider, 'enabled', True) else disabled_health(health)
        return rows

    @staticmethod
    def coverage(symbol: str, quote: dict | None, proxy: dict | None, states: dict[str, str], config: dict,
                 unset: set[str] | None = None, health: dict[str, dict] | None = None) -> dict:
        """OK (a live level is shown) | DAILY_ONLY (a daily close is shown) | PROXY_ONLY (only a proxy) |
        NO_DATA_YET (a source is ready, nothing received) | DISABLED (every level source is switched off in Settings
        and nothing is shown) | NOT_CONFIGURED (missing_keys says which keys would add a source).
        missing_keys lists every unset key of a source that needs several (all of them when `unset` is not given);
        disabled lists the asset's sources switched off by the admin. providers merges the ready level and proxy
        sources; level_providers / proxy_providers keep them apart (a proxy only gives a direction, never the level),
        and sources lists every configured source with its role, configuration state and current provider health
        (`health`: coverage source -> health row), e.g. a READY level source that answers AUTH_ERROR."""
        keys = source_keys(config)
        level = config['coverage']['assets'].get(symbol, {}).get('level', [])
        proxies = config['coverage']['proxies'].get(symbol, [])
        sources = [(s['provider'], entry_keys(s)) for s in level]
        sources += [(p['source'], keys.get(p['source'], [])) for p in proxies]
        providers = list(dict.fromkeys(name for name, _ in sources if states.get(name) == 'READY'))
        missing = list(dict.fromkeys(key for name, needed in sources if states.get(name) == 'MISSING_KEY'
                                     for key in needed if unset is None or key in unset))
        disabled = list(dict.fromkeys(name for name, _ in sources if states.get(name) == 'DISABLED'))
        if quote and not quote.get('stub'):
            status = 'OK' if quote.get('data_class', 'LIVE') == 'LIVE' else 'DAILY_ONLY'
        elif proxy:
            status = 'PROXY_ONLY'
        elif providers:
            status = 'NO_DATA_YET'
        elif level and all(states.get(s['provider']) == 'DISABLED' for s in level):
            status = 'DISABLED'
        else:
            status = 'NOT_CONFIGURED'
        roles = [(s['provider'], 'level') for s in level] + [(p['source'], 'proxy') for p in proxies]
        detail = []
        for name, role in dict.fromkeys(roles):
            row = (health or {}).get(name) or {}
            detail.append({'name': name, 'label': row.get('label'), 'role': role, 'state': states.get(name),
                           'health': row.get('status'), 'error': row.get('error')})
        return {'status': status, 'providers': providers, 'missing_keys': missing, 'disabled': disabled,
                'level_providers': [n for n in dict.fromkeys(s['provider'] for s in level) if states.get(n) == 'READY'],
                'proxy_providers': [n for n in dict.fromkeys(p['source'] for p in proxies) if states.get(n) == 'READY'],
                'sources': detail}

    def proxy_rows(self) -> dict[str, dict]:
        """All stored proxy quotes in one read, keyed '<source>:<symbol>'."""
        try:
            rows = self.store.list('proxy_quotes', PROXY_ROWS_LIMIT)
        except SQLAlchemyError:  # a database from before migration 0004
            return {}
        return {row['id']: {k: v for k, v in row.items() if k != 'id'} for row in rows}

    def proxy(self, symbol: str, rows: dict[str, dict], now: datetime, config: dict) -> dict | None:
        """Best proxy for an asset: the first fresh one in coverage priority (age within its max_age_seconds), else the
        latest one, marked STALE. Shown under its own name for direction only; never a tick, a decision input or a gate."""
        candidates = []
        for spec in config['coverage']['proxies'].get(symbol, []):
            row = rows.get(f"{spec['source']}:{spec['symbol']}")
            if not row or not _finite(row.get('price')) or not row.get('timestamp'):
                continue
            try:
                stamp = aware(row['timestamp'])
            except ValueError:
                continue
            candidates.append((spec, row, stamp, (now - stamp).total_seconds()))
        if not candidates:
            return None
        future = self.settings.max_future_seconds
        fresh = next((c for c in candidates if -future <= c[3] <= c[0]['max_age_seconds']), None)
        spec, row, stamp, age = fresh or max(candidates, key=lambda c: c[2])
        kind = config['proxy_sessions']['sources'].get(spec['source'], 'unknown')
        info = sessions.session_info(kind, now, spec['symbol'])
        return dict(row, label=row.get('label') or spec['label'], caveat=row.get('caveat') or spec['caveat'],
                    proxy_for=row.get('proxy_for') or symbol, relation='direction_only', timestamp=iso(stamp),
                    market_status=info['state'], freshness_status='FRESH' if fresh else 'STALE',
                    age_seconds=round(age, 2), max_age_seconds=spec['max_age_seconds'])

    def assets(self, now: datetime | None = None, *, event_overlay: bool = True) -> list[dict]:
        """Published decisions with their read-time invalidation. With event_overlay each entry also gets its
        event_risk block {score, level, event_id, minutes_until, trade_status, adjusted_confidence, awaiting_reopen}
        beside the decision, which it never changes (docs/EVENT_AWARENESS.md §4); callers that do not show it pass
        event_overlay=False (it reads the news events and the calendar once)."""
        now = now or utcnow()
        config = rules()
        result = []
        with self.store.session() as session:
            decisions = {}
            for symbol, _, _ in config['assets']:
                row = session.scalar(select(Decision).where(Decision.symbol == symbol).order_by(Decision.created_at.desc()).limit(1))
                published = dict(row.payload) if row else None
                if row:
                    assert published is not None
                    expires = aware(row.payload.get('created_at') or row.created_at) + timedelta(
                        seconds=self.settings.analysis_seconds)
                    published['expires_at'] = iso(expires)
                    # The levels of a decision past its deadline (the app was stopped, the cycle is late) are anchored
                    # to bars of that time: no break alert, brief line or page may test today's price against them.
                    if aware(now) > expires and isinstance(published.get('levels'), dict) and published['levels']:
                        published['levels'] = technicals.withhold(
                            published['levels'], 'Decision expired: levels await the next analysis cycle')
                decisions[symbol] = published
        proxies, states, unset = self.proxy_rows(), self._source_states(config), self._unset_keys(config)
        health = self._source_health(states)
        for symbol, name, kind in config['assets']:
            quote = self.quote(symbol, kind, now)
            published = decisions[symbol]
            # Gates read the quote only: a proxy never makes or keeps a setup actionable.
            if published and category_side(published.get('category', published['direction'])) and (
                    not quote or quote['freshness_status'] != 'FRESH' or quote['market_status'] != 'LIVE'):
                self._downgrade(published, ['Critical quote unavailable, stale or market closed'])
            if published and category_side(published.get('category', published['direction'])):
                reasons = self.publication_blocks(published, quote, now)
                if reasons:
                    self._downgrade(published, reasons)
            proxy = self.proxy(symbol, proxies, now, config)
            result.append({'symbol': symbol, 'name': name, 'asset_class': kind, 'quote': quote, 'decision': published,
                           'session': quote['session'] if quote else sessions.session_info(kind, now, symbol),
                           'coverage': self.coverage(symbol, quote, proxy, states, config, unset, health), 'proxy': proxy})
        if event_overlay:
            try:
                event_risk.overlay(self, result, now)
            except Exception as exc:  # the event layer never breaks the asset endpoints
                error = event_risk_error('assets', exc)
                for entry in result:
                    entry.update(event_risk=None, event_risk_error=error)
        return result

    @staticmethod
    def _downgrade(published: dict, reasons: list[str]) -> None:
        """Read-time invalidation: directional guidance never outlives its evidence, prices or session."""
        published.update(direction='WAIT', category='WAIT / DATA INSUFFICIENT', audit_status='BLOCK',
                         blocks=list(dict.fromkeys(published.get('blocks', []) + reasons)),
                         reasons=list(dict.fromkeys(published.get('reasons', []) + reasons)))

    def publication_blocks(self, decision_payload: dict, quote: dict | None, now) -> list[str]:
        blocks = []
        if (now - aware(decision_payload['created_at'])).total_seconds() > self.settings.analysis_seconds:
            blocks.append('Decision expired; awaiting recalculation')
        if not quote or quote['freshness_status'] != 'FRESH':
            blocks.append('Critical quote stale')
        if quote and not technicals.is_live_quote(quote):
            blocks.append('Non-live quote cannot confirm a decision')
        for old in decision_payload.get('input_snapshot', {}).get('events', []):
            current = self.event(old['id'])
            if current['verification_status'] != 'CONFIRMED' or current['last_updated_utc'] != old['last_updated_utc']:
                blocks.append('Evidence changed or expired')
        side = category_side(decision_payload.get('category', decision_payload['direction']))
        invalidation = decision_payload.get('invalidation_level')
        if quote and side and isinstance(invalidation, (int, float)) and (quote['price'] - invalidation) * side <= 0:
            blocks.append('Price invalidation crossed')
        provider = self.providers.get(quote['provider']) if quote else None
        if provider is not None and provider.health['status'] in {'OFFLINE', 'AUTH_ERROR', 'RATE_LIMITED'}:
            blocks.append('Critical market provider unavailable')
        return list(dict.fromkeys(blocks))

    def _trigger_keywords(self) -> dict[str, list[str]]:
        keywords: dict[str, list[str]] = {}
        for rule in rules()['causal_triggers']['keyword_rules']:
            keywords.setdefault(rule['event_type'], []).extend(p for group in rule['all'] for p in group)
        return keywords

    @staticmethod
    def _asset_names() -> dict[str, list[str]]:
        return {symbol: [symbol, name] for symbol, name, _ in rules()['assets']}

    def _specialist(self, event: dict) -> str:
        config = rules()['decision']['specialists']
        classes = {c for s, _, c in rules()['assets'] if s in event.get('assets', [])}
        for role, rule in config.items():
            if event.get('trigger') in rule.get('triggers', []) or event['event_type'] in rule.get('event_types', []):
                return role
        for role, rule in config.items():
            if classes & set(rule.get('asset_classes', [])):
                return role
        return next(role for role, rule in config.items() if rule.get('default'))

    def event(self, event_id: str, context: dict | None = None) -> dict:
        """Ingestion.event with the importance recomputed from its stored factors (importance_fields), so events
        enriched before tier_weight existed read the same way as new ones."""
        found = super().event(event_id, context)
        return dict(found, **importance_fields(found))

    def events(self, limit: int = 100) -> list[dict]:
        """The `limit` events published most recently, newest publication first (first seen breaks ties). The cut and
        the order use the same key, the event's publication time (event_time: the published_at of the document that
        created it), not ingestion time: a backfill run that ingests old releases late never pushes newer publications
        off the list, and an older document added to an event later never moves it. Events without an event_time
        (none are written so) come last, newest first seen first."""
        published = Event.payload['event_time'].as_string()
        documented = select(Evidence.id).where(Evidence.event_id == Event.id).exists()
        with self.store.session() as session:
            ids = list(session.scalars(select(Event.id).where(documented)
                                       .order_by(published.desc().nulls_last(), Event.created_at.desc()).limit(limit)))
        context = self.source_context()
        found = [self.event(event_id, context) for event_id in ids]
        return sorted(found, key=lambda e: (e.get('event_time') is not None, e.get('event_time') or '', e['first_seen_utc']),
                      reverse=True)

    def extraction_strict(self, config: dict) -> bool:
        """True once the extractor has used extraction.strict_after_cap_share of its daily cap: from then on the tier
        alone no longer sends a page to it (worth_extracting strict)."""
        share = config.get('strict_after_cap_share')
        if not isinstance(share, (int, float)) or isinstance(share, bool) or share >= 1:
            return False
        budget = getattr(self.ai, 'budget', None)
        if budget is None:
            return False
        used, cap = budget.role_used('extractor')
        return cap is not None and cap > 0 and used >= cap * share

    @staticmethod
    def refused_page(record: dict, document: dict, now: datetime, today: str, config: dict) -> str:
        """What to do with a page whose extraction record exists: 'skip' (extracted, failed, or refused and now too old
        for extraction.max_page_age_hours), 'wait' (refused by the hourly pace until its retry_after, or by the daily or
        role cap for the rest of that budget day) or 'retry'."""
        if record.get('status') != 'refused':
            return 'skip'
        if record.get('refusal') == 'pace' and record.get('retry_after'):
            if now < aware(record['retry_after']):
                return 'wait'
        elif record.get('day') == today:
            return 'wait'
        stamp = document.get('published_at') or record.get('first_refused_at') or record.get('at')
        hours = config.get('max_page_age_hours')
        if stamp and isinstance(hours, (int, float)) and (now - aware(stamp)).total_seconds() > hours * 3600:
            return 'skip'
        return 'retry'

    def extraction_pages(self, event: dict) -> list[str]:
        """Canonical URLs of the event's documents that are worth an AI extraction (worth_extracting, strict once the
        extractor used part of its cap) and were never extracted (research_jobs extraction_marker). A page the budget
        refused waits (refused_page): burst_hours after an hourly-pace refusal, the next budget day after a daily or
        role refusal, and is dropped once older than max_page_age_hours. An event judged to have no page is remembered
        with its evidence count for the day (unless a page only waits for the pace), so a feed poll that brings nothing
        new costs no lookups."""
        evidence = event.get('evidence') or []
        now = utcnow()
        today = budget_day(now)
        if self._nothing_to_extract.get(event['id']) == (len(evidence), today):
            return []
        config = rules_view().get('extraction') or {}
        strict = self.extraction_strict(config)
        pages: list[str] = []
        waiting = False
        for document in evidence:
            url = document.get('url')
            if not url or url in pages or not worth_extracting(document, config, strict=strict):
                continue
            record = self.store.get('research_jobs', extraction_marker(url))
            if record:
                action = self.refused_page(record, document, now, today, config)
                if action == 'wait':
                    waiting = waiting or record.get('refusal') == 'pace'
                    continue
                if action == 'skip':
                    if record.get('status') == 'refused':  # too old: it stays on the rule-based path for good
                        self.store.put('research_jobs', extraction_marker(url), dict(record, status='expired'))
                    continue
            pages.append(url)
        if not pages and not waiting:
            if len(self._nothing_to_extract) >= EXTRACTION_GATE_CACHE:
                self._nothing_to_extract.clear()
            self._nothing_to_extract[event['id']] = (len(evidence), today)
        return pages

    def mark_extracted(self, pages: list[str], event_id: str, run_id: str | None) -> None:
        """Records the pages an extraction request was made for (sent, failed or refused by the daily budget); with no
        run (no extractor model, AI switched off) nothing was asked and nothing is recorded. A pace refusal stores
        retry_after (now + budget.spread.burst_hours); the first refusal time is kept for max_page_age_hours."""
        if not run_id:
            return
        run = self.store.get('ai_runs', run_id) or {}
        refusal = run.get('budget_refusal')
        now = utcnow()
        budget = getattr(self.ai, 'budget', None)
        wait = timedelta(hours=limits(budget.settings() if budget is not None else budget_settings()).window_hours)
        records = {}
        for url in pages:
            key = extraction_marker(url)
            record = {'kind': 'EXTRACTED_PAGE', 'url': url, 'status': 'refused' if refusal else 'done',
                      'day': budget_day(now), 'event_id': event_id, 'run_id': run_id,
                      'success': bool(run.get('success')), 'at': now.isoformat()}
            if refusal:
                previous = self.store.get('research_jobs', key) or {}
                record.update(refusal=refusal, first_refused_at=previous.get('first_refused_at') or now.isoformat())
                if refusal == 'pace':
                    record['retry_after'] = (now + wait).isoformat()
            records[key] = record
        self.store.put_many('research_jobs', records)

    async def enrich_event(self, event_id: str):
        """Extraction and fact-check proposals, then specialist analysis once the event is CONFIRMED (re-run on change).
        Only pages worth an AI read and never extracted before go to the extractor and the fact checker
        (extraction_pages); other events keep the rule-based fields (source-tier importance, causal rules)."""
        event = self.event(event_id)
        if event['verification_status'] == 'STALE':
            return
        inputs = {'event': event, 'evidence_ids': [e['id'] for e in event['evidence']]}
        updates: dict[str, Any] = {}
        runs = dict(event.get('ai_run_ids') or {})
        pages = self.extraction_pages(event) if not event.get('ai_enriched') else []
        if pages:
            extracted, runs['extractor'] = await self.ai.run_with_id('extractor', inputs)
            self.mark_extracted(pages, event_id, runs['extractor'])
            if extracted:
                texts = [f"{x['title']} {x.get('excerpt', '')}" for x in event['evidence']]
                accepted = accept_extraction(event['event_type'], event.get('assets', []), extracted, texts,
                                             self._trigger_keywords(), self._asset_names())
                # Importance = source tier weight x extractor importance (docs/DECISION_ENGINE.md), stored with its basis.
                updates.update(extraction=extracted, ai_enriched=True, ai_acceptance=accepted, assets=accepted['assets'],
                               **importance_fields(dict(event, extraction=extracted)))
                if accepted['event_type'] != event['event_type']:
                    updates['event_type'] = accepted['event_type']
            if len(event['evidence']) > 1:
                proposal, runs['fact_checker'] = await self.ai.run_with_id('fact_checker', inputs)
                if proposal:
                    updates['fact_check'] = proposal
                    self._apply_fact_check(event_id, proposal)
        role = self._specialist(event) if (event['verification_status'] == 'CONFIRMED'
                                           and event.get('analysed_status') != 'CONFIRMED') else None
        if role is not None and not self.budget_refuses(role):
            analysis, runs[role] = await self.ai.run_with_id(role, dict(inputs, extraction=event.get('extraction') or updates.get('extraction')))
            refused = not analysis and bool(runs[role]) and bool(
                (self.store.get('ai_runs', str(runs[role])) or {}).get('budget_refusal'))
            if not refused:  # a call the daily budget refused sent nothing: a later enrichment asks again
                updates.update(interpretation=analysis, interpretation_role=role, analysed_status='CONFIRMED')
        if not updates:
            return
        with self.store.session() as session:
            row = session.get(Event, event_id)
            if row:
                if 'event_type' in updates:
                    row.event_type = updates.pop('event_type')
                row.payload = dict(row.payload, **updates, ai_run_ids=runs)
                row.updated_at = utcnow()
                session.commit()
        self.verify(event_id)

    def budget_refuses(self, role: str) -> bool:
        """True while the role has a model but the AI budget (daily cap, role cap or hourly pace) would refuse its
        request now: the specialist call is then not prepared at all (no inputs built, no refusal row written on every
        enrichment of the event) and a later enrichment asks again. A role with no model keeps the earlier behaviour
        (nothing is sent and the event is marked analysed)."""
        available, allowed = getattr(self.ai, 'available', None), getattr(self.ai, 'allowed', None)
        if not callable(available) or not callable(allowed):
            return False
        try:
            return bool(available(role)) and not allowed(role)
        except Exception as exc:  # the budget check never breaks enrichment: the request path decides
            logger.warning('ai_budget_check_failed', extra={'operation': role, 'error_type': type(exc).__name__})
            return False

    def _apply_fact_check(self, event_id: str, proposal: dict) -> None:
        """Asymmetric use of AI proposals: they can only lower confidence (second-hand copy) or raise a dispute
        flag that blocks publication; they can never confirm a fact."""
        with self.store.session() as session:
            rows = session.execute(select(Evidence, Document).join(Document, Evidence.document_id == Document.id)
                                   .where(Evidence.event_id == event_id)).all()
            by_id = {evidence.id: (evidence, doc) for evidence, doc in rows}
            disputed = []
            for relation in proposal['relations']:
                pair = by_id.get(relation['evidence_id'])
                if not pair:
                    continue
                _, doc = pair
                if relation['relation'] == 'second_hand' and doc.payload.get('direct', True):
                    doc.payload = dict(doc.payload, direct=False, second_hand_basis='ai_fact_check_proposal',
                                       origin=relation.get('original_source') or doc.payload.get('origin'))
                elif relation['relation'] == 'contradicts':
                    disputed.append(relation['evidence_id'])
            event = session.get(Event, event_id)
            if event and disputed:
                event.payload = dict(event.payload, ai_disputed_evidence=disputed)
            session.commit()

    @staticmethod
    def minute_bars(ticks: list[dict]) -> list[dict]:
        """One-minute bars {time, open, high, low, close, volume} of already read ticks (one time field)."""
        return [dict({k: v for k, v in b.items() if k != 'timestamp'}, time=iso(aware(b['time'])))
                for b in technicals.aggregate_ticks(ticks)]

    def page_bars(self, symbol: str, now: datetime) -> tuple[list[dict], list[dict]]:
        """One-minute bars for display: (all ticks, live ticks only). Levels, charts and overlays use the live series.
        A read at most ui.asset_page_bars_cache_seconds old is reused:
        the page polls every few seconds, two days of BTC/ETH ticks are ~90,000 rows, and a completed minute bar does
        not change. Decisions and the regime always read fresh bars."""
        ttl = rules_view()['ui'].get('asset_page_bars_cache_seconds', 0)
        cached = self._page_bars.get(symbol)
        if cached and 0 <= (now - cached[0]).total_seconds() < ttl:
            return cached[1], cached[2]
        ticks = technicals.ticks(self.store, symbol, now - timedelta(days=2), now)
        every = self.minute_bars(ticks)
        live = [t for t in ticks if t['live']]
        chart = every if len(live) == len(ticks) else self.minute_bars(live)  # an official close is not a minute bar
        self._page_bars[symbol] = (now, every, chart)
        return every, chart

    def bars(self, symbol: str, days: int = 2, live_only: bool = False) -> list[dict]:
        """One-minute bars of the last `days` days. `live_only` excludes official closes for decision scoring and charts."""
        now = utcnow()
        return self.minute_bars(technicals.ticks(self.store, symbol, now - timedelta(days=days), now, live_only))

    def daily_closes(self, symbol: str, now: datetime) -> list[dict]:
        """Official daily closes stored as ticks (FRED, Treasury, ECB, EIA: database.daily_close_providers), one per
        observation date in the source's own timezone (the latest tick of a date wins), oldest first. Only the close is
        published, so open/high/low stay None; stub ticks never count."""
        since = aware(now) - timedelta(days=rules()['technical']['daily_bars']['lookback_days'])
        with self.store.session() as session:
            rows = session.execute(select(Tick.timestamp, Tick.price, Tick.provider, Tick.payload).where(
                Tick.symbol == symbol, Tick.provider.in_(daily_close_providers()), Tick.timestamp >= since,
                Tick.timestamp <= aware(now)).order_by(Tick.timestamp)).all()
        by_date: dict[str, dict] = {}
        for stamp, price, provider, payload in rows:
            if data_class(payload or {}) == 'LIVE' or (payload or {}).get('stub') or not _finite(price):
                continue
            schedule = sessions.close_schedule(provider, symbol)
            day = aware(stamp).astimezone(ZoneInfo(schedule['timezone']) if schedule else UTC).date().isoformat()
            by_date[day] = {'time': day, 'open': None, 'high': None, 'low': None, 'close': price, 'volume': None,
                            'provider': provider}
        return [by_date[day] for day in sorted(by_date)]

    def hourly_closes(self, symbol: str, kind: str, now: datetime, sessions_limit: int) -> list[dict]:
        """Daily closes of a 24/7 asset (a continuous venue such as crypto, which no source publishes an official close
        for) from its stored hourly bars: the last hourly close of each completed venue day (the day starts at the
        venue's close_utc, 00:00 = UTC days), oldest first. Hours missed while the app was not running would make a
        day's range partial, so open/high/low/volume stay None. [] for any other venue."""
        venue = sessions.venue(kind, symbol)
        if not venue or venue.get('kind') != 'continuous' or sessions_limit <= 0:
            return []
        hours, minutes = (int(part) for part in str(venue.get('close_utc') or '00:00').split(':'))
        cutoff, now = timedelta(hours=hours, minutes=minutes), aware(now)
        current = (now - cutoff).date()  # the forming day is never a close
        since = datetime.combine(current - timedelta(days=sessions_limit + 1), time(0), UTC) + cutoff
        rows = self.store.bars(symbol, technicals.HOUR, since, now, limit=(sessions_limit + 2) * 24)
        by_date: dict[str, dict] = {}
        for row in rows:  # oldest first: the day's last hourly close wins
            day = (aware(row['start']) - cutoff).date()
            if day < current and _finite(row.get('close')):
                by_date[day.isoformat()] = {'time': day.isoformat(), 'open': None, 'high': None, 'low': None,
                                            'close': row['close'], 'volume': None, 'provider': row.get('provider') or None}
        return [by_date[day] for day in sorted(by_date)]

    def daily_bars(self, symbol: str, now: datetime, sessions_limit: int,
                   kind: str | None = None) -> tuple[list[dict], list[str], str | None]:
        """Up to `sessions_limit` daily bars keyed by session date ('YYYY-MM-DD'), oldest first, their providers and
        their basis text: stored daily OHLC bars (Alpaca, New York session dates) of completed sessions, as the levels
        use them (a forming session's bar is partial), else the official daily closes (close only), else for a 24/7
        asset the daily closes of its stored hourly bars (close only). ([], [], None) when there is none."""
        kind = kind or next((c for s, _, c in rules()['assets'] if s == symbol), 'unknown')
        stored = technicals.stored_daily(self.store, symbol, now)
        if stored:
            zone = ZoneInfo(rules()['technical']['daily_bars']['timezone'])
            current = technicals.reference_session(now, zone).isoformat()
            by_session = {b['session_date']: {'time': b['session_date'], 'open': b['open'], 'high': b['high'],
                                              'low': b['low'], 'close': b['close'], 'volume': b.get('volume'),
                                              'provider': b.get('provider') or None}
                          for b in stored if b['session_date'] < current}
            bars, prefix = [by_session[day] for day in sorted(by_session)], 'Daily bars'
        elif (sessions.venue(kind, symbol) or {}).get('kind') == 'continuous':
            # No source publishes an official close for a 24/7 venue, so the official-close query (which would walk
            # every stored BTC/ETH tick of the lookback to find none) is skipped.
            bars, prefix = self.hourly_closes(symbol, kind, now, sessions_limit), 'Daily closes from hourly bars'
        else:
            bars, prefix = self.daily_closes(symbol, now), 'Daily closes'
        bars = bars[-sessions_limit:] if sessions_limit > 0 else []
        providers = list(dict.fromkeys(str(b['provider']) for b in bars if b.get('provider')))
        return bars, providers, self.daily_basis(bars, providers, prefix)

    @staticmethod
    def daily_basis(bars: list[dict], providers: list[str], prefix: str | None = None) -> str | None:
        """'Daily bars: Alpaca IEX' / 'Daily closes: FRED' / 'Daily closes from hourly bars: Binance (crypto spot)'
        (config technical.daily_bars.labels, else the provider's display label), None without bars."""
        if not bars:
            return None
        labels = rules()['technical']['daily_bars']['labels']
        kind = prefix or ('Daily closes' if all(b.get('open') is None for b in bars) else 'Daily bars')
        names = ', '.join(labels.get(p) or provider_label(p) for p in providers)
        return f'{kind}: {names}' if names else kind

    def asset(self, symbol: str) -> dict:
        entries = self.assets()
        asset = next((a for a in entries if a['symbol'] == symbol), None)
        if not asset:
            raise ValueError('Asset not found')
        now = utcnow()
        ui = rules()['ui']
        _, chart = self.page_bars(symbol, now)  # levels and charts share eligible live observations
        quote = asset['quote']
        summary = None
        if symbol in rules()['provider_routing']['deribit']['currencies']:
            options, futures, as_of = positioning.stored_rows(self.store, symbol)
            summary = positioning.summarize(symbol, options, futures, now, as_of) if options or futures else None
        # Stored daily bars (NY session dates) give previous day/week and SMA20/50 when present; a proxy never does.
        levels = technicals.asset_levels(self.store, symbol, chart, now, price=quote['price'] if quote else None,
                                         option_strikes=positioning.option_strike_levels(summary) if summary else None,
                                         keep_closed=True)
        daily, daily_providers, daily_basis = self.daily_bars(symbol, now, ui['daily_bars_sessions'], asset['asset_class'])
        # Overlays use live one-minute bars. A daily-close series (its latest quote is not LIVE: DXY, yields, indices
        # from FRED) or one with fewer than 2 minute bars sends its daily closes instead; cross_asset_interval says which
        # (60 or 86400 seconds per point). Daily overlays are compared with the subject's daily_bars.
        overlay = ui['cross_asset_overlay']
        quotes = {entry['symbol']: entry.get('quote') or {} for entry in entries}
        classes = {entry['symbol']: entry['asset_class'] for entry in entries}
        cross: dict[str, list[dict]] = {}
        intervals: dict[str, int] = {}
        providers = displayed_providers([asset]) + daily_providers  # a FRED value shown anywhere carries its notice
        for other in overlay.get(asset['asset_class'], overlay['default']):
            if other == symbol:
                continue
            minute = [{'time': b['time'], 'close': b['close']} for b in self.page_bars(other, now)[1]]
            daily_series = quotes.get(other, {}).get('data_class', 'LIVE') != 'LIVE'
            closes, closes_providers, _ = self.daily_bars(other, now, ui['cross_asset_daily_sessions'], classes.get(other)) \
                if daily_series or len(minute) < 2 else ([], [], None)
            if closes:
                cross[other] = [{'time': b['time'], 'close': b['close']} for b in closes]
                intervals[other] = DAILY_BAR_SECONDS
                providers += closes_providers
            else:
                cross[other], intervals[other] = minute, MINUTE_BAR_SECONDS
        return dict(asset, bars=chart, daily_bars=[{k: v for k, v in b.items() if k != 'provider'} for b in daily],
                    daily_bars_basis=daily_basis, technical_levels=levels,
                    positioning=summary, cross_asset=cross, cross_asset_interval=intervals,
                    events=[e for e in self.events() if symbol in e.get('assets', [])],
                    notices=notices(providers, rules()))

    def event_detail(self, event_id: str) -> dict:
        event = self.event(event_id)
        found = analytics.event_reactions(self.store, event_id)
        paths = causal.event_paths(event, utcnow())
        # The publication lag and its configured limit let the page say when a baseline is not the release reaction.
        return dict(event, reactions=found['reactions'], baskets=found['baskets'], causal_paths=paths,
                    why_it_matters=causal.summary(event.get('trigger')),
                    publication_lag_seconds=analytics.publication_lag_seconds(event),
                    max_publication_lag_seconds=rules_view()['reaction']['max_publication_lag_seconds'])

    def calendar(self, window: bool = False, upcoming_hours: int | None = None) -> list[dict]:
        rows = sorted(self.store.list('macro_calendar', 2000), key=lambda e: e.get('scheduled_at', ''))
        return self.calendar_window(rows, utcnow(), upcoming_hours) if window else rows

    @staticmethod
    def calendar_window(rows: list[dict], now: datetime, upcoming_hours: int | None = None) -> list[dict]:
        """The UI window (recent hours back, upcoming hours ahead) of already loaded calendar rows. A date-only row
        (time_known false, anchored at the start of its local day) can happen at any time that day, so it counts as
        recent until calendar_date_only_hours after its anchor instead of dropping out hours before the event."""
        config = rules()['ui']
        start = now - timedelta(hours=config['calendar_recent_hours'])
        end = now + timedelta(hours=upcoming_hours or config['calendar_upcoming_hours'])
        day = timedelta(hours=config['calendar_date_only_hours'])
        result = []
        for row in rows:
            if not row.get('scheduled_at'):
                continue
            at = aware(row['scheduled_at'])
            last = at + day if row.get('time_known') is False else at
            if start <= last and at <= end:
                result.append(row)
        return result

    def regime(self, assets: list[dict] | None = None, events: list[dict] | None = None, last_close: bool = False,
               bars: dict[str, list[dict]] | None = None) -> dict:
        """Live basis (FRESH quotes). `last_close` lets the display (dashboard, /api/regime, briefs) fall back on the
        labelled last-close basis when the live one is insufficient; decision scoring calls it without, so closes and
        proxies never reach a decision. `bars` are one-minute bars the caller already read this cycle (reused)."""
        now = utcnow()
        assets = self.assets(now, event_overlay=False) if assets is None else assets
        events = self.events() if events is None else events
        credit = [dict(row, name=row.get('name')) for row in self.store.list('credit_spreads', 10)]
        bars = self.correlation_bars(now, reuse=last_close, known=bars)
        return dict(evaluate_regime(assets, events, now, bars=bars, credit=credit, last_close=last_close),
                    as_of=now.isoformat())

    def correlation_bars(self, now: datetime, reuse: bool = False,
                         known: dict[str, list[dict]] | None = None) -> dict[str, list[dict]]:
        """One-minute bars of the regime correlation legs, read once per symbol (`known` bars the caller read this cycle
        are used as they are). With `reuse` (the display regime only) a read at most
        regime.correlation_bars_cache_seconds old is returned instead: the dashboard, /api/regime and the event-risk
        snapshot poll it every few seconds and a two-day correlation does not move within that window."""
        config = rules()['regime']
        symbols = tuple(sorted({a for pair in config['correlation_pairs'] for a in pair}))
        cached, ttl = self._correlation_bars, config.get('correlation_bars_cache_seconds', 0)
        if reuse and cached and cached[1] == symbols and 0 <= (now - cached[0]).total_seconds() < ttl:
            return cached[2]
        known = known or {}
        bars = {symbol: known[symbol] if symbol in known else self.bars(symbol) for symbol in symbols}
        self._correlation_bars = (now, symbols, bars)
        return bars

    def latest_interpretation(self) -> dict | None:
        """The dashboard 'interpretation' block: interpretation.latest(store) (newest stored market_interpreter run), or
        None when the module or its table is missing or the stored row cannot be read (logged; never a 500)."""
        module = interpretation_module()
        if module is None:
            return None
        try:
            return module.latest(self.store)
        except Exception as exc:  # a bad stored row never breaks the dashboard
            logger.error('interpretation_read_failed', extra={'error_type': type(exc).__name__})
            return None

    def event_risk_inputs(self, now: datetime) -> dict:
        """What the event-risk snapshot reads, loaded once: published assets (without the overlay), news events, calendar
        rows, the display regime and the latest interpretation."""
        assets, events = self.assets(now, event_overlay=False), self.events(rules()['ui']['events_list_limit'])
        return {'assets': assets, 'events': events, 'calendar': self.calendar(),
                'regime': self.regime(assets, events, last_close=True), 'interpretation': self.latest_interpretation()}

    def event_risk_snapshot(self, now: datetime | None = None) -> dict:
        """GET /api/event-risk: the same block as dashboard()['event_risk']."""
        now = now or utcnow()
        return event_risk.snapshot(self, now, **self.event_risk_inputs(now))

    def upcoming_events(self, min_relevance: float = 0.0, now: datetime | None = None) -> list[dict]:
        """GET /api/events/upcoming: every scheduled event not yet resolved, hidden ones included."""
        now = now or utcnow()
        return event_risk.upcoming(self, now, min_relevance, **self.event_risk_inputs(now))

    def dashboard(self) -> dict:
        from backend import refresh
        now = utcnow()
        inputs = self.event_risk_inputs(now)
        assets, events, regime, rows = inputs['assets'], inputs['events'], inputs['regime'], inputs['calendar']
        risk: dict | None = None
        error: str | None = None
        try:
            risk = event_risk.snapshot(self, now, **inputs)  # computed once; the asset entries reuse its per-asset blocks
            event_risk.apply_assets(assets, risk['asset_event_risk'])
        except Exception as exc:  # the event layer never breaks the dashboard: event_risk null + event_risk_error
            risk, error = None, event_risk_error('dashboard', exc)
            for entry in assets:
                entry.update(event_risk=None, event_risk_error=error)
        calendar = self.calendar_window(rows, now)
        analysis = self.analysis_status(now, assets)
        providers = self.health()
        interpretation = inputs['interpretation']
        module = interpretation_module()
        if risk is not None and module is not None:
            try:
                interpretation = module.for_snapshot(interpretation, risk, regime, providers, now, events)
            except Exception as exc:
                logger.error('interpretation_snapshot_failed', extra={'error_type': type(exc).__name__})
                interpretation = None  # old prose must not contradict a newer snapshot
        return {'as_of': now.isoformat(), 'timezone': 'Europe/Bucharest', 'assets': assets,
                'market_strip': rules()['ui']['market_strip'], 'calendar_min_impact': rules()['brief']['min_calendar_impact'],
                'events': events, 'calendar': calendar, 'providers': providers,
                'alerts': self.alerts(rules()['ui']['dashboard_alerts'], providers), 'alerts_total': self.alerts_total(),
                'regimes': regime['regimes'], 'regime': regime,
                'watch': refresh.watch_state(now, calendar), 'last_analysis': analysis['last_success_at'],
                'analysis': analysis, 'freshness_threshold_seconds': self.settings.quote_stale_seconds,
                'notices': notices(displayed_providers(assets, regime), rules()),
                'event_risk': risk, **({'event_risk_error': error} if error else {}),
                'interpretation': interpretation}

    def alerts(self, limit: int, providers: list[dict] | None = None) -> list[dict]:
        """The newest `limit` stored alerts as people read them (notifications.readable_alerts: legacy provider alerts
        with labels and plain causes, provider failures marked resolved); the stored rows never change."""
        current = {str(p.get('name')): str(p.get('status')) for p in (providers if providers is not None else self.health())}
        return notifications.readable_alerts(self.store.list('alerts', limit), current)

    def alerts_total(self) -> int:
        """Number of stored alerts: the dashboard carries only the newest ui.dashboard_alerts of them."""
        with self.store.session() as session:
            return int(session.scalar(select(func.count()).select_from(RECORDS['alerts'])) or 0)

    def analysis_status(self, now: datetime, assets: list[dict] | None = None) -> dict:
        """State of the full analysis cycle for the header: {last_success_at, last_attempt_at, last_error_type,
        cadence_seconds, stale, failed_symbols}. Only a completed cycle (HubService._recalculate) advances
        last_success_at (the cycle time its decisions carry); the morning brief and every other job never do. Before the
        first recorded cycle, the newest published decision gives the last success.
        stale: the last attempt failed, or more than ui.analysis_stale_cadences x the analysis cadence (refresh cadences
        interpretation) passed since the last successful cycle FINISHED. The loop waits a full cadence after a cycle
        ends, so a slow cycle (AI explanations and audits) is not stale. While the next cycle runs, it is stale only
        when that cycle started more than the limit after the last success finished (e.g. after the app was stopped) or
        has itself run longer than the limit (a hung cycle). failed_symbols: assets the last cycle could not
        recalculate (they keep their previous decision; empty when every asset was recalculated)."""
        from backend import refresh
        record = self.store.get('asset_scores', ANALYSIS_STATUS_KEY) or {}
        success = record.get('last_success_at')
        if not success:
            stamps = [aware(a['decision']['created_at']) for a in assets or [] if (a.get('decision') or {}).get('created_at')]
            success = max(stamps).isoformat() if stamps else None
        cadence = int(refresh.cadence('interpretation')['seconds'])
        error = record.get('last_error_type')
        limit = cadence * rules()['ui']['analysis_stale_cadences']
        stale = True
        if success is not None and error is None:
            finished = aware(record.get('last_success_completed_at') or success)
            running = aware(record['running_since']) if record.get('running_since') else None
            if running is not None and running >= finished:  # started on time and not hanging: not stale yet
                stale = (running - finished).total_seconds() > limit or (aware(now) - running).total_seconds() > limit
            else:
                stale = (aware(now) - finished).total_seconds() > limit
        return {'last_success_at': success, 'last_attempt_at': record.get('last_attempt_at'), 'last_error_type': error,
                'cadence_seconds': cadence, 'stale': stale, 'failed_symbols': list(record.get('failed_symbols') or [])}

    def _record_analysis(self, started: datetime, error_type: str | None, failed: list[str], published: int) -> None:
        """Stores the outcome of one full analysis cycle (asset_scores/analysis-status) when it ends. A failed cycle
        keeps the previous last_success_at and last_success_completed_at."""
        record = self.store.get('asset_scores', ANALYSIS_STATUS_KEY) or {}
        stamp, finished = aware(started).isoformat(), utcnow().isoformat()
        self.store.put('asset_scores', ANALYSIS_STATUS_KEY, {
            'last_attempt_at': stamp, 'last_success_at': record.get('last_success_at') if error_type else stamp,
            'last_success_completed_at': record.get('last_success_completed_at') if error_type else finished,
            'last_completed_at': finished, 'last_error_type': error_type, 'failed_symbols': failed,
            'decisions_published': published, 'running_since': None})

    def _mark_running(self, started: datetime) -> None:
        """Marks a full analysis cycle as running (running_since) in the stored status, which every process reads."""
        record = self.store.get('asset_scores', ANALYSIS_STATUS_KEY) or {}
        self.store.put('asset_scores', ANALYSIS_STATUS_KEY, dict(record, running_since=aware(started).isoformat()))

    async def recalculate(self) -> list[dict]:
        async with self.analysis_lock:
            return await self._recalculate()

    async def _recalculate(self) -> list[dict]:
        """One full analysis cycle. Its outcome is recorded for /api/dashboard 'analysis': a cycle fails when it raises
        or when every asset failed (nothing was published); one asset's failure alone keeps that asset's previous
        decision and never aborts the cycle."""
        now = utcnow()
        output: list[dict] = []
        failures: dict[str, str] = {}
        try:
            self._mark_running(now)
            ctx = await asyncio.to_thread(decision.CycleContext, self, now)
            for asset in ctx.assets:
                try:
                    payload = await decision.analyse(ctx, asset)
                except Exception as exc:
                    failures[str(asset.get('symbol'))] = type(exc).__name__
                    logger.error('decision_failed', extra={'symbol': asset.get('symbol'), 'error_type': type(exc).__name__},
                                 exc_info=True)
                    continue
                self.publish('decisions', {k: payload[k] for k in ('id', 'symbol', 'category', 'direction', 'score', 'audit_status')})
                output.append(payload)
            self.publish_briefs(now)
        except Exception as exc:
            self._record_analysis(now, type(exc).__name__, sorted(failures), len(output))
            raise
        # Every asset failing is a failed cycle even though the briefs were refreshed (their decisions are old).
        error = next(iter(failures.values())) if failures and not output else None
        self._record_analysis(now, error, sorted(failures), len(output))
        return output

    def publish_briefs(self, now) -> list[dict]:
        """Stores the cycle state; intraday updates describe changes since the last PUBLISHED update, and one
        morning brief is produced per Bucharest day."""
        assets, events, calendar = self.assets(event_overlay=False), self.events(), self.calendar()
        # Briefs are display: the regime carries its basis (last close on weekends), labelled; decisions stay live.
        regime = self.regime(assets, events, last_close=True)
        self.store.put('market_regimes', uid(), regime)
        # Finished post-event measurements feed the dominance 'historical impact' input (event_risk.record_history).
        try:
            event_risk.record_history(self.store, event_risk.snapshot(self, now, assets=assets, events=events,
                                                                      calendar=calendar, regime=regime, interpretation=None))
        except Exception as exc:  # an event-layer failure never stops the briefs of the cycle
            event_risk_error('record_history', exc)
        current = briefs.snapshot(now, assets, events, calendar, regime)
        self.store.put('asset_scores', 'latest-cycle', current)
        last = self.store.get('asset_scores', 'last-update')
        changed, baseline = briefs.intraday(last, current, events, calendar)
        self.store.put('asset_scores', 'last-update', baseline)
        published = []
        analysis = self.analysis_status(now, assets)  # a brief on out-of-date decisions says so
        if changed:
            published.append(briefs.compose('INTRADAY', now, assets, events, calendar, regime, changed,
                                            since=last['generated_at'] if last else None, analysis=analysis))
        due = briefs.morning_due(now)
        if due and not self.store.get('briefs', f'morning:{due}'):
            published.append(briefs.compose('MORNING', now, assets, events, calendar, regime, analysis=analysis))
        for item in published:
            key = f"morning:{item['local_date']}" if item['kind'] == 'MORNING' else uid()
            self.store.put('briefs', key, item)
            self.publish('briefs', {'id': key, 'kind': item['kind'], 'generated_at': item['generated_at']})
        return published
