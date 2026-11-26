import asyncio
import importlib.util
import io
import json
import logging
import time
from contextlib import contextmanager

import pytest

from backend import metrics, telemetry
from backend.main import JsonFormatter, configure_logging, redact, secret_values
from backend.notifications import NotificationSettings


@pytest.fixture
def json_logs():
    stream = io.StringIO()
    configure_logging(telemetry.TelemetrySettings(_env_file=None, log_format='json', log_level='INFO'),
                      ['super-secret-value'], stream)
    yield stream
    configure_logging(telemetry.TelemetrySettings(_env_file=None), [])


def lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.startswith('{')]


def test_json_logs_keep_extras_request_id_and_redact_secrets(json_logs):
    token = telemetry.request_id_var.set('req-12345678')
    try:
        logging.getLogger('backend.runtime').warning(
            'news_ingestion_failed https://api.example.test/v3/cal?from=1&apikey=abc123XYZ&x=1 super-secret-value',
            extra={'provider': 'calendar', 'error_type': 'HTTPError', 'url': 'https://x.test/?token=tok-999',
                   'headers': {'Authorization': 'Bearer abc.def.ghi'}})
    finally:
        telemetry.request_id_var.reset(token)
    [entry] = [e for e in lines(json_logs) if e['logger'] == 'backend.runtime']
    assert entry['provider'] == 'calendar' and entry['error_type'] == 'HTTPError' and entry['request_id'] == 'req-12345678'
    text = json.dumps(entry)
    assert 'abc123XYZ' not in text and 'tok-999' not in text and 'abc.def.ghi' not in text and 'super-secret-value' not in text
    assert 'apikey=[REDACTED]' in entry['message']


def test_exceptions_are_logged_with_type_and_httpx_urls_are_quiet(json_logs):
    try:
        raise ValueError('bad payload apikey=leak123')
    except ValueError:
        logging.getLogger('backend.test').exception('parse_failed')
    logging.getLogger('httpx').info('HTTP Request: GET https://x.test/?apikey=leak456')
    entries = lines(json_logs)
    [failure] = [e for e in entries if e['message'] == 'parse_failed']
    assert failure['exc_type'] == 'ValueError' and 'leak123' not in failure['exception']
    assert not any(e['logger'] == 'httpx' for e in entries)
    assert logging.getLogger('uvicorn.access').propagate and not logging.getLogger('uvicorn.access').handlers


def test_redaction_rules_and_secret_collection():
    assert redact('https://api.telegram.org/bot123456:AAE-secret_x/sendMessage') == 'https://api.telegram.org/bot[REDACTED]/sendMessage'
    assert redact('GET /q?key=abc&symbol=BTC') == 'GET /q?key=[REDACTED]&symbol=BTC'
    found = secret_values(NotificationSettings(_env_file=None, telegram_bot_token='123456:token-value', smtp_password='short'))
    assert found == ['123456:token-value']  # values under 6 characters would redact ordinary words
    record = logging.LogRecord('x', logging.INFO, __file__, 1, 'hello %s', ('world',), None)
    assert json.loads(JsonFormatter().format(record))['message'] == 'hello world'


def test_request_ids_accept_valid_values_and_traceparent_only():
    assert telemetry.incoming_request_id({'x-request-id': 'abc-12345678'}) == 'abc-12345678'
    assert telemetry.incoming_request_id({'x-request-id': '<script>'}) != '<script>'
    traceparent = '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01'
    assert telemetry.incoming_request_id({'traceparent': traceparent}) == '4bf92f3577b34da6a3ce929d0e0e4736'
    assert len(telemetry.incoming_request_id({})) == 32


def test_noop_spans_record_attributes_errors_and_feed_listeners():
    seen = []
    listener = lambda *args: seen.append(args)  # noqa: E731
    telemetry.add_span_listener(listener)
    try:
        with telemetry.span('provider.request', provider='fmp') as current:
            current.set_attribute('status', 'ONLINE')
        with pytest.raises(RuntimeError), telemetry.span('ai.run', agent='auditor'):
            raise RuntimeError('boom')
    finally:
        telemetry.remove_span_listener(listener)
    assert seen[0][0] == 'provider.request' and seen[0][1]['status'] == 'ONLINE' and seen[0][3] is None
    assert seen[1][0] == 'ai.run' and seen[1][3] == 'RuntimeError'


