"""OpenTelemetry-ready tracing. Spans are no-ops unless OTEL_EXPORTER_OTLP_ENDPOINT is set and the optional
opentelemetry packages are installed; request/trace ids still flow into JSON logs either way."""
import importlib
import inspect
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, Literal, Protocol

from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.config import ROOT

logger = logging.getLogger(__name__)
request_id_var: ContextVar[str | None] = ContextVar('request_id', default=None)
trace_id_var: ContextVar[str | None] = ContextVar('trace_id', default=None)
REQUEST_ID_PATTERN = re.compile(r'^[A-Za-z0-9._:-]{8,128}$')
TRACEPARENT_PATTERN = re.compile(r'^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$')


class TelemetrySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    log_level: str = 'INFO'
    log_format: Literal['json', 'text'] = 'json'
    otel_exporter_otlp_endpoint: str = ''
    otel_service_name: str = 'market-intelligence-hub'


class SpanLike(Protocol):
    def set_attribute(self, key: str, value: Any) -> None: ...
    def record_exception(self, exception: BaseException) -> None: ...


class NoopSpan:
    """Keeps attributes so callers and tests see what would be exported."""

    def __init__(self, name: str):
        self.name = name
        self.attributes: dict[str, Any] = {}
        self.exception: str | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def record_exception(self, exception: BaseException) -> None:
        self.exception = type(exception).__name__


class Tracer(Protocol):
    def start_as_current_span(self, name: str) -> Any: ...


_tracer: Tracer | None = None
_listeners: list[Callable[[str, dict, float, str | None], None]] = []


def setup_tracing(settings: TelemetrySettings | None = None) -> bool:
    """Installs the OTLP exporter when configured and importable; returns True when spans are exported."""
    global _tracer
    settings = settings or TelemetrySettings()
    if not settings.otel_exporter_otlp_endpoint:
        _tracer = None
        return False
    try:
        trace = importlib.import_module('opentelemetry.trace')
        sdk = importlib.import_module('opentelemetry.sdk.trace')
        export = importlib.import_module('opentelemetry.sdk.trace.export')
        resources = importlib.import_module('opentelemetry.sdk.resources')
        otlp = importlib.import_module('opentelemetry.exporter.otlp.proto.http.trace_exporter')
    except ImportError:
        logger.warning('otel_packages_missing', extra={'hint': 'pip install .[otel]'})
        _tracer = None
        return False
    provider = sdk.TracerProvider(resource=resources.Resource.create({'service.name': settings.otel_service_name}))
    provider.add_span_processor(export.BatchSpanProcessor(otlp.OTLPSpanExporter(
        endpoint=settings.otel_exporter_otlp_endpoint.rstrip('/') + '/v1/traces')))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer('market_hub')
    return True


def use_tracer(tracer: Tracer | None) -> None:
    """Test/extension hook: any object with start_as_current_span (e.g. an SDK tracer with an in-memory exporter)."""
    global _tracer
    _tracer = tracer


def add_span_listener(listener: Callable[[str, dict, float, str | None], None]) -> None:
    """Receives (name, attributes, duration_ms, error_type) for every finished span, exported or not."""
    _listeners.append(listener)


def remove_span_listener(listener: Callable[[str, dict, float, str | None], None]) -> None:
    if listener in _listeners:
        _listeners.remove(listener)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[SpanLike]:
    started = time.monotonic()
    error: str | None = None
    context = _tracer.start_as_current_span(name) if _tracer is not None else None
    current: Any = context.__enter__() if context is not None else NoopSpan(name)
    for key, value in attributes.items():
        current.set_attribute(key, value)
    token = None
    trace_id = _trace_id(current)
    if trace_id:
        token = trace_id_var.set(trace_id)
    try:
        yield current
    except BaseException as exc:
        error = type(exc).__name__
        current.record_exception(exc)
        current.set_attribute('error.type', error)
        raise
    finally:
        duration = round((time.monotonic() - started) * 1000, 2)
        current.set_attribute('duration_ms', duration)
        if token is not None:
            trace_id_var.reset(token)
        if context is not None:
            context.__exit__(None, None, None)
        for listener in list(_listeners):
            try:
                listener(name, dict(getattr(current, 'attributes', None) or attributes), duration, error)
            except Exception:  # a metrics listener must never break the traced operation
                logger.debug('span_listener_failed')


def traced(name: str | None = None) -> Callable:
    """Decorator for sync or async callables."""
    def wrap(function: Callable) -> Callable:
        label = name or f'{function.__module__}.{function.__qualname__}'
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def run_async(*args, **kwargs):
                with span(label):
                    return await function(*args, **kwargs)
            return run_async

        @wraps(function)
        def run(*args, **kwargs):
            with span(label):
                return function(*args, **kwargs)
        return run
    return wrap


def _trace_id(current: Any) -> str | None:
    try:
        value = current.get_span_context().trace_id
        return f'{value:032x}' if value else None
    except AttributeError:
        return None


def incoming_request_id(headers: dict[str, str]) -> str:
    """Accepts a well-formed X-Request-ID or W3C traceparent trace id; otherwise a new id. Never echoes junk."""
    supplied = headers.get('x-request-id', '')
    if REQUEST_ID_PATTERN.match(supplied):
        return supplied
    match = TRACEPARENT_PATTERN.match(headers.get('traceparent', ''))
    return match.group(1) if match else uuid.uuid4().hex
