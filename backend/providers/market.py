import json
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from defusedxml import DefusedXmlException, ElementTree

from backend.config import rules
from backend.providers.base import HTTPProvider, ProviderUnavailable, label_day
from backend.providers.models import (
    BinanceTicker,
    BinanceTrade,
    CoinbaseStats,
    CoinbaseTicker,
    DeribitInstrument,
    DeribitSummary,
    validate_rows,
)
from backend.schemas import Quote

ODATA_PROPERTIES = '{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}properties'


class BinanceProvider(HTTPProvider):
    ATTRIBUTION = 'Source: Binance'

    def __init__(self):
        super().__init__('binance', 'https://data-api.binance.vision')

    async def quotes(self) -> list[dict]:
        response = await self.request('GET', '/api/v3/ticker/24hr', params={'symbols': '["BTCUSDT","ETHUSDT"]'})
        return [Quote(symbol=row['symbol'].removesuffix('USDT'), price=float(row['lastPrice']),
                      timestamp=datetime.fromtimestamp(row['closeTime'] / 1000, UTC), provider='binance',
                      bid=float(row['bidPrice']), ask=float(row['askPrice']), volume=float(row['volume']),
                      change_pct=float(row['priceChangePercent']), change_basis='rolling_24h',
                      attribution=self.ATTRIBUTION, latency_ms=self.health['latency_ms']).model_dump(mode='json')
                for row in response.json()]

    async def proxy_quotes(self, pairs: dict[str, str]) -> list[dict]:
        """Proxy instruments ({'PAXGUSDT': 'PAXG'}): last-trade price and time (the 24h ticker's closeTime is the end of
        its rolling window, which would overstate freshness), with the rolling 24h change. Rows for proxy_quotes only."""
        if not pairs:
            return []
        response = await self.request('GET', '/api/v3/ticker/24hr',
                                      params={'symbols': json.dumps(sorted(pairs), separators=(',', ':'))})
        result = []
        for ticker in validate_rows(BinanceTicker, response.json(), self.health):
            if ticker['symbol'] not in pairs:
                continue
            trades = validate_rows(BinanceTrade, (await self.request('GET', '/api/v3/trades', params={
                'symbol': ticker['symbol'], 'limit': 1})).json(), self.health)
            if not trades:
                continue
            last = trades[-1]
            result.append({'symbol': pairs[ticker['symbol']], 'price': last['price'],
                           'timestamp': datetime.fromtimestamp(last['time'] / 1000, UTC).isoformat(),
                           'change_pct': ticker['priceChangePercent'], 'change_basis': 'rolling_24h', 'provider': 'binance',
                           'delay_label': 'real-time (Binance REST, last trade)', 'attribution': self.ATTRIBUTION})
        return result


class CoinbaseProvider(HTTPProvider):
    ATTRIBUTION = 'Source: Coinbase Exchange'

    def __init__(self):
        super().__init__('coinbase', 'https://api.exchange.coinbase.com')

    async def quotes(self) -> list[dict]:
        result = []
        for symbol in ['BTC', 'ETH']:
            row = (await self.request('GET', f'/products/{symbol}-USD/ticker')).json()
            result.append(Quote(symbol=symbol, price=float(row['price']), timestamp=row['time'], provider='coinbase',
                                bid=float(row['bid']), ask=float(row['ask']), volume=float(row['volume']),
                                attribution=self.ATTRIBUTION, latency_ms=self.health['latency_ms']).model_dump(mode='json'))
        return result

    async def proxy_quotes(self, products: dict[str, str]) -> list[dict]:
        """Proxy instruments ({'PAXG-USD': 'PAXG'}): last-trade price and time; the change is the rolling 24h change
        from /stats (open = the price 24 hours ago), or none when the stats call fails."""
        result = []
        for product, symbol in sorted(products.items()):
            tickers = validate_rows(CoinbaseTicker, [(await self.request('GET', f'/products/{product}/ticker')).json()],
                                    self.health)
            if not tickers:
                continue
            change = None
            try:
                stats = validate_rows(CoinbaseStats, [(await self.request('GET', f'/products/{product}/stats')).json()],
                                      self.health)
                if stats:
                    change = round((stats[0]['last'] / stats[0]['open'] - 1) * 100, 4)
            except ProviderUnavailable:
                pass  # the price stands on its own; the change is simply not given
            result.append({'symbol': symbol, 'price': tickers[0]['price'], 'timestamp': tickers[0]['time'],
                           'change_pct': change, 'change_basis': 'rolling_24h' if change is not None else None,
                           'provider': 'coinbase', 'delay_label': 'last trade (Coinbase REST)',
                           'attribution': self.ATTRIBUTION})
        return result


