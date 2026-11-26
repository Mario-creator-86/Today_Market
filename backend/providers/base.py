import asyncio
import html
import math
import re
import ssl
import time
from contextvars import ContextVar
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import httpx

from backend.config import rules_view
from backend.database import utcnow

_TLS: ssl.SSLContext | None = None


def tls_context() -> ssl.SSLContext:
    """One verifying TLS context shared by every provider request. Building it loads and parses the CA bundle (tens of
    milliseconds of CPU on the event loop), which a client per request otherwise repeated for every call."""
    global _TLS
    if _TLS is None:
        _TLS = httpx.create_ssl_context()
    return _TLS


@runtime_checkable
class MarketDataProvider(Protocol):
    async def quotes(self) -> list[dict]: ...


@runtime_checkable
class NewsProvider(Protocol):
    async def documents(self) -> list[dict]: ...


@runtime_checkable
class EconomicCalendarProvider(Protocol):
    async def calendar(self, start: str, end: str) -> list[dict]: ...


@runtime_checkable
class SocialProvider(Protocol):
    async def search(self, query: str) -> list[dict]: ...


@runtime_checkable
class RegulatoryProvider(Protocol):
    async def filings(self, cik: str) -> dict: ...


@runtime_checkable
class CryptoDerivativesProvider(Protocol):
    async def metrics(self, currency: str) -> list[dict]: ...


CAPABILITIES: dict[str, type] = {
    'market_data': MarketDataProvider, 'news': NewsProvider, 'calendar': EconomicCalendarProvider,
    'social': SocialProvider, 'regulatory': RegulatoryProvider, 'derivatives': CryptoDerivativesProvider}

# Official sites (e.g. BLS) reject anonymous clients; identify the application honestly.
DEFAULT_HEADERS = {'User-Agent': 'MarketIntelligenceHub/1.0 (+research; contact via SEC_USER_AGENT)',
                   'Accept': 'application/json, application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8'}


# Markup in feed text: comments, script/style blocks and tags (a '<' followed by a letter or '/', so 'a < b' stays).
MARKUP = re.compile(r'<!--.*?-->|<(script|style)\b.*?</\1\s*>|</?[A-Za-z][A-Za-z0-9:-]*(?:\s[^<>]*)?/?>',
                    re.DOTALL | re.IGNORECASE)
ENTITY = re.compile(r'&(?:#\d{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});')
CUT_TAG = re.compile(r'<[A-Za-z/!][^<>]*$')  # a tag cut off where the text was truncated
SPACES = re.compile(r'\s+')


def plain_text(text: str | None) -> str:
    """Feed or page text as people read it: tags removed, character entities decoded ('&amp;' -> '&', '&#160;' -> a
    space) and whitespace collapsed. RSS descriptions are HTML escaped inside XML, and some feeds escape twice, so a
    second pass decodes what the first one revealed. Text without tags or entities keeps its line breaks. Never HTML
    rendering: the result is only ever shown as text."""
    value = text or ''
    for _ in range(2):
        if not (MARKUP.search(value) or ENTITY.search(value)):
            break
        # Only complete references (ending in ';') are decoded: html.unescape would also read '&region=' in a URL as
        # the legacy '&reg' and corrupt it.
        stripped = CUT_TAG.sub(' ', MARKUP.sub(' ', value))
        value = SPACES.sub(' ', ENTITY.sub(lambda match: html.unescape(match[0]), stripped))
    return value.strip()


# Short plain-English causes for provider health, alerts and connection tests (never a URL, key or response body);
# the exception class stays in health['error_type'] for diagnostics.
HTTP_FAILURES = {400: 'Request rejected', 401: 'Authentication rejected', 403: 'Access denied', 404: 'Not found',
                 408: 'Timed out', 410: 'No longer available', 429: 'Rate limited', 451: 'Unavailable in this location'}


