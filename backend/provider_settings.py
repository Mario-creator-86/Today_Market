"""Provider configuration from the UI (docs/PROVIDER_SETTINGS.md): the field registry, validated atomic .env writes,
the Settings -> Data providers summary and connection tests. Secret values are written, never returned or logged."""
import asyncio
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key, unset_key
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsError

from backend import config
from backend.ai import OllamaProvider, OpenAIProvider
from backend.config import Settings, rules
from backend.database import uid, utcnow
from backend.notifications import (
    PLAINTEXT_LOGIN,
    DeliveryError,
    NotificationSettings,
    build_channels,
    make_alert,
    plaintext_login,
)
from backend.providers.alpaca import AlpacaDataProvider
from backend.providers.base import ProviderUnavailable, provider_label
from backend.providers.calendars import (
    CongressCalendar,
    CryptoExpiryCalendar,
    FOMCCalendar,
    TreasuryAuctionCalendar,
)
from backend.providers.cb_calendars import CentralBankCalendar
from backend.providers.discovery import (
    EdgarFullTextProvider,
    GdeltEventsProvider,
    OfacRecentActionsProvider,
    OfacSanctionsListProvider,
    parse_lastupdate,
    sec_contact_configured,
)
from backend.providers.eia_spot import EIASpotPriceProvider
from backend.providers.finnhub import FinnhubProvider
from backend.providers.licensed import LicensedNewsProvider
from backend.providers.market import (
    BinanceProvider,
    CoinbaseProvider,
    DeribitProvider,
    MassiveProvider,
    TreasuryYieldProvider,
)
from backend.providers.news import FMPProvider, RSSProvider, SECProvider, XProvider
from backend.providers.nyfed import NYFedRatesProvider
from backend.providers.official_data import (
    BLSProvider,
    EcbDataProvider,
    EIAWeeklyPetroleumCalendar,
    EIAWeeklyPetroleumProvider,
    EurostatProvider,
    FredProvider,
    TreasuryFiscalProvider,
)
from backend.providers.positioning_sources import (
    BinanceFuturesProvider,
    CFTCCOTProvider,
    CoinbaseIntxProvider,
)
from backend.providers.registry import build_providers
from backend.providers.stubs import StubProvider

logger = logging.getLogger(__name__)
CHANNELS = ('telegram', 'email', 'webpush')
LLM_GROUPS = {'ollama', 'openai', 'llm'}
TEMP_NAME = '.env.provider-settings.tmp'
MAX_CHARS, MAX_URL_CHARS = 4096, 2048
LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}
EMAIL = re.compile(r'^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z0-9-]{2,}$')
UNPRINTABLE = {'Cc', 'Cs', 'Zl', 'Zp'}  # control characters and line separators (dotenv would split or keep them)
TEST_TIMEOUT_SECONDS = 30
TEST_INTERVAL_SECONDS = 10
OK_STATUSES = {'ONLINE', 'SENT', 'DEVELOPMENT STUB'}
OFFLINE_STATUSES = {'DISABLED', 'NOT CONFIGURED', 'DEVELOPMENT STUB', 'UNAVAILABLE'}  # results without a network call
# Worst first: a group shows the worst status among its providers (unknown statuses rank next to PENDING).
STATUS_ORDER = ['OFFLINE', 'AUTH_ERROR', 'RATE_LIMITED', 'DEGRADED', 'TIMEOUT', 'UNAVAILABLE', 'NOT CONFIGURED', 'PENDING',
                'DEVELOPMENT STUB', 'DISABLED', 'CONFIGURED', 'SENT', 'ONLINE']

_write_lock = threading.Lock()
_tested: dict[str, float] = {}  # target -> monotonic time of its last test


class RateLimited(Exception):
    """A target was tested less than TEST_INTERVAL_SECONDS ago (the API answers 429)."""


# --- registry ------------------------------------------------------------------------------------------------------

def registry() -> dict:
    return rules()['provider_settings']


def groups() -> list[dict]:
    return registry()['groups']


