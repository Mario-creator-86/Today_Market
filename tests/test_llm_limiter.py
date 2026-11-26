"""The process-wide LLM limiter (backend/llm_limiter.py): concurrency cap, priorities, 429 back-off without the provider
circuit, recovery, and slots that are never leaked. No network: httpx is replaced by fakes."""
import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from backend import llm_limiter
from backend.ai import AIService, OllamaProvider
from backend.database import Store
from backend.llm_limiter import LLMLimiter, QueueWaitExceeded
from backend.providers.base import ProviderUnavailable, retry_after_seconds

CHAT_OK = {'done': True, 'message': {'content': '{"status":"PASS","reasons":[]}'}}


def settings(**overrides):
    # reserved_slots 0 unless a test is about the reservation: most tests count the plain concurrency cap.
    config = {'max_concurrent': 4, 'min_concurrent': 1, 'start_concurrent': 4, 'recover_after_successes': 3,
              'recover_after_seconds': 0, 'probe_after_seconds': 0,
              'role_priority': {'note': 'test', 'default': 3, 'market_interpreter': 0, 'chief_analyst': 0,
                                'integration_probe': 0, 'auditor': 1, 'fact_checker': 3, 'extractor': 5},
              'reserved_max_priority': 0, 'reserved_slots': 0, 'priority_aging_seconds': 60,
              'max_queue_wait_seconds': {'note': 'test', 'default': 5},
              'rate_limit_retries': 3, 'backoff_statuses': [429, 502, 503, 504], 'rate_limit_backoff_seconds': 0.05,
              'rate_limit_backoff_min_seconds': 0.005, 'rate_limit_backoff_growth': 1.5,
              'rate_limit_backoff_max_seconds': 0.2}
    config.update(overrides)
    return lambda: config


def install(monkeypatch, **overrides) -> LLMLimiter:
    limiter = LLMLimiter(settings(**overrides))
    monkeypatch.setattr(llm_limiter, 'LIMITER', limiter)
    return limiter


def response(status: int, url: str, headers: dict | None = None, body: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body if body is not None else {}, headers=headers or {},
                          request=httpx.Request('POST', url))


async def queue_up(coroutines):
    """Tasks started one by one, each given a turn to reach the queue, so their arrival order is known."""
    tasks = []
    for coroutine in coroutines:
        tasks.append(asyncio.create_task(coroutine))
        await asyncio.sleep(0)
    return tasks


@pytest.mark.asyncio
async def test_concurrency_cap_holds_under_20_simultaneous_calls():
    limiter = LLMLimiter(settings())
    running, peak = 0, 0

    async def send():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return 'ok'
    results = await asyncio.gather(*(limiter.run('extractor', send) for _ in range(20)))
    assert results == ['ok'] * 20
    assert peak == 4
    assert limiter.in_flight == 0 and limiter.queued() == 0


@pytest.mark.asyncio
async def test_a_queued_interpreter_starts_before_queued_extractors_and_fifo_holds_within_a_priority():
    limiter = LLMLimiter(settings(max_concurrent=1, start_concurrent=1))
    gate, started = asyncio.Event(), []

    def call(name):
        async def send():
            started.append(name)
            if name == 'holder':
                await gate.wait()
        return send
    tasks = await queue_up([limiter.run('extractor', call('holder')),
                            limiter.run('extractor', call('extractor-1')),
                            limiter.run('extractor', call('extractor-2')),
                            limiter.run('auditor', call('auditor')),
                            limiter.run('market_interpreter', call('interpreter')),
                            limiter.run('extractor', call('extractor-3'))])
    assert started == ['holder'] and limiter.queued() == 5
    gate.set()
    await asyncio.gather(*tasks)
    assert started == ['holder', 'interpreter', 'auditor', 'extractor-1', 'extractor-2', 'extractor-3']


