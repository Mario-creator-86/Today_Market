"""Finnhub (free key, FINNHUB_API_KEY): US equity quotes for the tracked stocks plus ETF proxies, REST and WebSocket.

Honesty rules enforced here:
- every record carries as_of (last-trade time, UTC) and a delay label derived from its age and the NYSE session state
  (backend.sessions): real-time only when the last trade is recent; an old trade during the session is STALE; outside
  the session it is labelled LAST CLOSE / FRIDAY CLOSE / AFTER HOURS / PREMARKET with daily_close=True. Nothing old
  is presented as live.
- the client rate guard is enforced inside quote() itself, so no caller can exceed the plan limit by calling it directly.
- ETF proxies (SPY/QQQ/IWM) keep their own ticker and an 'ETF (proxy)' label; they are never mapped onto index symbols
  (index levels are a premium Finnhub product).
- the documented free-plan limit (60 calls/min) is enforced client-side by a sliding-window guard.
- Finnhub terms: personal use, no redistribution; records carry 'Source: Finnhub'.

Records are backend.schemas.Quote-compatible (same fields and constraints) with provider 'finnhub' plus labelling
fields; quote_fields() strips them to the plain Quote shape.

Earnings calendar (capability calendar): /calendar/earnings per tracked equity, session only (bmo/amc/dmh, time_known
False) and EPS/revenue estimates labelled 'Finnhub estimate' (they include Finnhub's own estimates; never 'consensus')."""
import asyncio
import json
import os
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, ValidationError

from backend import sessions
from backend.config import rules
from backend.database import utcnow
from backend.providers.base import HTTPProvider, ProviderUnavailable, label_time
from backend.providers.calendars import _clock, calendar_row
from backend.providers.models import ProviderRow, validate_rows
from backend.schemas import Quote

SYMBOL = r'^[A-Z][A-Z0-9.]{0,9}$'


class FinnhubQuoteRow(ProviderRow):
    """GET /quote: c current, d change, dp change %, h/l/o day range, pc previous close, t last-trade unix seconds.
    Finnhub answers unknown symbols with zeros, so c and t must be positive."""
    c: float = Field(gt=0)
    d: float | None = None
    dp: float | None = None
    h: float | None = Field(default=None, ge=0)
    l: float | None = Field(default=None, ge=0)  # noqa: E741 - vendor field name
    o: float | None = Field(default=None, ge=0)
    pc: float | None = Field(default=None, ge=0)
    t: int = Field(gt=0)


class FinnhubTrade(ProviderRow):
    """WebSocket trade: s symbol, p price, t unix milliseconds, v volume, c trade conditions."""
    s: str = Field(pattern=SYMBOL)
    p: float = Field(gt=0)
    t: int = Field(gt=0)
    v: float | None = Field(default=None, ge=0)
    c: list[str] | None = None


class FinnhubRecord(Quote):
    """Quote-compatible record. NOTE: backend.schemas.Quote.provider must gain 'finnhub' in its Literal before the
    stripped dict (quote_fields) can be validated as a plain Quote."""
    provider: Literal['finnhub'] = 'finnhub'  # type: ignore[assignment]
    as_of: AwareDatetime
    fetched_at: AwareDatetime
    delay: str = Field(min_length=1, max_length=120)
    attribution: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=60)
    asset_class: Literal['equity', 'etf']
    proxy: bool = False
    proxy_for: str | None = None
    source: Literal['rest', 'websocket']
    previous_close: float | None = Field(default=None, gt=0)
    trade_volume: float | None = Field(default=None, ge=0)


def quote_fields(record: dict) -> dict:
    """Only the backend.schemas.Quote fields of a Finnhub record (for stores that expect the plain Quote shape)."""
    return {key: value for key, value in record.items() if key in Quote.model_fields}


class RateGuardFull(ProviderUnavailable):
    """The client-side sliding window is full: the call was NOT made (no quota used)."""


class RateGuard:
    """Sliding-window client-side limit: at most `calls` acquisitions per `per_seconds`. Never sleeps; callers skip."""

    def __init__(self, calls: int, per_seconds: float, clock: Callable[[], float] = time.monotonic):
        if calls <= 0 or per_seconds <= 0:
            raise ValueError('rate limit must be positive')
        self.calls, self.per_seconds, self.clock = calls, per_seconds, clock
        self.stamps: deque[float] = deque()

    def _trim(self) -> float:
        now = self.clock()
        while self.stamps and now - self.stamps[0] >= self.per_seconds:
            self.stamps.popleft()
        return now

    def remaining(self) -> int:
        self._trim()
        return self.calls - len(self.stamps)

    def try_acquire(self) -> bool:
        now = self._trim()
        if len(self.stamps) >= self.calls:
            return False
        self.stamps.append(now)
        return True