def group(group_id: str) -> dict | None:
    return next((g for g in groups() if g['id'] == group_id), None)


def fields() -> dict[str, dict]:
    """Every editable field by Settings/NotificationSettings attribute name, with its group id."""
    return {f['name']: dict(f, group=g['id']) for g in groups() for f in g['fields']}


def group_of_provider(name: str) -> str | None:
    return next((g['id'] for g in groups() if name in g.get('providers', [])), None)


def instances(hub: Any) -> dict[str, Any]:
    """Every provider the hub knows, including the LLM vendor that is not currently selected."""
    return {'ollama': hub.ollama, 'openai': hub.openai, **hub.providers}


def known_target(hub: Any, target: str) -> bool:
    return (target in instances(hub) or target in CHANNELS
            or target in {g['test']['target'] for g in groups() if g['test']['target']})


def affected(changed: Iterable[str]) -> tuple[set[str], bool]:
    """(provider names to rebuild, whether the LLM providers change) for saved field names."""
    registered = fields()
    names: set[str] = set()
    llm = False
    for name in changed:
        field = registered.get(name)
        if field is None:
            continue
        llm = llm or field['group'] in LLM_GROUPS
        names.update((group(field['group']) or {}).get('providers', []))
        if name == 'provider_mode':  # every paid adapter may switch between stub and real
            names.update(p for g in groups() if any(n.endswith('-stub') for n in g.get('providers', []))
                         for p in g['providers'])
    return names - {'ollama', 'openai'}, llm


# --- reading -------------------------------------------------------------------------------------------------------

def read_env_file() -> dict[str, str | None]:
    """The project .env as {UPPER-CASE KEY: raw value}; empty when the file does not exist."""
    path = config.ENV_FILE
    return {key.upper(): value for key, value in dotenv_values(path).items()} if path.exists() else {}


def saved_settings(fallback: Settings | None = None) -> tuple[Settings, NotificationSettings]:
    """Settings as saved now (.env re-read); an unreadable file falls back to the running settings."""
    try:
        main = Settings(_env_file=config.ENV_FILE)
    except ValueError:  # pydantic ValidationError or pydantic-settings SettingsError
        main = fallback or Settings(_env_file=None)
    try:
        notify = NotificationSettings(_env_file=config.ENV_FILE)
    except ValueError:
        notify = NotificationSettings(_env_file=None)
    return main, notify


def in_environment(name: str) -> bool:
    """OS environment variables win over .env in pydantic-settings, so such a field cannot be edited here."""
    return name.upper() in {key.upper() for key in os.environ}


def field_state(field: dict, main: Settings, notify: NotificationSettings, file_keys: Iterable[str],
                environment: Iterable[str]) -> dict:
    env = field['name'].upper()
    value = getattr(main if field['settings'] == 'main' else notify, field['name'])
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    secret = field['kind'] == 'secret'
    source = 'environment' if env in environment else 'env_file' if env in file_keys else 'default'
    valid = raw is not None and raw != '' and _valid(field, raw)
    # A built-in placeholder that would be rejected (e.g. an example.com contact) is not shown as a value: the field
    # stays empty so its placeholder is visible. A saved invalid value is shown, so it can be corrected.
    placeholder = source == 'default' and raw not in (None, '') and not valid
    return {'env': env, 'secret': secret, 'configured': valid, 'source': source,
            'value': None if secret or placeholder else raw, 'editable': source != 'environment'}


def _valid(field: dict, raw: Any) -> bool:
    """A placeholder such as the default SEC contact (example.com) does not count as configured."""
    try:
        validate_value(field, raw)
    except ValueError:
        return False
    return True


# --- validation and writes ------------------------------------------------------------------------------------------

