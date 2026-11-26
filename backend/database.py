import builtins
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    Dialect,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    create_engine,
    delete,
    event,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from backend.config import rules


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Normalises every stored timestamp to UTC. SQLite drops offsets, so a +03:00 source time would shift."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else as_utc(value)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else as_utc(value)


def uid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class RecordMixin:
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Asset(RecordMixin, Base):
    __tablename__ = 'assets'
    symbol: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    asset_class: Mapped[str] = mapped_column(String(32))


class Document(RecordMixin, Base):
    __tablename__ = 'raw_documents'
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)


class Event(RecordMixin, Base):
    __tablename__ = 'events'
    summary_fact: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String(64))
    verification_status: Mapped[str] = mapped_column(String(32), default='UNCONFIRMED')
    fact_confidence: Mapped[float] = mapped_column(Float, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Evidence(RecordMixin, Base):
    __tablename__ = 'event_evidence'
    event_id: Mapped[str] = mapped_column(ForeignKey('events.id'), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey('raw_documents.id'), unique=True)
    relation: Mapped[str] = mapped_column(String(32), default='supports')


class Tick(Base):
    __tablename__ = 'market_ticks'
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=uid)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    price: Mapped[float] = mapped_column(Float)
    provider: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index('ix_ticks_symbol_timestamp', 'symbol', 'timestamp'),)


class Decision(RecordMixin, Base):
    __tablename__ = 'decisions'
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(32))
    score: Mapped[float] = mapped_column(Float)


# JSON payloads preserve versioned provider-specific fields without unsafe schema guessing.
# Core evidence, quotes and decisions above carry relational and time-series constraints.
RECORD_TABLES = (
    'providers provider_health sources source_reliability social_posts event_relationships '
    'market_bars macro_calendar macro_releases central_bank_state derivative_metrics '
    'technical_levels market_regimes causal_rules ai_models ai_role_assignments ai_runs '
    'asset_scores score_components decision_components alerts signal_outcomes paper_trades '
    'calibration_models configuration_versions audit_logs briefs research_jobs '
    'notification_deliveries push_subscriptions shadow_decisions backtest_runs provider_metrics recalc_triggers '
    'credit_spreads positioning_reports official_series discovery_items proxy_quotes interpretations'
).split()
RECORDS: dict[str, Any] = {
    name: type(''.join(part.title() for part in name.split('_')), (RecordMixin, Base), {'__tablename__': name})
    for name in RECORD_TABLES
}


class Bar(Base):
    """Aggregated OHLC bars (1s/5s/1m/5m/1h) so raw ticks are kept only as long as necessary."""
    __tablename__ = 'ohlc_bars'
    # The natural key includes the time column so PostgreSQL can make this a Timescale hypertable.
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, primary_key=True)
    start: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0)
    ticks: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str] = mapped_column(String(64), default='')