class MassiveProvider(HTTPProvider):
    """Massive (formerly Polygon): US equities, indices, FX, futures and options. Symbols come from configuration."""

    def __init__(self, key: str, name: str = 'massive'):
        super().__init__(name, 'https://api.massive.com', key, bool(key))

    @property
    def routing(self) -> dict:
        return rules()['provider_routing']['massive']

    @property
    def auth(self) -> dict:
        return {'Authorization': f'Bearer {self.key}'}

    def _latency(self) -> float:
        return self.health['latency_ms'] or 0

    async def quotes(self) -> list[dict]:
        response = await self.request('GET', '/v2/snapshot/locale/us/markets/stocks/tickers',
                                      params={'tickers': ','.join(self.routing['equities'])}, headers=self.auth)
        result = []
        for row in response.json().get('tickers', []):
            trade = row.get('lastTrade', {})
            if trade.get('p') and trade.get('t'):
                result.append(Quote(symbol=row['ticker'], price=trade['p'],
                                    timestamp=datetime.fromtimestamp(trade['t'] / 1e9, UTC), provider='massive',
                                    change_pct=row.get('todaysChangePerc'), latency_ms=self._latency()).model_dump(mode='json'))
        return result

    async def bars(self, ticker: str, start: str, end: str) -> list[dict]:
        response = await self.request('GET', f'/v2/aggs/ticker/{ticker}/range/1/minute/{start}/{end}',
                                      headers=self.auth, params={'limit': 5000, 'sort': 'asc'})
        return response.json().get('results', [])

    async def fx_quote(self, symbol: str) -> dict:
        if len(symbol) != 6 or not symbol.isalpha():
            raise ValueError('Expected six-letter FX pair')
        response = await self.request('GET', f'/v1/last_quote/currencies/{symbol[:3]}/{symbol[3:]}', headers=self.auth)
        row = response.json()['last']
        return Quote(symbol=symbol, price=(row['ask'] + row['bid']) / 2, bid=row['bid'], ask=row['ask'],
                     timestamp=datetime.fromtimestamp(row['timestamp'] / 1000, UTC), provider='massive',
                     latency_ms=self._latency()).model_dump(mode='json')

    async def fx_quotes(self) -> list[dict]:
        return [await self.fx_quote(pair) for pair in self.routing['fx']]

    async def indices(self, mapping: dict[str, str] | None = None) -> list[dict]:
        mapping = mapping or self.routing['indices']
        response = await self.request('GET', '/v3/snapshot/indices', params={'ticker.any_of': ','.join(mapping)}, headers=self.auth)
        result = []
        for row in response.json().get('results', []):
            if row.get('ticker') not in mapping or not row.get('value') or not row.get('last_updated'):
                continue
            result.append(Quote(symbol=mapping[row['ticker']], price=row['value'],
                                timestamp=datetime.fromtimestamp(row['last_updated'] / 1e9, UTC), provider='massive',
                                change_pct=row.get('session', {}).get('change_percent'),
                                latency_ms=self._latency()).model_dump(mode='json'))
        return result

    async def options(self, underlying: str) -> list[dict]:
        """Option chain snapshot (OI, IV) for positioning; descriptive only, never directional."""
        if underlying not in self.routing['option_underlyings']:
            raise ValueError('Underlying not configured for options')
        response = await self.request('GET', f'/v3/snapshot/options/{underlying}', headers=self.auth, params={'limit': 250})
        result = []
        for row in response.json().get('results', []):
            details = row.get('details') or {} if isinstance(row, dict) else {}
            if not details.get('ticker'):
                continue
            result.append({'underlying': underlying, 'contract': details['ticker'], 'strike': details.get('strike_price'),
                           'expiry': details.get('expiration_date'), 'type': details.get('contract_type'),
                           'open_interest': row.get('open_interest'), 'implied_volatility': row.get('implied_volatility'),
                           'provider': self.name})
        return result

    def stream_subscription(self) -> list[dict]:
        """WebSocket handshake: authenticate, then subscribe to per-second aggregates of configured equities."""
        return [{'action': 'auth', 'params': self.key},
                {'action': 'subscribe', 'params': ','.join(f'A.{s}' for s in self.routing['equities'])}]

    def parse_stream_message(self, message: str) -> list[dict]:
        """Aggregate events (ev='A') become quotes with traded volume; status and other events are ignored."""
        quotes = []
        for event in json.loads(message):
            if not isinstance(event, dict) or event.get('ev') != 'A' or not event.get('c') or not event.get('e'):
                continue
            quotes.append(Quote(symbol=event['sym'], price=event['c'], volume=event.get('v'),
                                timestamp=datetime.fromtimestamp(event['e'] / 1000, UTC), provider='massive').model_dump(mode='json'))
        return quotes


