"""Market coverage sources (docs/MARKET_COVERAGE.md): Treasury and ECB previous closes, the DXY replica, the PAXG gold
proxy, EIA spot closes, FRED quote mapping, Alpaca (guards, snapshots, bars, news) and Finnhub ETF proxies.
No test touches the network: every HTTP client gets an httpx.MockTransport, and settings never read the real .env."""
import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from backend import provider_settings as ps
from backend.config import Settings, rules
from backend.database import Store, Tick, utcnow
from backend.providers import alpaca as alpaca_module
from backend.providers import finnhub as finnhub_module
from backend.providers.alpaca import AlpacaDataProvider, session_start
from backend.providers.base import ProviderUnavailable
from backend.providers.eia_spot import EIASpotPriceProvider, parse_spot_table, spot_quotes
from backend.providers.finnhub import FinnhubProvider, quote_fields
from backend.providers.market import BinanceProvider, CoinbaseProvider, TreasuryYieldProvider
from backend.providers.official_data import fred_quote_attribution, fred_quotes, usd_index_replica
from backend.providers.registry import build_providers
from backend.runtime import Runtime
from backend.schemas import DocumentInput, Quote
from backend.service import HubService

NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)  # Friday 14:00 New York: NYSE open
BACKEND = Path(__file__).resolve().parent.parent / 'backend'
CLIENT_INIT = httpx.AsyncClient.__init__


def mock_http(monkeypatch, handler) -> list[httpx.Request]:
    """Every httpx.AsyncClient gets a MockTransport calling handler(request): nothing can leave the process."""
    seen: list[httpx.Request] = []

    def init(self, *args, **kwargs):
        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)
        kwargs['transport'] = httpx.MockTransport(record)
        CLIENT_INIT(self, *args, **kwargs)  # the unpatched constructor: a later mock_http replaces an earlier one
    monkeypatch.setattr(httpx.AsyncClient, '__init__', init)
    return seen


def offline(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f'unexpected HTTP request to {request.url.host}')