@pytest.mark.asyncio
async def test_429_halves_pauses_and_retries_without_the_circuit_or_refusing_another_role(monkeypatch):
    limiter = install(monkeypatch)
    sends: list[str] = []
    second_extractor_send = asyncio.Event()

    async def request(self, method, url, **kwargs):
        model = kwargs['json']['model']
        sends.append(model)
        if model == 'extractor-model' and sends.count(model) == 1:
            return response(429, url, {'Retry-After': '0.1'})
        if model == 'extractor-model':
            second_extractor_send.set()
        return response(200, url, body=CHAT_OK)
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = OllamaProvider('https://ollama.com/api', 'test-secret')
    extraction = asyncio.create_task(provider.chat('extractor-model', [], {}, role='extractor'))
    while limiter.rate_limited_total == 0:
        await asyncio.sleep(0.005)
    # During the pause: RATE_LIMITED with its end time, the limit halved, the circuit closed.
    assert provider.health['status'] == 'RATE_LIMITED' and provider.health['error'] == 'Rate limited (HTTP 429)'
    assert provider.health['paused_until'] is not None and provider.health['concurrency_limit'] == 2
    assert provider.next_attempt == 0.0
    # Another role is queued behind the pause, never refused with 'circuit open'.
    interpretation = await provider.chat('interpreter-model', [], {}, role='market_interpreter')
    assert interpretation.content == CHAT_OK['message']['content']
    assert (await extraction).content == CHAT_OK['message']['content']
    assert second_extractor_send.is_set() and sends.count('extractor-model') == 2
    assert provider.health['status'] == 'ONLINE' and provider.health['paused_until'] is None
    assert provider.health['rate_limited_total'] == 1 and provider.health['in_flight'] == 0
    assert provider.health['queued'] == 0 and limiter.capacity() == 2


@pytest.mark.asyncio
async def test_429_after_all_retries_fails_plainly_and_leaves_the_circuit_closed(monkeypatch):
    limiter = install(monkeypatch, rate_limit_retries=2, rate_limit_backoff_seconds=0.01, rate_limit_backoff_max_seconds=0.02)
    sends = []

    async def request(self, method, url, **kwargs):
        sends.append(url)
        return response(429, url)
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = OllamaProvider('https://ollama.com/api', 'test-secret')
    with pytest.raises(ProviderUnavailable, match=r'^Rate limited \(HTTP 429\)$') as caught:
        await provider.chat('model', [], {}, role='extractor')
    assert caught.value.status_code == 429
    assert len(sends) == 3  # the first send and 2 retries
    assert provider.next_attempt == 0.0 and limiter.in_flight == 0
    assert limiter.rate_limited_total == 3


@pytest.mark.asyncio
async def test_a_429_answering_a_request_sent_before_the_pause_does_not_halve_again():
    limiter = LLMLimiter(settings())
    limiter._bind_loop()
    limiter.rate_limited(None)
    limiter.rate_limited(None)
    assert limiter.capacity() == 2 and limiter.rate_limited_total == 2 and limiter.strikes == 1


def test_retry_after_seconds_and_http_date():
    now = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
    assert retry_after_seconds('30') == 30
    assert retry_after_seconds(' 2.5 ') == 2.5
    assert retry_after_seconds(format_datetime(now + timedelta(seconds=90), usegmt=True), now) == 90
    assert retry_after_seconds(format_datetime(now - timedelta(seconds=90), usegmt=True), now) == 0
    assert retry_after_seconds(None) is None and retry_after_seconds('') is None
    assert retry_after_seconds('soon') is None and retry_after_seconds('inf') is None


@pytest.mark.asyncio
async def test_backoff_uses_retry_after_capped_else_its_own_growing_pause():
    limiter = LLMLimiter(settings(max_concurrent=8, start_concurrent=8))
    limiter._bind_loop()

    def pause() -> float:
        return limiter.paused_until - time.monotonic()
    limiter.rate_limited(3600)  # Retry-After far above the cap
    assert 0.15 < pause() <= 0.2 and limiter.capacity() == 4
    limiter.paused_until = 0.0  # the pause is over; the next 429 is consecutive (no success in between)
    limiter.rate_limited(None)
    assert 0.07 < pause() <= 0.075 and limiter.capacity() == 2  # 0.05 x 1.5
    limiter.paused_until = 0.0
    limiter.rate_limited(0.01)  # a short Retry-After is used as given
    assert pause() <= 0.01 and limiter.capacity() == 1
    limiter.paused_until = 0.0
    limiter.rate_limited(None)
    assert limiter.capacity() == 1  # never below min_concurrent
    assert 0.15 < pause() <= 0.2  # 0.05 x 1.5^3 = 0.169
    limiter.succeeded()
    assert limiter.strikes == 0


