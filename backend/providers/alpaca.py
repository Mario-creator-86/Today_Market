"""Alpaca Market Data (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY; free Basic plan, paper account): IEX snapshots for
US stocks and ETF proxies, daily bars for technical levels, and Benzinga news (docs/MARKET_COVERAGE.md).

Safety rails (an Alpaca key pair can also trade, so this adapter is fenced to market data):
- the host is pinned to https://data.alpaca.markets: the constructor refuses any other base URL;
- GET only, and only the paths in ALLOWED_PATHS; any other method, path or feed raises before any network I/O;
- feed=iex always (a Paper Only account is entitled to IEX data only);
- the credentials travel only as the APCA-API-KEY-ID / APCA-API-SECRET-KEY request headers, never in a URL, a query
  parameter or provider health; the client rate guard (finnhub.RateGuard) runs before every call.

Honesty rules:
- the price is the latest IEX trade (never a quote midpoint), stamped with the trade time; an extended-hours
  (premarket or after-hours) trade is never real-time or a close: the IEX regular-session close of the latest closed
  session (that session's daily bar close, stamped at its close) is shown instead, or the trade labelled as an
  extended-hours trade when no such bar is available;
- IEX is one exchange (a few percent of US volume), not the consolidated tape: records carry feed 'iex', the label
  '<SYM> (IEX)' and delay labels from backend.sessions (real-time / STALE / LAST CLOSE ...), like Finnhub's;
- the change is vs the IEX prior-session close (the latest daily bar of an earlier New York session);
- daily bars are keyed by the New York session date (Alpaca stamps them at New York midnight), and a session that has
  not closed yet is never stored;
- news rows are Benzinga headlines (character entities such as '&amp;' decoded) plus the HTML-stripped summary only,
  with their benzinga.com URL.
Alpaca terms: personal, non-commercial use, no redistribution."""
import html
import re
import time
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from backend import sessions
from backend.config import rules
from backend.database import utcnow
from backend.providers import proxies
from backend.providers.base import HTTPProvider, ProviderUnavailable, plain_text
from backend.providers.finnhub import RateGuard, RateGuardFull, _index_symbols, trade_time
from backend.providers.models import AlpacaBar, AlpacaNewsRow, AlpacaSnapshot, parse_utc, validate_rows
from backend.schemas import Quote

HOST = 'data.alpaca.markets'
BASE_URL = f'https://{HOST}'
ALLOWED_PATHS = frozenset({'/v2/stocks/snapshots', '/v2/stocks/bars', '/v1beta1/news'})
FEED = 'iex'
DAY_SECONDS = 86400
NEW_YORK = ZoneInfo('America/New_York')
TAGS = re.compile(r'<[^>]+>')
SPACES = re.compile(r'\s+')
SEEN_NEWS_LIMIT = 5000
NEWS_RETRY_SECONDS = 6 * 3600  # after the News API refuses the account (403/404): news is a separate entitlement


def _config() -> dict:
    return rules()['provider_routing']['alpaca']


def session_date(stamp: datetime) -> date:
    """New York calendar date of a timestamp (the day boundary Alpaca uses for daily bars)."""
    return stamp.astimezone(NEW_YORK).date()


def session_start(day: date) -> datetime:
    """Daily bar start for a New York session date: 00:00 America/New_York in UTC (04:00Z in EDT, 05:00Z in EST)."""
    return datetime.combine(day, clock(0), NEW_YORK).astimezone(UTC)


def strip_html(text: str | None) -> str:
    return SPACES.sub(' ', html.unescape(TAGS.sub(' ', text or ''))).strip()