@pytest.fixture
def runtime(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/coverage.db')
    store.initialize()
    hub = HubService(store, Settings(_env_file=None, enable_ingestion=False))
    hub.initialize()
    return Runtime(hub)


def ticks(runtime, symbol: str) -> list[dict]:
    with runtime.hub.store.session() as session:
        return [dict(t.payload, provider=t.provider) for t in session.scalars(select(Tick).where(Tick.symbol == symbol))]


class Fake:
    configured = True

    def __init__(self, name, **methods):
        self.name, self.health = name, {'name': name, 'status': 'ONLINE'}
        for key, value in methods.items():
            setattr(self, key, value)


def returns(value):
    async def call(*args, **kwargs):
        return value
    return call


def fails(*args, **kwargs):
    raise ProviderUnavailable('offline in tests')


async def raising(*args, **kwargs):
    fails()


# --- 1. rejected quotes are logged -----------------------------------------------------------------------------------

def test_rejected_quote_rows_are_logged_with_symbol_provider_and_reason(runtime, caplog):
    caplog.set_level(logging.WARNING)
    stamp = (utcnow() - timedelta(minutes=1)).isoformat()
    accepted = runtime.ingest_quotes([
        {'symbol': 'XAU', 'price': 1.0, 'timestamp': stamp, 'provider': 'binance'},  # not a tracked asset
        {'symbol': 'US2Y', 'price': -1, 'timestamp': stamp, 'provider': 'treasury'},  # invalid value
        {'symbol': 'US2Y', 'price': 3.5, 'timestamp': stamp, 'provider': 'treasury', 'daily_close': True,
         'data_class': 'DAILY_CLOSE', 'previous_close': 3.6, 'change_bp': -10.0, 'change_basis': 'previous_close',
         'label': 'US Treasury 2Y par yield', 'attribution': 'Source: U.S. Department of the Treasury',
         'as_of_label': 'daily close'}])
    assert accepted == 1
    rejected = [r for r in caplog.records if r.getMessage() == 'quote_rejected']
    assert [(r.symbol, r.provider) for r in rejected] == [('XAU', 'binance'), ('US2Y', 'treasury')]
    assert rejected[0].reason == 'Unknown asset' and 'price' in rejected[1].reason
    stored = ticks(runtime, 'US2Y')[0]  # the whole Quote is the payload: provenance survives storage
    assert stored['previous_close'] == 3.6 and stored['change_bp'] == -10.0 and stored['data_class'] == 'DAILY_CLOSE'
    assert stored['attribution'] == 'Source: U.S. Department of the Treasury'


# --- 2. Treasury previous close ---------------------------------------------------------------------------------------

TREASURY_HEAD = ('<feed xmlns="http://www.w3.org/2005/Atom" '
                 'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" '
                 'xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">')


def treasury_xml(rows: dict[str, dict[str, str]]) -> bytes:
    entries = ''.join('<entry><content><m:properties><d:NEW_DATE>' + day + 'T00:00:00</d:NEW_DATE>'
                      + ''.join(f'<d:{k}>{v}</d:{k}>' for k, v in fields.items()) + '</m:properties></content></entry>'
                      for day, fields in rows.items())
    return (TREASURY_HEAD + entries + '</feed>').encode()


def test_treasury_close_carries_the_previous_business_day():
    quotes = {q['symbol']: q for q in TreasuryYieldProvider.parse(treasury_xml({
        '2026-09-24': {'BC_2YEAR': '4.70', 'BC_10YEAR': '5.10', 'BC_30YEAR': '5.40'},
        '2026-09-25': {'BC_2YEAR': '4.81', 'BC_10YEAR': '5.04', 'BC_30YEAR': ''}}))}
    assert set(quotes) == {'US2Y', 'US10Y'}  # a blank latest cell is never filled with an older value
    two = quotes['US2Y']
    assert two['price'] == 4.81 and two['previous_close'] == 4.70 and two['change_bp'] == 11.0
    assert two['previous_close_at'] == '2026-09-24T20:00:00Z' and two['timestamp'] == '2026-09-25T20:00:00Z'
    assert two['change_pct'] == pytest.approx((4.81 / 4.70 - 1) * 100, abs=1e-4) and two['change_basis'] == 'previous_close'
    assert two['data_class'] == 'DAILY_CLOSE' and two['daily_close'] is True
    assert two['attribution'] == 'Source: U.S. Department of the Treasury' and 'Fri 25 Sep' in two['as_of_label']
    assert '2026-09-25' not in two['as_of_label']  # readable dates in labels, never ISO
    assert two['label'] == 'US Treasury 2Y par yield' and quotes['US10Y']['change_bp'] == -6.0


@pytest.mark.asyncio
async def test_treasury_first_business_day_takes_the_previous_close_from_last_months_file(monkeypatch):
    files = {'202610': {'2026-10-01': {'BC_10YEAR': '4.20'}},
             '202609': {'2026-09-29': {'BC_10YEAR': '4.10'}, '2026-09-30': {'BC_10YEAR': '4.15'}},
             '202601': {'2026-01-02': {'BC_10YEAR': '4.00'}}, '202512': {'2025-12-31': {'BC_10YEAR': '3.98'}}}
    seen = mock_http(monkeypatch, lambda r: httpx.Response(200, content=treasury_xml(
        files[r.url.params['field_tdr_date_value_month']])))
    ten = next(q for q in await TreasuryYieldProvider().quotes(month='202610') if q['symbol'] == 'US10Y')
    assert [r.url.params['field_tdr_date_value_month'] for r in seen] == ['202610', '202609']
    assert ten['price'] == 4.20 and ten['previous_close'] == 4.15 and ten['change_bp'] == 5.0
    assert ten['previous_close_at'] == '2026-09-30T20:00:00Z'
    january = next(q for q in await TreasuryYieldProvider().quotes(month='202601') if q['symbol'] == 'US10Y')
    assert seen[-1].url.params['field_tdr_date_value_month'] == '202512' and january['previous_close'] == 3.98
    seen.clear()
    await TreasuryYieldProvider().quotes(month='202609')  # two rows already: no second file
    assert len(seen) == 1


MAINTENANCE_PAGE = '<html><body>Scheduled maintenance&nbsp;<br></body></html>'  # HTML, not XML: ParseError


@pytest.mark.asyncio
async def test_treasury_answer_that_is_not_xml_is_a_refused_payload(monkeypatch):
    mock_http(monkeypatch, lambda r: httpx.Response(200, text=MAINTENANCE_PAGE))
    provider = TreasuryYieldProvider()
    with pytest.raises(ValueError, match='not the expected XML'):
        await provider.quotes(month='202609')
    assert provider.health['status'] == 'DEGRADED' and provider.health['error_type'] == 'ParseError'
    assert provider.health['error'] == 'Treasury yield curve: the answer is not the expected XML'  # no class names
    for body in (b'', b'<feed', b'<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>'):  # empty, truncated, entity expansion
        with pytest.raises(ValueError):
            TreasuryYieldProvider.rows(body)


@pytest.mark.asyncio
async def test_treasury_has_its_own_hourly_loop_and_a_bad_answer_never_stops_the_quotes_loop(runtime, monkeypatch,
                                                                                            caplog):
    from backend import refresh
    seen = mock_http(monkeypatch, lambda r: httpx.Response(200, text=MAINTENANCE_PAGE))
    treasury = TreasuryYieldProvider()
    runtime.hub.providers = {'treasury_yields': treasury}
    steps: list[str] = []

    async def record(name):
        steps.append(name)
    monkeypatch.setattr(runtime, 'poll_proxy_fallback', lambda: record('proxy_fallback'))
    monkeypatch.setattr(runtime, 'save_health', lambda: record('save_health'))
    await runtime.poll_market()  # the 5 s quotes loop never waits for Treasury any more
    assert seen == [] and steps == ['proxy_fallback', 'save_health']
    caplog.set_level(logging.WARNING)
    await runtime.poll_treasury_yields()  # an HTML maintenance page: logged, never raised
    assert len(seen) == 1 and 'treasury_yields_attempt' in runtime.last_run and steps[-1] == 'save_health'
    assert treasury.health['status'] == 'DEGRADED'
    assert [(r.operation, r.error_type) for r in caplog.records if r.getMessage() == 'provider_call_failed'] == [
        ('quotes', 'ValueError')]
    assert refresh.cadence('treasury_yields')['seconds'] == 3600  # the cadence applies to attempts, not successes
    started: list[str] = []

    async def cycle(kind, operation):
        started.append(kind)

    async def idle():
        return None
    monkeypatch.setattr(runtime, 'cycle', cycle)
    monkeypatch.setattr(runtime, 'crypto_stream', idle)
    monkeypatch.setattr(runtime, 'analysis_loop', idle)
    await runtime.start()
    await asyncio.sleep(0)  # let every task begin
    await runtime.stop()
    assert 'treasury_yields' in started and 'quotes' in started


# --- 3. ECB previous fix, SEK and the DXY replica -------------------------------------------------------------------

FIX_0925 = {'USD': 1.1403, 'JPY': 179.70, 'GBP': 0.86045, 'CAD': 1.6127, 'SEK': 11.2900, 'CHF': 0.9445}
FIX_0924 = {'USD': 1.1367, 'JPY': 180.57, 'GBP': 0.85986, 'CAD': 1.6047, 'SEK': 11.2645, 'CHF': 0.9409}


def test_usd_index_replica_reproduces_the_documented_ecb_fixes():
    # ECB reference rates for 25 and 24 Sep 2026 (per EUR), ICE formula: 100.984 and 101.293 (-0.305%).
    assert usd_index_replica(FIX_0925) == pytest.approx(100.984, abs=0.0005)
    assert usd_index_replica(FIX_0924) == pytest.approx(101.293, abs=0.0005)
    assert (usd_index_replica(FIX_0925) / usd_index_replica(FIX_0924) - 1) * 100 == pytest.approx(-0.305, abs=0.001)
    assert usd_index_replica({k: v for k, v in FIX_0925.items() if k != 'SEK'}) is None  # never a partial index
    assert usd_index_replica(dict(FIX_0925, CHF=0)) is None
    config = rules()['official_data']['ecb']['fx']
    assert 'SEK' in config['currencies'] and config['usd_index_replica']['provider'] == 'ecb-derived'
    label = config['usd_index_replica']['label']
    assert label.startswith('USD index replica (ICE DXY formula)') and 'derived by this app' in label
    assert not any(name in label for name in ('U.S. Dollar Index', 'USDX'))


# --- 4. PAXG gold proxy -------------------------------------------------------------------------------------------------

def ticker(pair: str, price: str, stamp: datetime, change: str = '0.25', trade: int | None = None) -> str:
    data = {'e': '24hrTicker', 'E': int(stamp.timestamp() * 1000), 's': pair, 'c': price, 'b': price, 'a': price, 'P': change,
            'v': '10.5'}
    if trade is not None:
        data['L'] = trade  # last trade id: unchanged when the ticker pushed only a new best bid/ask
    return json.dumps({'stream': f'{pair.lower()}@ticker', 'data': data})


def ms(moment: datetime) -> datetime:
    """A time as the stream carries it (whole milliseconds)."""
    return datetime.fromtimestamp(int(moment.timestamp() * 1000) / 1000, UTC)


def streamed(runtime, message: str) -> bool:
    """What crypto_stream does with one message (the proxy write runs in a thread there)."""
    row = runtime.handle_binance_message(message, runtime.hub.providers['binance'])
    return row is not None and runtime.write_stream_proxy(row)


def test_stream_uri_adds_the_configured_proxy_pairs_to_btc_and_eth():
    uri = Runtime.stream_uri()
    assert uri.startswith('wss://data-stream.binance.vision/stream?streams=')
    assert uri.endswith('btcusdt@ticker/ethusdt@ticker/paxgusdt@ticker')


def test_paxg_message_is_a_proxy_record_never_a_gold_tick(runtime):
    binance = runtime.hub.providers['binance']
    stamp = utcnow() - timedelta(seconds=3)
    assert not streamed(runtime, ticker('PAXGUSDT', '4278.00', stamp, '-0.110', trade=100))  # baseline: trade time unknown
    assert runtime.hub.store.get('proxy_quotes', 'binance:PAXG') is None and runtime.proxy_stream_tick == 0
    assert streamed(runtime, ticker('PAXGUSDT', '4278.42', stamp + timedelta(seconds=1), '-0.103', trade=101))
    assert runtime.handle_binance_message(ticker('XRPUSDT', '0.51', stamp), binance) is None  # never subscribed: ignored
    assert runtime.handle_binance_message(json.dumps({'result': None, 'id': 1}), binance) is None  # control frame
    proxy = runtime.hub.store.get('proxy_quotes', 'binance:PAXG')
    coverage = rules()['coverage']['proxies']['GOLD'][0]
    assert proxy['price'] == 4278.42 and proxy['proxy_for'] == 'GOLD' and proxy['relation'] == 'direction_only'
    assert datetime.fromisoformat(proxy['timestamp']) == ms(stamp + timedelta(seconds=1))  # the trade's message
    assert proxy['label'] == coverage['label'] and proxy['caveat'] == coverage['caveat']
    assert proxy['change_pct'] == -0.103 and proxy['change_basis'] == 'rolling_24h' and proxy['provider'] == 'binance'
    assert proxy['attribution'] == 'Source: Binance' and proxy['feed'] is None and proxy['delay_label']
    assert proxy['previous_close'] is None  # a rolling 24h window has no close: never invented
    assert ticks(runtime, 'GOLD') == [] and ticks(runtime, 'PAXG') == []
    runtime.handle_binance_message(ticker('BTCUSDT', '65000.5', stamp), binance)
    btc = ticks(runtime, 'BTC')[0]
    assert btc['price'] == 65000.5 and btc['change_basis'] == 'rolling_24h' and btc['attribution'] == 'Source: Binance'


def test_paxg_stream_time_advances_only_with_a_new_trade(runtime):
    """The ticker also pushes on best bid/ask changes: without a new trade the stored time never moves forward, so a
    quiet market turns the proxy STALE instead of showing an old last price as real-time."""
    base = utcnow() - timedelta(seconds=60)
    at = lambda seconds: base + timedelta(seconds=seconds)  # noqa: E731

    def stored():
        row = runtime.hub.store.get('proxy_quotes', 'binance:PAXG')
        return row['price'], datetime.fromisoformat(row['timestamp'])

    streamed(runtime, ticker('PAXGUSDT', '4278.00', at(0), trade=1))
    assert streamed(runtime, ticker('PAXGUSDT', '4278.42', at(1), trade=2))
    assert stored() == (4278.42, ms(at(1)))
    for second in (10, 20, 40):  # quote-only pushes, minutes of them: no new trade, no fresher time
        assert not streamed(runtime, ticker('PAXGUSDT', '4278.42', at(second), '-0.2', trade=2))
    assert stored() == (4278.42, ms(at(1)))
    runtime.proxy_written['binance:PAXG'] = time.monotonic()  # a write just happened: the next trade is throttled
    assert not streamed(runtime, ticker('PAXGUSDT', '4278.42', at(41), trade=3))  # same price, new trade: kept pending
    assert not streamed(runtime, ticker('PAXGUSDT', '4278.42', at(42), trade=3))
    runtime.proxy_written['binance:PAXG'] -= 60  # the throttle window has passed
    assert streamed(runtime, ticker('PAXGUSDT', '4278.42', at(45), trade=3))  # stamped when the trade showed, not now
    assert stored() == (4278.42, ms(at(41)))
    runtime.proxy_written['binance:PAXG'] -= 60
    assert streamed(runtime, ticker('PAXGUSDT', '4279.10', at(46)))  # no trade id in the message: a price change counts
    assert stored() == (4279.10, ms(at(46)))
    runtime.proxy_written['binance:PAXG'] -= 60
    runtime.proxy_trades.clear()  # what a reconnect does: the first message is a baseline again
    assert not streamed(runtime, ticker('PAXGUSDT', '4280.00', at(50), trade=9))
    assert stored() == (4279.10, ms(at(46)))


class FakeSocket:
    def __init__(self, messages: list[str], done: asyncio.Event):
        self.messages, self.done = messages, done

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self.stream()

    async def stream(self):
        for message in self.messages:
            yield message
        self.done.set()
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_bad_or_unknown_stream_messages_never_drop_the_socket(runtime, monkeypatch):
    from backend import runtime as loops
    stamp = utcnow() - timedelta(seconds=1)
    done, connects = asyncio.Event(), []
    messages = ['not json', ticker('PAXGUSDT', 'abc', stamp), ticker('PAXGUSDT', '4280.10', stamp, trade=7),
                ticker('DOGEUSDT', '0.1', stamp), ticker('BTCUSDT', '65001', stamp), ticker('ETHUSDT', '2500', stamp),
                ticker('PAXGUSDT', '4280.35', stamp, trade=8)]

    def connect(uri, **kwargs):
        connects.append(uri)
        return FakeSocket(messages, done)
    monkeypatch.setattr(loops.websockets, 'connect', connect)
    task = asyncio.create_task(runtime.crypto_stream())
    await asyncio.wait_for(done.wait(), 5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert len(connects) == 1 and 'paxgusdt@ticker' in connects[0]
    assert [t['price'] for t in ticks(runtime, 'BTC')] == [65001] and len(ticks(runtime, 'ETH')) == 1
    assert runtime.hub.store.get('proxy_quotes', 'binance:PAXG')['price'] == 4280.35
    assert runtime.hub.providers['binance'].health['rejected_rows'] == 2 and ticks(runtime, 'GOLD') == []


@pytest.mark.asyncio
async def test_crypto_stream_outlives_database_and_unexpected_connection_errors(runtime, monkeypatch, caplog):
    """Any failure except cancellation is logged by type and the stream goes on: a locked database during a write, or an
    unexpected error while connecting (retried with backoff), never ends the task for the life of the process."""
    import threading

    from sqlalchemy.exc import OperationalError

    from backend import runtime as loops
    stamp = utcnow() - timedelta(seconds=1)
    done, connects, writers = asyncio.Event(), [], []
    locked = OperationalError('INSERT INTO ticks', {}, Exception('database is locked'))
    messages = [ticker('BTCUSDT', '65001', stamp), ticker('BTCUSDT', '65002', stamp + timedelta(seconds=1)),
                ticker('PAXGUSDT', '4280.10', stamp, trade=1), ticker('PAXGUSDT', '4280.20', stamp, trade=2),
                ticker('ETHUSDT', '2500', stamp), ticker('PAXGUSDT', '4280.30', stamp + timedelta(seconds=1), trade=3)]

    def connect(uri, **kwargs):
        connects.append(uri)
        if len(connects) == 1:
            raise RuntimeError('not a network error')  # outside the old except tuple: used to end the task
        return FakeSocket(messages, done)
    ingest, write = runtime.hub.ingest_quote, runtime.write_stream_proxy
    failures = {'ingest': 1, 'write': 1}

    def flaky_ingest(quote):
        if failures['ingest']:
            failures['ingest'] -= 1
            raise locked
        return ingest(quote)

    def flaky_write(row):
        writers.append(threading.get_ident())
        if failures['write']:
            failures['write'] -= 1
            raise locked
        return write(row)
    monkeypatch.setattr(loops.websockets, 'connect', connect)
    monkeypatch.setattr(runtime.hub, 'ingest_quote', flaky_ingest)
    monkeypatch.setattr(runtime, 'write_stream_proxy', flaky_write)
    caplog.set_level(logging.WARNING)
    task = asyncio.create_task(runtime.crypto_stream())
    await asyncio.wait_for(done.wait(), 10)
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert len(connects) == 2
    assert [t['price'] for t in ticks(runtime, 'BTC')] == [65002] and len(ticks(runtime, 'ETH')) == 1
    assert runtime.hub.store.get('proxy_quotes', 'binance:PAXG')['price'] == 4280.30
    assert writers and threading.get_ident() not in writers  # proxy writes run off the event loop
    failed = [r for r in caplog.records if r.getMessage() == 'stream_message_failed']
    assert [r.error_type for r in failed] == ['OperationalError']  # one line per error type per minute
    assert [r.error_type for r in caplog.records if r.getMessage() == 'crypto_stream_reconnecting'] == ['RuntimeError']
    assert 'rejected_rows' not in runtime.hub.providers['binance'].health  # not the messages' fault


@pytest.mark.asyncio
async def test_rest_fallback_uses_binance_then_coinbase_when_binance_fails(runtime, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == 'data-api.binance.vision':
            return httpx.Response(451, content=b'restricted location')
        if request.url.path.endswith('/ticker'):
            return httpx.Response(200, json={'price': '4277.59', 'time': (utcnow() - timedelta(minutes=4)).isoformat(),
                                             'bid': '4275.83', 'ask': '4277.61'})
        return httpx.Response(200, json={'open': '4281.42', 'last': '4277.59'})
    seen = mock_http(monkeypatch, handler)
    runtime.hub.providers = {'binance': BinanceProvider(), 'coinbase': CoinbaseProvider()}
    await runtime.poll_proxy_fallback()
    assert runtime.hub.store.get('proxy_quotes', 'binance:PAXG') is None
    proxy = runtime.hub.store.get('proxy_quotes', 'coinbase:PAXG')
    assert proxy['price'] == 4277.59 and proxy['proxy_for'] == 'GOLD' and proxy['provider'] == 'coinbase'
    assert proxy['change_pct'] == pytest.approx((4277.59 / 4281.42 - 1) * 100, abs=1e-4) and proxy['change_basis'] == 'rolling_24h'
    assert {r.url.path for r in seen if r.url.host == 'api.exchange.coinbase.com'} == {'/products/PAXG-USD/ticker',
                                                                                       '/products/PAXG-USD/stats'}
    count = len(seen)
    await runtime.poll_proxy_fallback()  # at most once per proxy_rest_fallback seconds
    assert len(seen) == count and ticks(runtime, 'GOLD') == []


@pytest.mark.asyncio
async def test_binance_rest_proxy_uses_the_last_trade_time(monkeypatch):
    trade_ms = int((utcnow() - timedelta(seconds=40)).timestamp() * 1000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/api/v3/ticker/24hr':
            assert json.loads(request.url.params['symbols']) == ['PAXGUSDT']
            return httpx.Response(200, json=[{'symbol': 'PAXGUSDT', 'lastPrice': '4278.09', 'priceChangePercent': '-0.103',
                                              'bidPrice': '4278.08', 'askPrice': '4278.09', 'closeTime': 1}])
        return httpx.Response(200, json=[{'price': '4278.10', 'time': trade_ms}])
    mock_http(monkeypatch, handler)
    row = (await BinanceProvider().proxy_quotes({'PAXGUSDT': 'PAXG'}))[0]
    assert row['price'] == 4278.10 and row['timestamp'] == datetime.fromtimestamp(trade_ms / 1000, UTC).isoformat()
    assert row['change_pct'] == -0.103 and row['change_basis'] == 'rolling_24h'


# --- 5. EIA spot -------------------------------------------------------------------------------------------------------

def eia_page(brent_latest: str = '114.89') -> str:
    dates = ['09/15/26', '09/16/26', '09/17/26', '09/18/26', '09/21/26', '09/22/26']

    # Mirrors the live page: every DataRow's stub cell nests its own <table><tr>…</tr></table> (a lazy '<tr>.*?</tr>'
    # row match used to stop inside it and lose every price cell).
    def row(stub: str, code: str, values: list[str]) -> str:
        cells = ''.join(f'<td class="DataB">{v}</td>' for v in values[:-1]) + f'<td class="Current2">{values[-1]}</td>'
        return (f'<tr class="DataRow"><td class="DataStub"><table class="data2"><tr><td width="3"></td>'
                f'<td class="DataStub1">{stub}</td></tr></table></td>{cells}<td class="DataHist">'
                f'<a href="./hist/LeafHandler.ashx?n=PET&amp;s={code}&amp;f=D" class="Hist">1986-2026</a></td></tr>\n')
    header = ''.join(f'<th class="Series5">{d}</th>' for d in dates)
    return ('<html><body><table class="data1">\n'
            f'<tr><th class="Series">Product by Area</th>{header}<th class="Series">View History</th></tr>\n'
            '<tr class="DataRow"><td class="DataStub2"><table class="data2"><tr><td>Crude Oil ($/Barrel)</td></tr>'
            '</table></td></tr>\n'
            + row('WTI - Cushing, Oklahoma', 'RWTC', ['107.02', '103.620', '103.21', '101.44', '96.97', '96.41'])
            + row('Brent - Europe', 'RBRTE', ['130.80', '127.840', '121.18', '119.66', '116.15', brent_latest])
            + row('New York Harbor Conventional Gasoline', 'EER_EPMRU_PF4_Y35NY_DPG',
                  ['2.101', '2.050', 'NA', '2.000', '1.990', '1.985'])
            + '</table><p>Release Date: 9/23/2026 &nbsp; Next Release Date: 9/30/2026</p></body></html>')


def test_eia_spot_table_parses_by_series_code_into_daily_closes():
    table = parse_spot_table(eia_page())
    assert table['dates'][-1] == date(2026, 9, 22) and len(table['series']['RWTC']) == 6
    assert table['release_date'] == '2026-09-23' and table['next_release_date'] == '2026-09-30'
    quotes = {q['symbol']: q for q in spot_quotes(table)}
    wti = quotes['WTI']
    assert wti['price'] == 96.41 and wti['timestamp'] == '2026-09-22T20:00:00Z' and wti['provider'] == 'eia'
    assert wti['previous_close'] == 96.97 and wti['previous_close_at'] == '2026-09-21T20:00:00Z'
    assert wti['change_pct'] == pytest.approx((96.41 / 96.97 - 1) * 100, abs=1e-4) and wti['change_basis'] == 'previous_close'
    assert wti['data_class'] == 'DAILY_CLOSE' and wti['daily_close'] is True and wti['as_of_label'] == 'EIA spot, as of Tue 22 Sep'
    assert wti['attribution'] == 'Source: U.S. Energy Information Administration' and wti['label'] == 'WTI spot (Cushing, OK)'
    assert quotes['BRENT']['price'] == 114.89 and quotes['BRENT']['previous_close'] == 116.15
    blank = {q['symbol']: q for q in spot_quotes(parse_spot_table(eia_page('&nbsp;')))}['BRENT']
    assert blank['price'] == 116.15 and blank['timestamp'] == '2026-09-21T20:00:00Z'  # never filled forward
    assert blank['previous_close'] == 119.66
    assert spot_quotes(parse_spot_table('<html>layout changed</html>')) == []
    page = eia_page()
    header = page[page.index('<tr><th'):page.index('</tr>', page.index('<tr><th')) + 5]
    brent = page.index('<tr class="DataRow">', page.index('RWTC'))
    repeated = page[:brent] + header + page[brent:]
    assert parse_spot_table(repeated)['series'] == table['series']  # a header repeated per section is not a new column


@pytest.mark.asyncio
async def test_eia_spot_uses_conditional_get_and_flags_a_changed_layout(monkeypatch):
    answers = [httpx.Response(200, text=eia_page(), headers={'Last-Modified': 'Wed, 23 Sep 2026 15:44:11 GMT'}),
               httpx.Response(304)]
    seen = mock_http(monkeypatch, lambda r: answers.pop(0))
    provider = EIASpotPriceProvider()
    first = await provider.quotes()
    assert {q['symbol'] for q in first} == {'WTI', 'BRENT'} and seen[0].url.path == '/dnav/pet/pet_pri_spt_s1_d.htm'
    assert 'if-modified-since' not in seen[0].headers
    again = await provider.quotes()
    assert seen[1].headers['if-modified-since'] == 'Wed, 23 Sep 2026 15:44:11 GMT' and again == first
    assert provider.health['status'] == 'ONLINE' and provider.health['next_release_date'] == '2026-09-30'
    mock_http(monkeypatch, lambda r: httpx.Response(200, text='<html>new layout</html>'))
    changed = EIASpotPriceProvider()
    assert await changed.quotes() == [] and changed.health['status'] == 'DEGRADED'


@pytest.mark.asyncio
async def test_eia_spot_loop_ingests_daily_closes(runtime, monkeypatch):
    mock_http(monkeypatch, lambda r: httpx.Response(200, text=eia_page()))
    runtime.hub.providers = {'eia_spot': EIASpotPriceProvider()}
    await runtime.poll_eia_spot()
    wti = ticks(runtime, 'WTI')[0]
    assert wti['provider'] == 'eia' and wti['price'] == 96.41 and wti['previous_close'] == 96.97


# --- 6. FRED quote mapping -------------------------------------------------------------------------------------------

def fred_record(series: str, value: float, period: str, previous: float, previous_period: str) -> dict:
    return {'provider': 'fred', 'series': series, 'value': value, 'period': period, 'previous_value': previous,
            'previous_period': previous_period, 'attribution': 'Source: FRED, Federal Reserve Bank of St. Louis'}


def test_fred_quote_symbols_are_configured_series_and_tracked_assets():
    config = rules()['official_data']['fred']
    assets = {symbol for symbol, _, _ in rules()['assets']}
    assert config['quote_symbols'] == {'SP500': 'SP500', 'NASDAQCOM': 'NASDAQ', 'VIXCLS': 'VIX', 'DCOILWTICO': 'WTI',
                                       'DCOILBRENTEU': 'BRENT'}
    assert set(config['quote_symbols']) <= set(config['series']) and set(config['quote_symbols'].values()) <= assets
    assert 'DTWEXBGS' not in config['quote_symbols']  # the Fed broad index is never mapped onto DXY
    for series in config['quote_symbols']:  # the owner's notice, FRED as the channel, and no licence FRED does not grant
        text = config['series'][series]['quote_attribution'] + ' ' + config['series'][series].get('attribution_extra', '')
        assert 'via FRED' in config['series'][series]['quote_attribution'] and 'personal' not in text.lower()
        assert len(config['series'][series]['quote_attribution']) <= 200  # Quote.attribution limit
    assert config['api_notice'] == ('This product uses the FRED® API but is not endorsed or certified by the Federal '
                                    'Reserve Bank of St. Louis.')
    assert fred_quote_attribution({'title': 'Some series'}) == 'Source: Some series via FRED'
    assert fred_quote_attribution({'title': 'x', 'quote_attribution': 'Source: Owner'}) == 'Source: Owner (via FRED)'


def test_fred_observations_become_labelled_daily_closes():
    quotes = {q['symbol']: q for q in fred_quotes([
        fred_record('SP500', 7743.41, '2026-09-25', 7700.0, '2026-09-24'),
        fred_record('VIXCLS', 14.21, '2026-09-22', 15.0, '2026-09-21'),
        fred_record('DGS10', 4.1, '2026-09-25', 4.0, '2026-09-24'),  # not a quote series
        dict(fred_record('NASDAQCOM', 27068.72, '2026-09-25', 0, '2026-09-24'), previous_value=None)])}
    assert set(quotes) == {'SP500', 'VIX', 'NASDAQ'}
    sp = Quote.model_validate(quotes['SP500'])
    assert sp.provider == 'fred' and sp.data_class == 'DAILY_CLOSE' and sp.daily_close
    assert sp.timestamp == datetime(2026, 9, 25, 20, 0, tzinfo=UTC) and sp.previous_close == 7700.0
    assert sp.change_basis == 'previous_close' and sp.change_pct == pytest.approx((7743.41 / 7700 - 1) * 100, abs=1e-4)
    assert 'S&P Dow Jones Indices LLC via FRED' in sp.attribution and 'prior written permission' in sp.attribution
    assert 'Copyright NASDAQ OMX Group' in quotes['NASDAQ']['attribution']
    assert all('via FRED' in q['attribution'] and 'personal' not in q['attribution'].lower() for q in quotes.values())
    assert 'Cboe via FRED' in quotes['VIX']['attribution'] and 'FRED lags' in quotes['VIX']['as_of_label']
    assert 'previous_close' not in quotes['NASDAQ'] and 'change_pct' not in quotes['NASDAQ']


def official_fakes(fred_configured: bool, latest: list[dict]) -> dict:
    fred = Fake('fred', latest=returns(latest), observations=returns([]))
    fred.configured = fred_configured
    return {'fred': fred, 'nyfed_rates': Fake('nyfed_rates', policy_rate=returns({}))}


@pytest.mark.asyncio
async def test_official_loop_emits_fred_closes_only_with_a_key(runtime):
    day, before = (utcnow() - timedelta(days=3)).date(), (utcnow() - timedelta(days=4)).date()
    latest = [fred_record('SP500', 7743.41, day.isoformat(), 7700.0, before.isoformat()),
              fred_record('DCOILWTICO', 96.41, day.isoformat(), 96.97, before.isoformat())]
    runtime.hub.providers = official_fakes(False, latest)
    await runtime.poll_official()
    assert ticks(runtime, 'SP500') == []  # NOT CONFIGURED: nothing is shown or invented
    runtime.hub.providers = official_fakes(True, latest)
    await runtime.poll_official()
    sp = ticks(runtime, 'SP500')[0]
    assert sp['provider'] == 'fred' and sp['price'] == 7743.41 and sp['previous_close'] == 7700.0
    assert sp['data_class'] == 'DAILY_CLOSE' and 'S&P Dow Jones Indices' in sp['attribution']
    assert ticks(runtime, 'WTI')[0]['provider'] == 'fred'


# --- 7. Alpaca ------------------------------------------------------------------------------------------------------

FORBIDDEN = ('paper-api.alpaca.markets', 'api.alpaca.markets/v2/orders', '/v2/account', '/v2/positions', 'trade_updates')


def test_backend_never_references_alpaca_trading_endpoints():
    offenders = []
    for path in BACKEND.rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts:
            text = path.read_bytes().decode('utf-8', errors='ignore')
            offenders += [f'{path.relative_to(BACKEND)}: {needle}' for needle in FORBIDDEN if needle in text]
    assert offenders == []
    source = (BACKEND / 'providers' / 'alpaca.py').read_text(encoding='utf-8')
    assert 'api.alpaca.markets' not in source.replace('data.alpaca.markets', '')


@pytest.fixture
def clock(monkeypatch):
    moments = {'now': NOW}
    monkeypatch.setattr(alpaca_module, 'utcnow', lambda: moments['now'])
    monkeypatch.setattr(finnhub_module, 'utcnow', lambda: moments['now'])
    return moments


def alpaca() -> AlpacaDataProvider:
    return AlpacaDataProvider('PKTESTKEYID', 'test-secret-value')


@pytest.mark.parametrize('url', ['https://api.alpaca.markets', 'http://data.alpaca.markets', 'https://data.alpaca.markets/v2',
                                 'https://data.alpaca.markets.example.com', 'https://data.sandbox.alpaca.markets',
                                 'https://user@data.alpaca.markets', 'https://data.alpaca.markets:8443'])
def test_alpaca_host_is_pinned_to_the_market_data_host(url):
    with pytest.raises(ValueError):
        AlpacaDataProvider('PKTESTKEYID', 'test-secret-value', base_url=url)
    assert alpaca().base_url == 'https://data.alpaca.markets'


@pytest.mark.asyncio
async def test_alpaca_refuses_other_methods_paths_and_feeds_before_any_request(monkeypatch):
    seen = mock_http(monkeypatch, offline)
    provider = alpaca()
    for method, path, params in [('POST', '/v2/stocks/snapshots', {}), ('DELETE', '/v2/stocks/bars', {}),
                                 ('GET', '/v2/stocks/AAPL/trades/latest', {}), ('GET', '/v2/clock', {}),
                                 ('GET', '/v2/stocks/snapshots/../../v2/assets', {}),
                                 ('GET', '/v2/stocks/snapshots', {'feed': 'sip'}),
                                 ('GET', '/v2/stocks/bars', {'apca-api-key-id': 'x'})]:
        with pytest.raises(ValueError):
            await provider.request(method, path, params=params)
    provider.base_url = 'https://elsewhere.example.com'  # even a mutated instance cannot leave the pinned host
    with pytest.raises(ValueError):
        await provider.request('GET', '/v2/stocks/snapshots')
    assert seen == []


@pytest.mark.asyncio
async def test_alpaca_without_keys_is_not_configured_and_never_calls_out(monkeypatch):
    seen = mock_http(monkeypatch, offline)
    provider = AlpacaDataProvider('', '')
    assert provider.configured is False and provider.health['status'] == 'NOT CONFIGURED'
    for call in (provider.snapshots(['SPY']), provider.daily_bars(['SPY'], date(2026, 1, 1)), provider.documents()):
        with pytest.raises(ProviderUnavailable, match='NOT CONFIGURED'):
            await call
    assert seen == [] and AlpacaDataProvider('PKTESTKEYID', '').configured is False


def snapshot(price: float, trade_at: str, daily: tuple[str, float] | None, previous: tuple[str, float] | None) -> dict:
    def bar(t: str, close: float) -> dict:
        return {'t': t, 'o': close, 'h': close + 1, 'l': close - 1, 'c': close, 'v': 1000, 'n': 10, 'vw': close}
    return {'latestTrade': {'t': trade_at, 'p': price, 's': 100, 'x': 'V', 'c': ['@'], 'i': 7, 'z': 'C'},
            'latestQuote': {'t': trade_at, 'bp': price - 0.01, 'ap': price + 0.01, 'bs': 1, 'as': 1, 'bx': 'V', 'ax': 'V'},
            'dailyBar': bar(*daily) if daily else None, 'prevDailyBar': bar(*previous) if previous else None}


SNAPSHOTS = {
    # regular session: dailyBar is today, so the previous close is prevDailyBar
    'AAPL': snapshot(251.25, '2026-09-25T17:59:30.123456789Z', ('2026-09-25T04:00:00Z', 251.2), ('2026-09-24T04:00:00Z', 250.0)),
    # no IEX trade for 10 minutes while the market is open
    'SPY': snapshot(660.0, '2026-09-25T17:50:00Z', ('2026-09-25T04:00:00Z', 660.0), ('2026-09-24T04:00:00Z', 655.0)),
    'GLD': snapshot(390.0, '2026-09-25T17:59:50Z', None, None),
    'BMNR': {'latestQuote': {'t': '2026-09-25T17:59:50Z', 'bp': 50, 'ap': 51}},  # no trade: never priced from a quote
}


def snapshot_handler(bodies: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == 'data.alpaca.markets' and request.method == 'GET'
        if request.url.path == '/v2/stocks/snapshots':
            wanted = request.url.params['symbols'].split(',')
            return httpx.Response(200, json={s: bodies[s] for s in wanted if s in bodies},
                                  headers={'X-RateLimit-Remaining': '199'})
        return httpx.Response(404)
    return handler


@pytest.mark.asyncio
async def test_alpaca_snapshots_are_labelled_iex_quotes_with_credentials_in_headers_only(monkeypatch, clock):
    seen = mock_http(monkeypatch, snapshot_handler(SNAPSHOTS))
    provider = alpaca()
    records = {r['symbol']: r for r in await provider.snapshots(['AAPL', 'SPY', 'GLD', 'BMNR', 'SP500', 'XYZ'])}
    request = seen[0]
    assert request.url.params['symbols'] == 'AAPL,BMNR,GLD,SPY' and request.url.params['feed'] == 'iex'
    assert request.headers['APCA-API-KEY-ID'] == 'PKTESTKEYID' and request.headers['APCA-API-SECRET-KEY'] == 'test-secret-value'
    assert 'test-secret-value' not in str(request.url) and 'PKTESTKEYID' not in str(request.url)
    assert 'test-secret-value' not in json.dumps(provider.health) and provider.health['x_ratelimit_remaining'] == '199'
    assert set(records) == {'AAPL', 'SPY', 'GLD'} and provider.health['rejected_rows'] == 1  # BMNR has no trade
    aapl = records['AAPL']
    assert aapl['price'] == 251.25 and aapl['timestamp'].startswith('2026-09-25T17:59:30.123456')
    assert aapl['previous_close'] == 250.0 and aapl['change_pct'] == pytest.approx(0.5) and aapl['change_basis'] == 'previous_close'
    assert aapl['previous_close_at'] == '2026-09-24T20:00:00Z'
    assert aapl['provider'] == 'alpaca' and aapl['feed'] == 'iex' and aapl['label'] == 'AAPL (IEX)'
    assert aapl['delay_label'] == 'real-time IEX (single exchange, not consolidated)' and aapl['daily_close'] is False
    assert 'vs IEX prior-session close' in aapl['as_of_label'] and aapl['attribution'].startswith('Source: Alpaca')
    assert aapl['bid'] is None and aapl['volume'] is None  # IEX top of book is not the NBBO
    assert records['SPY']['delay_label'].startswith('STALE: no IEX trade for 10 min')
    assert 'previous_close' not in records['GLD'] or records['GLD']['previous_close'] is None
    Quote.model_validate(quote_fields(aapl))


@pytest.mark.asyncio
async def test_alpaca_premarket_and_closed_market_use_the_prior_session_and_last_close_labels(monkeypatch, clock):
    premarket = snapshot(249.0, '2026-09-25T12:29:00Z', ('2026-09-24T04:00:00Z', 250.0), ('2026-09-23T04:00:00Z', 247.0))
    mock_http(monkeypatch, snapshot_handler({'AAPL': premarket}))
    clock['now'] = datetime(2026, 9, 25, 12, 30, tzinfo=UTC)  # 08:30 New York
    record = (await alpaca().snapshots(['AAPL']))[0]
    # A premarket print is not a regular-session price: the record is the IEX close of the latest closed session
    # (Alpaca's dailyBar is still the previous session before the open), compared with the session before it.
    assert record['price'] == 250.0 and record['timestamp'] == '2026-09-24T20:00:00Z' and record['daily_close'] is True
    assert record['previous_close'] == 247.0
    assert record['delay_label'] == 'PREMARKET (IEX regular-session close Thu 24 Sep 16:00 ET)'
    friday = snapshot(251.0, '2026-09-25T19:59:59Z', ('2026-09-25T04:00:00Z', 251.0), ('2026-09-24T04:00:00Z', 250.0))
    mock_http(monkeypatch, snapshot_handler({'AAPL': friday}))
    clock['now'] = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)  # Saturday
    record = (await alpaca().snapshots(['AAPL']))[0]
    assert record['delay_label'].startswith('FRIDAY CLOSE (IEX last trade') and record['daily_close'] is True
    assert record['previous_close'] == 250.0


@pytest.mark.asyncio
async def test_alpaca_auth_errors_are_reported_without_parsing_the_body(monkeypatch):
    mock_http(monkeypatch, lambda r: httpx.Response(401, text='<html>401 Authorization Required</html>'))
    provider = alpaca()
    with pytest.raises(ProviderUnavailable) as caught:
        await provider.snapshots(['SPY'])
    assert caught.value.status_code == 401 and provider.health['status'] == 'AUTH_ERROR'
    assert 'test-secret-value' not in json.dumps(provider.health)


@pytest.mark.asyncio
async def test_alpaca_rate_guard_blocks_calls_before_the_vendor_does(monkeypatch):
    seen = mock_http(monkeypatch, snapshot_handler(SNAPSHOTS))
    provider = alpaca()
    provider.guard.stamps.extend([provider.guard.clock()] * provider.guard.calls)
    with pytest.raises(finnhub_module.RateGuardFull):
        await provider.snapshots(['SPY'])
    assert seen == []


def bar(t: str, close: float, n: int = 5) -> dict:
    return {'t': t, 'o': close, 'h': close + 2, 'l': close - 2, 'c': close, 'v': 12345, 'n': n, 'vw': close}


@pytest.mark.asyncio
async def test_alpaca_daily_bars_follow_pages_and_key_by_new_york_session_date(monkeypatch, clock):
    pages = {None: {'bars': {'AAPL': [bar('2026-09-24T04:00:00Z', 250.0), {'t': '2026-09-23T04:00:00Z', 'o': 1, 'h': 0.5,
                                                                          'l': 1, 'c': 1, 'v': 1}]},
                    'next_page_token': 'page-2'},
             'page-2': {'bars': {'SPY': [bar('2026-01-15T05:00:00Z', 590.0), bar('2026-09-25T04:00:00Z', 660.0)],
                                 'MSFT': [bar('2026-09-24T04:00:00Z', 500.0)]},
                        'next_page_token': None}}
    seen = mock_http(monkeypatch, lambda r: httpx.Response(200, json=pages[r.url.params.get('page_token')]))
    provider = alpaca()
    rows = await provider.daily_bars(['AAPL', 'SPY'], date(2025, 7, 1))
    assert [r.url.params.get('page_token') for r in seen] == [None, 'page-2']
    params = seen[0].url.params
    assert (params['timeframe'], params['feed'], params['adjustment'], params['start']) == ('1Day', 'iex', 'split', '2025-07-01')
    assert params['symbols'] == 'AAPL,SPY'
    starts = {(r['symbol'], r['start'].isoformat()) for r in rows}
    # EDT session -> 04:00Z, EST session -> 05:00Z; today's bar is still forming at 14:00 New York; MSFT was not asked
    assert starts == {('AAPL', '2026-09-24T04:00:00+00:00'), ('SPY', '2026-01-15T05:00:00+00:00')}
    assert all(r['interval_seconds'] == 86400 and r['provider'] == 'alpaca' for r in rows)
    assert provider.health['rejected_rows'] == 1  # the incoherent bar
    clock['now'] = datetime(2026, 9, 25, 21, 0, tzinfo=UTC)  # after the close: today's bar is final
    rows = await provider.daily_bars(['SPY'], date(2026, 9, 1))
    assert ('SPY', '2026-09-25T04:00:00+00:00') in {(r['symbol'], r['start'].isoformat()) for r in rows}
    assert session_start(date(2026, 12, 1)).isoformat() == '2026-12-01T05:00:00+00:00'


NEWS = {'news': [
    {'id': 101, 'headline': 'Apple beats estimates on services', 'author': 'Staff', 'created_at': '2026-09-25T17:30:00Z',
     'updated_at': '2026-09-25T17:31:00Z', 'summary': '<p>Apple &amp; co <b>rose</b> after the report.</p>',
     'content': '<p>Full article body that must not be requested or stored</p>', 'images': [],
     'url': 'https://www.benzinga.com/news/26/09/101/apple-beats', 'symbols': ['AAPL', 'XYZ'], 'source': 'benzinga'},
    {'id': 102, 'headline': 'No link for this one', 'author': 'Staff', 'created_at': '2026-09-25T17:32:00Z',
     'updated_at': '2026-09-25T17:32:00Z', 'summary': '', 'url': None, 'symbols': ['MSFT'], 'source': 'benzinga'},
    {'id': 103, 'headline': 'Another wire says something', 'author': 'Staff', 'created_at': '2026-09-25T17:33:00Z',
     'updated_at': '2026-09-25T17:33:00Z', 'summary': 'x', 'url': 'https://example.com/a', 'symbols': ['TSLA'],
     'source': 'other-wire'},
    {'id': 104, 'headline': 'Bitcoin climbs past a round number', 'author': 'Staff', 'created_at': '2026-09-25T17:40:00Z',
     'updated_at': '2026-09-25T17:45:00Z', 'summary': 'Crypto rally.', 'url': 'https://www.benzinga.com/crypto/104',
     'symbols': ['BTCUSD', 'COIN'], 'source': 'benzinga'}],
    'next_page_token': None}


@pytest.mark.asyncio
async def test_alpaca_news_become_benzinga_documents_headline_and_summary_only(monkeypatch, clock):
    seen = mock_http(monkeypatch, lambda r: httpx.Response(200, json=NEWS))
    provider = alpaca()
    documents = await provider.documents()
    params = seen[0].url.params
    assert seen[0].url.path == '/v1beta1/news' and params['include_content'] == 'false' and 'feed' not in params
    assert {'AAPL', 'COIN', 'BTCUSD', 'ETHUSD'} <= set(params['symbols'].split(','))
    assert params['start'] == '2026-09-25T12:00:00Z'  # first run: first_lookback_hours back
    assert [d['url'] for d in documents] == ['https://www.benzinga.com/news/26/09/101/apple-beats',
                                             'https://www.benzinga.com/crypto/104']
    apple, bitcoin = (DocumentInput.model_validate(d) for d in documents)
    assert apple.source_id == 'benzinga' and apple.content == 'Apple & co rose after the report.'
    assert apple.assets == ['AAPL'] and bitcoin.assets == ['BTC', 'COIN'] and 'Full article' not in apple.content
    assert provider.health['news_dropped'] == 2
    assert await provider.documents() == []  # already ingested
    assert seen[1].url.params['start'] == '2026-09-25T17:40:00Z'  # cursor (latest update) minus the overlap


@pytest.mark.asyncio
async def test_alpaca_news_loop_uses_the_document_pipeline(runtime, monkeypatch, clock):
    clock['now'] = utcnow()
    body = json.loads(json.dumps(NEWS))
    for row in body['news']:
        row['created_at'] = row['updated_at'] = (utcnow() - timedelta(minutes=5)).isoformat()
    mock_http(monkeypatch, lambda r: httpx.Response(200, json=body))

    async def no_enrichment(event_id):
        return None
    monkeypatch.setattr(runtime.hub, 'enrich_event', no_enrichment)
    runtime.hub.providers = {'alpaca': alpaca()}
    await runtime.poll_alpaca_news()
    source = next(s for s in rules()['sources'] if s['id'] == 'benzinga')
    assert (source['tier'], source['kind'], source['domain'], source['official']) == (4, 'specialist', 'benzinga.com', False)
    events = runtime.hub.events()
    assert sorted(e['summary_fact'] for e in events) == ['Apple beats estimates on services',
                                                         'Bitcoin climbs past a round number']


@pytest.mark.asyncio
async def test_alpaca_bars_loop_stores_daily_bar_rows(runtime, monkeypatch, clock):
    clock['now'] = utcnow()
    day = (utcnow() - timedelta(days=5)).date()
    start = datetime.combine(day, datetime.min.time(), alpaca_module.NEW_YORK).astimezone(UTC)
    stamp = start.isoformat().replace('+00:00', 'Z')
    seen = mock_http(monkeypatch, lambda r: httpx.Response(200, json={
        'bars': {'AAPL': [bar(stamp, 250.0)], 'GLD': [bar(stamp, 390.0)]}, 'next_page_token': None}))
    runtime.hub.providers = {'alpaca': alpaca()}
    await runtime.poll_alpaca_bars()
    await runtime.poll_alpaca_bars()  # upsert, not a duplicate
    requested = set(seen[0].url.params['symbols'].split(','))
    assert {'AAPL', 'TSLA', 'SPY', 'QQQ', 'IWM', 'GLD', 'USO', 'BNO', 'UUP'} <= requested and 'SP500' not in requested
    assert seen[0].url.params['start'] == (utcnow() - timedelta(days=420)).date().isoformat()
    assert seen[1].url.params['start'] == (utcnow() - timedelta(days=14)).date().isoformat()  # later runs refresh
    stored = runtime.hub.store.bars('AAPL', 86400, start - timedelta(days=1))
    assert [(b['start'], b['close'], b['provider']) for b in stored] == [(start.isoformat(), 250.0, 'alpaca')]
    assert runtime.hub.store.bars('GLD', 86400, start - timedelta(days=1))[0]['close'] == 390.0
    assert int(rules()['refresh']['storage']['bar_retention_days']['86400']) >= 400
    assert '86400' not in [str(i) for i in rules()['refresh']['storage']['bar_intervals_seconds']]


def test_alpaca_settings_group_registry_and_probe():
    group = ps.group('alpaca')
    assert group['category'] == 'market' and group['providers'] == ['alpaca'] and group['test']['target'] == 'alpaca'
    assert [(f['name'], f['kind'], f['required']) for f in group['fields']] == [
        ('alpaca_api_key_id', 'secret', True), ('alpaca_api_secret_key', 'secret', True)]
    assert group['signup_url'] == 'https://alpaca.markets'
    assert group['pricing'] == 'Free (paper account, IEX real-time, personal use)'
    assert ps.affected(['alpaca_api_key_id']) == ({'alpaca'}, False)
    providers = build_providers(Settings(_env_file=None))
    assert providers['alpaca'].configured is False and providers['eia_spot'].configured is True
    keyed = build_providers(Settings(_env_file=None, alpaca_api_key_id='PKTESTKEYID', alpaca_api_secret_key='test-secret-value'))
    assert keyed['alpaca'].configured is True and 'test-secret-value' not in json.dumps(keyed['alpaca'].health)
    assert any(cls is AlpacaDataProvider for cls, _ in ps.PROBES) and ps.can_test(keyed['alpaca'])
    assert ps.can_test(providers['eia_spot'])


@pytest.mark.asyncio
async def test_alpaca_connection_probe_is_one_spy_snapshot(monkeypatch, clock):
    seen = mock_http(monkeypatch, snapshot_handler(SNAPSHOTS))
    probe = next(call for cls, call in ps.PROBES if cls is AlpacaDataProvider)
    await probe(alpaca(), NOW.date())
    assert len(seen) == 1 and seen[0].url.params['symbols'] == 'SPY'


# --- 8. Finnhub ETF proxies and the Alpaca equity fallback ------------------------------------------------------------

def finnhub_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host != 'finnhub.io':
        return snapshot_handler(SNAPSHOTS)(request)
    symbol = request.url.params['symbol']
    if symbol == 'BMNR':
        return httpx.Response(200, json={'c': 0, 'd': None, 'dp': None, 'h': 0, 'l': 0, 'o': 0, 'pc': 0, 't': 0})
    return httpx.Response(200, json={'c': 101.5, 'd': 1.5, 'dp': 1.5, 'h': 102, 'l': 99, 'o': 100, 'pc': 100,
                                     't': int(NOW.timestamp()) - 30})


@pytest.mark.asyncio
async def test_finnhub_etf_proxies_are_stored_as_proxies_and_equities_keep_their_labels(runtime, monkeypatch, clock):
    seen = mock_http(monkeypatch, finnhub_handler)
    runtime.hub.providers = {'finnhub': FinnhubProvider('test-finnhub-key')}
    await runtime.poll_equities()
    asked = {r.url.params['symbol'] for r in seen}
    assert {'SPY', 'QQQ', 'IWM', 'AAPL'} <= asked and not asked & {'SP500', 'NASDAQ', 'RUSSELL'}
    aapl = ticks(runtime, 'AAPL')[0]
    assert aapl['previous_close'] == 100 and aapl['change_pct'] == 1.5 and aapl['change_basis'] == 'previous_close'
    assert aapl['label'] == 'AAPL' and aapl['attribution'] == 'Source: Finnhub' and aapl['as_of_label'].startswith('real-time')
    spy = runtime.hub.store.get('proxy_quotes', 'finnhub:SPY')
    spec = rules()['coverage']['proxies']['SP500'][0]
    assert spy['proxy_for'] == 'SP500' and spy['label'] == spec['label'] and spy['caveat'] == spec['caveat']
    assert spy['price'] == 101.5 and spy['previous_close'] == 100 and spy['change_basis'] == 'previous_close'
    assert spy['delay_label'].startswith('real-time') and spy['attribution'] == 'Source: Finnhub'
    assert runtime.hub.store.get('proxy_quotes', 'finnhub:QQQ')['proxy_for'] == 'NASDAQ'
    assert runtime.hub.store.get('proxy_quotes', 'finnhub:IWM')['proxy_for'] == 'RUSSELL'
    assert ticks(runtime, 'SPY') == [] and ticks(runtime, 'SP500') == []
    assert runtime.finnhub_missing == {'BMNR'}  # Finnhub answered zeros: only that equity falls back to Alpaca


@pytest.mark.asyncio
async def test_alpaca_fills_only_what_finnhub_could_not_serve_plus_its_etf_proxies(runtime, monkeypatch, clock):
    snapshots = dict(SNAPSHOTS, BMNR=snapshot(50.5, '2026-09-25T17:59:40Z', ('2026-09-25T04:00:00Z', 50.5),
                                              ('2026-09-24T04:00:00Z', 50.0)))
    seen = mock_http(monkeypatch, lambda r: finnhub_handler(r) if r.url.host == 'finnhub.io' else snapshot_handler(snapshots)(r))
    runtime.hub.providers = {'finnhub': FinnhubProvider('test-finnhub-key'), 'alpaca': alpaca()}
    await runtime.poll_equities()
    await runtime.poll_alpaca_quotes()
    asked = set(next(r for r in seen if r.url.host == 'data.alpaca.markets').url.params['symbols'].split(','))
    assert asked == {'BMNR', 'SPY', 'QQQ', 'IWM', 'GLD', 'UUP', 'USO', 'BNO'}
    bmnr = ticks(runtime, 'BMNR')[0]
    assert bmnr['provider'] == 'alpaca' and bmnr['feed'] == 'iex' and bmnr['label'] == 'BMNR (IEX)'
    assert [t['provider'] for t in ticks(runtime, 'AAPL')] == ['finnhub']  # never interleaved with IEX ticks
    gld = runtime.hub.store.get('proxy_quotes', 'alpaca:GLD')
    assert gld['proxy_for'] == 'GOLD' and gld['feed'] == 'iex' and gld['provider'] == 'alpaca' and gld['price'] == 390.0
    assert runtime.hub.store.get('proxy_quotes', 'alpaca:SPY')['proxy_for'] == 'SP500' and ticks(runtime, 'GLD') == []
    # Finnhub switched off: Alpaca serves every tracked equity
    runtime.hub.providers['finnhub'].enabled = False
    await runtime.poll_equities()
    await runtime.poll_alpaca_quotes()
    asked = set([r for r in seen if r.url.host == 'data.alpaca.markets'][-1].url.params['symbols'].split(','))
    assert {'AAPL', 'MSFT', 'TSLA', 'BMNR'} <= asked


@pytest.mark.asyncio
async def test_new_loops_never_call_disabled_or_unconfigured_providers(runtime, monkeypatch, caplog):
    seen = mock_http(monkeypatch, offline)
    caplog.set_level(logging.WARNING)
    alpaca_off, eia_off = alpaca(), EIASpotPriceProvider()
    alpaca_off.enabled = eia_off.enabled = False
    runtime.hub.providers = {'alpaca': alpaca_off, 'eia_spot': eia_off, 'binance': Fake('binance', proxy_quotes=raising),
                             'coinbase': Fake('coinbase', proxy_quotes=raising)}
    runtime.hub.providers['binance'].enabled = runtime.hub.providers['coinbase'].enabled = False
    for loop in (runtime.poll_alpaca_quotes, runtime.poll_alpaca_bars, runtime.poll_alpaca_news, runtime.poll_eia_spot,
                 runtime.poll_proxy_fallback):
        await loop()
    runtime.hub.providers = {'alpaca': AlpacaDataProvider('', '')}
    for loop in (runtime.poll_alpaca_quotes, runtime.poll_alpaca_bars, runtime.poll_alpaca_news):
        await loop()
    assert seen == [] and not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_a_news_refusal_never_blocks_or_discredits_the_market_data(monkeypatch, clock):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/v1beta1/news':
            return httpx.Response(403, text='forbidden')
        return snapshot_handler(SNAPSHOTS)(request)
    seen = mock_http(monkeypatch, handler)
    provider = alpaca()
    assert await provider.documents() == []
    assert provider.health['status'] != 'AUTH_ERROR' and provider.health['news'].startswith('NOT AVAILABLE')
    assert (await provider.snapshots(['AAPL']))[0]['price'] == 251.25  # the shared circuit stayed closed
    assert provider.health['status'] == 'ONLINE'
    count = len(seen)
    assert await provider.documents() == [] and len(seen) == count  # not asked again before the retry time
    mock_http(monkeypatch, lambda r: httpx.Response(401, text='unauthorized'))
    with pytest.raises(ProviderUnavailable):  # bad keys are still an error for everything
        await alpaca().documents()