@pytest.mark.asyncio
async def test_recovery_raises_the_limit_after_consecutive_successes_up_to_the_max():
    limiter = LLMLimiter(settings(start_concurrent=2, recover_after_successes=3))

    async def send():
        return 'ok'

    async def fail():
        raise ProviderUnavailable('Timed out')
    for _ in range(2):
        await limiter.run('extractor', send)
    with pytest.raises(ProviderUnavailable):
        await limiter.run('extractor', fail)  # a failure breaks the streak
    for _ in range(2):
        await limiter.run('extractor', send)
    assert limiter.capacity() == 2
    await limiter.run('extractor', send)
    assert limiter.capacity() == 3
    for _ in range(9):
        await limiter.run('extractor', send)
    assert limiter.capacity() == 4


@pytest.mark.asyncio
async def test_a_slot_is_released_on_exception_and_on_cancellation():
    limiter = LLMLimiter(settings(max_concurrent=1, start_concurrent=1))

    async def boom():
        raise RuntimeError('parser bug')
    with pytest.raises(RuntimeError):
        await limiter.run('extractor', boom)
    assert limiter.in_flight == 0

    gate = asyncio.Event()

    async def hold():
        await gate.wait()
    holder, waiter = await queue_up([limiter.run('extractor', hold), limiter.run('extractor', hold)])
    assert limiter.in_flight == 1 and limiter.queued() == 1
    waiter.cancel()  # cancelled while queued: leaves the queue
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert limiter.queued() == 0 and limiter.in_flight == 1
    holder.cancel()  # cancelled while its request runs: the slot is freed
    with pytest.raises(asyncio.CancelledError):
        await holder
    assert limiter.in_flight == 0

    # Cancelled in the instant between being granted a slot and resuming: the slot is handed on, not kept.
    await limiter.acquire('extractor')
    granted, = await queue_up([limiter.acquire('extractor')])
    limiter.release()  # grants the queued waiter
    assert limiter.in_flight == 1
    granted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await granted
    assert limiter.in_flight == 0 and limiter.queued() == 0
    await asyncio.wait_for(limiter.run('extractor', lambda: asyncio.sleep(0)), 1)  # still usable


@pytest.mark.asyncio
async def test_queue_wait_timeout_gives_a_clear_error_and_the_role_falls_back(monkeypatch, tmp_path):
    limiter = install(monkeypatch, max_concurrent=1, start_concurrent=1,
                      max_queue_wait_seconds={'default': 5, 'auditor': 0.05})
    sends = []

    async def request(self, method, url, **kwargs):
        sends.append(kwargs['json']['model'])
        return response(200, url, body=CHAT_OK)
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    await limiter.acquire('market_interpreter')  # a long generation holds the only slot
    with pytest.raises(QueueWaitExceeded, match=r'^LLM queue wait exceeded 0.05 s$'):
        await limiter.acquire('auditor')
    assert limiter.queued() == 0 and limiter.in_flight == 1

    store = Store(f'sqlite:///{tmp_path}/queue.db')
    store.initialize()
    for name in ['primary', 'backup']:
        store.put('ai_models', name, {'name': name, 'availability': True})
    store.put('ai_role_assignments', 'auditor', {'primary': 'primary', 'backup': 'backup'})
    result, run_id = await AIService(store, OllamaProvider('https://ollama.com/api', 'test-secret')).run_with_id('auditor', {})
    assert result is None and sends == []  # nothing sent; the backup would wait in the same queue, so it is not tried
    runs = store.list('ai_runs')
    assert len(runs) == 1 and runs[0]['queue_wait_exceeded'] is True
    assert runs[0]['error_detail'] == 'LLM queue wait exceeded 0.05 s' and runs[0]['http_status'] is None
    limiter.release()


@pytest.mark.asyncio
async def test_the_send_timeout_starts_when_the_request_is_sent_not_while_queued(monkeypatch):
    limiter = install(monkeypatch, max_concurrent=1, start_concurrent=1)
    clients = []

    class Client:
        def __init__(self, *args, timeout=None, **kwargs):
            self.timeout, self.created = timeout, time.monotonic()
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, **kwargs):
            await asyncio.sleep(0.02)
            if time.monotonic() - self.created > self.timeout:
                raise httpx.ReadTimeout('generation too slow')
            return response(200, url, body=CHAT_OK)
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    await limiter.acquire('extractor')
    queued_at = time.monotonic()
    chat = asyncio.create_task(OllamaProvider('https://ollama.com/api', 'k').chat('m', [], {}, timeout=0.1, role='extractor'))
    await asyncio.sleep(0.3)  # three times the send timeout spent waiting for a slot
    assert not chat.done() and clients == []
    limiter.release()
    assert (await chat).content == CHAT_OK['message']['content']
    assert clients[0].timeout == 0.1 and clients[0].created - queued_at >= 0.25