def aggregate(ticks: list[tuple[datetime, float, float, str]], interval_seconds: int) -> list[dict]:
    """Epoch-aligned OHLC buckets from (timestamp, price, traded quantity, provider) in any order."""
    buckets: dict[datetime, dict] = {}
    for stamp, price, quantity, provider in sorted(ticks, key=lambda t: as_utc(t[0])):
        start = datetime.fromtimestamp(int(as_utc(stamp).timestamp()) // interval_seconds * interval_seconds, UTC)
        bar = buckets.get(start)
        if bar is None:
            buckets[start] = {'start': start, 'interval_seconds': interval_seconds, 'open': price, 'high': price,
                              'low': price, 'close': price, 'volume': quantity, 'ticks': 1, 'provider': provider}
        else:
            bar.update(high=max(bar['high'], price), low=min(bar['low'], price), close=price,
                       volume=bar['volume'] + quantity, ticks=bar['ticks'] + 1)
    return list(buckets.values())


DAILY_BAR_SECONDS = 86400
# Floors under the configured horizons: daily bars feed previous day/week and SMA20/SMA50 (docs/MARKET_COVERAGE.md).
MIN_BAR_RETENTION_DAYS = {DAILY_BAR_SECONDS: 400}
DAILY_CLOSE_RETENTION_DAYS = 400


def daily_close_providers(config: dict | None = None) -> tuple[str, ...]:
    """Providers whose ticks are official daily closes, from config daily_closes.schedules (the single source of
    truth, '<provider>:<symbol>' entries included) without its live_source entries (live feeds whose schedule only
    dates their last trade). A close arrives once per business day and is the "previous stored close" of the next
    one, so these ticks are kept DAILY_CLOSE_RETENTION_DAYS."""
    schedules = (config or rules())['daily_closes']['schedules']
    return tuple(sorted({key.split(':', 1)[0] for key, spec in schedules.items() if not spec.get('live_source')}))


def retention_cutoffs(now: datetime, tick_hours: float, bar_days: dict[str, float]) -> tuple[datetime, dict[int, datetime]]:
    return now - timedelta(hours=tick_hours), {int(k): now - timedelta(days=max(v, MIN_BAR_RETENTION_DAYS.get(int(k), 0)))
                                               for k, v in bar_days.items()}


def _sqlite_pragmas(connection: Any, _record: Any) -> None:
    """WAL lets the API read while ingestion writes (the default rollback journal blocks readers during writes);
    NORMAL sync is safe with WAL and avoids an fsync per commit."""
    cursor = connection.cursor()
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.execute('PRAGMA synchronous=NORMAL')
    cursor.execute('PRAGMA foreign_keys=ON')  # enforce event_evidence links as PostgreSQL does
    cursor.close()


class Store:
    def __init__(self, url: str):
        args = {'check_same_thread': False, 'timeout': 30} if url.startswith('sqlite') else {}
        self.engine = create_engine(url, connect_args=args, pool_pre_ping=True)
        if url.startswith('sqlite') and ':memory:' not in url:
            event.listen(self.engine, 'connect', _sqlite_pragmas)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.sessions()

    def put(self, table: str, key: str, payload: dict) -> None:
        with self.session() as session:
            cls = RECORDS[table]
            row: Any = session.get(cls, key)
            if row is None:
                session.add(cls(id=key, payload=payload))
            else:
                row.payload = payload
            session.commit()

    def put_many(self, table: str, items: dict[str, dict]) -> None:
        """Upsert a batch in one transaction; per-row commits block the event loop for seconds."""
        if not items:
            return
        with self.session() as session:
            cls = RECORDS[table]
            rows: list[Any] = list(session.scalars(select(cls).where(cls.id.in_(list(items)))))
            existing = {row.id: row for row in rows}
            for key, payload in items.items():
                if key in existing:
                    existing[key].payload = payload
                else:
                    session.add(cls(id=key, payload=payload))
            session.commit()

    def get(self, table: str, key: str) -> dict | None:
        with self.session() as session:
            row: Any = session.get(RECORDS[table], key)
            return row.payload if row else None

    def list(self, table: str, limit: int = 500) -> list[dict]:
        with self.session() as session:
            cls = RECORDS[table]
            rows: list[Any] = list(session.scalars(select(cls).order_by(cls.created_at.desc()).limit(limit)))
            return [dict(row.payload, id=row.id) for row in rows]

    def bars(self, symbol: str, interval_seconds: int, since: datetime, until: datetime | None = None,
             limit: int = 5000) -> builtins.list[dict]:
        with self.session() as session:
            query = select(Bar).where(Bar.symbol == symbol, Bar.interval_seconds == interval_seconds, Bar.start >= since)
            if until is not None:
                query = query.where(Bar.start <= until)
            rows = list(session.scalars(query.order_by(Bar.start.desc()).limit(limit)))[::-1]
            return [{'symbol': r.symbol, 'interval_seconds': r.interval_seconds, 'start': r.start.isoformat(),
                     'time': r.start.isoformat(), 'open': r.open, 'high': r.high, 'low': r.low, 'close': r.close,
                     'volume': r.volume, 'ticks': r.ticks, 'provider': r.provider} for r in rows]

    def aggregate_bars(self, symbol: str, intervals: Sequence[int], since: datetime, until: datetime) -> int:
        """Rebuilds bars over [since, until] from ticks ingested by `until`; stub ticks never become bars.
        Callers pass `since` aligned to the largest interval so no bucket is rebuilt from a partial window."""
        with self.session() as session:
            quantity, stub = Tick.payload['trade_quantity'].as_float(), Tick.payload['stub'].as_boolean()
            rows = session.execute(select(Tick.timestamp, Tick.price, quantity, Tick.provider, stub).where(
                Tick.symbol == symbol, Tick.timestamp >= since, Tick.timestamp <= until, Tick.ingested_at <= until))
            ticks = [(stamp, price, float(traded or 0), provider) for stamp, price, traded, provider, is_stub in rows
                     if not provider.endswith('-stub') and not is_stub]
            written = 0
            for interval in intervals:
                built = aggregate(ticks, interval)
                existing = {as_utc(b.start): b for b in session.scalars(select(Bar).where(
                    Bar.symbol == symbol, Bar.interval_seconds == interval,
                    Bar.start.in_([b['start'] for b in built])))} if built else {}
                for item in built:
                    row = existing.get(item['start'])
                    if row is None:
                        session.add(Bar(symbol=symbol, **item))
                    else:
                        for key in ('open', 'high', 'low', 'close', 'volume', 'ticks', 'provider'):
                            setattr(row, key, item[key])
                    written += 1
            session.commit()
            return written

    def prune(self, tick_before: datetime, bar_before: dict[int, datetime]) -> dict[str, int]:
        """Retention: raw ticks older than the cutoff, and bars past their per-interval horizon. Official daily-close
        ticks and daily bars are kept at least DAILY_CLOSE_RETENTION_DAYS / MIN_BAR_RETENTION_DAYS before the cutoff."""
        daily_before = tick_before - timedelta(days=DAILY_CLOSE_RETENTION_DAYS)
        providers = daily_close_providers()
        with self.session() as session:
            ticks = session.execute(delete(Tick).where(Tick.timestamp < tick_before, Tick.provider.not_in(providers)))
            closes = session.execute(delete(Tick).where(Tick.timestamp < daily_before, Tick.provider.in_(providers)))
            removed: dict[str, int] = {'ticks': (getattr(ticks, 'rowcount', 0) or 0) + (getattr(closes, 'rowcount', 0) or 0)}
            for interval, cutoff in bar_before.items():
                if interval in MIN_BAR_RETENTION_DAYS:
                    cutoff = min(cutoff, tick_before - timedelta(days=MIN_BAR_RETENTION_DAYS[interval]))
                result = session.execute(delete(Bar).where(Bar.interval_seconds == interval, Bar.start < cutoff))
                removed[f'bars_{interval}s'] = getattr(result, 'rowcount', 0) or 0
            session.commit()
            return removed