def failure_text(exc: BaseException, status_code: int | None = None) -> str:
    """'Rate limited (HTTP 429)', 'Timed out', 'Connection failed', ...: what a person can act on."""
    if status_code is not None:
        phrase = HTTP_FAILURES.get(status_code) or ('Server error' if status_code >= 500 else 'Unexpected redirect'
                                                    if 300 <= status_code < 400 else 'Request rejected')
        return f'{phrase} (HTTP {status_code})'
    if isinstance(exc, httpx.TimeoutException):
        return 'Timed out'
    if isinstance(exc, httpx.ConnectError):
        return 'Connection failed'
    if isinstance(exc, httpx.TooManyRedirects):
        return 'Too many redirects'
    if isinstance(exc, httpx.TransportError):  # read/write errors, dropped connections, protocol errors
        return 'Network error'
    return 'Invalid request or response'


def provider_label(name: str) -> str:
    """Human name of a provider id from config provider_labels ('finnhub' -> 'Finnhub (US equities)'). A news feed is
    named after its source, a development stub after the adapter it replaces; an unknown id is returned unchanged."""
    config = rules_view()
    labels = config.get('provider_labels') or {}
    known = (labels.get('names') or {}).get(name)
    if known:
        return str(known['label'])
    if name.endswith('-stub') and labels.get('stub'):
        return str(labels['stub']).format(label=provider_label(name.removesuffix('-stub')))
    sources = {s['id']: s['name'] for s in config.get('sources', [])}
    if name in sources and labels.get('feed'):
        return str(labels['feed']).format(source=sources[name])
    owner = max((s for s in sources if name.startswith(f'{s}-')), key=len, default=None)
    if owner and labels.get('extra_feed'):
        return str(labels['extra_feed']).format(source=sources[owner], feed=name[len(owner) + 1:].replace('_', ' '))
    return name


def provider_category(name: str) -> str | None:
    """Settings category id (provider_settings.categories) a provider is grouped under, or None when not configured."""
    config = rules_view()
    labels = config.get('provider_labels') or {}
    known = (labels.get('names') or {}).get(name)
    if known:
        return known.get('category')
    base = name.removesuffix('-stub')
    if base != name:
        return provider_category(base)
    sources = [s['id'] for s in config.get('sources', [])]
    return labels.get('feed_category') if any(name == s or name.startswith(f'{s}-') for s in sources) else None


def identity(name: str) -> dict:
    """Health fields every provider row carries: its id, human label and settings category."""
    return {'name': name, 'label': provider_label(name), 'category': provider_category(name)}


def label_day(day: date | datetime | str) -> str:
    """'Fri 25 Sep' for provider labels (no ISO dates in text shown to people)."""
    value = date.fromisoformat(day[:10]) if isinstance(day, str) else day
    return f'{value:%a} {value.day} {value:%b}'


def label_time(moment: datetime, zone: ZoneInfo, zone_name: str) -> str:
    """'Fri 25 Sep 16:59 ET': a timestamp in the venue's own zone, minute precision, for provider labels."""
    local = moment.astimezone(zone)
    return f'{label_day(local)} {local:%H:%M} {zone_name}'


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """An HTTP Retry-After header in seconds from now: delta-seconds ('30') or an HTTP date ('Wed, 21 Oct 2026
    07:28:00 GMT'; a date in the past is 0). None when absent or unreadable, so the caller applies its own default."""
    text = (value or '').strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:  # an HTTP date is always GMT
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - (now or utcnow())).total_seconds())


class SendMeter:
    """HTTP requests one call really sent (AIService counts them against the daily AI budget): a chat call's transport
    retries and the limiter's retries after a 429/5xx each send the whole prompt again. `seen` is set once the call
    reached HTTPProvider.request at all, so a test double that never does is told apart from a refused (circuit open,
    not configured) call that sent nothing."""

    def __init__(self) -> None:
        self.sends = 0
        self.seen = False


# Set by AIService around one chat call; HTTPProvider.request counts each HTTP request it sends into it.
SEND_METER: ContextVar[SendMeter | None] = ContextVar('provider_send_meter', default=None)


