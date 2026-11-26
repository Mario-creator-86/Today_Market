import json
from datetime import UTC, datetime

import httpx
import pytest

from backend.providers import finnhub as finnhub_module
from backend.providers.base import ProviderUnavailable
from backend.providers.finnhub import FinnhubProvider, RateGuard, quote_fields
from backend.schemas import Quote

NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)  # Friday 14:00 New York, market open
FRESH = int(NOW.timestamp()) - 30
STALE = int(NOW.timestamp()) - 600
CLOSED = int(datetime(2026, 9, 24, 20, 0, tzinfo=UTC).timestamp())


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(finnhub_module, 'utcnow', lambda: NOW)
    monkeypatch.delenv('FINNHUB_API_KEY', raising=False)


def serve(monkeypatch, handler, seen: list | None = None):
    async def request(self, method, url, **kwargs):
        if seen is not None:
            seen.append({'url': url, 'params': kwargs.get('params'), 'headers': dict(kwargs.get('headers') or {})})
        status, body = handler(url, kwargs.get('params') or {})
        return httpx.Response(status, content=json.dumps(body).encode(), request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, 'request', request)


def quote_body(symbol: str) -> dict:
    stamps = {'AAPL': FRESH, 'NVDA': STALE, 'SPY': CLOSED}
    if symbol == 'BMNR':
        return {'c': 0, 'd': None, 'dp': None, 'h': 0, 'l': 0, 'o': 0, 'pc': 0, 't': 0}  # Finnhub's 'unknown' answer
    return {'c': 101.5, 'd': 1.5, 'dp': 1.5, 'h': 102, 'l': 99, 'o': 100, 'pc': 100, 't': stamps.get(symbol, FRESH)}


def test_not_configured_without_key_and_never_calls_out(monkeypatch):
    provider = FinnhubProvider()
    assert provider.configured is False and provider.health['status'] == 'NOT CONFIGURED'
    with pytest.raises(ProviderUnavailable):
        provider.stream_url()
    monkeypatch.setenv('FINNHUB_API_KEY', 'test-key')
    assert FinnhubProvider().configured is True


@pytest.mark.asyncio
async def test_not_configured_quotes_raise_without_http(monkeypatch):
    seen: list = []
    serve(monkeypatch, lambda url, params: (200, {}), seen)
    with pytest.raises(ProviderUnavailable, match='NOT CONFIGURED'):
        await FinnhubProvider().quotes()
    assert seen == []


@pytest.mark.asyncio
async def test_quotes_are_quote_compatible_labelled_and_key_stays_out_of_urls(monkeypatch):
    seen: list = []
    serve(monkeypatch, lambda url, params: (200, quote_body(params['symbol'])), seen)
    provider = FinnhubProvider('test-key')
    records = await provider.quotes()
    by_symbol = {r['symbol']: r for r in records}
    assert len(seen) == 13 and 'BMNR' not in by_symbol and provider.health['rejected_rows'] == 1
    assert all(call['headers'] == {'X-Finnhub-Token': 'test-key'} for call in seen)
    assert not any('test-key' in call['url'] or 'token' in (call['params'] or {}) for call in seen)
    aapl = by_symbol['AAPL']
    assert aapl['provider'] == 'finnhub' and aapl['price'] == 101.5 and aapl['change_pct'] == 1.5
    assert aapl['as_of'] == aapl['timestamp'] and aapl['as_of'].endswith('Z') and aapl['attribution'] == 'Source: Finnhub'
    assert aapl['delay'].startswith('real-time') and aapl['daily_close'] is False and aapl['source'] == 'rest'
    # Plain Quote shape validates once 'finnhub' is in the Literal (checked here with an existing provider value).
    assert Quote.model_validate(dict(quote_fields(aapl), provider='massive')).price == 101.5
    assert set(quote_fields(aapl)) <= set(Quote.model_fields)


@pytest.mark.asyncio
async def test_old_trades_during_the_session_are_stale_never_live_nor_a_close(monkeypatch):
    serve(monkeypatch, lambda url, params: (200, quote_body(params['symbol'])))
    by_symbol = {r['symbol']: r for r in await FinnhubProvider('k').quotes()}
    nvda = by_symbol['NVDA']  # 10 min old while NYSE is open (e.g. a halt): stale, not a close
    assert nvda['delay'].startswith('STALE: no trade for 10 min') and nvda['daily_close'] is False
    spy = by_symbol['SPY']  # yesterday's trade while the market is open: still not labelled LAST CLOSE
    assert spy['delay'].startswith('STALE') and spy['daily_close'] is False and 'real-time' not in spy['delay']