class AlpacaDataProvider(HTTPProvider):
    """Capability market_data (snapshots), daily bars and news. NOT CONFIGURED without both keys (never invented)."""

    def __init__(self, key_id: str = '', secret_key: str = '', name: str = 'alpaca', base_url: str | None = None):
        config = _config()
        url = config['base_url'] if base_url is None else base_url
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.hostname != HOST or parts.port not in (None, 443) or parts.username
                or parts.password or parts.path.strip('/') or parts.query or parts.fragment):
            raise ValueError(f'Alpaca market data must use {BASE_URL}; no other host is allowed')
        super().__init__(name, BASE_URL, '', bool(key_id and secret_key))
        self._key_id, self._secret_key = key_id, secret_key
        clash = set(self.symbols) & _index_symbols()
        if clash:
            raise ValueError(f'Alpaca symbols may not use index symbols: {sorted(clash)}')
        limit = config['rate_limit']
        self.guard = RateGuard(int(limit['calls']), float(limit['per_seconds']))
        self.news_cursor: datetime | None = None
        self.seen_news: dict[int, str] = {}  # article id -> updated_at already ingested
        self.news_retry_at = 0.0  # monotonic time before which a refused News API is not asked again
        self.health.update(rate_limit=f"{limit['calls']}/{limit['per_seconds']}s", feed=FEED)
        if key_id and not key_id.startswith('PK'):
            self.health['note'] = ('The key ID does not look like a paper-account key (PK...): market data works, '
                                   'but a paper key limits the damage if it ever leaks.')

    @property
    def symbols(self) -> list[str]:
        """Tracked equities (shared with Finnhub), the ETF proxies coverage.json assigns to Alpaca, and the extra
        daily-bar symbols. Never an index symbol."""
        config = _config()
        equities = rules()['finnhub']['equities'] if config.get('equities_from') == 'finnhub' else config.get('equities', [])
        return list(dict.fromkeys([*equities, *proxies.specs('alpaca'), *config['daily_bars']['extra_symbols']]))

    def _auth(self) -> dict[str, str]:
        return {'APCA-API-KEY-ID': self._key_id, 'APCA-API-SECRET-KEY': self._secret_key}

    async def request(self, method: str, path: str, *, not_modified_ok: bool = False, **kwargs):
        """Market-data reads only: GET, an allowlisted path on the pinned host, feed iex, credentials as headers."""
        if method.upper() != 'GET':
            raise ValueError('Alpaca: only GET requests are allowed (market data only)')
        if path not in ALLOWED_PATHS or self.base_url != BASE_URL:
            raise ValueError('Alpaca: not an allowed market-data endpoint')
        params = dict(kwargs.pop('params', None) or {})
        if params.get('feed', FEED) != FEED:
            raise ValueError('Alpaca: only the IEX feed is allowed')
        if any('key' in str(name).lower() or 'secret' in str(name).lower() for name in params):
            raise ValueError('Alpaca: credentials are sent as headers only')
        headers = {**(kwargs.pop('headers', None) or {}), **self._auth()}
        return await super().request('GET', path, params=params, headers=headers, not_modified_ok=not_modified_ok, **kwargs)

    async def _get(self, path: str, params: dict) -> Any:
        """One guarded call. Raises RateGuardFull (a ProviderUnavailable) instead of exceeding the client limit."""
        if not self.configured:
            raise ProviderUnavailable(f'{self.name}: NOT CONFIGURED')
        if time.monotonic() < self.next_attempt:
            raise ProviderUnavailable(f'{self.name}: circuit open')
        if not self.guard.try_acquire():
            raise RateGuardFull(f'{self.name}: client rate guard full ({self.health["rate_limit"]})')
        response = await self.request('GET', path, params=params, headers={'Accept': 'application/json'})
        for header in ('x-ratelimit-limit', 'x-ratelimit-remaining', 'x-ratelimit-reset'):
            if header in response.headers:
                self.health[header.replace('-', '_')] = response.headers[header][:32]
        return response.json()

    # --- quotes --------------------------------------------------------------------------------------------------

    def _delay(self, symbol: str, as_of: datetime, now: datetime) -> tuple[str, bool]:
        """(delay label, daily_close) from the last IEX trade's age and the NYSE session, as finnhub._delay does."""
        config = _config()
        labels = config['delay_labels']
        age = (now - as_of).total_seconds()
        state = sessions.market_state('equity', now, symbol)
        if age <= config['realtime_max_age_seconds']:
            return (labels['realtime'] if state == 'LIVE' else f"{labels['realtime']}, {state}"), False
        if state == 'LIVE':
            return labels['stale_open'].format(minutes=max(1, int(age // 60)), as_of=trade_time(as_of)), False
        if state == 'UNKNOWN':
            return labels['unknown_session'].format(as_of=trade_time(as_of)), False
        return labels['last_close'].format(state=state, as_of=trade_time(as_of)), True

    def _bars(self, snapshot: dict) -> list[dict]:
        """The snapshot's valid daily bars (dailyBar, prevDailyBar), each validated once."""
        return validate_rows(AlpacaBar, [b for b in (snapshot.get('dailyBar'), snapshot.get('prevDailyBar')) if b],
                             self.health)

    @staticmethod
    def _prior_bar(bars: list[dict], trade_day: date) -> dict | None:
        """The latest daily bar of a New York session before the trade's session: prevDailyBar during and after a
        session, dailyBar in the premarket (Alpaca's dailyBar is then still the previous session)."""
        earlier = [b for b in bars if session_date(parse_utc(b['t'])) < trade_day]
        return max(earlier, key=lambda b: parse_utc(b['t'])) if earlier else None

    def _session_close(self, bars: list[dict], as_of: datetime) -> tuple[date, float] | None:
        """(session date, IEX close) of the latest session that closed at or before an extended-hours trade.
        Extended-hours trades never set a daily bar's price (Alpaca trade conditions), so that bar's close is the IEX
        regular-session close."""
        closed = [(day, b['c']) for b in bars if self._close_at(day := session_date(parse_utc(b['t']))) <= as_of]
        return max(closed) if closed else None

    @staticmethod
    def _close_at(day: date) -> datetime:
        """Close of that NYSE session (early closes included), or its nominal 16:00 ET when the calendar is unknown."""
        close = sessions.session_close('equity', datetime.combine(day, clock(12), NEW_YORK).astimezone(UTC))
        return close if close is not None and session_date(close) == day else \
            datetime.combine(day, clock(16), NEW_YORK).astimezone(UTC)

    def _record(self, symbol: str, snapshot: dict, now: datetime) -> dict | None:
        """The latest IEX trade of the regular session, labelled by its age and the session (_delay). A trade outside
        the regular session (premarket or after hours) is never called real-time and never a daily close: when a daily
        bar of a session that closed at or before it is available, the record is that session's IEX regular-session
        close (the bar's close, stamped at the session close) whatever the trade's age, so an extended-hours print is
        never stored after the close it would hide; without such a bar it is the extended-hours trade itself, labelled
        as one (daily_close False)."""
        config = _config()
        labels = config['delay_labels']
        trade = snapshot['latestTrade']
        as_of, price = parse_utc(trade['t']), trade['p']
        bars = self._bars(snapshot)
        if sessions.market_state('equity', as_of, symbol) in {'LIVE', 'UNKNOWN'}:
            delay, daily_close = self._delay(symbol, as_of, now)
        elif (close := self._session_close(bars, as_of)) is not None:
            as_of, price, daily_close = self._close_at(close[0]), close[1], True
            state = sessions.market_state('equity', now, symbol)
            # In the open session this is the close of the previous one: no regular-session IEX trade yet today.
            template = labels['session_close_open'] if state == 'LIVE' else labels['session_close']
            delay = template.format(state=state, as_of=trade_time(as_of))
        else:
            delay, daily_close = labels['extended_hours'].format(as_of=trade_time(as_of)), False
        quote: dict[str, Any] = {'symbol': symbol, 'price': price, 'timestamp': as_of, 'provider': 'alpaca', 'feed': FEED,
                                 'daily_close': daily_close, 'label': f'{symbol} (IEX)', 'attribution': config['attribution'],
                                 'as_of_label': delay, 'latency_ms': self.health['latency_ms'] or 0}
        prior = self._prior_bar(bars, session_date(as_of))
        if prior is not None:
            quote.update(previous_close=prior['c'], previous_close_at=self._close_at(session_date(parse_utc(prior['t']))),
                         change_pct=round((price / prior['c'] - 1) * 100, 4), change_basis='previous_close',
                         as_of_label=f"{delay} · change {config['change_basis_label']}")
        try:
            record = Quote.model_validate(quote).model_dump(mode='json')
        except ValidationError:
            self.health['rejected_rows'] = self.health.get('rejected_rows', 0) + 1
            return None
        return dict(record, delay_label=delay, fetched_at=now.isoformat(),
                    change_basis_label=config['change_basis_label'] if prior is not None else None)

    async def snapshots(self, symbols: Iterable[str], now: datetime | None = None) -> list[dict]:
        """One /v2/stocks/snapshots call (feed iex) for the configured symbols among `symbols`: Quote-compatible records
        (plus delay_label / fetched_at) with the latest IEX trade as price and its time as timestamp, except that an
        extended-hours trade gives way to the IEX regular-session close of the snapshot's closed daily bar (_record).
        A snapshot without a valid latest trade is dropped and counted, never filled from a quote or a bar."""
        wanted = sorted(set(symbols) & set(self.symbols))
        if not wanted:
            return []
        body = await self._get('/v2/stocks/snapshots', {'symbols': ','.join(wanted), 'feed': FEED})
        if not isinstance(body, dict):
            raise ValueError('Alpaca snapshots: expected an object keyed by symbol')
        now = now or utcnow()
        result = []
        for symbol in wanted:
            if body.get(symbol) is None:
                continue
            rows = validate_rows(AlpacaSnapshot, [body[symbol]], self.health)
            record = self._record(symbol, rows[0], now) if rows else None
            if record is not None:
                result.append(record)
        return result

    async def quotes(self) -> list[dict]:
        return await self.snapshots(self.symbols)

    # --- daily bars ----------------------------------------------------------------------------------------------

    def _closed(self, day: date, now: datetime) -> bool:
        today = session_date(now)
        if day != today:
            return day < today
        close = sessions.session_close('equity', datetime.combine(day, clock(12), NEW_YORK).astimezone(UTC))
        return close is not None and session_date(close) == day and now >= close

    async def daily_bars(self, symbols: Iterable[str], start: date, now: datetime | None = None) -> list[dict]:
        """Daily bars (1Day, feed iex, split-adjusted) since `start`, following next_page_token up to max_pages: rows
        for backend.database.Bar {symbol, interval_seconds 86400, start = New York session date 00:00 in UTC, open,
        high, low, close, volume, ticks, provider}. The bar of a session that has not closed yet is left out."""
        config = _config()['daily_bars']
        wanted = sorted(set(symbols) & set(self.symbols))
        if not wanted:
            return []
        now = now or utcnow()
        params: dict[str, Any] = {'symbols': ','.join(wanted), 'timeframe': '1Day', 'start': start.isoformat(),
                                  'adjustment': config['adjustment'], 'feed': FEED, 'limit': int(config['limit']),
                                  'sort': 'asc'}
        result: list[dict] = []
        self.health['bars_truncated'] = False
        for _ in range(int(config['max_pages'])):
            body = await self._get('/v2/stocks/bars', params)
            bars = body.get('bars') if isinstance(body, dict) else None
            if not isinstance(bars, dict):
                raise ValueError('Alpaca bars: expected {"bars": {symbol: [bar]}}')
            for symbol, rows in bars.items():
                if symbol not in wanted:
                    continue
                for bar in validate_rows(AlpacaBar, rows, self.health):
                    day = session_date(parse_utc(bar['t']))
                    if self._closed(day, now):
                        result.append({'symbol': symbol, 'interval_seconds': DAY_SECONDS, 'start': session_start(day),
                                       'open': bar['o'], 'high': bar['h'], 'low': bar['l'], 'close': bar['c'],
                                       'volume': bar['v'], 'ticks': int(bar.get('n') or 0), 'provider': self.name})
            token = body.get('next_page_token')
            if not token:
                break
            params = dict(params, page_token=str(token))
        else:
            self.health['bars_truncated'] = True  # more pages than max_pages: the oldest history is incomplete
        return result

    # --- news ----------------------------------------------------------------------------------------------------

    def news_symbols(self) -> list[str]:
        crypto = [symbol for symbol in _config()['news']['crypto_symbols'] if '/' not in symbol]
        equities = rules()['finnhub']['equities'] if _config().get('equities_from') == 'finnhub' else []
        return sorted({*equities, *crypto})

    async def documents(self) -> list[dict]:
        """Benzinga articles for the tracked symbols since the last cursor (with overlap; first run: a few hours)."""
        config = _config()['news']
        if self.configured and time.monotonic() < self.news_retry_at:
            return []  # the account is not entitled to news (see health['news']); market data is unaffected
        start = (self.news_cursor - timedelta(minutes=config['overlap_minutes']) if self.news_cursor
                 else utcnow() - timedelta(hours=config['first_lookback_hours']))
        return await self.news(self.news_symbols(), start)

    async def _news_page(self, params: dict) -> Any:
        """One News API page. A 403/404 means the account (or the beta endpoint) does not serve news: that must not
        mark the market data as failing or open the shared circuit, so the previous health is restored and news is
        retried after NEWS_RETRY_SECONDS. Any other failure propagates."""
        status, error, error_type = self.health.get('status'), self.health.get('error'), self.health.get('error_type')
        next_attempt, failures = self.next_attempt, self.failures
        try:
            body = await self._get('/v1beta1/news', params)
        except ProviderUnavailable as exc:
            if exc.status_code not in {403, 404}:
                raise
            self.next_attempt, self.failures = next_attempt, failures
            self.health.update(status=status, error=error, error_type=error_type,
                               news=f'NOT AVAILABLE for this account (HTTP {exc.status_code}); retried later')
            self.news_retry_at = time.monotonic() + NEWS_RETRY_SECONDS
            return None
        self.health['news'] = 'ONLINE'
        return body

    async def news(self, symbols: Iterable[str], start: datetime) -> list[dict]:
        """DocumentInput-shaped Benzinga documents: headline and HTML-stripped summary only (include_content=false),
        the article URL (rows without one are dropped, never invented), source 'benzinga' only, assets mapped onto the
        tracked symbols (BTCUSD -> BTC, ETHUSD -> ETH). Already ingested (id, updated_at) pairs are skipped."""
        config = _config()['news']
        tracked = {symbol for symbol, _, _ in rules()['assets']}
        crypto = config['crypto_symbols']
        params: dict[str, Any] = {'symbols': ','.join(sorted(set(symbols))), 'sort': 'desc', 'limit': int(config['limit']),
                                  'include_content': 'false',
                                  'start': start.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')}
        documents: list[dict] = []
        newest = self.news_cursor
        for _ in range(int(config['max_pages'])):
            body = await self._news_page(params)
            if body is None:
                break
            if not isinstance(body, dict):
                raise ValueError('Alpaca news: expected {"news": [...]}')
            for row in validate_rows(AlpacaNewsRow, body.get('news'), self.health):
                updated = parse_utc(row['updated_at'])
                newest = updated if newest is None else max(newest, updated)
                headline = plain_text(row['headline'])  # Benzinga sends 'Storage &amp; Peripherals'
                if not row.get('url') or row['source'].casefold() != config['source_id'] or len(headline) < 5:
                    self.health['news_dropped'] = self.health.get('news_dropped', 0) + 1
                    continue
                if self.seen_news.get(row['id']) == row['updated_at']:
                    continue
                self.seen_news[row['id']] = row['updated_at']
                assets = [a for a in dict.fromkeys(crypto.get(s, s) for s in row['symbols']) if a in tracked][:30]
                documents.append({'source_id': config['source_id'], 'title': headline[:1000],
                                  'content': strip_html(row.get('summary'))[:40000], 'url': row['url'],
                                  'published_at': row['created_at'], 'event_type': 'NEWS', 'assets': assets,
                                  'direct': True, 'discovery_only': False})
            token = body.get('next_page_token')
            if not token:
                break
            params = dict(params, page_token=str(token))
        self.news_cursor = newest
        if len(self.seen_news) > SEEN_NEWS_LIMIT:
            self.seen_news = dict(list(self.seen_news.items())[-SEEN_NEWS_LIMIT // 2:])
        if documents:
            latest = max(parse_utc(d['published_at']) for d in documents)
            self.health['news_lag_seconds'] = round((utcnow() - latest).total_seconds(), 1)
        return documents