def validate_value(field: dict, raw: Any) -> str | None:
    """The .env text for one value (None removes the key). Messages name the field, never the value."""
    name, kind = field['name'], field['kind']
    if raw is None:
        return None
    if kind == 'bool':
        if not isinstance(raw, bool):
            raise ValueError(f'{name}: expected true or false')
        return 'true' if raw else 'false'
    if kind == 'int':
        text = str(raw).strip() if isinstance(raw, (int, str)) and not isinstance(raw, bool) else ''
        if not re.fullmatch(r'-?\d{1,10}', text):
            raise ValueError(f'{name}: expected an integer')
        low, high = field.get('min'), field.get('max')
        if (low is not None and int(text) < low) or (high is not None and int(text) > high):
            raise ValueError(f'{name}: must be between {low} and {high}')
        return str(int(text))
    if not isinstance(raw, str):
        raise ValueError(f'{name}: expected text')
    text = raw.strip()
    if not text:
        raise ValueError(f'{name}: empty; send null to remove the value')
    if len(text) > MAX_CHARS:
        raise ValueError(f'{name}: longer than {MAX_CHARS} characters')
    if any(unicodedata.category(c) in UNPRINTABLE for c in text):
        raise ValueError(f'{name}: control characters are not allowed')
    if '${' in text:
        raise ValueError(f'{name}: "${{" is not allowed (the .env reader would interpolate it)')
    if kind == 'url':
        _check_url(name, text)
    elif kind == 'email' and not EMAIL.match(text):
        raise ValueError(f'{name}: expected an e-mail address (name@domain.tld)')
    elif kind == 'email_list':
        parts = [part.strip() for part in text.split(',')]
        if not all(EMAIL.match(part) for part in parts):
            raise ValueError(f'{name}: expected e-mail addresses separated by commas')
        text = ', '.join(parts)
    elif kind == 'select' and text not in (field.get('options') or []):
        raise ValueError(f"{name}: must be one of {', '.join(field.get('options') or [])}")
    elif name == 'sec_user_agent' and not sec_contact_configured(text):
        raise ValueError(f'{name}: must contain your contact e-mail address (not example.com)')
    elif name == 'webpush_subject' and not text.startswith(('mailto:', 'https://')):
        raise ValueError(f'{name}: must start with mailto: or https://')
    return text


def _check_url(name: str, text: str) -> None:
    if len(text) > MAX_URL_CHARS:
        raise ValueError(f'{name}: longer than {MAX_URL_CHARS} characters')
    try:
        parts = urlsplit(text)
        host = parts.hostname
        userinfo = parts.username or parts.password
    except ValueError:
        raise ValueError(f'{name}: expected an http(s):// URL') from None
    if parts.scheme not in {'http', 'https'} or not host or any(c.isspace() for c in text):
        raise ValueError(f'{name}: expected an http(s):// URL')
    if userinfo:
        raise ValueError(f'{name}: credentials inside the URL are not allowed; use the key field')
    if parts.scheme == 'http' and host not in LOCAL_HOSTS:
        raise ValueError(f'{name}: must use https:// (http:// is allowed only for localhost)')


def _check_candidate(path: Path) -> NotificationSettings:
    """Both settings classes must load from the candidate file before it replaces .env."""
    invalid: set[str] = set()
    notify: NotificationSettings | None = None
    for cls in (Settings, NotificationSettings):
        try:
            loaded = cls(_env_file=path)
        except ValidationError as exc:
            invalid |= {str(error['loc'][0]) for error in exc.errors() if error['loc']}
        except SettingsError as exc:  # a complex value (CORS_ORIGINS) that is not JSON
            invalid |= set(re.findall(r'field "(\w+)"', str(exc))) or {cls.__name__}
        else:
            if isinstance(loaded, NotificationSettings):
                notify = loaded
    if invalid or notify is None:
        raise ValueError(f"Rejected, .env left unchanged: invalid {', '.join(sorted(invalid))}")
    return notify


def destination_secrets() -> dict[str, str]:
    """Destination field -> the secret that is sent to it (base URLs, the SMTP server)."""
    return dict(registry().get('destination_secrets') or {})


def _effective(name: str, file_values: dict[str, str | None]) -> str:
    """The text a settings class would use for a registered field: the OS environment, then .env, then the default."""
    environment = {key.upper(): value for key, value in os.environ.items()}
    if name.upper() in environment:
        return environment[name.upper()].strip()
    if name.upper() in file_values:
        return (file_values[name.upper()] or '').strip()
    cls = Settings if fields()[name]['settings'] == 'main' else NotificationSettings
    default = cls.model_fields[name].default
    return str(default.get_secret_value() if isinstance(default, SecretStr) else default).strip()