@pytest.mark.parametrize(('now', 'trade', 'label', 'daily_close'), [
    # Saturday: Friday's 16:00 ET close.
    (datetime(2026, 9, 26, 15, 0, tzinfo=UTC), datetime(2026, 9, 25, 20, 0, tzinfo=UTC), 'FRIDAY CLOSE', True),
    # Friday 16:30 ET: the close half an hour ago is the close, not a 'delay'.
    (datetime(2026, 9, 25, 20, 30, tzinfo=UTC), datetime(2026, 9, 25, 20, 0, tzinfo=UTC), 'AFTER HOURS', True),
    # Thursday 03:00 ET: overnight, Wednesday's close.
    (datetime(2026, 9, 24, 7, 0, tzinfo=UTC), datetime(2026, 9, 23, 20, 0, tzinfo=UTC), 'LAST CLOSE', True),
    # A fresh after-hours trade is real-time but carries the session state.
    (datetime(2026, 9, 25, 20, 30, tzinfo=UTC), datetime(2026, 9, 25, 20, 29, 30, tzinfo=UTC), 'real-time', False),
])
def test_labels_follow_the_nyse_session(now, trade, label, daily_close):
    record = FinnhubProvider('k').record('AAPL', 100.0, trade, 'rest', now=now)
    assert record['delay'].startswith(label) and record['daily_close'] is daily_close
    assert record['as_of'] == trade.isoformat().replace('+00:00', 'Z')
    if label == 'real-time':
        assert record['delay'].endswith('AFTER HOURS')


@pytest.mark.asyncio
async def test_direct_quote_calls_cannot_bypass_the_rate_guard(monkeypatch):
    seen: list = []
    serve(monkeypatch, lambda url, params: (200, quote_body(params['symbol'])), seen)
    provider = FinnhubProvider('k')
    provider.guard = RateGuard(1, 60, clock=lambda: 0.0)
    assert (await provider.quote('AAPL'))['symbol'] == 'AAPL'
    with pytest.raises(finnhub_module.RateGuardFull):
        await provider.quote('MSFT')
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_etf_proxies_keep_their_ticker_and_proxy_label(monkeypatch):
    serve(monkeypatch, lambda url, params: (200, quote_body(params['symbol'])))
    records = await FinnhubProvider('k').quotes()
    proxies = {r['symbol']: r for r in records if r['proxy']}
    assert set(proxies) == {'SPY', 'QQQ', 'IWM'}
    assert proxies['SPY']['label'] == 'SPY ETF (proxy)' and proxies['SPY']['asset_class'] == 'etf'
    assert proxies['SPY']['proxy_for'] == 'S&P 500 direction'
    assert not {r['symbol'] for r in records} & {'SP500', 'NASDAQ', 'RUSSELL', 'VIX', 'SPX', 'NDX'}
    assert all(r['asset_class'] == 'equity' and not r['proxy'] for r in records if r['symbol'] not in proxies)


def test_proxy_config_cannot_take_an_index_symbol(monkeypatch):
    original = finnhub_module.rules

    def patched():
        config = original()
        config['finnhub']['etf_proxies']['SP500'] = {'label': 'bad', 'proxy_for': 'x'}
        return config
    monkeypatch.setattr(finnhub_module, 'rules', patched)
    with pytest.raises(ValueError, match='index symbols'):
        FinnhubProvider('k')


def test_rate_guard_sliding_window():
    now = [0.0]
    guard = RateGuard(60, 60, clock=lambda: now[0])
    assert all(guard.try_acquire() for _ in range(60)) and not guard.try_acquire() and guard.remaining() == 0
    now[0] = 59.9
    assert not guard.try_acquire()
    now[0] = 60.0
    assert guard.try_acquire() and guard.remaining() == 59  # all 60 stamps from t=0 expired
    with pytest.raises(ValueError):
        RateGuard(0, 60)


