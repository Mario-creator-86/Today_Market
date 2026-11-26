import hashlib
import json
import pickle
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
# The dotenv file every settings class reads, resolved at call time (tests point it at a temporary file).
ENV_FILE = ROOT / '.env'


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    database_url: str = 'sqlite:///./market_hub.db'
    redis_url: str = ''
    admin_token: SecretStr = SecretStr('')
    # local: no password for the single user on this computer (loopback + anti-CSRF checks); token: X-Admin-Token.
    admin_auth: str = Field('local', pattern='^(local|token)$')
    enable_ingestion: bool = True
    cors_origins: list[str] = ['http://localhost:3000', 'http://127.0.0.1:3000']
    ollama_base_url: str = 'https://ollama.com/api'
    ollama_api_key: SecretStr = SecretStr('')
    massive_api_key: SecretStr = SecretStr('')
    x_bearer_token: SecretStr = SecretStr('')
    fmp_api_key: SecretStr = SecretStr('')
    reuters_base_url: str = ''
    reuters_api_key: SecretStr = SecretStr('')
    ap_base_url: str = ''
    ap_api_key: SecretStr = SecretStr('')
    sec_user_agent: str = ''  # 'Name contact@yourdomain' (SEC fair access); empty = SEC/BLS schedule adapters NOT CONFIGURED
    quote_stale_seconds: int = Field(15, gt=0)
    analysis_seconds: int = Field(300, ge=5)
    max_document_chars: int = Field(40000, ge=100, le=40000)
    max_request_bytes: int = Field(1_000_000, ge=1024)
    max_future_seconds: int = Field(5, ge=0)
    search_hourly_budget: int = Field(60, ge=0)
    search_topic_cooldown: int = Field(60, ge=1)
    llm_provider: str = Field('ollama', pattern='^(ollama|openai)$')
    openai_base_url: str = 'https://api.openai.com/v1'
    openai_api_key: SecretStr = SecretStr('')
    provider_mode: str = Field('live', pattern='^(live|stub)$')
    congress_api_key: SecretStr = SecretStr('')
    finnhub_api_key: SecretStr = SecretStr('')
    fred_api_key: SecretStr = SecretStr('')
    bls_api_key: SecretStr = SecretStr('')
    # Alpaca Market Data (paper-account key pair; market data only, IEX feed). Both halves are secrets: the pair can trade.
    alpaca_api_key_id: SecretStr = SecretStr('')
    alpaca_api_secret_key: SecretStr = SecretStr('')


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=ENV_FILE)


CONFIG_DIR = ROOT / 'config'
RULES_RECHECK_SECONDS = 1.0
_rules_cache: tuple[tuple, bytes] | None = None
_rules_checked = 0.0


def rule_files() -> list[Path]:
    """Core rules plus one fragment per domain (config/rules.d/*.json), loaded in name order."""
    return [CONFIG_DIR / 'rules.json', *sorted((CONFIG_DIR / 'rules.d').glob('*.json'))]


def reload_rules() -> dict:
    """Bypass the re-check interval (used right after an administrative configuration write)."""
    global _rules_checked, _rules_cache, _rules_view
    _rules_checked, _rules_cache, _rules_view = 0.0, None, None
    return rules()


def _merged() -> tuple[tuple, bytes]:
    """The pickled merged rules, re-read when a file changed (files are re-checked at most once a second)."""
    global _rules_cache, _rules_checked
    now = time.monotonic()
    if _rules_cache and now - _rules_checked < RULES_RECHECK_SECONDS:
        return _rules_cache
    _rules_checked = now
    files = rule_files()
    stamp = tuple((str(path), path.stat().st_mtime_ns) for path in files)
    if _rules_cache and _rules_cache[0] == stamp:
        return _rules_cache
    merged: dict = {}
    for path in files:
        fragment = json.loads(path.read_text(encoding='utf-8'))
        duplicate = merged.keys() & fragment.keys()
        if duplicate:
            raise ValueError(f'{path.name} redefines configuration keys: {sorted(duplicate)}')
        merged.update(fragment)
    canonical = json.dumps(merged, sort_keys=True, separators=(',', ':')).encode()
    merged['content_hash'] = hashlib.sha256(canonical).hexdigest()
    _rules_cache = (stamp, pickle.dumps(merged))
    return _rules_cache