def _check_destinations(values: dict[str, str | None], before: dict[str, str | None],
                        after: dict[str, str | None]) -> None:
    """A stored secret never follows a new address: a change of destination (base URL, SMTP host or port) must carry
    its secret in the same request (re-entered, or null to remove it). Nothing is required when no secret is stored
    or the new destination is empty (nothing is sent). Messages name fields, never values."""
    moved: dict[str, list[str]] = {}
    for destination, secret in destination_secrets().items():
        if destination not in values or secret in values:
            continue
        target = _effective(destination, after)
        if not target or target == _effective(destination, before) or not _effective(secret, before):
            continue
        moved.setdefault(secret, []).append(destination)
    problems = []
    for secret, destinations in moved.items():
        changed = ', '.join(destinations)
        if in_environment(secret):
            problems.append(f'{changed}: {secret} is set in the OS environment, so this destination can only be '
                            f'changed there')
        else:
            problems.append(f'{changed} changed without {secret}: re-enter {secret} in the same request '
                            f'(a stored key is never sent to a new address)')
    if problems:
        raise ValueError('Rejected, .env left unchanged: ' + '; '.join(problems))


def _check_smtp_login(values: dict[str, str | None], notify: NotificationSettings) -> None:
    """E-mail edits may not leave a login that would travel in clear text (no SSL/TLS or STARTTLS, remote server)."""
    if any(name.startswith('smtp_') for name in values) and plaintext_login(notify):
        raise ValueError('Rejected, .env left unchanged: smtp_ssl, smtp_starttls: a login to a server other than '
                         'localhost needs SSL/TLS or STARTTLS (the password would be sent in clear text)')


def _check_round_trip(before: dict[str, str | None], after: dict[str, str | None], expected: dict[str, str | None],
                      names: dict[str, str]) -> None:
    """The edited copy must read back exactly: every written key has its value, every removed key is gone and every
    other key is unchanged (an older python-dotenv mis-escaped backslashes and could swallow the next line)."""
    wanted = {key: value for key, value in before.items() if key not in expected}
    wanted.update({key: value for key, value in expected.items() if value is not None})
    if after != wanted:
        missing = object()
        broken = {key for key in set(wanted) | set(after) if wanted.get(key, missing) != after.get(key, missing)}
        shown = sorted({names.get(key, key.lower()) for key in broken})
        raise ValueError(f"Rejected, .env left unchanged: {', '.join(shown)} would not read back exactly "
                         f"(python-dotenv 1.2.3 or later is required)")


def _private_copy(path: Path, temporary: Path) -> None:
    """The working copy is created 0600 before any secret is written into it (a crashed earlier copy is replaced)."""
    temporary.unlink(missing_ok=True)
    handle = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0), 0o600)
    with os.fdopen(handle, 'wb') as stream:
        stream.write(path.read_bytes() if path.exists() else b'')