class TreasuryYieldProvider(HTTPProvider):
    """Official US Treasury daily par yield curve. End-of-day levels flagged daily_close, never shown as live."""

    def __init__(self):
        url = rules()['provider_routing']['treasury_yields']['url']
        base, _, path = url.partition('/resource-center')
        super().__init__('treasury_yields', base)
        self.path = '/resource-center' + path

    @staticmethod
    def rows(content: bytes) -> dict[str, dict[str, str]]:
        """Every business day of a monthly file: NEW_DATE (YYYY-MM-DD) -> field values. A body that is not XML (an HTML
        maintenance or bot-protection page, an empty or truncated answer) raises ValueError, like any refused payload."""
        try:
            root = ElementTree.fromstring(content)
        except (ElementTree.ParseError, DefusedXmlException) as exc:
            error = ValueError('Treasury yield curve: the answer is not the expected XML')
            error.add_note(type(exc).__name__)  # the parser's exception, kept for diagnostics (health error_type)
            raise error from None
        result: dict[str, dict[str, str]] = {}
        for props in root.iter(ODATA_PROPERTIES):
            values = {child.tag.split('}')[-1]: (child.text or '').strip() for child in props}
            if values.get('NEW_DATE'):
                result[date.fromisoformat(values['NEW_DATE'][:10]).isoformat()] = values
        return result

    @staticmethod
    def build(rows: dict[str, dict[str, str]]) -> list[dict]:
        """Latest close per tenor with the previous business day's close from the same data (change in bp). A tenor
        left blank on the latest day is not emitted: an older value is never shown as the latest close."""
        config = rules()['provider_routing']['treasury_yields']
        zone = ZoneInfo(config['record_timezone'])
        hour, minute = (int(v) for v in config['record_time'].split(':'))

        def stamp(day: str) -> datetime:
            return datetime.combine(date.fromisoformat(day), time(hour, minute), zone).astimezone(UTC)

        days = sorted(rows, reverse=True)
        if not days:
            return []
        latest = days[0]
        result = []
        for field, symbol in config['fields'].items():
            value = _level(rows[latest].get(field))
            if value is None:
                continue
            previous_day = next((day for day in days[1:] if _level(rows[day].get(field)) is not None), None)
            previous = _level(rows[previous_day].get(field)) if previous_day else None
            change: dict = {}
            if previous_day and previous:
                change = {'previous_close': previous, 'previous_close_at': stamp(previous_day),
                          'change_bp': round((value - previous) * 100, 2),
                          'change_pct': round((value / previous - 1) * 100, 4), 'change_basis': 'previous_close'}
            result.append(Quote(symbol=symbol, price=value, timestamp=stamp(latest), provider='treasury', daily_close=True,
                                data_class='DAILY_CLOSE', label=config['labels'].get(symbol, symbol),
                                attribution=config['attribution'], as_of_label=config['as_of_label'].format(date=label_day(latest)),
                                **change).model_dump(mode='json'))
        return result

    @classmethod
    def parse(cls, content: bytes) -> list[dict]:
        return cls.build(cls.rows(content))

    async def _month(self, month: str) -> dict[str, dict[str, str]]:
        response = await self.request('GET', self.path, params={'data': 'daily_treasury_yield_curve',
                                                                'field_tdr_date_value_month': month})
        try:
            return self.rows(response.content)
        except ValueError as exc:  # answered, but not the data: DEGRADED, never a silent ONLINE
            self.health.update(status='DEGRADED', error=str(exc),
                               error_type=next(iter(getattr(exc, '__notes__', [])), type(exc).__name__))
            raise

    async def quotes(self, month: str | None = None) -> list[dict]:
        month = month or datetime.now(UTC).strftime('%Y%m')
        rows = await self._month(month)
        if len(rows) < 2:
            # First business day of a month (or nothing published yet): the previous close is in last month's file.
            year, number = int(month[:4]), int(month[4:])
            previous = f'{year - 1}12' if number == 1 else f'{year}{number - 1:02d}'
            rows = {**await self._month(previous), **rows}
        return self.build(rows)


def _level(value: str | None) -> float | None:
    """A published yield, or None for a blank / 'N/A' cell (never zero)."""
    try:
        return float(value) if value else None
    except ValueError:
        return None


class DeribitProvider(HTTPProvider):
    def __init__(self):
        super().__init__('deribit', 'https://www.deribit.com/api/v2')

    async def metrics(self, currency: str, kind: str = 'option') -> list[dict]:
        if currency not in {'BTC', 'ETH'} or kind not in {'option', 'future'}:
            raise ValueError('Unsupported derivatives request')
        response = await self.request('GET', '/public/get_book_summary_by_currency', params={'currency': currency, 'kind': kind})
        return validate_rows(DeribitSummary, response.json().get('result'), self.health)

    async def futures(self, currency: str) -> list[dict]:
        """Futures and perpetual summaries: funding, mark vs index (basis) and open interest."""
        return await self.metrics(currency, 'future')

    async def instruments(self, currency: str) -> list[dict]:
        if currency not in {'BTC', 'ETH'}:
            raise ValueError('Unsupported derivatives currency')
        response = await self.request('GET', '/public/get_instruments', params={'currency': currency, 'kind': 'option', 'expired': 'false'})
        return validate_rows(DeribitInstrument, response.json().get('result'), self.health)