@pytest.mark.asyncio
async def test_quotes_respect_guard_and_rotate_skipped_symbols(monkeypatch):
    seen: list = []
    serve(monkeypatch, lambda url, params: (200, quote_body(params['symbol'])), seen)
    provider = FinnhubProvider('k')
    now = [0.0]
    provider.guard = RateGuard(5, 60, clock=lambda: now[0])
    first = await provider.quotes()
    assert len(seen) == 5 and [r['symbol'] for r in first] == ['COIN', 'MSTR', 'NVDA', 'MSFT']  # BMNR rejected
    assert provider.health['rate_guard_skipped'][0] == 'AAPL' and len(provider.health['rate_guard_skipped']) == 8
    assert await provider.quotes() == [] and len(seen) == 5  # window still full: no call made
    now[0] = 61
    await provider.quotes()
    assert [call['params']['symbol'] for call in seen[5:]] == ['AAPL', 'AMZN', 'META', 'GOOGL', 'TSLA']


@pytest.mark.asyncio
async def test_auth_error_opens_circuit_and_is_reported(monkeypatch):
    serve(monkeypatch, lambda url, params: (401, {'error': 'Invalid API key'}))
    provider = FinnhubProvider('bad-key')
    with pytest.raises(ProviderUnavailable):
        await provider.quotes()
    assert provider.health['status'] == 'AUTH_ERROR' and 'bad-key' not in json.dumps(provider.health)


def test_stream_subscription_and_url(monkeypatch):
    provider = FinnhubProvider('k')
    messages = provider.stream_subscription()
    assert messages[0] == {'type': 'subscribe', 'symbol': 'COIN'} and len(messages) == 13
    assert provider.stream_subscription(['SPY'], 'unsubscribe') == [{'type': 'unsubscribe', 'symbol': 'SPY'}]
    assert provider.stream_url() == 'wss://ws.finnhub.io?token=k'
    with pytest.raises(ValueError):
        provider.stream_subscription(['SP500'])
    with pytest.raises(ValueError):
        provider.stream_subscription(['AAPL'] * 51)


def test_stream_trades_parse_latest_per_symbol_and_ignore_noise():
    provider = FinnhubProvider('k')
    ms = int(NOW.timestamp() * 1000)
    message = json.dumps({'type': 'trade', 'data': [
        {'s': 'AAPL', 'p': 101.0, 't': ms - 2000, 'v': 10, 'c': ['1']},
        {'s': 'AAPL', 'p': 101.4, 't': ms - 1000, 'v': 5},
        {'s': 'SPY', 'p': 650.2, 't': ms - 500, 'v': 100},
        {'s': 'BINANCE:BTCUSDT', 'p': 1, 't': ms, 'v': 1},
        {'s': 'AAPL', 'p': -1, 't': ms, 'v': 1}]})
    records = {r['symbol']: r for r in provider.parse_stream_message(message, now=NOW)}
    assert set(records) == {'AAPL', 'SPY'} and provider.health['rejected_rows'] == 2
    assert records['AAPL']['price'] == 101.4 and records['AAPL']['trade_volume'] == 15 and records['AAPL']['volume'] is None
    assert records['AAPL']['source'] == 'websocket' and records['AAPL']['delay'] == 'real-time trade (Finnhub WebSocket)'
    assert records['SPY']['label'] == 'SPY ETF (proxy)' and records['SPY']['as_of'] == '2026-09-25T17:59:59.500000Z'
    assert provider.parse_stream_message('{"type":"ping"}') == []
    assert provider.parse_stream_message('not json') == [] and provider.health['rejected_rows'] == 3
    assert provider.parse_stream_message('{"type":"error","msg":"Subscribing to too many symbols"}') == []
    assert provider.health['status'] == 'DEGRADED' and 'too many' in provider.health['error']


@pytest.mark.asyncio
async def test_quotes_round_can_be_limited_to_tracked_symbols():
    provider = FinnhubProvider('key')
    called = []

    async def quote(symbol):
        called.append(symbol)
        return None
    provider.quote = quote
    await provider.quotes(only={'AAPL', 'NVDA'})
    assert sorted(called) == ['AAPL', 'NVDA']  # SPY/QQQ/IWM proxies are not fetched when nobody uses them
