"""Descriptive positioning sources: CFTC Commitments of Traders (weekly), Binance USDⓈ-M / COIN-M futures public
statistics (real-time, hourly buckets) and Coinbase International (INTX) perpetuals as a slow cross-check.

Every record carries `as_of` (aware UTC ISO), a `delay` label, the source `attribution` and `label='descriptive'`.
Nothing here is a direction or a price target: `directional_score` is always 0 (same convention as
backend.positioning). Weekly CFTC data is never marked live; venue perp data is labelled as venue perp, not spot.
"""
import copy
import re
import time
from collections import deque
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, create_model, field_validator

from backend.config import rules
from backend.database import utcnow
from backend.providers.base import HTTPProvider, ProviderUnavailable, label_day
from backend.providers.models import ProviderRow, parse_utc, validate_rows

RESTRICTED = 'unavailable - restricted location'
CONTRACT_CODE = re.compile(r'^[0-9A-Z]{6}$')


@runtime_checkable
class PositioningProvider(Protocol):
    """Capability for the registry: descriptive positioning records (never directional scores)."""
    async def positioning(self) -> list[dict]: ...


def config() -> dict:
    return rules()['positioning_sources']


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class RateGuard:
    """Sliding-window request budget (cost units per window) that can also be paused until a given time."""

    def __init__(self, limit: float, window_seconds: float):
        self.limit, self.window = limit, window_seconds
        self.spent: deque[tuple[float, float]] = deque()
        self.blocked_until = 0.0

    def _trim(self, now: float) -> None:
        while self.spent and now - self.spent[0][0] >= self.window:
            self.spent.popleft()

    def used(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        self._trim(now)
        return sum(cost for _, cost in self.spent)

    def try_spend(self, cost: float = 1, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if now < self.blocked_until or self.used(now) + cost > self.limit:
            return False
        self.spent.append((now, cost))
        return True

    def block(self, seconds: float, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.blocked_until = max(self.blocked_until, now + seconds)


class _Throttled:
    """Cache + minimum refresh interval shared by the adapters: calling faster returns the cached records."""
    _cache: list[dict]
    _refreshed: float | None

    def _init_cache(self) -> None:
        self._cache, self._refreshed = [], None

    def _fresh_cache(self, min_interval: float) -> list[dict] | None:
        if self._refreshed is not None and time.monotonic() - self._refreshed < min_interval:
            return copy.deepcopy(self._cache)  # records hold nested dicts: callers must not mutate the cache
        return None

    def _store(self, records: list[dict]) -> list[dict]:
        self._cache, self._refreshed = records, time.monotonic()
        return copy.deepcopy(records)


def _json(response: Any, name: str) -> Any:
    """Decode a JSON body; a non-JSON body is a provider failure, not a crash."""
    try:
        return response.json()
    except ValueError:
        raise ProviderUnavailable(f'{name}: invalid JSON body') from None


# --- CFTC Commitments of Traders ------------------------------------------------------------------------------------

class CotRow(ProviderRow):
    id: str = Field(min_length=1, max_length=40)
    report_date_as_yyyy_mm_dd: str
    cftc_contract_market_code: str = Field(pattern=r'^[0-9A-Z]{6}$')
    contract_market_name: str = Field(min_length=1, max_length=200)
    market_and_exchange_names: str | None = Field(default=None, max_length=300)
    contract_units: str | None = Field(default=None, max_length=200)
    open_interest_all: int = Field(ge=0)
    change_in_open_interest_all: int | None = None

    @field_validator('report_date_as_yyyy_mm_dd')
    @classmethod
    def calendar_date(cls, value: str) -> str:
        # Socrata floating timestamp (no zone) that only carries a calendar date: keep the date, reject junk.
        date.fromisoformat(value[:10])
        return value[:10]


@lru_cache(maxsize=8)
def _cot_model(report: str, fields: tuple[tuple[str, bool], ...]) -> type[BaseModel]:
    """Strict per-report model: positions are non-negative integers, weekly changes are integers."""
    definitions: dict[str, Any] = {name: (int, Field(ge=0) if position else Field()) for name, position in fields}
    return create_model(f'Cot{report.title()}Row', __base__=CotRow, **definitions)


def cot_model(report: str) -> type[BaseModel]:
    groups = config()['cftc_cot']['reports'][report]['groups']
    fields = {(field, key in {'long', 'short', 'spread'}) for spec in groups.values() for key, field in spec.items()}
    return _cot_model(report, tuple(sorted(fields)))


def _cot_times(day: date) -> tuple[datetime, datetime]:
    """Positions as of Tuesday close (configured time/zone) and the scheduled Friday 15:30 ET release."""
    cfg = config()['cftc_cot']
    zone = ZoneInfo(cfg['positions_timezone'])
    hour, minute = (int(v) for v in cfg['positions_time'].split(':'))
    as_of = parse_utc(datetime.combine(day, dtime(hour, minute), zone).isoformat())
    release_day = day + timedelta(days=(cfg['release_weekday'] - day.weekday()) % 7)
    hour, minute = (int(v) for v in cfg['release_time'].split(':'))
    release = parse_utc(datetime.combine(release_day, dtime(hour, minute), zone).isoformat())
    return as_of, release


def percentile_rank(value: float, history: list[float]) -> float | None:
    """Share (0..100) of observations at or below `value`; None without history."""
    if not history:
        return None
    return round(100 * sum(1 for h in history if h <= value) / len(history), 1)


def build_cot_records(report: str, rows: list[dict], now: datetime | None = None) -> list[dict]:
    """Validated rows of one report (any order, several contracts, up to 3 years) -> one record per contract."""
    cfg = config()['cftc_cot']
    spec = cfg['reports'][report]
    now = now or utcnow()
    by_code: dict[str, list[dict]] = {}
    for row in rows:
        by_code.setdefault(row['cftc_contract_market_code'], []).append(row)
    records = []
    for code, series in sorted(by_code.items()):
        contract = cfg['contracts'].get(code)
        if contract is None or report not in contract['reports']:
            continue
        series.sort(key=lambda r: r['report_date_as_yyyy_mm_dd'], reverse=True)
        latest = series[0]
        latest_day = date.fromisoformat(latest['report_date_as_yyyy_mm_dd'])
        start = latest_day - timedelta(weeks=cfg['history_weeks'])
        window = [r for r in series if date.fromisoformat(r['report_date_as_yyyy_mm_dd']) > start]
        enough = len(window) >= cfg['percentile_min_weeks']
        oi = latest['open_interest_all']
        groups = {}
        for name, fields in spec['groups'].items():
            long_, short = latest[fields['long']], latest[fields['short']]
            net = long_ - short
            nets = [r[fields['long']] - r[fields['short']] for r in window]
            group = {'long': long_, 'short': short, 'net': net,
                     'net_change_wow': latest[fields['change_long']] - latest[fields['change_short']],
                     'net_pct_oi': round(100 * net / oi, 2) if oi else None,
                     'net_percentile_3y': percentile_rank(net, nets) if enough else None,
                     'percentile_weeks': len(window)}
            if 'spread' in fields:
                group['spread'] = latest[fields['spread']]
            groups[name] = group
        as_of, release = _cot_times(latest_day)
        relation = contract['relation']
        records.append({
            'provider': 'cftc_cot', 'kind': 'cot_positioning', 'report': report, 'report_title': spec['title'],
            'primary': contract['primary'] == report, 'symbol': contract['symbol'], 'contract_code': code,
            'contract_name': latest['contract_market_name'], 'market': latest.get('market_and_exchange_names'),
            'units': latest.get('contract_units'), 'as_of': _iso(as_of), 'as_of_date': latest_day.isoformat(),
            'scheduled_release_at': _iso(release), 'retrieved_at': _iso(now), 'frequency': 'weekly', 'live': False,
            'delay': f'weekly, as of {label_day(latest_day)}',
            'stale': now - as_of > timedelta(days=cfg['stale_after_days']),
            'open_interest': oi, 'open_interest_change_wow': latest.get('change_in_open_interest_all'),
            'groups': groups, 'history_weeks': len(window),
            'history_start': window[-1]['report_date_as_yyyy_mm_dd'],
            'percentile_basis': (f'net contracts vs {len(window)} weekly reports' if enough else
                                 f'insufficient history ({len(window)} < {cfg["percentile_min_weeks"]} weeks)'),
            'relation': relation, 'relation_note': cfg['relation_notes'][relation],
            'attribution': cfg['attribution'], 'label': 'descriptive', 'directional_score': 0,
            'note': config()['descriptive_note']})
    return records


class CFTCCOTProvider(HTTPProvider, _Throttled):
    """CFTC COT (Legacy, Disaggregated, TFF futures-only) via the public Socrata API. Weekly, public domain."""

    def __init__(self, timeout: float = 30):
        cfg = config()['cftc_cot']
        super().__init__('cftc_cot', cfg['base_url'], timeout=timeout)
        self.guard = RateGuard(cfg['max_requests_per_hour'], 3600)
        self._latest_day: date | None = None
        self._init_cache()

    def next_release(self, latest_day: date) -> datetime:
        """Scheduled release of the report after `latest_day` (next Tuesday's positions, published that Friday)."""
        return _cot_times(latest_day + timedelta(weeks=1))[1]

    def _cache_valid(self, now: datetime) -> bool:
        """Weekly data: after a successful refresh, keep the cache until the next scheduled Friday release
        (then poll every min_interval until the new report appears), with a daily safety refresh."""
        cfg = config()['cftc_cot']
        if self._refreshed is None:
            return False
        age = time.monotonic() - self._refreshed
        if age < cfg['min_interval_seconds']:
            return True
        return (self._latest_day is not None and now < self.next_release(self._latest_day)
                and age < cfg['max_cache_seconds'])

    def _where(self, report: str, since: date) -> str:
        codes = sorted(code for code, c in config()['cftc_cot']['contracts'].items() if report in c['reports'])
        if not codes or not all(CONTRACT_CODE.match(code) for code in codes):
            raise ValueError('Invalid CFTC contract codes in configuration')
        listed = ','.join(f"'{code}'" for code in codes)
        return f"cftc_contract_market_code in ({listed}) AND report_date_as_yyyy_mm_dd >= '{since.isoformat()}T00:00:00'"

    async def history(self, report: str, now: datetime | None = None) -> list[dict]:
        """Validated weekly rows for all configured contracts of one report, covering the percentile window."""
        cfg = config()['cftc_cot']
        spec = cfg['reports'][report]
        model = cot_model(report)
        since = (now or utcnow()).date() - timedelta(weeks=cfg['history_weeks'] + 2)
        select = sorted(set(model.model_fields))
        rows: list[dict] = []
        limit = cfg['page_limit']
        for page in range(cfg['max_pages']):
            if not self.guard.try_spend(1):
                raise ProviderUnavailable(f'{self.name}: local request budget exhausted')
            response = await self.request('GET', f"/resource/{spec['dataset']}.json", params={
                '$select': ','.join(select), '$where': self._where(report, since),
                '$order': 'report_date_as_yyyy_mm_dd DESC,cftc_contract_market_code', '$limit': limit,
                '$offset': page * limit})
            batch = _json(response, self.name)
            if not isinstance(batch, list):
                raise ProviderUnavailable(f'{self.name}: unexpected {report} payload (not a list)')
            rows.extend(validate_rows(model, batch, self.health))
            if len(batch) < limit:
                break
        return rows

    async def positioning(self, now: datetime | None = None) -> list[dict]:
        cfg = config()['cftc_cot']
        now = now or utcnow()
        if self._cache_valid(now):
            cached = copy.deepcopy(self._cache)
            for record in cached:  # the stale flag depends on the reading time, not on the fetch time
                record['stale'] = now - parse_utc(record['as_of']) > timedelta(days=cfg['stale_after_days'])
            return cached
        records = []
        for report in cfg['reports']:
            records.extend(build_cot_records(report, await self.history(report, now), now))
        days = [date.fromisoformat(r['as_of_date']) for r in records]
        self._latest_day = max(days) if days else None
        return self._store(records)


def primary_cot(records: list[dict]) -> dict[str, dict]:
    """Primary COT record per tracked symbol (Disaggregated for commodities, TFF for financials)."""
    return {r['symbol']: r for r in records if r.get('provider') == 'cftc_cot' and r.get('primary')}


# --- Binance USDⓈ-M / COIN-M futures -------------------------------------------------------------------------------

class BinancePremiumIndex(ProviderRow):
    symbol: str = Field(pattern=r'^[A-Z0-9_]{3,30}$')
    markPrice: float = Field(gt=0)
    indexPrice: float = Field(gt=0)
    lastFundingRate: float = Field(gt=-1, lt=1)
    nextFundingTime: int = Field(ge=0)
    time: int = Field(gt=0)


class BinanceOpenInterest(ProviderRow):
    symbol: str = Field(pattern=r'^[A-Z0-9_]{3,30}$')
    openInterest: float = Field(ge=0)
    time: int = Field(gt=0)


class BinanceOpenInterestHist(ProviderRow):
    sumOpenInterest: float = Field(ge=0)
    sumOpenInterestValue: float = Field(ge=0)
    timestamp: int = Field(gt=0)


class BinanceLongShortRatio(ProviderRow):
    longAccount: float = Field(ge=0, le=1)
    shortAccount: float = Field(ge=0, le=1)
    longShortRatio: float = Field(ge=0)
    timestamp: int = Field(gt=0)


class RestrictedLocation(ProviderUnavailable):
    pass


PERIOD_SECONDS = {'5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600,
                  '12h': 43200, '1d': 86400}


class BinanceFuturesProvider(HTTPProvider, _Throttled):
    """Binance futures public data. HTTP 451 is surfaced as 'unavailable - restricted location' (never silent);
    REQUEST_WEIGHT and /futures/data budgets are enforced locally and against x-mbx-used-weight-1m."""

    def __init__(self, timeout: float = 15):
        # Two hosts (fapi/dapi): base_url stays empty and each call passes the full configured URL.
        super().__init__('binance_futures', '', timeout=timeout)
        cfg = config()['binance_futures']
        self.weight = RateGuard(cfg['request_weight_limit_per_minute'] * cfg['request_weight_budget_fraction'], 60)
        self.stats = RateGuard(cfg['stats_limit_per_5min'] * cfg['stats_budget_fraction'], 300)
        self.restricted = False
        self._stats_cache: dict[tuple[str, str], tuple[int, dict]] = {}
        self._init_cache()

    def _base(self, market: str) -> str:
        cfg = config()['binance_futures']
        return cfg['usdm_base_url'] if market == 'usdm' else cfg['coinm_base_url']

    async def _get(self, market: str, path: str, params: dict, weight: float, stats: bool = False) -> Any:
        cfg = config()['binance_futures']
        guard = self.stats if stats else self.weight
        if self.restricted and time.monotonic() < self.next_attempt:
            raise RestrictedLocation(f'{self.name}: {RESTRICTED}', 451)
        if not guard.try_spend(weight):
            raise ProviderUnavailable(f'{self.name}: local request budget exhausted')
        try:
            response = await self.request('GET', self._base(market) + path, params=params)
        except ProviderUnavailable as exc:
            if exc.status_code == 451:
                self.restricted = True
                self.next_attempt = time.monotonic() + cfg['restricted_backoff_seconds']
                self.health.update(status='DEGRADED', error=f'{RESTRICTED} (HTTP 451)', restricted=True)
                raise RestrictedLocation(f'{self.name}: {RESTRICTED}', 451) from None
            if exc.status_code == 418:  # IP auto-banned after ignoring 429s: back off hard.
                self.next_attempt = time.monotonic() + cfg['banned_backoff_seconds']
                self.health.update(status='RATE_LIMITED')
            if exc.status_code in {418, 429}:
                self.weight.block(60)
            raise
        self.restricted = False
        self.health.pop('restricted', None)
        used = response.headers.get('x-mbx-used-weight-1m')
        if used and used.isdigit():
            self.health['used_weight_1m'] = int(used)
            if int(used) >= self.weight.limit:  # Other clients on this IP already used our share: wait for the next minute.
                self.weight.block(60 - time.time() % 60)
        return _json(response, self.name)

    async def _stats(self, inst: dict, now_ms: int) -> dict:
        """Hourly-bucket statistics, fetched only when a newer bucket can exist (respects the 5-min budget)."""
        cfg = config()['binance_futures']
        if inst['market'] not in cfg['stats_markets']:
            return {'open_interest_hist': None, 'long_short_accounts': None, 'stats_note': cfg['stats_disabled_note']}
        period = cfg['stats_period']
        key = (inst['market'], inst['instrument'])
        cached = self._stats_cache.get(key)
        if cached and now_ms < cached[0] + (PERIOD_SECONDS[period] + 60) * 1000:
            return cached[1]
        coinm = inst['market'] == 'coinm'
        prefix = '/futures/data'
        base = {'period': period, 'limit': cfg['stats_limit']}
        oi_params = dict(base, pair=inst['pair'], contractType='PERPETUAL') if coinm else dict(base, symbol=inst['instrument'])
        ratio_params = dict(base, pair=inst['pair']) if coinm else dict(base, symbol=inst['instrument'])
        hist_raw = await self._get(inst['market'], f'{prefix}/openInterestHist', oi_params, 1, True)
        ratio_raw = await self._get(inst['market'], f'{prefix}/globalLongShortAccountRatio', ratio_params, 1, True)
        if not isinstance(hist_raw, list) or not isinstance(ratio_raw, list):
            raise ProviderUnavailable(f'{self.name}: unexpected statistics payload (not a list)')
        hist = validate_rows(BinanceOpenInterestHist, hist_raw, self.health)
        ratio = validate_rows(BinanceLongShortRatio, ratio_raw, self.health)
        result: dict[str, Any] = {'open_interest_hist': self.hist_summary(hist, inst),
                                  'long_short_accounts': self.ratio_summary(ratio)}
        frozen = [name for name, rows, field in (('open interest', hist, 'sumOpenInterest'),
                                                  ('long/short', ratio, 'longShortRatio')) if self.frozen(rows, field)]
        if frozen:
            # Observed live on COIN-M: identical values bucket after bucket. A frozen series is not an hourly reading.
            result = {'open_interest_hist': None, 'long_short_accounts': None,
                      'stats_note': f'hourly statistics withheld: {" and ".join(frozen)} series unchanged across '
                                    f'{cfg["stats_limit"]} buckets (vendor series looks frozen)'}
        latest = max([r['timestamp'] for r in hist + ratio], default=0)
        if latest:
            self._stats_cache[key] = (latest, result)
        return result

    @staticmethod
    def frozen(rows: list[dict], field: str) -> bool:
        return len(rows) >= 2 and len({r[field] for r in rows}) == 1

    @staticmethod
    def hist_summary(rows: list[dict], inst: dict) -> dict | None:
        if not rows:
            return None
        rows = sorted(rows, key=lambda r: r['timestamp'])
        last = rows[-1]
        prev = rows[-2] if len(rows) > 1 else None
        change = (round(100 * (last['sumOpenInterest'] / prev['sumOpenInterest'] - 1), 3)
                  if prev and prev['sumOpenInterest'] else None)
        summary = {'as_of': _iso(parse_utc(last['timestamp'])), 'delay': 'hourly bucket',
                   'sum_open_interest': last['sumOpenInterest'], 'units': inst['oi_units'],
                   'sum_open_interest_value': last['sumOpenInterestValue'],
                   'value_units': inst['symbol'] if inst['market'] == 'coinm' else 'USD',
                   'change_pct_vs_prev_bucket': change}
        if inst['market'] == 'coinm':
            # Observed live: the COIN-M statistics sum is about 2x the live openInterest; compare within the series only.
            summary['note'] = 'COIN-M statistics series; not directly comparable with the live open_interest field'
        return summary

    @staticmethod
    def ratio_summary(rows: list[dict]) -> dict | None:
        if not rows:
            return None
        last = max(rows, key=lambda r: r['timestamp'])
        return {'as_of': _iso(parse_utc(last['timestamp'])), 'delay': 'hourly bucket',
                'long_account': last['longAccount'], 'short_account': last['shortAccount'],
                'long_short_ratio': last['longShortRatio'],
                'basis': 'share of Binance accounts net long vs net short (all accounts, not size-weighted)'}

    async def instrument(self, inst: dict, now: datetime | None = None) -> dict:
        cfg = config()['binance_futures']
        now = now or utcnow()
        market, symbol = inst['market'], inst['instrument']
        prefix = '/fapi/v1' if market == 'usdm' else '/dapi/v1'
        raw = await self._get(market, f'{prefix}/premiumIndex', {'symbol': symbol}, cfg['endpoint_weights']['premiumIndex'])
        rows = raw if isinstance(raw, list) else [raw]
        premium = [r for r in validate_rows(BinancePremiumIndex, rows, self.health) if r['symbol'] == symbol]
        oi_raw = await self._get(market, f'{prefix}/openInterest', {'symbol': symbol}, cfg['endpoint_weights']['openInterest'])
        oi = [r for r in validate_rows(BinanceOpenInterest, [oi_raw], self.health) if r['symbol'] == symbol]
        if not premium:
            raise ProviderUnavailable(f'{self.name}: invalid premiumIndex for {symbol}')
        p = premium[0]
        try:
            stats = await self._stats(inst, int(now.timestamp() * 1000))
        except RestrictedLocation:
            raise
        except ProviderUnavailable as exc:  # Hourly statistics are optional; the real-time fields still stand.
            stats = {'open_interest_hist': None, 'long_short_accounts': None,
                     'stats_error': str(exc).removeprefix(f'{self.name}: ')}
        return {
            'provider': self.name, 'kind': 'perp_positioning', 'status': 'ok',
            'venue': 'Binance USDⓈ-M' if market == 'usdm' else 'Binance COIN-M', 'market': market,
            'symbol': inst['symbol'], 'instrument': symbol, 'as_of': _iso(parse_utc(p['time'])),
            'retrieved_at': _iso(now), 'delay': 'real-time venue perpetual (not spot)', 'live': True,
            'mark_price': p['markPrice'], 'index_price': p['indexPrice'],
            'basis_bps': round((p['markPrice'] / p['indexPrice'] - 1) * 1e4, 2),
            'funding_rate': p['lastFundingRate'], 'funding_rate_bps': round(p['lastFundingRate'] * 1e4, 3),
            'funding_rate_basis': ("premiumIndex lastFundingRate (Binance 'latest funding rate'; moves within the "
                                   'period until it settles at next_funding_time)'),
            'next_funding_time': _iso(parse_utc(p['nextFundingTime'])) if p['nextFundingTime'] else None,
            'open_interest': oi[0]['openInterest'] if oi else None, 'open_interest_units': inst['oi_units'],
            'open_interest_as_of': _iso(parse_utc(oi[0]['time'])) if oi else None,
            **stats, 'attribution': cfg['attribution'], 'label': 'descriptive', 'directional_score': 0,
            'note': config()['descriptive_note']}

    def unavailable(self, inst: dict, reason: str, now: datetime) -> dict:
        return {'provider': self.name, 'kind': 'perp_positioning', 'status': 'unavailable', 'reason': reason,
                'venue': 'Binance USDⓈ-M' if inst['market'] == 'usdm' else 'Binance COIN-M', 'market': inst['market'],
                'symbol': inst['symbol'], 'instrument': inst['instrument'], 'as_of': None, 'retrieved_at': _iso(now),
                'delay': 'unavailable (no data)', 'live': False,
                'attribution': config()['binance_futures']['attribution'], 'label': 'descriptive', 'directional_score': 0}

    async def positioning(self, now: datetime | None = None) -> list[dict]:
        cfg = config()['binance_futures']
        cached = self._fresh_cache(cfg['min_interval_seconds'])
        if cached is not None:
            return cached
        now = now or utcnow()
        records, errors = [], []
        for index, inst in enumerate(cfg['instruments']):
            try:
                records.append(await self.instrument(inst, now))
            except RestrictedLocation:
                # Geo-block applies to every instrument: report all as unavailable instead of failing silently.
                records.extend(self.unavailable(i, RESTRICTED, now) for i in cfg['instruments'][index:])
                return self._store(records)
            except ProviderUnavailable as exc:
                errors.append(exc)
                records.append(self.unavailable(inst, str(exc).removeprefix(f'{self.name}: '), now))
        if errors and len(errors) == len(records):
            raise errors[-1]
        return self._store(records)


# --- Coinbase International Exchange (INTX) --------------------------------------------------------------------------

class IntxQuote(ProviderRow):
    mark_price: float = Field(gt=0)
    index_price: float = Field(gt=0)
    predicted_funding: float | None = Field(default=None, gt=-1, lt=1)
    timestamp: str

    @field_validator('timestamp')
    @classmethod
    def aware(cls, value: str) -> str:
        parse_utc(value)
        return value


class IntxInstrument(ProviderRow):
    symbol: str = Field(pattern=r'^[A-Z0-9]{2,12}-PERP$')
    type: str = Field(pattern=r'^PERP$')
    open_interest: float = Field(ge=0)
    funding_interval: str | None = Field(default=None, pattern=r'^\d{1,20}$')
    base_asset_name: str | None = Field(default=None, max_length=12)
    quote: IntxQuote


class IntxFunding(ProviderRow):
    funding_rate: float = Field(gt=-1, lt=1)
    mark_price: float | None = Field(default=None, gt=0)
    event_time: str

    @field_validator('event_time')
    @classmethod
    def aware(cls, value: str) -> str:
        parse_utc(value)
        return value


class CoinbaseIntxProvider(HTTPProvider, _Throttled):
    """INTX perpetual funding/basis/OI as a slow cross-check (small OI; not a standalone positioning signal)."""

    def __init__(self, timeout: float = 15):
        cfg = config()['coinbase_intx']
        super().__init__('coinbase_intx', cfg['base_url'], timeout=timeout)
        self.guard = RateGuard(cfg['max_requests_per_minute'], 60)
        self._init_cache()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        if not self.guard.try_spend(1):
            raise ProviderUnavailable(f'{self.name}: local request budget exhausted')
        return _json(await self.request('GET', path, params=params or {}), self.name)

    async def instrument(self, symbol: str, instrument: str, now: datetime | None = None) -> dict:
        cfg = config()['coinbase_intx']
        now = now or utcnow()
        rows = validate_rows(IntxInstrument, [await self._get(f'/api/v1/instruments/{instrument}')], self.health)
        if not rows or rows[0]['symbol'] != instrument:
            raise ProviderUnavailable(f'{self.name}: invalid instrument payload for {instrument}')
        row = rows[0]
        quote = row['quote']
        funding_raw = await self._get(f'/api/v1/instruments/{instrument}/funding', {'result_limit': 1})
        results = funding_raw.get('results') if isinstance(funding_raw, dict) else None
        if not isinstance(results, list):  # Funding history is secondary: count it as a rejected payload, keep the quote.
            self.health['rejected_rows'] = self.health.get('rejected_rows', 0) + 1
            results = []
        funding = validate_rows(IntxFunding, results, self.health)
        last = max(funding, key=lambda r: parse_utc(r['event_time'])) if funding else None
        interval = int(row['funding_interval']) / 3.6e12 if row.get('funding_interval') else None
        return {
            'provider': self.name, 'kind': 'perp_positioning', 'status': 'ok', 'role': 'cross-check',
            'venue': 'Coinbase International (INTX)', 'symbol': symbol, 'instrument': instrument,
            'as_of': _iso(parse_utc(quote['timestamp'])), 'retrieved_at': _iso(now),
            'delay': 'real-time venue perpetual (not spot); funding settles hourly', 'live': True,
            'mark_price': quote['mark_price'], 'index_price': quote['index_price'],
            'basis_bps': round((quote['mark_price'] / quote['index_price'] - 1) * 1e4, 2),
            'predicted_funding_rate': quote.get('predicted_funding'),
            'funding_interval_hours': round(interval, 3) if interval else None,
            'last_funding_rate': last['funding_rate'] if last else None,
            'last_funding_time': _iso(parse_utc(last['event_time'])) if last else None,
            'open_interest': row['open_interest'], 'open_interest_units': row.get('base_asset_name') or symbol,
            'attribution': cfg['attribution'], 'label': 'descriptive', 'directional_score': 0,
            'note': config()['descriptive_note']}

    async def positioning(self, now: datetime | None = None) -> list[dict]:
        cfg = config()['coinbase_intx']
        cached = self._fresh_cache(cfg['min_interval_seconds'])
        if cached is not None:
            return cached
        now = now or utcnow()
        records, errors = [], []
        for inst in cfg['instruments']:
            try:
                records.append(await self.instrument(inst['symbol'], inst['instrument'], now))
            except ProviderUnavailable as exc:
                errors.append(exc)
                records.append({
                    'provider': self.name, 'kind': 'perp_positioning', 'status': 'unavailable', 'role': 'cross-check',
                    'reason': str(exc).removeprefix(f'{self.name}: '), 'venue': 'Coinbase International (INTX)',
                    'symbol': inst['symbol'], 'instrument': inst['instrument'], 'as_of': None,
                    'retrieved_at': _iso(now), 'delay': 'unavailable (no data)', 'live': False,
                    'attribution': cfg['attribution'], 'label': 'descriptive', 'directional_score': 0})
        if errors and len(errors) == len(records):
            raise errors[-1]
        return self._store(records)
