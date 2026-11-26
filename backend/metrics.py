"""Provider and AI metrics: counters, rolling latency percentiles and circuit state, rendered as Prometheus
text or JSON. Only observed counts and timings are reported; a metric without samples is null, never zero."""
import math
import threading
import time
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from backend.config import rules
from backend.database import utcnow

FAILURE_STATUSES = {'AUTH_ERROR', 'RATE_LIMITED', 'OFFLINE', 'DEGRADED'}
QUANTILES = (0.5, 0.95, 0.99)
QUANTILE_LABELS = {f'p{int(q * 100)}': str(q) for q in QUANTILES}


def _config() -> dict:
    return rules()['refresh']['metrics']


def percentile(values: Iterable[float], q: float) -> float | None:
    """Nearest-rank percentile; deterministic and exact on the retained window."""
    ordered = sorted(v for v in values if math.isfinite(v))
    if not ordered:
        return None
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


class MetricsRegistry:
    def __init__(self, window: int | None = None):
        self.window = window
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.requests: Counter[str] = Counter()
        self.successes: Counter[str] = Counter()
        self.failures: defaultdict[str, Counter[str]] = defaultdict(Counter)
        self.consecutive: Counter[str] = Counter()
        self.latencies: dict[str, deque[float]] = {}
        self.last: dict[str, dict] = {}

    def _samples(self, provider: str) -> deque[float]:
        if provider not in self.latencies:
            self.latencies[provider] = deque(maxlen=self.window or _config()['latency_window'])
        return self.latencies[provider]

    def record(self, provider: str, *, ok: bool, latency_ms: float | None = None, status: str | None = None,
               error: str | None = None, at: datetime | None = None) -> None:
        """One provider call. status is the health status after the call (AUTH_ERROR, RATE_LIMITED, ...)."""
        with self.lock:
            self.requests[provider] += 1
            if latency_ms is not None and math.isfinite(latency_ms):
                self._samples(provider).append(float(latency_ms))
            if ok:
                self.successes[provider] += 1
                self.consecutive[provider] = 0
            else:
                self.failures[provider][status if status in FAILURE_STATUSES else 'OFFLINE'] += 1
                self.consecutive[provider] += 1
            self.last[provider] = {'status': status or ('ONLINE' if ok else 'OFFLINE'), 'error': error,
                                   'at': (at or utcnow()).isoformat()}

    def record_parse_failure(self, provider: str, error_type: str, at: datetime | None = None) -> None:
        """HTTP 200 with an invalid body still counts as a failure (health DEGRADED, kind PARSE_ERROR)."""
        with self.lock:
            self.failures[provider]['PARSE_ERROR'] += 1
            self.consecutive[provider] += 1
            self.last[provider] = {'status': 'DEGRADED', 'error': f'parse:{error_type}', 'at': (at or utcnow()).isoformat()}

    def snapshot(self, provider: str) -> dict:
        with self.lock:
            samples = list(self.latencies.get(provider, ()))
            failures = dict(self.failures.get(provider, {}))
            requests = self.requests[provider]
            return {'requests_total': requests, 'successes_total': self.successes[provider],
                    'failures_total': sum(failures.values()), 'failures_by_kind': failures,
                    'consecutive_failures': self.consecutive[provider],
                    'failure_rate': round(sum(failures.values()) / requests, 4) if requests else None,
                    'latency_samples': len(samples),
                    'latency_ms': {f'p{int(q * 100)}': percentile(samples, q) for q in QUANTILES},
                    'last': self.last.get(provider)}

    def names(self) -> set[str]:
        with self.lock:
            return set(self.requests) | set(self.failures)


REGISTRY = MetricsRegistry()


def span_listener(name: str, attributes: dict, duration_ms: float, error: str | None) -> None:
    """Feeds telemetry spans named provider.request into the registry (attributes: provider, status)."""
    if name != 'provider.request' or not attributes.get('provider'):
        return
    status = attributes.get('status')
    ok = error is None and status in (None, 'ONLINE')
    REGISTRY.record(str(attributes['provider']), ok=ok, latency_ms=duration_ms, status=status, error=error)


def provider_rows(providers: Mapping[str, Any], registry: MetricsRegistry = REGISTRY,
                  monotonic_now: float | None = None) -> list[dict]:
    """Health, circuit state (from HTTPProvider.failures/next_attempt) and registry counters per provider."""
    now = time.monotonic() if monotonic_now is None else monotonic_now
    rows = []
    for name in sorted(set(providers) | registry.names()):
        provider = providers.get(name)
        health = dict(getattr(provider, 'health', None) or {})
        next_attempt = float(getattr(provider, 'next_attempt', 0) or 0)
        open_for = max(0.0, next_attempt - now)
        counters = registry.snapshot(name)
        rows.append({'name': name, 'status': health.get('status') or (counters['last'] or {}).get('status', 'UNKNOWN'),
                     'configured': getattr(provider, 'configured', None), 'last_success': health.get('last_success'),
                     'last_latency_ms': health.get('latency_ms'), 'error': health.get('error'),
                     'circuit': {'state': 'OPEN' if open_for > 0 else 'CLOSED', 'open_for_seconds': round(open_for, 1),
                                 'consecutive_failures': max(int(getattr(provider, 'failures', 0) or 0),
                                                             counters['consecutive_failures'])},
                     **{k: v for k, v in counters.items() if k != 'consecutive_failures'}})
    return rows


