"""Proxy instruments (docs/MARKET_COVERAGE.md, config coverage.proxies): an ETF or token shown under its own name next to
an asset that has no direct live source (PAXG for gold, SPY for the S&P 500, ...). Proxies are for direction only:
their payload goes to the proxy_quotes table, keyed '<source>:<symbol>', and never becomes a tick of the underlying."""
from datetime import datetime
from typing import Any

from backend.config import rules
from backend.providers.models import ProxyQuote

ATTRIBUTION = {'binance': 'Source: Binance', 'coinbase': 'Source: Coinbase Exchange',
               'alpaca': 'Source: Alpaca Market Data (IEX exchange only)', 'finnhub': 'Source: Finnhub'}


def specs(source: str, config: dict | None = None) -> dict[str, tuple[str, dict]]:
    """{proxy symbol: (asset it stands in for, coverage spec)} for one source. When a symbol serves several assets
    (never the case today), the first asset in configuration order wins."""
    proxies = (config or rules())['coverage']['proxies']
    result: dict[str, tuple[str, dict]] = {}
    for asset, entries in proxies.items():
        for spec in entries:
            if spec['source'] == source and spec['symbol'] not in result:
                result[spec['symbol']] = (asset, spec)
    return result


def key(source: str, symbol: str) -> str:
    return f'{source}:{symbol}'


def payload(source: str, row: dict[str, Any], config: dict | None = None) -> tuple[str, dict]:
    """(proxy_quotes key, validated payload) for a provider row {symbol, price, timestamp, change_pct, ...}.
    Raises KeyError for a symbol the source does not serve as a proxy and ValueError for an invalid row."""
    asset, spec = specs(source, config)[row['symbol']]
    record = ProxyQuote(symbol=row['symbol'], proxy_for=asset, label=spec['label'], caveat=spec['caveat'],
                        price=row['price'], change_pct=row.get('change_pct'), change_basis=row.get('change_basis'),
                        previous_close=row.get('previous_close'), timestamp=row['timestamp'], provider=source,
                        feed=row.get('feed'), delay_label=row.get('delay_label') or row.get('as_of_label'),
                        attribution=row.get('attribution') or ATTRIBUTION[source])
    return key(source, record.symbol), record.model_dump(mode='json')


def newer(existing: dict | None, stamp: datetime) -> bool:
    """A stored proxy is only replaced by a later observation (REST and stream answers can arrive out of order)."""
    if not existing or not existing.get('timestamp'):
        return True
    return datetime.fromisoformat(str(existing['timestamp']).replace('Z', '+00:00')) < stamp