@pytest.mark.asyncio
async def test_401_on_chat_still_opens_the_circuit(monkeypatch):
    limiter = install(monkeypatch)

    async def request(self, method, url, **kwargs):
        return response(401, url)
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = OllamaProvider('https://ollama.com/api', 'test-secret')
    with pytest.raises(ProviderUnavailable) as caught:
        await provider.chat('model', [], {}, role='extractor')
    assert caught.value.status_code == 401 and provider.health['status'] == 'AUTH_ERROR'
    assert provider.next_attempt > 0 and limiter.in_flight == 0 and limiter.rate_limited_total == 0
    with pytest.raises(ProviderUnavailable, match='circuit open'):
        await provider.chat('model', [], {}, role='market_interpreter')


@pytest.mark.asyncio
async def test_a_web_search_429_pauses_only_searches_and_chat_keeps_working(monkeypatch):
    limiter = install(monkeypatch)

    async def request(self, method, url, **kwargs):
        if url.endswith('/web_search'):
            return response(429, url, {'Retry-After': '120'})
        return response(200, url, body=CHAT_OK)
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = OllamaProvider('https://ollama.com/api', 'test-secret')
    with pytest.raises(ProviderUnavailable):
        await provider.search('fed')
    assert provider.web.next_attempt - time.monotonic() > 100  # the search circuit keeps its protective pause
    with pytest.raises(ProviderUnavailable, match='^ollama_web: circuit open$'):
        await provider.fetch('https://www.federalreserve.gov/')
    # The chat of any role is not refused (it was: one search 429 locked every role out with 'circuit open').
    assert provider.next_attempt == 0.0
    assert (await provider.chat('model', [], {}, role='market_interpreter')).content == CHAT_OK['message']['content']
    assert provider.health['status'] == 'ONLINE' and limiter.rate_limited_total == 0


@pytest.mark.asyncio
async def test_a_web_search_401_also_stops_chat_because_the_key_is_shared(monkeypatch):
    install(monkeypatch)
    sends = []

    async def request(self, method, url, **kwargs):
        sends.append(url)
        return response(401, url)
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = OllamaProvider('https://ollama.com/api', 'test-secret')
    with pytest.raises(ProviderUnavailable) as caught:
        await provider.search('fed')
    assert caught.value.status_code == 401
    assert provider.health['status'] == 'AUTH_ERROR' and provider.health['error'] == 'Authentication rejected (HTTP 401)'
    with pytest.raises(ProviderUnavailable, match='^ollama: circuit open$'):
        await provider.chat('model', [], {}, role='market_interpreter')
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_chat_429s_then_a_503_leave_the_circuit_closed(monkeypatch):
    limiter = install(monkeypatch, rate_limit_retries=0, backoff_statuses=[429])
    answers = [429] * 8 + [503]
    sends = []

    async def request(self, method, url, **kwargs):
        sends.append(kwargs['json']['model'])
        status = answers.pop(0) if answers else 200
        return response(status, url, body=CHAT_OK if status == 200 else None)
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = OllamaProvider('https://ollama.com/api', 'test-secret')
    for _ in range(8):
        limiter.paused_until = 0.0  # each 429 in its own pause, as over an hour of load
        with pytest.raises(ProviderUnavailable, match='HTTP 429'):
            await provider.chat('extractor-model', [], {}, role='extractor')
    assert provider.failures == 0  # chat 429s never size a later circuit pause
    limiter.paused_until = 0.0
    with pytest.raises(ProviderUnavailable, match=r'^Server error \(HTTP 503\)$') as caught:
        await provider.chat('interpreter-model', [], {}, attempts=1, role='market_interpreter')
    assert caught.value.status_code == 503
    assert provider.next_attempt == 0.0 and provider.health['status'] == 'DEGRADED' and provider.failures == 0
    limiter.paused_until = 0.0
    assert (await provider.chat('chief-model', [], {}, role='chief_analyst')).content == CHAT_OK['message']['content']
    assert sends[-1] == 'chief-model' and provider.health['status'] == 'ONLINE'