def rules() -> dict:
    """Merged, versioned rules. Fragments may not redefine a key; content_hash makes silent edits visible.
    Each call returns an independent copy (callers may modify it); files are re-checked at most once a second.
    The copy costs about a millisecond: code that runs per row or per tick reads rules_view() instead."""
    return pickle.loads(_merged()[1])


def _read_only(*_args: Any, **_kwargs: Any) -> Any:
    raise TypeError('rules_view() is shared and read-only: take a private copy with rules() before changing it')


def _thaw(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_thaw(item) for item in value]
    return value


class FrozenDict(dict):
    """A dict that refuses changes. copy/deepcopy/pickle give plain, mutable structures."""
    __setitem__ = __delitem__ = __ior__ = clear = pop = popitem = setdefault = update = _read_only  # type: ignore[assignment]

    def __copy__(self) -> dict:
        return dict(self)

    def __deepcopy__(self, memo: dict) -> dict:
        return _thaw(self)

    def __reduce__(self) -> tuple:
        return dict, (_thaw(self),)


class FrozenList(list):
    """A list that refuses changes. copy/deepcopy/pickle give plain, mutable structures."""
    __setitem__ = __delitem__ = __iadd__ = __imul__ = append = extend = insert = pop = remove = clear = sort = reverse = _read_only  # type: ignore[assignment]

    def __copy__(self) -> list:
        return list(self)

    def __deepcopy__(self, memo: dict) -> list:
        return _thaw(self)

    def __reduce__(self) -> tuple:
        return list, (_thaw(self),)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenDict((key, _freeze(item)) for key, item in value.items())
    if isinstance(value, list):
        return FrozenList(_freeze(item) for item in value)
    return value


_rules_view: tuple[bytes, dict] | None = None


def freshness_seconds(provider: str | None, default: float | None = None) -> float:
    """THE freshness limit of a live quote, in seconds, by provider: the one lookup behind HubService.quote, the
    decision gates, the pre-publication audit, data quality, reactions, the replay and outcome measurement.
    A provider listed in market_data.freshness.providers (a REST-polled feed, whose quote is older than a stream's by
    construction) has its own limit; every other provider (the one-second streams) uses `default`, the caller's
    settings.quote_stale_seconds (QUOTE_STALE_SECONDS when not given). A provider's limit is never above that
    provider's own definition of real-time (its realtime_max_age_seconds), so a quote the provider would label stale
    is never FRESH here."""
    view = rules_view()
    base = float(default if default is not None else get_settings().quote_stale_seconds)
    name = provider or ''
    listed = ((view.get('market_data') or {}).get('freshness') or {}).get('providers') or {}
    value = listed.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return base
    routing = view.get(name) if isinstance(view.get(name), dict) else (view.get('provider_routing') or {}).get(name)
    cap = (routing or {}).get('realtime_max_age_seconds')
    return float(min(value, cap)) if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0 else float(value)


def rules_view() -> dict:
    """The same merged rules as one shared, deeply read-only object (FrozenDict/FrozenList), rebuilt only when a file
    changes (same re-check as rules()). No copy per call, so per-row and per-tick code can read it freely; any attempt
    to change it raises TypeError instead of silently altering the configuration for every other reader."""
    global _rules_view
    cached = _merged()
    if _rules_view is None or _rules_view[0] is not cached[1]:
        _rules_view = (cached[1], _freeze(pickle.loads(cached[1])))
    return _rules_view[1]