@pytest.mark.asyncio
async def test_traced_decorator_and_injected_tracer_set_trace_ids():
    class Context:
        trace_id = int('4bf92f3577b34da6a3ce929d0e0e4736', 16)

    class Span:
        def __init__(self):
            self.attributes = {}

        def set_attribute(self, key, value):
            self.attributes[key] = value

        def record_exception(self, exc):
            self.attributes['exception'] = type(exc).__name__

        def get_span_context(self):
            return Context()

    class Tracer:
        spans: list = []

        @contextmanager
        def start_as_current_span(self, name):
            span = Span()
            Tracer.spans.append((name, span))
            yield span

    @telemetry.traced('work.async')
    async def work():
        await asyncio.sleep(0)
        return telemetry.trace_id_var.get()

    @telemetry.traced()
    def sync_work():
        return 7

    telemetry.use_tracer(Tracer())
    try:
        assert await work() == '4bf92f3577b34da6a3ce929d0e0e4736'
        assert sync_work() == 7
    finally:
        telemetry.use_tracer(None)
    assert Tracer.spans[0][0] == 'work.async' and Tracer.spans[1][0].endswith('<locals>.sync_work')
    assert telemetry.trace_id_var.get() is None


def test_tracing_stays_off_without_endpoint_or_packages():
    assert telemetry.setup_tracing(telemetry.TelemetrySettings(_env_file=None)) is False
    if importlib.util.find_spec('opentelemetry') is None:
        configured = telemetry.TelemetrySettings(_env_file=None, otel_exporter_otlp_endpoint='http://localhost:4318')
        assert telemetry.setup_tracing(configured) is False


def test_registry_counts_failures_percentiles_and_parse_errors():
    registry = metrics.MetricsRegistry(window=10)
    for latency in [10, 20, 30, 40, 1000]:
        registry.record('fmp', ok=True, latency_ms=latency)
    registry.record('fmp', ok=False, latency_ms=5, status='RATE_LIMITED', error='HTTPStatusError: HTTP 429')
    registry.record_parse_failure('fmp', 'KeyError')
    snap = registry.snapshot('fmp')
    assert snap['requests_total'] == 6 and snap['failures_total'] == 2
    assert snap['failures_by_kind'] == {'RATE_LIMITED': 1, 'PARSE_ERROR': 1}
    assert snap['consecutive_failures'] == 2 and snap['latency_ms']['p50'] == 20 and snap['latency_ms']['p99'] == 1000
    assert registry.snapshot('unused')['latency_ms']['p50'] is None and registry.snapshot('unused')['failure_rate'] is None
    assert metrics.percentile([], 0.5) is None


def test_provider_rows_expose_circuit_state_and_prometheus_text():
    class Provider:
        configured, failures = True, 3
        next_attempt = time.monotonic() + 120
        health = {'name': 'massive', 'status': 'RATE_LIMITED', 'latency_ms': 80.0, 'last_success': None, 'error': 'HTTP 429'}
    registry = metrics.MetricsRegistry(window=10)
    registry.record('massive', ok=False, latency_ms=80, status='RATE_LIMITED')
    registry.record('binance_ws', ok=True, latency_ms=12)
    rows = metrics.provider_rows({'massive': Provider()}, registry)
    massive = next(r for r in rows if r['name'] == 'massive')
    assert massive['circuit']['state'] == 'OPEN' and massive['circuit']['consecutive_failures'] == 3
    assert {r['name'] for r in rows} == {'massive', 'binance_ws'}
    runs = [{'agent': 'auditor', 'model': 'gpt-oss:120b', 'success': True, 'latency_ms': 900, 'tokens': 50},
            {'agent': 'auditor', 'model': 'gpt-oss:120b', 'success': False, 'latency_ms': 1500, 'error': 'ValidationError'}]
    models = metrics.ai_model_rows(runs)
    assert models[0]['failure_rate'] == 0.5 and models[0]['errors_by_kind'] == {'ValidationError': 1}
    text = metrics.render_prometheus(rows, models)
    assert 'hub_provider_circuit_open{provider="massive"} 1' in text
    assert 'hub_provider_failures_total{provider="massive",kind="RATE_LIMITED"} 1' in text
    assert 'hub_ai_latency_ms{model="gpt-oss:120b",quantile="0.95"} 1500.0' in text
    assert '# TYPE hub_provider_requests_total counter' in text
    assert metrics.render_json(rows, models)['ai_models'][0]['model'] == 'gpt-oss:120b'


def test_span_listener_feeds_the_global_registry():
    metrics.REGISTRY.reset()
    metrics.span_listener('provider.request', {'provider': 'deribit', 'status': 'OFFLINE'}, 12.0, None)
    metrics.span_listener('other', {'provider': 'deribit'}, 1.0, None)
    assert metrics.REGISTRY.snapshot('deribit')['failures_by_kind'] == {'OFFLINE': 1}
    metrics.REGISTRY.reset()