@pytest.mark.asyncio
async def test_a_chat_5xx_on_every_attempt_does_not_open_the_circuit_but_other_calls_keep_it(monkeypatch):
    from backend.providers.base import HTTPProvider
    calls = []

    async def request(self, method, url, **kwargs):
        calls.append(url)
        return response(502, url)

    async def no_sleep(_delay):
        return None
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    monkeypatch.setattr('backend.providers.base.asyncio.sleep', no_sleep)
    provider = HTTPProvider('ollama', 'https://example.com', timeout=60)
    with pytest.raises(ProviderUnavailable, match=r'^Server error \(HTTP 502\)$'):
        await provider.request('POST', '/chat', attempts=2, transport_opens_circuit=False, rate_limit_opens_circuit=False)
    assert len(calls) == 2  # the transport attempts still retry a 5xx
    assert provider.next_attempt == 0.0 and provider.failures == 0 and provider.health['status'] == 'DEGRADED'
    with pytest.raises(ProviderUnavailable):
        await provider.request('GET', '/tags', attempts=1)  # ordinary endpoints keep the protective circuit
    assert provider.next_attempt > 0


@pytest.mark.asyncio
async def test_a_503_backs_off_like_a_429_and_is_retried(monkeypatch):
    limiter = install(monkeypatch)
    answers = [503]

    async def request(self, method, url, **kwargs):
        status = answers.pop(0) if answers else 200
        return response(status, url, body=CHAT_OK if status == 200 else None)
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = OllamaProvider('https://ollama.com/api', 'test-secret')
    result = await provider.chat('model', [], {}, attempts=1, role='market_interpreter')
    assert result.content == CHAT_OK['message']['content']
    assert limiter.capacity() == 2 and limiter.rate_limited_total == 0  # halved; only 429s count as rate limited
    assert provider.next_attempt == 0.0 and provider.health['status'] == 'ONLINE'


@pytest.mark.asyncio
async def test_a_retry_after_above_the_cap_is_not_retried_and_the_pause_stays_capped(monkeypatch):
    limiter = install(monkeypatch)
    sends = []

    async def request(self, method, url, **kwargs):
        sends.append(url)
        return response(429, url, {'Retry-After': '3600'})
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = OllamaProvider('https://ollama.com/api', 'test-secret')
    with pytest.raises(ProviderUnavailable, match=r'^Rate limited \(HTTP 429\): retry after 3600 s$') as caught:
        await provider.chat('model', [], {}, role='extractor')
    assert caught.value.status_code == 429 and len(sends) == 1  # an exhausted quota: no resend every pause
    assert limiter.paused_until - time.monotonic() <= 0.2 and provider.next_attempt == 0.0


@pytest.mark.asyncio
async def test_a_zero_retry_after_still_pauses_for_the_floor():
    limiter = LLMLimiter(settings(rate_limit_backoff_min_seconds=0.1))
    limiter._bind_loop()
    limiter.rate_limited(0.0)
    assert limiter.paused() and 0.05 < limiter.paused_until - time.monotonic() <= 0.1
    limiter.rate_limited(0.0)  # a concurrent 429 inside that pause does not halve again
    assert limiter.capacity() == 2


@pytest.mark.asyncio
async def test_aging_lets_a_long_waiting_extractor_start_before_a_fresh_higher_priority_call():
    limiter = LLMLimiter(settings(max_concurrent=1, start_concurrent=1, priority_aging_seconds=60))
    extractor, auditor = limiter.ticket('extractor'), limiter.ticket('auditor')
    assert extractor.key - auditor.key == pytest.approx(240, abs=1)  # 4 priority levels x 60 s
    assert not extractor.reserved and limiter.ticket('market_interpreter').reserved
    await limiter.acquire('extractor')  # holds the only slot
    started = []

    async def wait(ticket, name):
        await limiter.acquire(None, ticket)
        started.append(name)
    # Arrival times relative to the extractor's: the key is arrival + priority x 60 s.
    base = time.monotonic()
    tickets = [(replace(extractor, key=base + 5 * 60), 'old extractor'),
               (replace(limiter.ticket('fact_checker'), key=base + 250 + 3 * 60), 'fact_checker 250 s later'),
               (replace(auditor, key=base + 100 + 60), 'auditor 100 s later')]
    tasks = await queue_up([wait(ticket, name) for ticket, name in tickets])
    for _ in range(3):
        limiter.release()
        await asyncio.sleep(0)
    await asyncio.gather(*tasks)
    assert started == ['auditor 100 s later', 'old extractor', 'fact_checker 250 s later']
    limiter.release()


