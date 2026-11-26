"""Strict validation of every external provider payload before it is stored or used (spec §43).
Invalid rows are dropped and counted; nothing is coerced into plausible-looking values."""
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)

from backend.schemas import StrictModel


class ProviderRow(BaseModel):
    """Provider rows may carry extra vendor fields (kept), but typed fields must be valid."""
    model_config = ConfigDict(extra='allow', allow_inf_nan=False)


class XPost(ProviderRow):
    id: str = Field(pattern=r'^\d{1,25}$')
    text: str = Field(min_length=1, max_length=4000)
    created_at: AwareDatetime
    author_id: str | None = Field(default=None, pattern=r'^\d{1,25}$')
    author_username: str | None = Field(default=None, max_length=50)


class FMPCalendarRow(ProviderRow):
    date: str
    event: str = Field(min_length=1, max_length=300)
    country: str | None = Field(default=None, max_length=10)
    impact: str | None = None
    estimate: float | None = None
    previous: float | None = None
    actual: float | None = None
    unit: str | None = Field(default=None, max_length=32)

    @field_validator('date')
    @classmethod
    def parseable(cls, value: str) -> str:
        # FMP documents calendar dates as UTC without an offset; the value must still be a real timestamp.
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value


class FMPEarningsRow(ProviderRow):
    symbol: str = Field(pattern=r'^[A-Z.]{1,10}$')
    date: str
    epsEstimated: float | None = None
    epsActual: float | None = None
    revenueEstimated: float | None = None
    revenueActual: float | None = None


class DeribitSummary(ProviderRow):
    instrument_name: str = Field(pattern=r'^[A-Z]{2,5}-[0-9A-Z]+(-[0-9]+(\.[0-9]+)?-[CP])?$|^[A-Z]{2,5}-PERPETUAL$')
    open_interest: float | None = Field(default=None, ge=0)
    mark_price: float | None = Field(default=None, ge=0)
    mark_iv: float | None = Field(default=None, ge=0)
    creation_timestamp: int | None = Field(default=None, ge=0)


class DeribitInstrument(ProviderRow):
    instrument_name: str
    expiration_timestamp: int = Field(ge=0)
    strike: float | None = Field(default=None, ge=0)
    option_type: str | None = None


class TreasuryAuction(ProviderRow):
    cusip: str = Field(pattern=r'^[0-9A-Z]{9}$')
    securityType: str
    securityTerm: str
    auctionDate: str
    announcementDate: str | None = None
    offeringAmount: str | float | None = None


class LicensedArticle(ProviderRow):
    title: str = Field(min_length=5, max_length=1000)
    content: str = Field(default='', max_length=40000)
    url: HttpUrl
    published_at: AwareDatetime


class CongressBill(ProviderRow):
    congress: int = Field(ge=1)
    type: str = Field(max_length=10)
    number: str = Field(max_length=10)
    title: str = Field(max_length=2000)
    updateDate: str | None = None
    latestAction: dict | None = None


class YieldObservation(StrictModel):
    symbol: str
    value: float
    record_date: datetime


class BinanceTicker(ProviderRow):
    """GET /api/v3/ticker/24hr row. priceChangePercent is the rolling 24h change; closeTime is the end of that window
    (about the request time), NOT the last trade time."""
    symbol: str = Field(pattern=r'^[A-Z0-9]{2,20}$')
    lastPrice: float = Field(gt=0)
    priceChangePercent: float
    bidPrice: float | None = Field(default=None, ge=0)
    askPrice: float | None = Field(default=None, ge=0)


class BinanceTrade(ProviderRow):
    """GET /api/v3/trades row: the last trade gives the honest as-of time of a thinly traded pair."""
    price: float = Field(gt=0)
    time: int = Field(gt=0)


class CoinbaseTicker(ProviderRow):
    """GET /products/<id>/ticker: price and time of the last trade."""
    price: float = Field(gt=0)
    time: AwareDatetime
    bid: float | None = Field(default=None, ge=0)
    ask: float | None = Field(default=None, ge=0)