NEW_YORK = ZoneInfo('America/New_York')


def trade_time(as_of: datetime) -> str:
    """Last-trade time in delay labels, in the venue's zone: 'Fri 25 Sep 15:59 ET' (never a raw ISO timestamp)."""
    return label_time(as_of, NEW_YORK, 'ET')


def _config() -> dict:
    return rules()['finnhub']


class FinnhubEarningsRow(ProviderRow):
    """GET /calendar/earnings item: date YYYY-MM-DD, hour bmo/amc/dmh/'' (session only, never a clock time), fiscal
    quarter/year and Finnhub's estimates (which include Finnhub's own proprietary estimates)."""
    symbol: str = Field(pattern=SYMBOL)
    date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    hour: str | None = None
    quarter: int | None = Field(default=None, ge=1, le=4)
    year: int | None = Field(default=None, ge=1990, le=2100)
    epsEstimate: float | None = None
    revenueEstimate: float | None = None
    epsActual: float | None = None
    revenueActual: float | None = None


def earnings_rows(items: list[dict], provider: str = 'finnhub') -> list[dict]:
    """Finnhub earnings -> EARNINGS calendar rows. The session is stated in time_basis and `session` (BMO / AMC; null
    during market hours or when unknown) and the row is stamped at the session anchor (New York) with time_known False:
    the exact minute is never published. Estimates are kept apart from `consensus` (which stays empty, so no scalar
    reader calls them a consensus) in the §8 consensus shape {label: 'Finnhub estimate', values: [{name, value, unit}]}
    with consensus_source naming the provider (never Street consensus); the flat eps / revenue keys stay beside `values`
    for readers of the earlier shape, and the disclaimer is its `note`."""
    config = _config()['earnings']
    zone = ZoneInfo(config['timezone'])
    health: dict = {}
    rows = []
    for item in validate_rows(FinnhubEarningsRow, items, health):
        hour = str(item.get('hour') or '').lower()
        spec = config['sessions'][hour if hour in config['sessions'] else '']
        day = datetime.strptime(item['date'], '%Y-%m-%d').date()
        present = [v for v in config['estimate_values'] if item.get(v['field']) is not None]
        values = [{'name': v['name'], 'value': item[v['field']], 'unit': v['unit']} for v in present]
        flat = {v['key']: item[v['field']] for v in present}
        period = f"Q{item['quarter']} FY{item['year']}" if item.get('quarter') and item.get('year') else None
        rows.append(calendar_row(f"{item['symbol']} earnings", 'US', 'EARNINGS', datetime.combine(day, _clock(spec['anchor']), zone),
                                 config['impact'], provider, config['source_url'].format(symbol=item['symbol']),
                                 symbol=item['symbol'], event_class='EARNINGS', session=spec.get('session'),
                                 time_known=False, time_source='none', time_basis=spec['time_basis'],
                                 reference_period=period, attribution=_config()['attribution'],
                                 estimates=dict(flat, label=config['estimate_label'], values=values,
                                                note=config['estimate_note']) if values else None,
                                 consensus_source=provider))
    return rows


def _index_symbols() -> set[str]:
    """Symbols the app uses for index levels; ETF proxies must never take one of them."""
    config = rules()
    symbols = set(config.get('provider_routing', {}).get('massive', {}).get('indices', {}).values())
    return symbols | {'SP500', 'SPX', 'NASDAQ', 'NDX', 'RUSSELL', 'RUT', 'VIX', 'DJI', 'DOW'}