@pytest.mark.asyncio
async def test_reserved_slot_lets_the_interpreter_start_while_extraction_fills_the_rest():
    limiter = LLMLimiter(settings(start_concurrent=2, reserved_slots=1))
    gate, started = asyncio.Event(), []

    def call(name):
        async def send():
            started.append(name)
            await gate.wait()
        return send
    tasks = await queue_up([limiter.run('extractor', call(f'extractor-{n}')) for n in range(3)]
                           + [limiter.run('market_interpreter', call('interpreter'))])
    # The extraction takes only the shared slot; the reserved one goes to the interpreter at once.
    assert started == ['extractor-0', 'interpreter'] and limiter.in_flight == 2 and limiter.queued() == 2
    gate.set()
    await asyncio.gather(*tasks)
    # At a limit of 1 nothing is reserved (the extraction would never run).
    limiter.limit = 1
    assert limiter.shared_capacity(limiter.settings(), limiter.capacity()) == 1


@pytest.mark.asyncio
async def test_a_config_error_raises_before_any_slot_is_taken():
    limiter = LLMLimiter(settings(max_concurrent=1, start_concurrent=1, max_queue_wait_seconds={'extractor': 5}))
    await limiter.acquire('extractor')
    with pytest.raises(KeyError):
        await limiter.acquire('fact_checker')  # no 'default' in the table and the role is not listed
    assert limiter.in_flight == 1 and limiter.queued() == 0
    limiter.release()
    assert limiter.in_flight == 0


@pytest.mark.asyncio
async def test_429_retries_keep_their_place_and_one_overall_queue_deadline(monkeypatch):
    limiter = install(monkeypatch, max_concurrent=1, start_concurrent=1, rate_limit_backoff_min_seconds=0.02,
                      rate_limit_backoff_seconds=0.02, max_queue_wait_seconds={'default': 5, 'fact_checker': 0.15})
    order = []

    async def limited():
        order.append('first')
        if order.count('first') == 1:
            raise ProviderUnavailable('Rate limited (HTTP 429)', 429)
        return 'ok'

    async def later():
        order.append('later')
        return 'ok'
    first = asyncio.create_task(limiter.run('extractor', limited))
    while 'first' not in order:
        await asyncio.sleep(0)
    second, = await queue_up([limiter.run('extractor', later)])  # queued behind the first call's retry
    assert await first == 'ok' and await second == 'ok'
    assert order == ['first', 'first', 'later']  # the retry kept its place at the head of its priority

    # A retried call does not get a new full wait: the deadline is the one from its first enqueue.
    await limiter.acquire('market_interpreter')  # a long generation holds the only slot

    async def rate_limited_once():
        raise ProviderUnavailable('Rate limited (HTTP 429)', 429)
    started = time.monotonic()
    holder_release = asyncio.get_running_loop().call_later(0.1, limiter.release)
    with pytest.raises(QueueWaitExceeded, match=r'^LLM queue wait exceeded 0.15 s$'):
        await limiter.run('fact_checker', rate_limited_once)
    assert time.monotonic() - started < 0.3
    holder_release.cancel()
    assert limiter.in_flight == 0 and limiter.queued() == 0


def test_recovery_waits_for_quiet_time_before_probing_the_limit_that_got_a_429():
    limiter = LLMLimiter(settings(recover_after_successes=2, recover_after_seconds=30, probe_after_seconds=300))

    def quiet_for(seconds: float) -> None:
        limiter.last_backoff = time.monotonic() - seconds
    limiter.rate_limited(None)  # at 4: halved to 2
    assert limiter.capacity() == 2 and limiter.refused_at == 4
    limiter.paused_until = 0.0
    quiet_for(10)
    limiter.succeeded()
    limiter.succeeded()
    assert limiter.capacity() == 2  # enough successes, but a 429 came 10 s ago
    quiet_for(31)
    limiter.succeeded()
    assert limiter.capacity() == 3  # quiet for recover_after_seconds: 2 -> 3
    limiter.succeeded()
    limiter.succeeded()
    assert limiter.capacity() == 3  # 4 is where the provider refused: it waits probe_after_seconds
    quiet_for(301)
    limiter.succeeded()
    assert limiter.capacity() == 4