def ai_model_rows(runs: Iterable[dict]) -> list[dict]:
    """Per-model AI metrics from ai_runs records (agent, model, success, latency_ms, error, tokens). Calls the daily AI
    budget refused (budget_refusal) never reached a model and are left out."""
    grouped: defaultdict[str, list[dict]] = defaultdict(list)
    for run in runs:
        if run.get('model') and not run.get('budget_refusal'):
            grouped[run['model']].append(run)
    rows = []
    for model, items in sorted(grouped.items()):
        latencies = [float(r['latency_ms']) for r in items if isinstance(r.get('latency_ms'), (int, float))]
        failures = [r for r in items if not r.get('success')]
        tokens = [r['tokens'] for r in items if isinstance(r.get('tokens'), int)]
        rows.append({'model': model, 'runs_total': len(items), 'successes_total': len(items) - len(failures),
                     'failures_total': len(failures), 'failure_rate': round(len(failures) / len(items), 4),
                     'errors_by_kind': dict(Counter(r.get('error') or 'unknown' for r in failures)),
                     'agents': sorted({r['agent'] for r in items if r.get('agent')}),
                     'latency_ms': {f'p{int(q * 100)}': percentile(latencies, q) for q in QUANTILES},
                     'tokens_total': sum(tokens) if tokens else None,
                     'last_run': max((r.get('timestamp') or '' for r in items), default=None) or None})
    return rows


def _label(value: Any) -> str:
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _line(name: str, labels: dict[str, Any], value: float | int | None) -> str | None:
    if value is None:
        return None
    inner = ','.join(f'{k}="{_label(v)}"' for k, v in labels.items())
    return f'{name}{{{inner}}} {value}'


def render_prometheus(providers: list[dict], models: list[dict]) -> str:
    """Prometheus text exposition format 0.0.4. Missing samples are omitted rather than exported as 0."""
    families: list[tuple[str, str, str, list[str | None]]] = [
        ('hub_provider_requests_total', 'counter', 'Provider calls observed',
         [_line('hub_provider_requests_total', {'provider': p['name']}, p['requests_total']) for p in providers]),
        ('hub_provider_failures_total', 'counter', 'Provider failures by kind',
         [_line('hub_provider_failures_total', {'provider': p['name'], 'kind': k}, v)
          for p in providers for k, v in sorted(p['failures_by_kind'].items())]),
        ('hub_provider_consecutive_failures', 'gauge', 'Consecutive failures',
         [_line('hub_provider_consecutive_failures', {'provider': p['name']}, p['circuit']['consecutive_failures'])
          for p in providers]),
        ('hub_provider_circuit_open', 'gauge', '1 while the circuit breaker is open',
         [_line('hub_provider_circuit_open', {'provider': p['name']}, int(p['circuit']['state'] == 'OPEN'))
          for p in providers]),
        ('hub_provider_up', 'gauge', '1 when the provider status is ONLINE',
         [_line('hub_provider_up', {'provider': p['name'], 'status': p['status']}, int(p['status'] == 'ONLINE'))
          for p in providers]),
        ('hub_provider_latency_ms', 'summary', 'Provider latency over the retained window',
         [_line('hub_provider_latency_ms', {'provider': p['name'], 'quantile': QUANTILE_LABELS[key]}, value)
          for p in providers for key, value in p['latency_ms'].items()]),
        ('hub_ai_runs_total', 'counter', 'AI runs per model',
         [_line('hub_ai_runs_total', {'model': m['model']}, m['runs_total']) for m in models]),
        ('hub_ai_failures_total', 'counter', 'Failed AI runs per model',
         [_line('hub_ai_failures_total', {'model': m['model']}, m['failures_total']) for m in models]),
        ('hub_ai_latency_ms', 'summary', 'AI latency per model',
         [_line('hub_ai_latency_ms', {'model': m['model'], 'quantile': QUANTILE_LABELS[key]}, value)
          for m in models for key, value in m['latency_ms'].items()]),
    ]
    out: list[str] = []
    for name, kind, text, lines in families:
        present = [line for line in lines if line is not None]
        if present:
            out += [f'# HELP {name} {text}', f'# TYPE {name} {kind}', *present]
    return '\n'.join(out) + '\n'


def render_json(providers: list[dict], models: list[dict], now: datetime | None = None) -> dict:
    return {'as_of': (now or utcnow()).isoformat(), 'providers': providers, 'ai_models': models}