class FinnhubProvider(HTTPProvider):
    """Capabilities market_data and calendar (earnings). NOT CONFIGURED without FINNHUB_API_KEY (never invented)."""

    def __init__(self, key: str | None = None, name: str = 'finnhub'):
        config = _config()
        key = os.environ.get(config['key_env'], '') if key is None else key
        super().__init__(name, config['base_url'], key, bool(key))
        clash = set(config['etf_proxies']) & _index_symbols()
        if clash:
            raise ValueError(f'ETF proxies may not use index symbols: {sorted(clash)}')
        limit = config['rate_limit']
        self.guard = RateGuard(int(limit['calls']), float(limit['per_seconds']))
        self.offset = 0
        self.last_scope: set[str] = set()
        self.earnings_client = HTTPProvider(f'{name}_earnings', config['base_url'], key, bool(key))
        self.health.update(rate_limit=f"{limit['calls']}/{limit['per_seconds']}s", rate_guard_skipped=[])

    @property
    def symbols(self) -> list[str]:
        config = _config()
        return [*config['equities'], *config['etf_proxies']]

    @property
    def auth(self) -> dict:
        # Header auth keeps the key out of URLs (and therefore out of logs and error strings).
        return {'X-Finnhub-Token': self.key}

    def _labels(self, symbol: str) -> dict:
        proxy = _config()['etf_proxies'].get(symbol)
        if proxy:
            return {'label': proxy['label'], 'asset_class': 'etf', 'proxy': True, 'proxy_for': proxy['proxy_for']}
        return {'label': symbol, 'asset_class': 'equity', 'proxy': False, 'proxy_for': None}

    def _delay(self, symbol: str, as_of: datetime, now: datetime, live_label: str) -> tuple[str, bool]:
        """(delay label, daily_close) from the last-trade age AND the NYSE session (backend.sessions calendar).

        Only a trade younger than realtime_max_age_seconds is called real-time. An old trade while the market is open is
        'stale' (never 'LAST CLOSE': the market is not closed). Outside the regular session an old trade is the last
        close, labelled with the session state; UNKNOWN sessions are never assumed closed or open."""
        config = _config()
        labels = config['delay_labels']
        age = (now - as_of).total_seconds()
        state = sessions.market_state('equity', now, symbol)
        if age <= config['realtime_max_age_seconds']:
            return (live_label if state == 'LIVE' else f'{live_label}, {state}'), False
        if state == 'LIVE':
            return labels['stale_open'].format(minutes=max(1, int(age // 60)), as_of=trade_time(as_of)), False
        if state == 'UNKNOWN':
            return labels['unknown_session'].format(as_of=trade_time(as_of)), False
        return labels['last_close'].format(state=state, as_of=trade_time(as_of)), True

    def record(self, symbol: str, price: float, as_of: datetime, source: Literal['rest', 'websocket'], *,
               change_pct: float | None = None, previous_close: float | None = None,
               trade_volume: float | None = None, now: datetime | None = None) -> dict:
        now = now or utcnow()
        config = _config()
        live = config['delay_labels']['realtime' if source == 'rest' else 'stream']
        delay, daily_close = self._delay(symbol, as_of, now, live)
        # Finnhub's dp is the change vs its pc (previous close); without pc the basis is simply the provider's.
        basis = None if change_pct is None else 'previous_close' if previous_close else 'provider'
        return FinnhubRecord(symbol=symbol, price=price, timestamp=as_of, as_of=as_of, fetched_at=now, delay=delay,
                             daily_close=daily_close, change_pct=change_pct, previous_close=previous_close,
                             change_basis=basis, as_of_label=delay, trade_volume=trade_volume,
                             attribution=config['attribution'], source=source,
                             latency_ms=self.health['latency_ms'] or 0, **self._labels(symbol)).model_dump(mode='json')

    async def quote(self, symbol: str) -> dict | None:
        """One guarded /quote call. Raises RateGuardFull (a ProviderUnavailable) instead of exceeding the plan limit."""
        if symbol not in self.symbols:
            raise ValueError('Symbol not configured for Finnhub')
        if not self.configured:
            raise ProviderUnavailable(f'{self.name}: NOT CONFIGURED')
        if not self.guard.try_acquire():
            raise RateGuardFull(f'{self.name}: client rate guard full ({self.health["rate_limit"]})')
        return await self._fetch(symbol)

    async def _fetch(self, symbol: str) -> dict | None:
        response = await self.request('GET', '/quote', params={'symbol': symbol}, headers=self.auth)
        rows = validate_rows(FinnhubQuoteRow, [response.json()], self.health)
        if not rows:
            return None
        row = rows[0]
        return self.record(symbol, row['c'], datetime.fromtimestamp(row['t'], UTC), 'rest', change_pct=row.get('dp'),
                           previous_close=row.get('pc') or None)

    async def quotes(self, only: Iterable[str] | None = None) -> list[dict]:
        """One /quote call per configured symbol while the rate guard allows; the rest are skipped this round and
        served first next round (rotating start), so no symbol starves and the 60/min limit is never exceeded.
        `only` limits the round to symbols the caller will use (no rate budget spent on quotes that are dropped)."""
        wanted = set(only) if only is not None else None
        symbols = [s for s in self.symbols if wanted is None or s in wanted]
        if not symbols:
            return []
        self.offset = self.offset if self.offset < len(symbols) else 0
        order = symbols[self.offset:] + symbols[:self.offset]
        result: list[dict] = []
        skipped: list[str] = []
        for index, symbol in enumerate(order):
            try:
                record = await self.quote(symbol)
            except RateGuardFull:
                skipped = order[index:]
                break
            except ProviderUnavailable:
                if not result:
                    raise
                skipped = order[index:]
                break
            if record:
                result.append(record)
        self.offset = symbols.index(skipped[0]) if skipped else 0
        self.health['rate_guard_skipped'] = skipped
        return result

    # --- earnings calendar (capability: calendar) -----------------------------------------------------------------

    async def _guard_slot(self, wait_seconds: float) -> bool:
        """A slot of the shared 60/min guard, waiting up to wait_seconds (quotes and earnings share one budget)."""
        waited = 0.0
        while not self.guard.try_acquire():
            if waited >= wait_seconds:
                return False
            await asyncio.sleep(1.0)
            waited += 1.0
        return True

    async def calendar(self, start: str, end: str) -> list[dict]:
        """GET /calendar/earnings, one query per tracked equity (an all-symbol query is cut at 1500 rows and silently
        drops symbols). The calls go through their own client (own circuit and health, same key and guard), so an
        earnings failure never blocks quotes. Symbols not queried this round keep their stored rows (replace_scope)."""
        config = _config()['earnings']
        if not self.configured:
            raise ProviderUnavailable(f'{self.name}: NOT CONFIGURED')
        rows: list[dict] = []
        fetched: set[str] = set()
        failed: dict[str, str] = {}
        for symbol in _config()['equities']:
            if not await self._guard_slot(config['guard_wait_seconds']):
                break
            try:
                payload = (await self.earnings_client.request('GET', config['path'], headers=self.auth,
                                                              params={'from': start, 'to': end, 'symbol': symbol})).json()
            except (ProviderUnavailable, ValueError) as exc:
                failed[symbol] = str(exc)[:80]
                continue
            items = payload.get('earningsCalendar') if isinstance(payload, dict) else None
            fetched.add(symbol)
            rows += earnings_rows([i for i in items or [] if isinstance(i, dict) and i.get('symbol') == symbol], self.name)
        self.last_scope = fetched
        self.health['earnings'] = {'status': 'DEGRADED' if failed else self.earnings_client.health['status'],
                                   'symbols': sorted(fetched), 'failed': failed}
        if not fetched:
            raise ProviderUnavailable(f'{self.name}: no earnings query completed')
        return rows

    def replace_scope(self, row: dict) -> bool:
        """Stored rows a refresh may replace: only those of the symbols queried in the last round."""
        return row.get('symbol') in self.last_scope

    # --- WebSocket (wss://ws.finnhub.io?token=...) -------------------------------------------------------------
    def stream_url(self) -> str:
        """Contains the API key as Finnhub requires: never log or persist this URL."""
        if not self.configured:
            raise ProviderUnavailable(f'{self.name}: NOT CONFIGURED')
        return f"{_config()['websocket_url']}?token={self.key}"

    def stream_subscription(self, symbols: list[str] | None = None, action: str = 'subscribe') -> list[dict]:
        """One {'type': 'subscribe', 'symbol': ...} message per symbol (free plan: up to websocket_max_symbols)."""
        if action not in {'subscribe', 'unsubscribe'}:
            raise ValueError('action must be subscribe or unsubscribe')
        symbols = symbols or self.symbols
        if len(symbols) > _config()['websocket_max_symbols']:
            raise ValueError('Too many WebSocket symbols for the Finnhub plan')
        unknown = set(symbols) - set(self.symbols)
        if unknown:
            raise ValueError(f'Symbols not configured for Finnhub: {sorted(unknown)}')
        return [{'type': action, 'symbol': symbol} for symbol in symbols]

    def parse_stream_message(self, message: str | bytes, now: datetime | None = None) -> list[dict]:
        """'trade' messages -> one record per tracked symbol (latest trade, trade_volume summed over the message).
        'ping' and unknown types are ignored; 'error' is recorded in health; malformed input is counted and dropped."""
        try:
            payload = json.loads(message)
        except (ValueError, TypeError):
            self.health['rejected_rows'] = self.health.get('rejected_rows', 0) + 1
            return []
        if not isinstance(payload, dict):
            return []
        if payload.get('type') == 'error':
            self.health.update(status='DEGRADED', error=f"stream error: {str(payload.get('msg', ''))[:120]}")
            return []
        if payload.get('type') != 'trade':
            return []
        tracked = set(self.symbols)
        latest: dict[str, dict] = {}
        volume: dict[str, float] = {}
        for trade in validate_rows(FinnhubTrade, payload.get('data'), self.health) if isinstance(payload.get('data'), list) else []:
            if trade['s'] not in tracked:
                continue
            volume[trade['s']] = volume.get(trade['s'], 0.0) + (trade.get('v') or 0.0)
            if trade['s'] not in latest or trade['t'] >= latest[trade['s']]['t']:
                latest[trade['s']] = trade
        records = []
        for symbol, trade in latest.items():
            try:
                records.append(self.record(symbol, trade['p'], datetime.fromtimestamp(trade['t'] / 1000, UTC), 'websocket',
                                           trade_volume=volume[symbol], now=now))
            except ValidationError:
                self.health['rejected_rows'] = self.health.get('rejected_rows', 0) + 1
        return records