class ProviderUnavailable(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after  # seconds, from the Retry-After header of an HTTP 429 (None when absent)


class HTTPProvider:
    def __init__(self, name: str, base_url: str, key: str = '', configured: bool = True, timeout: float = 15,
                 headers: dict | None = None):
        self.name, self.base_url, self.key = name, base_url.rstrip('/'), key
        self.headers_default = dict(DEFAULT_HEADERS, **(headers or {}))
        self.configured, self.timeout = configured, timeout
        self.failures = 0
        self.next_attempt = 0.0
        self.health: dict = {**identity(name), 'status': 'PENDING' if configured else 'NOT CONFIGURED',
                             'latency_ms': None, 'last_success': None, 'error': None, 'error_type': None}

    async def request(self, method: str, path: str, *, not_modified_ok: bool = False, timeout: float | None = None,
                      attempts: int = 3, transport_opens_circuit: bool = True, rate_limit_opens_circuit: bool = True,
                      **kwargs):
        """not_modified_ok: a conditional GET (If-Modified-Since) may answer 304, which is a success, not an error.
        timeout/attempts override the provider defaults for one call (a long LLM generation). With
        transport_opens_circuit False (LLM chat), a transport failure (e.g. a read timeout of one slow generation) or a
        5xx on the last attempt is reported as DEGRADED, does not open the circuit and is not counted in the failures
        that size a later circuit pause, so other calls to the same provider are not blocked. With
        rate_limit_opens_circuit False (LLM chat only), an HTTP 429 sets RATE_LIMITED and raises at once with the
        parsed Retry-After, without opening the circuit or counting a failure: the caller's limiter
        (backend/llm_limiter.py) backs off. 401/403 open the circuit either way. Every HTTP request sent is counted in
        the SEND_METER of the calling context, if one is set."""
        meter = SEND_METER.get()
        if meter is not None:
            meter.seen = True
        if not self.configured:
            raise ProviderUnavailable(f'{self.name}: NOT CONFIGURED')
        if time.monotonic() < self.next_attempt:
            raise ProviderUnavailable(f'{self.name}: circuit open')
        started = time.monotonic()
        attempts = max(1, attempts)
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=timeout or self.timeout, follow_redirects=False,
                                             headers=self.headers_default, verify=tls_context()) as client:
                    if meter is not None:
                        meter.sends += 1
                    response = await client.request(method, self.base_url + path, **kwargs)
                if not (not_modified_ok and response.status_code == 304):
                    response.raise_for_status()
                self.failures = 0
                self.health.update(status='ONLINE', latency_ms=round((time.monotonic() - started) * 1000, 1),
                                   last_success=utcnow().isoformat(), error=None, error_type=None)
                return response
            except (httpx.HTTPError, ValueError) as exc:
                status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                status = 'AUTH_ERROR' if status_code in {401, 403} else 'RATE_LIMITED' if status_code == 429 else 'OFFLINE'
                # Never persist request URLs or exception bodies: they can contain API keys.
                self.health.update(status=status, error=failure_text(exc, status_code), error_type=type(exc).__name__)
                last = attempt == attempts - 1
                asked = None  # the Retry-After of a 429, in seconds
                if status_code == 429 and isinstance(exc, httpx.HTTPStatusError):
                    try:
                        asked = retry_after_seconds(exc.response.headers.get('Retry-After'))
                    except AttributeError:
                        asked = None
                if status_code == 429 and not rate_limit_opens_circuit:
                    raise ProviderUnavailable(self.health['error'], status_code, asked) from None
                if not transport_opens_circuit and status_code not in {401, 403, 429}:
                    # A slow generation, a dropped connection or an overloaded backend (5xx) on a chat call: the
                    # attempt is retried, and the last one is DEGRADED for this call only.
                    if last or (status_code is not None and status_code < 500):
                        self.health['status'] = 'DEGRADED'
                        raise ProviderUnavailable(self.health['error'], status_code) from None
                    await asyncio.sleep(.25 * 2 ** attempt)
                    continue
                self.failures += 1
                if status_code is not None and 400 <= status_code < 500 and status_code not in {401, 403, 429}:
                    self.health['status'] = 'DEGRADED'
                    raise ProviderUnavailable(self.health['error'], status_code) from None
                retry_after = min(3600, 60 if asked is None else asked) if status_code == 429 else 0.0
                if status_code in {401, 403, 429} or last:
                    self.next_attempt = time.monotonic() + max(retry_after, min(300, 2 ** self.failures))
                    raise ProviderUnavailable(self.health['error'], status_code) from None
                await asyncio.sleep(.25 * 2 ** attempt)
        raise ProviderUnavailable('request failed')