class CoinbaseStats(ProviderRow):
    """GET /products/<id>/stats: open is the price 24 hours ago (rolling window), last the latest trade."""
    open: float = Field(gt=0)
    last: float = Field(gt=0)


class AlpacaTrade(ProviderRow):
    """Alpaca stock trade (t RFC-3339 with nanoseconds, p price, s size, x exchange, z tape). A trade flagged as
    canceled or incorrect (u) is not a price."""
    t: AwareDatetime
    p: float = Field(gt=0)
    s: float | None = Field(default=None, ge=0)
    x: str | None = Field(default=None, max_length=4)
    u: str | None = None

    @field_validator('u')
    @classmethod
    def not_withdrawn(cls, value: str | None) -> str | None:
        if value in {'canceled', 'incorrect'}:
            raise ValueError('withdrawn trade')
        return value


class AlpacaBar(ProviderRow):
    """Alpaca OHLC bar. Alpaca emits a bar only when o, h, l, c and v are all non-zero; the range must be coherent."""
    t: AwareDatetime
    o: float = Field(gt=0)
    h: float = Field(gt=0)
    l: float = Field(gt=0)  # noqa: E741 - vendor field name
    c: float = Field(gt=0)
    v: float = Field(ge=0)
    n: int | None = Field(default=None, ge=0)
    vw: float | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def coherent(self):
        if not self.l <= min(self.o, self.c) <= max(self.o, self.c) <= self.h:
            raise ValueError('incoherent bar range')
        return self


class AlpacaSnapshot(ProviderRow):
    """GET /v2/stocks/snapshots entry. The price is only ever the latest trade (never a quote midpoint or a bar);
    bars are validated separately so a malformed bar never hides a valid trade."""
    latestTrade: AlpacaTrade
    dailyBar: dict | None = None
    prevDailyBar: dict | None = None


class AlpacaNewsRow(ProviderRow):
    """GET /v1beta1/news article (Benzinga). url may be null upstream; such rows are dropped by the adapter."""
    id: int = Field(gt=0)
    headline: str = Field(min_length=1, max_length=1000)
    author: str | None = Field(default=None, max_length=300)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    summary: str | None = Field(default=None, max_length=40000)
    url: HttpUrl | None = None
    symbols: list[str] = Field(default_factory=list, max_length=200)
    source: str = Field(min_length=1, max_length=64)


class ProxyQuote(StrictModel):
    """proxy_quotes payload (docs/MARKET_COVERAGE.md): another instrument shown under its own name next to an asset,
    for direction only. It is never stored as a tick of the asset it stands in for."""
    symbol: str = Field(pattern=r'^[A-Z0-9]{2,16}$')
    proxy_for: str = Field(pattern=r'^[A-Z0-9]{2,16}$')
    label: str = Field(min_length=1, max_length=120)
    caveat: str = Field(min_length=1, max_length=300)
    price: float = Field(gt=0)
    change_pct: float | None = None
    change_basis: Literal['previous_close', 'rolling_24h', 'provider'] | None = None
    previous_close: float | None = Field(default=None, gt=0)
    timestamp: AwareDatetime
    provider: Literal['binance', 'coinbase', 'alpaca', 'finnhub']
    feed: str | None = Field(default=None, max_length=32)
    delay_label: str | None = Field(default=None, max_length=200)
    attribution: str = Field(min_length=8, max_length=200)
    relation: Literal['direction_only'] = 'direction_only'


def parse_utc(value: str | int | float) -> datetime:
    """Normalise provider timestamps to aware UTC. Naive strings are rejected unless documented UTC by the caller."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp without timezone')
    return parsed.astimezone(UTC)


def validate_rows(model: type[BaseModel], rows: Any, health: dict | None = None) -> list[dict]:
    """Validate a list of vendor rows; invalid ones are dropped and counted in provider health."""
    if not isinstance(rows, list):
        raise ValueError(f'{model.__name__}: expected a list')
    valid, rejected = [], 0
    for row in rows:
        try:
            valid.append(model.model_validate(row).model_dump(mode='json'))
        except ValidationError:
            rejected += 1
    if health is not None:
        health['rejected_rows'] = health.get('rejected_rows', 0) + rejected
    return valid