@pytest.mark.asyncio
async def test_ai_runs_store_the_queue_wait_apart_from_the_provider_latency(monkeypatch, tmp_path):
    limiter = install(monkeypatch, max_concurrent=1, start_concurrent=1)

    async def request(self, method, url, **kwargs):
        return response(200, url, body=CHAT_OK)
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    store = Store(f'sqlite:///{tmp_path}/wait.db')
    store.initialize()
    store.put('ai_models', 'primary', {'name': 'primary', 'availability': True})
    store.put('ai_role_assignments', 'auditor', {'primary': 'primary'})
    await limiter.acquire('market_interpreter')
    acquire = limiter.acquire

    async def timed_acquire(role, ticket=None):
        # Start the hold at queue entry, after client/prompt setup has finished.
        asyncio.get_running_loop().call_later(0.2, limiter.release)
        await acquire(role, ticket)

    monkeypatch.setattr(limiter, 'acquire', timed_acquire)
    result, _ = await AIService(store, OllamaProvider('https://ollama.com/api', 'k')).run_with_id('auditor', {})
    assert result is not None and result['status'] == 'PASS'
    run, = store.list('ai_runs')
    assert run['queue_wait_ms'] >= 150 and run['latency_ms'] < 100


def test_the_settings_probe_is_queued_under_its_own_key(monkeypatch):
    from backend.ai import chat_options
    provider = OllamaProvider('https://ollama.com/api', 'k')
    assert chat_options(provider, 'auditor', 'integration_probe')['role'] == 'integration_probe'
    assert chat_options(provider, 'auditor')['role'] == 'auditor'
    config = llm_limiter.limiter_settings()
    assert llm_limiter.role_value(config['max_queue_wait_seconds'], 'integration_probe') <= 60
    assert llm_limiter.role_value(config['role_priority'], 'integration_probe') <= config['reserved_max_priority']
    assert (llm_limiter.role_value(config['max_queue_wait_seconds'], 'auditor')
            <= llm_limiter.role_value(config['max_queue_wait_seconds'], 'chief_analyst'))


def test_health_row_of_the_chat_provider_carries_the_limiter_fields():
    limiter = LLMLimiter(settings(start_concurrent=3))
    provider = OllamaProvider('https://ollama.com/api', 'test-secret')
    limiter.bind(provider.health)
    assert {key: provider.health[key] for key in ('concurrency_limit', 'in_flight', 'queued', 'paused_until',
                                                  'rate_limited_total')} == {
        'concurrency_limit': 3, 'in_flight': 0, 'queued': 0, 'paused_until': None, 'rate_limited_total': 0}


def test_live_limiter_counters_never_trigger_a_snapshot_on_their_own():
    from backend.streams import fingerprint
    row = {'providers': [{'name': 'ollama', 'status': 'ONLINE', 'concurrency_limit': 4, 'in_flight': 0, 'queued': 0}]}
    busy = {'providers': [dict(row['providers'][0], in_flight=4, queued=17)]}
    halved = {'providers': [dict(row['providers'][0], concurrency_limit=2)]}
    assert fingerprint(row) == fingerprint(busy)
    assert fingerprint(row) != fingerprint(halved)


@pytest.mark.asyncio
async def test_openai_chat_shares_the_limiter_and_its_429_policy(monkeypatch):
    from backend.ai import OpenAIProvider
    limiter = install(monkeypatch, rate_limit_backoff_max_seconds=0.02)
    sends = []

    async def request(self, method, url, **kwargs):
        sends.append(url)
        if len(sends) == 1:
            return response(429, url, {'Retry-After': '0.01'})
        return response(200, url, body={'choices': [{'message': {'content': '{}'}}], 'usage': {'completion_tokens': 1}})
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = OpenAIProvider('https://api.openai.com/v1', 'test-secret')
    assert (await provider.chat('gpt', [], {}, role='auditor')).content == '{}'
    assert len(sends) == 2 and provider.next_attempt == 0.0 and provider.health['status'] == 'ONLINE'
    assert limiter.rate_limited_total == 1 and provider.health['rate_limited_total'] == 1