def write_values(changes: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Writes validated values to the project .env: a private (0600) same-directory copy is edited, must read back
    exactly and load in Settings and NotificationSettings before it atomically replaces the file (an existing file
    keeps its mode, a new one is 0600). Returns (changed, removed) field names."""
    registered = fields()
    unknown = sorted(set(changes) - set(registered))
    if unknown:
        raise ValueError(f"Not editable here: {', '.join(unknown)}")
    values = {name: validate_value(registered[name], raw) for name, raw in changes.items()}
    path = config.ENV_FILE
    temporary = path.with_name(TEMP_NAME)
    changed: list[str] = []
    removed: list[str] = []
    with _write_lock:  # one edit at a time: read-modify-write of the same file
        try:
            _private_copy(path, temporary)
            parsed = dotenv_values(temporary)
            spelled = {key.upper(): key for key in parsed}  # keep an existing key's spelling
            before = {key.upper(): value for key, value in parsed.items()}
            after = dict(before)
            for name, value in values.items():
                if value is not None:
                    after[name.upper()] = value
                else:
                    after.pop(name.upper(), None)
            _check_destinations(values, before, after)
            expected: dict[str, str | None] = {}
            for name, value in values.items():
                key = spelled.get(name.upper(), name.upper())
                if value is not None:
                    set_key(temporary, key, value, quote_mode='always')
                    changed.append(name)
                    expected[key] = value
                elif name.upper() in spelled:
                    unset_key(temporary, key)
                    removed.append(name)
                    expected[key] = None
            _check_round_trip(dict(parsed), dict(dotenv_values(temporary)), expected,
                              {spelled.get(name.upper(), name.upper()): name for name in values})
            _check_smtp_login(values, _check_candidate(temporary))
            if path.exists():
                shutil.copymode(path, temporary)
            else:
                os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return changed, removed


# --- summary (GET /api/admin/provider-settings) -----------------------------------------------------------------------

def status_of(provider: Any) -> str:
    return 'DISABLED' if not getattr(provider, 'enabled', True) else str(provider.health.get('status') or 'PENDING')


def worst(statuses: Iterable[str]) -> str:
    rank = {status: index for index, status in enumerate(STATUS_ORDER)}
    return min(statuses, key=lambda s: rank.get(s, rank['PENDING']))


def provider_row(name: str, provider: Any) -> dict:
    return {'name': name, 'label': provider.health.get('label') or provider_label(name), 'status': status_of(provider),
            'enabled': getattr(provider, 'enabled', True), 'stub': isinstance(provider, StubProvider)}


def can_test(provider: Any) -> bool:
    return isinstance(provider, StubProvider) or any(isinstance(provider, cls) for cls, _ in PROBES)


def group_view(hub: Any, spec: dict, main: Settings, notify: NotificationSettings, file_keys: set[str],
               environment: set[str], channels: dict[str, Any]) -> dict:
    known = instances(hub)
    providers = [provider_row(name, known[name]) for name in spec.get('providers', []) if name in known]
    statuses = [p['status'] for p in providers]
    if spec.get('channel'):
        statuses.append(channels[spec['channel']].status()['status'])
    test = spec['test']
    return {'id': spec['id'], 'label': spec['label'], 'category': spec['category'], 'description': spec['description'],
            'signup_url': spec.get('signup_url'), 'pricing': spec.get('pricing'),
            # Settings-only groups (AI provider, provider mode) always have an effective value.
            'status': worst(statuses) if statuses else 'CONFIGURED', 'providers': providers,
            'fields': [dict({k: field.get(k) for k in ('name', 'label', 'kind', 'required', 'options', 'min', 'max', 'help',
                                                       'placeholder')},
                            **field_state(field, main, notify, file_keys, environment)) for field in spec['fields']],
            'test': {'available': test['target'] is not None, 'target': test['target'], 'label': test['label'],
                     'note': test.get('note')}}


def summary(hub: Any, only: str | None = None) -> dict:
    from backend.workers import celery_owned
    main, notify = saved_settings(hub.settings)
    file_keys, environment = set(read_env_file()), {key.upper() for key in os.environ}
    channels = {c.name: c for c in build_channels(hub.store, notify)}
    views = [group_view(hub, spec, main, notify, file_keys, environment, channels) for spec in groups()
             if only is None or spec['id'] == only]
    return {'env_file': str(config.ENV_FILE), 'runtime': 'celery' if celery_owned() else 'in_process', 'groups': views,
            'providers': [dict(provider_row(name, provider), group=group_of_provider(name), testable=can_test(provider))
                          for name, provider in hub.providers.items()]}


# --- connection tests ----------------------------------------------------------------------------------------------

async def _ollama(provider: OllamaProvider, _today: date) -> None:
    await provider.models()  # catalog: proves the base URL
    await provider.search('connection test')  # keyed call: proves the key


async def _gdelt(provider: GdeltEventsProvider, _today: date) -> None:
    parse_lastupdate((await provider.get(rules()['discovery']['gdelt']['lastupdate_path'])).text)  # never the export zip


def _cftc_path() -> str:
    return f"/resource/{rules()['positioning_sources']['cftc_cot']['reports']['legacy']['dataset']}.json"


# The cheapest read-only call per adapter (subclasses first): one small request, never a heavy download or a write.
PROBES: list[tuple[type, Callable[[Any, date], Awaitable[Any]]]] = [
    (OllamaProvider, _ollama),
    (OpenAIProvider, lambda p, _: p.models()),
    (FredProvider, lambda p, _: p.observations('DGS10', sort_order='desc', limit=1)),
    (BLSProvider, lambda p, day: p.series(day.year, day.year, ['CUUR0000SA0'])),
    (FinnhubProvider, lambda p, _: p.quote('AAPL')),
    (AlpacaDataProvider, lambda p, _: p.snapshots(['SPY'])),
    (MassiveProvider, lambda p, _: p.quotes()),
    (XProvider, lambda p, _: p.search('stock market')),
    (FMPProvider, lambda p, day: p.calendar(day.isoformat(), day.isoformat())),
    (CongressCalendar, lambda p, day: p.calendar(day.isoformat(), day.isoformat())),
    (SECProvider, lambda p, _: p.filings('320193')),
    (EdgarFullTextProvider, lambda p, day: p.search({'id': 'connection-test', 'q': '"annual report"', 'forms': ['8-K']},
                                                    day - timedelta(days=1), day)),
    (LicensedNewsProvider, lambda p, _: p.documents()),
    (RSSProvider, lambda p, _: p.documents()),
    (CentralBankCalendar, lambda p, _: p.meetings()),
    (DeribitProvider, lambda p, _: p.futures('BTC')),
    (BinanceProvider, lambda p, _: p.quotes()),
    (CoinbaseProvider, lambda p, _: p.quotes()),
    (TreasuryYieldProvider, lambda p, _: p.quotes()),
    (TreasuryAuctionCalendar, lambda p, day: p.calendar(day.isoformat(), day.isoformat())),
    (FOMCCalendar, lambda p, day: p.calendar(day.isoformat(), day.isoformat())),
    (CryptoExpiryCalendar, lambda p, _: p.request('GET', '/public/get_instruments',
                                                  params={'currency': 'BTC', 'kind': 'future', 'expired': 'false'})),
    (GdeltEventsProvider, _gdelt),
    (CFTCCOTProvider, lambda p, _: p.request('GET', _cftc_path(), params={'$limit': 1})),
    (BinanceFuturesProvider, lambda p, _: p._get('usdm', '/fapi/v1/premiumIndex', {'symbol': 'BTCUSDT'}, 10)),
    (CoinbaseIntxProvider, lambda p, _: p._get('/api/v1/instruments/BTC-PERP')),
    (OfacSanctionsListProvider, lambda p, day: p.publications(day.year)),
    (OfacRecentActionsProvider, lambda p, _: p.actions()),
    (NYFedRatesProvider, lambda p, _: p.policy_rate()),
    (EcbDataProvider, lambda p, _: p.policy_rates()),
    (EurostatProvider, lambda p, _: p.hicp_flash()),
    (TreasuryFiscalProvider, lambda p, day: p.tga(since=day - timedelta(days=7))),
    (EIAWeeklyPetroleumProvider, lambda p, _: p.inventories()),
    (EIAWeeklyPetroleumCalendar, lambda p, day: p.calendar(day.isoformat(), day.isoformat())),
    (EIASpotPriceProvider, lambda p, _: p.quotes()),
]


def note_for(target: str) -> str | None:
    spec = next((g for g in groups() if g['test']['target'] == target or target in g.get('providers', [])
                 or g.get('channel') == target), None)
    return spec['test'].get('note') if spec else None


def fresh_provider(target: str, main: Settings) -> Any:
    """A new instance from the saved settings: tests never touch the running provider's health, circuit, cursors
    or caches. A '<name>' target resolves to '<name>-stub' (and back) when the saved mode builds the other one."""
    if target == 'ollama':
        return OllamaProvider(main.ollama_base_url, main.ollama_api_key.get_secret_value())
    if target == 'openai':
        return OpenAIProvider(main.openai_base_url, main.openai_api_key.get_secret_value())
    built = build_providers(main)
    base = target.removesuffix('-stub')
    return built.get(target) or built.get(base) or built.get(f'{base}-stub')


def _scrub(text: str, secrets: Iterable[str]) -> str:
    """Details never carry a URL or a credential, whatever produced them."""
    text = re.sub(r'\b[a-z][a-z0-9+.-]*://\S+', '[url]', text, flags=re.IGNORECASE)
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4:
            text = text.replace(secret, '[REDACTED]')
    return text[:300]


def _secret_values(*sources: Any) -> list[str]:
    return [value.get_secret_value() for source in sources for name in type(source).model_fields
            if isinstance(value := getattr(source, name), SecretStr) and value.get_secret_value()]


def _provider_failure(provider: Any, exc: ProviderUnavailable) -> tuple[str, str]:
    code, health = exc.status_code, provider.health
    if code == 451:
        return 'DEGRADED', 'Unavailable - restricted location (HTTP 451).'
    if code in {401, 403}:
        return 'AUTH_ERROR', f'Access denied: check the key or the account (HTTP {code}).'
    if code == 400 and provider.name == 'fred':  # FRED answers an invalid api_key with 400
        return 'AUTH_ERROR', 'FRED rejected the API key (HTTP 400).'
    if code == 429:
        return 'RATE_LIMITED', 'The provider is rate limiting requests (HTTP 429); try again later.'
    if code is not None and 400 <= code < 500:
        return 'DEGRADED', f'The provider rejected the request (HTTP {code}).'
    if code is not None:
        return 'OFFLINE', f'Server error (HTTP {code}).'
    if health.get('status') in {'AUTH_ERROR', 'RATE_LIMITED', 'DEGRADED'}:
        return health['status'], str(health.get('error') or 'The provider reported an error.')
    if health.get('status') == 'ONLINE':  # answered, then the payload was refused
        return 'DEGRADED', f'Connected, but the response was refused: {exc}'
    return 'OFFLINE', 'Could not connect (network error, or the server did not answer).'


async def _test_provider(hub: Any, target: str) -> tuple[str, str]:
    current = instances(hub).get(target)
    main, _ = saved_settings(hub.settings)
    provider = fresh_provider(target, main)
    flags = hub.enabled_flags()
    if (current is not None and not getattr(current, 'enabled', True)) or (
            provider is not None and flags.get(provider.name) is False):
        return 'DISABLED', 'Disabled: switch it on to test the connection.'
    if provider is None:
        return 'UNAVAILABLE', 'Not available with the saved settings.'
    if isinstance(provider, StubProvider):
        return 'DEVELOPMENT STUB', 'Development stub (PROVIDER_MODE=stub and no key): serves sample data, no network call.'
    if not provider.configured:
        return 'NOT CONFIGURED', 'Not configured: save the required settings first.'
    probe = next((call for cls, call in PROBES if isinstance(provider, cls)), None)
    if probe is None:
        return 'UNAVAILABLE', 'No connection test exists for this provider.'
    try:
        await asyncio.wait_for(probe(provider, utcnow().date()), TEST_TIMEOUT_SECONDS)
    except TimeoutError:
        return 'TIMEOUT', f'No answer within {TEST_TIMEOUT_SECONDS} seconds.'
    except ProviderUnavailable as exc:
        return _provider_failure(provider, exc)
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning('provider_test_failed', extra={'provider': target, 'error_type': type(exc).__name__})
        return 'DEGRADED', 'Connected, but the response was not understood.'
    except Exception as exc:  # a test never answers 500: any other failure is a failed test (logged by type only)
        logger.warning('provider_test_failed', extra={'provider': target, 'error_type': type(exc).__name__})
        return 'DEGRADED', 'The test failed unexpectedly; see the server log.'
    return 'ONLINE', 'Connection OK.'


def _delivery_failure(target: str, reason: str) -> tuple[str, str]:
    match = re.search(r'HTTP (\d{3})', reason)
    code = int(match[1]) if match else None
    if reason == 'SMTP authentication failed':
        return 'AUTH_ERROR', 'The SMTP server rejected the username or password.'
    if reason == PLAINTEXT_LOGIN:
        return 'DEGRADED', ('Not sent: the login would travel in clear text. Turn on STARTTLS or SSL/TLS (only a server '
                            'on this computer may use a plain connection).')
    if reason == 'no active push subscriptions':
        return 'NOT CONFIGURED', 'No browser is subscribed yet: allow notifications in a browser first.'
    if code == 429:
        return 'RATE_LIMITED', 'The service is rate limiting messages (HTTP 429); try again later.'
    if code in {401, 403} or (target == 'telegram' and code == 404):
        return 'AUTH_ERROR', f'The credentials were rejected (HTTP {code}).'
    if target == 'telegram' and code == 400:
        return 'DEGRADED', 'Telegram refused the message: check the chat ID and that the bot is in the chat (HTTP 400).'
    if code is not None and code < 500:
        return 'DEGRADED', f'Delivery was refused (HTTP {code}).'
    if reason.startswith('transport:'):  # the exception type stays in the server log, never in the message
        return 'OFFLINE', 'Delivery failed: the service could not be reached.'
    if reason.startswith('smtp:'):
        return 'OFFLINE', 'Delivery failed: the mail server could not be reached or refused the message.'
    return 'OFFLINE', f'Delivery failed ({reason}).'


async def _test_channel(hub: Any, target: str, options: dict) -> tuple[str, str]:
    _, notify = saved_settings(hub.settings)
    channel = {c.name: c for c in build_channels(hub.store, notify, **options)}[target]
    if channel.status()['status'] == 'UNAVAILABLE':
        return 'UNAVAILABLE', 'pywebpush is not installed (pip install .[notifications]).'
    if not channel.configured:
        return 'NOT CONFIGURED', 'Not configured: save the required settings first.'
    alert = make_alert('PROVIDER', 'LOW', 'Test message from Market Intelligence',
                       'Sent from Settings -> Data providers to check this alert channel. No action is needed.',
                       utcnow(), f'channel-test:{target}:{uid()}')
    try:
        await asyncio.wait_for(channel.send(alert), TEST_TIMEOUT_SECONDS)  # the channel itself: no dispatcher records
    except TimeoutError:
        return 'TIMEOUT', f'No answer within {TEST_TIMEOUT_SECONDS} seconds.'
    except DeliveryError as exc:
        return _delivery_failure(target, exc.reason)
    except Exception as exc:  # a test never answers 500 (logged by type only: the text may carry an address)
        logger.warning('provider_test_failed', extra={'provider': target, 'error_type': type(exc).__name__})
        return 'OFFLINE', 'Delivery failed unexpectedly; see the server log.'
    return 'SENT', 'Test message sent.'


async def run_test(hub: Any, target: str, *, channel_options: dict | None = None) -> dict:
    """One connection test on fresh instances (channels really send one message). channel_options are passed to
    build_channels (transport, smtp_factory, push_sender)."""
    now = time.monotonic()
    last = _tested.get(target)
    if last is not None and now - last < TEST_INTERVAL_SECONDS:
        raise RateLimited(f'{target} was tested less than {TEST_INTERVAL_SECONDS} seconds ago; try again shortly')
    _tested[target] = now
    if target in CHANNELS:
        status, detail = await _test_channel(hub, target, channel_options or {})
    else:
        status, detail = await _test_provider(hub, target)
    main, notify = saved_settings(hub.settings)
    return {'target': target, 'ok': status in OK_STATUSES, 'status': status,
            'detail': _scrub(detail, _secret_values(main, notify, hub.settings)),
            'latency_ms': None if status in OFFLINE_STATUSES else round((time.monotonic() - now) * 1000),
            'checked_at': utcnow().isoformat(), 'note': note_for(target)}
