import httpx
import pytest

from backend.providers.base import HTTPProvider, ProviderUnavailable
from backend.schemas import Quote


@pytest.mark.asyncio
async def test_missing_credentials_never_call_network():
    provider = HTTPProvider('test', 'https://example.com', configured=False)
    with pytest.raises(ProviderUnavailable):
        await provider.request('GET', '/')
    assert provider.health['status'] == 'NOT CONFIGURED'

@pytest.mark.asyncio
async def test_auth_error_redacts_secret_and_opens_circuit(monkeypatch):
    async def request(*args, **kwargs):
        return httpx.Response(401, request=httpx.Request('GET', 'https://example.com?key=secret'))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    provider = HTTPProvider('test', 'https://example.com')
    with pytest.raises(ProviderUnavailable):
        await provider.request('GET', '/')
    assert provider.health['status'] == 'AUTH_ERROR'
    assert 'secret' not in str(provider.health)
    with pytest.raises(ProviderUnavailable, match='circuit open'):
        await provider.request('GET', '/')

def test_article_cannot_submit_price():
    with pytest.raises(ValueError):
        Quote(symbol='BTC', price=1, timestamp='2026-09-26T12:00:00Z', provider='news')

def test_nan_and_naive_timestamps_rejected():
    with pytest.raises(ValueError):
        Quote(symbol='BTC', price=float('nan'), timestamp='2026-09-26T12:00:00Z', provider='binance')
    with pytest.raises(ValueError):
        Quote(symbol='BTC', price=1, timestamp='2026-09-26T12:00:00', provider='binance')


@pytest.mark.asyncio
async def test_a_slow_llm_generation_never_opens_the_provider_circuit(monkeypatch):
    calls, timeouts = [], []

    class Client:
        def __init__(self, *args, timeout=None, **kwargs):
            timeouts.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *args, **kwargs):
            calls.append(1)
            raise httpx.ReadTimeout('generation too slow')
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    provider = HTTPProvider('ollama', 'https://example.com', timeout=60)
    with pytest.raises(ProviderUnavailable):
        await provider.request('POST', '/chat', timeout=150, attempts=1, transport_opens_circuit=False)
    assert calls == [1] and timeouts == [150]  # one long attempt, not three short ones
    assert provider.health['status'] == 'DEGRADED' and provider.next_attempt == 0.0
    assert provider.health['error'] == 'Timed out' and provider.health['error_type'] == 'ReadTimeout'
    with pytest.raises(ProviderUnavailable, match='^Timed out$'):  # the next role is not blocked (no 'circuit open')
        await provider.request('POST', '/chat', attempts=1, transport_opens_circuit=False)
    with pytest.raises(ProviderUnavailable):  # ordinary calls keep the protective circuit
        await provider.request('GET', '/tags')
    assert provider.next_attempt > 0


def test_chat_options_follow_the_role_config():
    from backend.ai import OllamaProvider, chat_options

    class Legacy:
        async def chat(self, model, messages, schema):
            return None
    # ai_runtime 1.1.0: longer send timeouts (they no longer include any queue wait); the role picks the limiter priority.
    assert chat_options(OllamaProvider('https://example.com', 'k'), 'market_interpreter') == {
        'timeout': 300, 'attempts': 1, 'role': 'market_interpreter'}
    # ai_runtime 1.4.0: one HTTP attempt per chat request (a timed-out generation is not sent, and paid for, twice).
    assert chat_options(OllamaProvider('https://example.com', 'k'), 'chief_analyst') == {
        'timeout': 240, 'attempts': 1, 'role': 'chief_analyst'}
    assert chat_options(OllamaProvider('https://example.com', 'k'), 'extractor') == {
        'timeout': 180, 'attempts': 1, 'role': 'extractor'}
    assert chat_options(Legacy(), 'market_interpreter') == {}  # older providers and test doubles keep working
