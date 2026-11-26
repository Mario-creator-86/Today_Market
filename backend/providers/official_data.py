"""Official macro, rates and reference-data adapters (no market prices, nothing live).

- ECB Data Portal (SDMX): deposit facility / MRO policy rates, euro-area AAA 2Y/10Y spot yields, euro-area HICP
  with first-known release times, and the ECB euro reference rates converted into the tracked USD pairs
  (daily fix, LAST CLOSE; crosses derived via EUR).
- Eurostat: euro-area HICP flash vs final (first released data, prc_hicp_fpd).
- BLS Public Data API v2: CPI, payrolls, unemployment, PPI with preliminary footnotes, batched in one query.
- US Treasury Fiscal Data: Treasury General Account from the Daily Treasury Statement (T+1).
- EIA Weekly Petroleum Status Report: release schedule and the table 1 inventory balances (no key).
- FRED/ALFRED (FRED_API_KEY, NOT CONFIGURED without it): observations with realtime vintages for no-lookahead
  history, first releases and point-in-time snapshots.
- central_bank_state(): {bank, policy_rate, rate, yield_2y, as_of, source, verified} per currency, matching the
  inputs of analytics.relative_policy. Nothing is guessed: a value the sources do not give stays missing.

Every record carries as_of (aware UTC), as_of_basis, a delay label, live=False and the attribution required by the
source's terms. Vendor rows are validated with strict models; invalid rows are dropped and counted in health."""
import csv
import io
import os
import re
import time
from collections import deque
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from backend.config import rules
from backend.database import utcnow
from backend.providers.base import HTTPProvider, ProviderUnavailable, label_day
from backend.providers.calendars import calendar_row
from backend.providers.models import ProviderRow, parse_utc, validate_rows

DATE = r'^\d{4}-\d{2}-\d{2}$'
MONTH = r'^\d{4}-\d{2}$'


def _config(section: str) -> dict:
    return rules()['official_data'][section]


def _local(day: date, hhmm: str, zone: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(':'))
    return datetime.combine(day, clock(hour, minute), ZoneInfo(zone)).astimezone(UTC)


def _number(value: Any) -> float | None:
    """Official numbers as floats; blanks, 'null', '.', 'NaN' and footnote markers are missing, never zero."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.replace(',', '').strip()
    if not re.fullmatch(r'-?\d+(\.\d+)?', text):
        return None
    return float(text)


# --- local rate guard -------------------------------------------------------------------------------------


class RateGuard:
    """Sliding-window budget per provider (documented limits live in config). Refuses before the vendor does."""

    def __init__(self, name: str, windows: list[dict], clock_fn=time.monotonic):
        self.name, self.clock = name, clock_fn
        self.windows = [(int(w['max_calls']), float(w['per_seconds'])) for w in windows]
        self.calls: deque[float] = deque()

    def acquire(self) -> None:
        now = self.clock()
        horizon = max((seconds for _, seconds in self.windows), default=0)
        while self.calls and now - self.calls[0] >= horizon:
            self.calls.popleft()
        for limit, seconds in self.windows:
            if sum(1 for stamp in self.calls if now - stamp < seconds) >= limit:
                raise ProviderUnavailable(f'{self.name}: local rate guard reached ({limit} calls per {seconds:g}s)', 429)
        self.calls.append(now)

    def usage(self) -> list[dict]:
        now = self.clock()
        return [{'max_calls': limit, 'per_seconds': seconds, 'used': sum(1 for s in self.calls if now - s < seconds)}
                for limit, seconds in self.windows]


class OfficialProvider(HTTPProvider):
    """HTTPProvider plus a configured rate guard; the guard is only charged when a request is really attempted."""

    def __init__(self, name: str, base_url: str, windows: list[dict], key: str = '', configured: bool = True):
        super().__init__(name, base_url, key, configured)
        self.guard = RateGuard(name, windows)

    async def guarded(self, method: str, path: str, **kwargs):
        if not self.configured:
            raise ProviderUnavailable(f'{self.name}: NOT CONFIGURED')
        if time.monotonic() < self.next_attempt:
            # Circuit open: no request will be sent, so the daily budget must not be charged.
            raise ProviderUnavailable(f'{self.name}: circuit open')
        try:
            self.guard.acquire()
        finally:
            self.health['budget'] = self.guard.usage()
        return await self.request(method, path, **kwargs)


# --- vendor row models (strict: invalid rows are dropped and counted) -----------------------------------------


class EcbCsvRow(ProviderRow):
    KEY: str = Field(min_length=3, max_length=120)
    TIME_PERIOD: str = Field(pattern=r'^\d{4}-\d{2}(-\d{2})?$')
    OBS_VALUE: float
    VALID_FROM: str | None = None

    @field_validator('VALID_FROM')
    @classmethod
    def aware_or_blank(cls, value: str | None) -> str | None:
        if value:
            parse_utc(value)
        return value or None


class EurostatDataset(ProviderRow):
    id: list[str] = Field(min_length=1)
    size: list[int] = Field(min_length=1)
    value: dict[str, float | None]
    dimension: dict[str, dict]
    updated: str
    status: dict[str, str] = Field(default_factory=dict)

    @field_validator('updated')
    @classmethod
    def aware(cls, value: str) -> str:
        # Eurostat writes '+0200'; normalise to an ISO offset before checking.
        parse_utc(re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', value))
        return value


class BlsFootnote(ProviderRow):
    code: str | None = None
    text: str | None = None


class BlsPoint(ProviderRow):
    year: str = Field(pattern=r'^\d{4}$')
    period: str = Field(pattern=r'^M(0[1-9]|1[0-2])$')  # M13 (annual average) is dropped
    value: float
    latest: str | None = None
    footnotes: list[BlsFootnote] = Field(default_factory=list)


class DtsRow(ProviderRow):
    record_date: str = Field(pattern=DATE)
    account_type: str = Field(min_length=3, max_length=200)
    open_today_bal: str | None = None
    close_today_bal: str | None = None


class WpsrStockRow(ProviderRow):
    name: str = Field(min_length=2, max_length=120)
    current: float
    previous: float
    change: float


class FredObservation(ProviderRow):
    date: str = Field(pattern=DATE)
    value: str
    realtime_start: str = Field(pattern=DATE)
    realtime_end: str = Field(pattern=DATE)


class FredReleaseDate(ProviderRow):
    release_id: int
    date: str = Field(pattern=DATE)


class OfficialRecord(BaseModel):
    """Outgoing shape shared by every adapter in this module (extra descriptive fields are allowed)."""
    model_config = ConfigDict(extra='allow', allow_inf_nan=False)
    provider: str
    series: str = Field(min_length=2, max_length=64)
    title: str
    value: float
    unit: str
    period: str = Field(pattern=r'^\d{4}-\d{2}(-\d{2})?$')
    as_of: AwareDatetime
    as_of_basis: str
    delay: str
    label: Literal['LAST CLOSE', 'POLICY RATE', 'OFFICIAL RELEASE', 'T+1', 'WEEKLY']
    live: Literal[False] = False
    attribution: str = Field(min_length=8)
    retrieved_at: AwareDatetime
    known_from: AwareDatetime | None = None


def _record(**fields: Any) -> dict:
    return OfficialRecord.model_validate(fields).model_dump(mode='json')


# --- ECB Data Portal --------------------------------------------------------------------------------------------


def ecb_rows(text: str) -> list[dict]:
    """SDMX csvdata rows; history 'Delete' actions and blank values are not observations."""
    return [row for row in csv.DictReader(io.StringIO(text)) if row.get('ACTION') != 'Delete']


def _cross(rates: dict[str, float], pair: str) -> float | None:
    """Unrounded XXXYYY = rate[YYY] / rate[XXX] from per-EUR rates; None when either side is missing or not positive."""
    rates = dict(rates, EUR=1.0)
    base, quote = rates.get(pair[:3], 0.0), rates.get(pair[3:], 0.0)
    return quote / base if base > 0 and quote > 0 else None


def fx_crosses(rates: dict[str, float], pairs: list[str]) -> dict[str, float]:
    """rates are units of currency per 1 EUR (ECB quotation). Price of XXXYYY = rate[YYY] / rate[XXX]."""
    decimals = _config('ecb')['fx']['decimals']
    result = {}
    for pair in pairs:
        cross = _cross(rates, pair)
        if cross is not None:
            result[pair] = round(cross, decimals.get(pair, decimals['default']))
    return result


def usd_index_replica(rates: dict[str, float], cfg: dict | None = None) -> float | None:
    """The published ICE DXY formula applied to one ECB fixing: constant x product of pair ** weight, with every pair
    derived via EUR from the same fix. None when any input rate is missing or not positive: a partial index is never
    produced. This is a replica computed by the app, not ICE's index."""
    cfg = cfg or _config('ecb')['fx']['usd_index_replica']
    value = float(cfg['constant'])
    for pair, weight in cfg['weights'].items():
        cross = _cross(rates, pair)
        if cross is None:
            return None
        value *= cross ** float(weight)
    return value


def _fx_quote(record: dict) -> dict:
    """Quote-shaped view of an ECB fix record: daily close, never live; the change is vs the previous fix."""
    change = {k: record[k] for k in ('previous_close', 'previous_close_at', 'change_pct', 'change_basis')
              if record.get(k) is not None}
    return {'symbol': record['symbol'], 'price': record['price'], 'timestamp': record['timestamp'],
            'provider': record['provider'], 'daily_close': True, 'data_class': record['data_class'],
            'derived': bool(record.get('derived')), 'label': record['display_label'], 'attribution': record['attribution'],
            'as_of_label': record['as_of_label'], **change}


class EcbDataProvider(OfficialProvider):
    def __init__(self):
        config = _config('ecb')
        super().__init__('ecb_data', config['base_url'], config['rate_limits'])

    async def _observations(self, flow: str, key: str, **params) -> list[dict]:
        response = await self.guarded('GET', f'/service/data/{flow}/{key}', params={'format': 'csvdata', **params},
                                      headers={'Accept': 'text/csv'})
        return validate_rows(EcbCsvRow, ecb_rows(response.text), self.health)

    async def policy_rates(self) -> list[dict]:
        config, cfg = _config('ecb'), _config('ecb')['policy_rates']
        now = utcnow()
        latest: dict[str, dict] = {}
        for row in await self._observations(cfg['flow'], cfg['key'], lastNObservations=1):
            name = row['KEY'].split('.')[-2]
            if name in cfg['series'] and (name not in latest or row['TIME_PERIOD'] > latest[name]['TIME_PERIOD']):
                latest[name] = row
        return [_record(provider='ecb', series=name, title=cfg['series'][name], value=row['OBS_VALUE'], unit='pct',
                        period=row['TIME_PERIOD'], effective_from=row['TIME_PERIOD'],
                        as_of=_local(date.fromisoformat(row['TIME_PERIOD']), '00:00', cfg['effective_timezone']),
                        as_of_basis='effective date of the latest rate change (ECB key interest rates series)',
                        delay=cfg['delay'], label='POLICY RATE', attribution=config['attribution'], retrieved_at=now,
                        source_url=f"{config['base_url']}/service/data/{cfg['flow']}/{row['KEY'].split('.', 1)[1]}")
                for name, row in latest.items()]

    async def yields(self, last_n: int = 1) -> list[dict]:
        config, cfg = _config('ecb'), _config('ecb')['yields']
        now = utcnow()
        result = []
        for row in await self._observations(cfg['flow'], cfg['key'], lastNObservations=last_n):
            series = cfg['series'].get(row['KEY'].split('.')[-1])
            if not series:
                continue
            day = date.fromisoformat(row['TIME_PERIOD'])
            result.append(_record(provider='ecb', series=series, title=f"{cfg['title']}, {series[2:]}",
                                  value=round(row['OBS_VALUE'], 4), unit='pct', period=row['TIME_PERIOD'],
                                  as_of=_local(day, cfg['record_time'], cfg['record_timezone']),
                                  as_of_basis='curve date at the configured nominal close; the date is authoritative',
                                  delay=cfg['delay'], label='LAST CLOSE', attribution=config['attribution'],
                                  retrieved_at=now))
        return sorted(result, key=lambda r: (r['series'], r['period']))

    async def hicp(self) -> list[dict]:
        """Euro-area HICP y/y with VALID_FROM (the time the value became public) as as_of and known_from."""
        config, cfg = _config('ecb'), _config('ecb')['hicp']
        now = utcnow()
        result = []
        for row in await self._observations(cfg['flow'], cfg['key'], lastNObservations=cfg['last_n'], includeHistory='true'):
            known = parse_utc(row['VALID_FROM']) if row.get('VALID_FROM') else None
            result.append(_record(provider='ecb', series=cfg['series'], title=cfg['title'], value=row['OBS_VALUE'],
                                  unit='pct', period=row['TIME_PERIOD'], as_of=known or now,
                                  as_of_basis='VALID_FROM release timestamp' if known else 'retrieved (no release timestamp)',
                                  known_from=known, delay=cfg['delay'], label='OFFICIAL RELEASE',
                                  attribution=config['attribution'], retrieved_at=now))
        return sorted(result, key=lambda r: (r['period'], r['as_of']))

    async def fx_reference(self) -> list[dict]:
        """ECB euro reference rates for the latest common fixing date, converted to the tracked USD pairs, each with the
        previous fix (change_basis 'previous_fix'); plus the USD index replica (ICE DXY formula) for both fixes."""
        config, cfg = _config('ecb'), _config('ecb')['fx']
        key = f"D.{'+'.join(cfg['currencies'])}.{cfg['key_suffix']}"
        by_day: dict[str, dict[str, float]] = {}
        for row in await self._observations(cfg['flow'], key, lastNObservations=cfg['last_n']):
            currency = row['KEY'].split('.')[2]
            if currency in cfg['currencies'] and row['OBS_VALUE'] > 0:
                by_day.setdefault(row['TIME_PERIOD'], {})[currency] = row['OBS_VALUE']
        # Only fixing dates where every configured currency is present: dates are never mixed within one value.
        complete = sorted((day for day, rates in by_day.items() if set(cfg['currencies']) <= rates.keys()), reverse=True)
        if not complete:
            return []
        day, previous = complete[0], complete[1] if len(complete) > 1 else None

        def stamp(fix: str) -> datetime:
            return _local(date.fromisoformat(fix), cfg['fixing_time'], cfg['fixing_timezone'])

        def change(price: float, before: float | None) -> dict:
            if previous is None or not before:
                return {}
            return {'previous_close': before, 'previous_close_at': stamp(previous), 'previous_period': previous,
                    'change_pct': round((price / before - 1) * 100, 4), 'change_basis': 'previous_fix'}

        now = utcnow()
        as_of_label = cfg['as_of_label'].format(date=label_day(day))
        before = fx_crosses(by_day[previous], cfg['pairs']) if previous else {}
        result = []
        for pair, price in fx_crosses(by_day[day], cfg['pairs']).items():
            derived = 'EUR' not in (pair[:3], pair[3:])
            result.append(_record(provider='ecb', series=pair, symbol=pair, title=f'{pair} ECB reference rate', value=price,
                                  price=price, unit='rate', period=day, as_of=stamp(day), timestamp=stamp(day),
                                  as_of_basis='ECB fixing time (14:15 CET) on the fixing date',
                                  delay=cfg['delay'], label='LAST CLOSE', daily_close=True, derived_via_eur=derived,
                                  data_class='DAILY_CLOSE', display_label=cfg['labels']['cross' if derived else 'direct'],
                                  as_of_label=as_of_label, description=cfg['description'],
                                  attribution=config['attribution'], retrieved_at=now,
                                  **change(price, before.get(pair))))
        replica = cfg.get('usd_index_replica')
        value = usd_index_replica(by_day[day], replica) if replica else None
        if replica and value is not None:
            decimals = int(replica['decimals'])
            prior = usd_index_replica(by_day[previous], replica) if previous else None
            price = round(value, decimals)
            inputs = sorted({code for pair in replica['weights'] for code in (pair[:3], pair[3:])} - {'EUR'})
            result.append(_record(provider=replica['provider'], series=replica['series'], symbol=replica['symbol'],
                                  title=replica['label'], value=price, price=price, unit='index', period=day,
                                  as_of=stamp(day), timestamp=stamp(day),
                                  as_of_basis='ECB fixing time (14:15 CET) on the fixing date; computed by this app',
                                  delay=cfg['delay'], label='LAST CLOSE', daily_close=True, derived=True,
                                  data_class='DERIVED_DAILY', display_label=replica['label'],
                                  as_of_label=replica['as_of_label'].format(date=label_day(day)),
                                  inputs={code: by_day[day][code] for code in inputs},
                                  methodology_url=replica['methodology_url'], attribution=replica['attribution'],
                                  retrieved_at=now, **change(price, round(prior, decimals) if prior is not None else None)))
        return result

    async def quotes(self) -> list[dict]:
        """Quote-shaped daily-fix records (daily_close=True, provider 'ecb' or 'ecb-derived'); never live FX."""
        return [_fx_quote(r) for r in await self.fx_reference()]


# --- Eurostat ---------------------------------------------------------------------------------------------------


def jsonstat_cells(dataset: dict) -> list[tuple[dict[str, str], float, str | None]]:
    """Flatten a JSON-stat 2.0 dataset into (coordinates, value, status) cells (row-major, last dimension fastest)."""
    ids, sizes = dataset['id'], dataset['size']
    positions = {dim: {index: code for code, index in dataset['dimension'][dim]['category']['index'].items()}
                 for dim in ids}
    cells = []
    for flat, value in dataset['value'].items():
        if value is None:
            continue
        remainder, coords = int(flat), {}
        for dim, size in zip(reversed(ids), reversed(sizes), strict=True):
            remainder, index = divmod(remainder, size)
            coords[dim] = positions[dim][index]
        cells.append((coords, float(value), dataset.get('status', {}).get(flat)))
    return cells


class EurostatProvider(OfficialProvider):
    def __init__(self):
        config = _config('eurostat')
        super().__init__('eurostat', config['base_url'], config['rate_limits'])

    async def hicp_flash(self) -> list[dict]:
        """Euro-area HICP y/y as first released: FLASH and FINAL per month (no revisions, no lookahead)."""
        config, cfg = _config('eurostat'), _config('eurostat')['hicp_flash']
        response = await self.guarded('GET', f"/statistics/1.0/data/{cfg['dataset']}",
                                      params={**cfg['params'], 'lastTimePeriod': cfg['last_periods']})
        datasets = validate_rows(EurostatDataset, [response.json()], self.health)
        if not datasets:
            return []
        dataset = datasets[0]
        updated = parse_utc(re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', dataset['updated']))
        now = utcnow()
        result = []
        for coords, value, status in jsonstat_cells(dataset):
            release = cfg['releases'].get(coords.get('release', ''))
            if not release or not re.fullmatch(MONTH, coords.get('time', '')):
                continue
            result.append(_record(provider='eurostat', series=cfg['series'], title=cfg['title'], value=value, unit='pct',
                                  period=coords['time'], release=release, estimated=status == 'e',
                                  as_of=updated, as_of_basis='dataset update timestamp (per-release times not published)',
                                  delay='monthly flash release' if release == 'FLASH' else 'monthly release',
                                  label='OFFICIAL RELEASE', attribution=config['attribution'], retrieved_at=now,
                                  dataset=cfg['dataset']))
        return sorted(result, key=lambda r: (r['period'], r['release'] != 'FLASH'))


# --- BLS ----------------------------------------------------------------------------------------------------------


class BLSProvider(OfficialProvider):
    """Keyless (unregistered) by default; BLS_API_KEY from the environment lifts the budget if the user registers."""

    def __init__(self, key: str | None = None):
        config = _config('bls')
        key = key if key is not None else os.environ.get(config['key_env'], '')
        super().__init__('bls_data', config['base_url'], config['registered_rate_limits' if key else 'rate_limits'], key)

    async def series(self, start_year: int | None = None, end_year: int | None = None,
                     series_ids: list[str] | None = None) -> list[dict]:
        config = _config('bls')
        ids = series_ids or list(config['series'])
        if len(ids) > config['max_series_per_query']:
            raise ValueError('Too many BLS series for one query')
        end = end_year or utcnow().year
        payload: dict[str, Any] = {'seriesid': ids, 'startyear': str(start_year or end - 1), 'endyear': str(end)}
        if self.key:
            payload['registrationkey'] = self.key
        response = await self.guarded('POST', config['path'], json=payload)
        body = response.json()
        if body.get('status') != 'REQUEST_SUCCEEDED':
            # Daily threshold and invalid-series messages come back as HTTP 200 with REQUEST_NOT_PROCESSED.
            status = str(body.get('status') or 'no status')
            self.health.update(status='DEGRADED', error='BLS did not process the request', error_type=status)
            raise ProviderUnavailable(f"{self.name}: {status}")
        now = utcnow()
        attribution = f"{config['attribution']}, retrieved {now.date().isoformat()} via the BLS Public Data API"
        result = []
        for block in (body.get('Results') or {}).get('series') or []:
            series_id = block.get('seriesID')
            meta = config['series'].get(series_id)
            if not meta:
                continue
            points = block.get('data')
            if isinstance(points, list):
                # '-' marks an official gap (e.g. Oct-2025, lapse in appropriations): missing, not a bad row.
                gaps = [p for p in points if isinstance(p, dict) and str(p.get('value', '')).strip() == '-']
                self.health['missing_values'] = self.health.get('missing_values', 0) + len(gaps)
                points = [p for p in points if not any(p is gap for gap in gaps)]
            for point in validate_rows(BlsPoint, points, self.health):
                notes = [n for n in point['footnotes'] if n.get('code') or n.get('text')]
                result.append(_record(provider='bls', series=series_id, title=meta['name'], value=point['value'],
                                      unit=meta['unit'], period=f"{point['year']}-{point['period'][1:]}",
                                      preliminary=any(n.get('code') == 'P' for n in notes),
                                      latest=point.get('latest') == 'true', footnotes=[n.get('text') for n in notes],
                                      as_of=now, as_of_basis='retrieved (BLS publishes no release timestamp)',
                                      delay=config['delay'], label='OFFICIAL RELEASE', attribution=attribution,
                                      retrieved_at=now))
        return sorted(result, key=lambda r: (r['series'], r['period']))


# --- US Treasury Fiscal Data ----------------------------------------------------------------------------------------


class TreasuryFiscalProvider(OfficialProvider):
    def __init__(self):
        config = _config('treasury_fiscal')
        super().__init__('treasury_fiscal', config['base_url'], config['rate_limits'])

    async def tga(self, since: date | None = None) -> list[dict]:
        """Treasury General Account lines of the Daily Treasury Statement (USD millions), labelled T+1."""
        config = _config('treasury_fiscal')
        since = since or (utcnow().date() - timedelta(days=config['lookback_days']))
        response = await self.guarded('GET', config['path'], params={
            'filter': f'record_date:gte:{since.isoformat()}', 'sort': '-record_date', 'page[size]': 100})
        now = utcnow()
        result = []
        for row in validate_rows(DtsRow, response.json().get('data'), self.health):
            series = config['accounts'].get(row['account_type'])
            # The closing-balance line carries its figure in open_today_bal while close_today_bal is 'null'.
            value = _number(row.get('close_today_bal'))
            value = value if value is not None else _number(row.get('open_today_bal'))
            if not series or value is None:
                continue
            day = date.fromisoformat(row['record_date'])
            result.append(_record(provider='treasury_fiscal', series=series, title=row['account_type'], value=value,
                                  unit=config['unit'], period=row['record_date'],
                                  as_of=_local(day, config['record_time'], config['record_timezone']),
                                  as_of_basis='close of business on the record date (published about 16:00 ET on D+1)',
                                  delay=config['delay'], label='T+1', attribution=config['attribution'], retrieved_at=now))
        return sorted(result, key=lambda r: (r['period'], r['series']))


# --- EIA Weekly Petroleum Status Report -------------------------------------------------------------------------------


def parse_wpsr_table1(text: str) -> tuple[date | None, list[dict]]:
    """First block of table1.csv: stock levels for the latest week vs the prior week (million barrels)."""
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or header[0] != 'STUB_1' or len(header) < 4:
        return None, []
    try:
        week = datetime.strptime(header[1], '%m/%d/%y').date()
    except ValueError:
        return None, []
    rows = []
    for line in reader:
        if not line or line[0] == 'STUB_1':
            break
        if len(line) >= 4:
            rows.append({'name': line[0].strip(), 'current': _number(line[1]), 'previous': _number(line[2]),
                         'change': _number(line[3])})
    return week, rows


def parse_wpsr_schedule(html: str) -> dict[date, datetime]:
    """Holiday exceptions: data week ending -> alternate release time (US Eastern)."""
    config = _config('eia_wpsr')['standard_release']
    zone = ZoneInfo(config['timezone'])
    result = {}
    pattern = re.compile(r'<th[^>]*>\s*([A-Z][a-z]+ \d{1,2}, \d{4})\s*</th>\s*<td[^>]*>\s*([A-Z][a-z]+ \d{1,2}, \d{4})\s*</td>'
                         r'\s*<td[^>]*>\s*\w+\s*</td>\s*<td[^>]*>\s*(\d{1,2}:\d{2}) ([ap])\.m\.', re.IGNORECASE)
    for week_text, release_text, hhmm, meridiem in pattern.findall(html):
        week = datetime.strptime(week_text, '%B %d, %Y').date()
        day = datetime.strptime(release_text, '%B %d, %Y').date()
        at = datetime.strptime(f'{hhmm} {meridiem.upper()}M', '%I:%M %p').time()
        result[week] = datetime.combine(day, at, zone).astimezone(UTC)
    return result


class EIAWeeklyPetroleumProvider(OfficialProvider):
    """Keyless WPSR table 1 (inventories). EIA redirects to a time-windowed signed URL on the same host."""

    def __init__(self):
        config = _config('eia_wpsr')
        super().__init__('eia_wpsr', config['tables_base_url'], config['rate_limits'])

    async def inventories(self) -> list[dict]:
        config = _config('eia_wpsr')
        response = await self.guarded('GET', config['table1_path'], follow_redirects=True, headers={'Accept': 'text/csv'})
        if response.url.host != config['allowed_redirect_host']:
            self.health.update(status='DEGRADED', error='Unexpected redirect host')
            raise ProviderUnavailable(f'{self.name}: unexpected redirect host')
        week, rows = parse_wpsr_table1(response.text)
        if week is None:
            self.health.update(status='DEGRADED', error='table1.csv layout not recognised')
            return []
        # Last-Modified is when EIA staged the file; public release is the scheduled 10:30 ET slot.
        modified = response.headers.get('last-modified')
        modified_at = parsedate_to_datetime(modified).astimezone(UTC) if modified else None
        wanted = [row for row in rows if row['name'] in config['rows']]
        now = utcnow()
        week_close = datetime.combine(week, clock(0), UTC)
        return [_record(provider='eia', series=config['rows'][row['name']], title=row['name'], value=row['current'],
                        previous=row['previous'], change=row['change'], unit=config['unit'], period=week.isoformat(),
                        week_ending=week.isoformat(), file_last_modified=modified_at.isoformat() if modified_at else None,
                        as_of=week_close, as_of_basis='data week ending date (Friday); released per the WPSR schedule',
                        delay=f'weekly, as of {label_day(week)}', label='WEEKLY',
                        attribution=config['attribution'], retrieved_at=now)
                for row in validate_rows(WpsrStockRow, wanted, self.health)]


class EIAWeeklyPetroleumCalendar(OfficialProvider):
    """WPSR release times: Wednesday 10:30 ET for the week ending the previous Friday, unless the schedule page
    lists a holiday alternate. Rows follow the macro_calendar contract."""

    def __init__(self):
        config = _config('eia_wpsr')
        super().__init__('eia_wpsr_calendar', config['schedule_base_url'], config['rate_limits'])

    @staticmethod
    def releases(start: date, end: date, exceptions: dict[date, datetime]) -> list[tuple[date, datetime]]:
        standard = _config('eia_wpsr')['standard_release']
        result = []
        friday = start - timedelta(days=(start.weekday() - 4) % 7 + 14)
        while friday <= end:
            release = exceptions.get(friday) or _local(friday + timedelta(days=standard['days_after_week_end']),
                                                       standard['time'], standard['timezone'])
            if start <= release.astimezone(ZoneInfo(standard['timezone'])).date() <= end:
                result.append((friday, release))
            friday += timedelta(days=7)
        return result

    async def calendar(self, start: str, end: str) -> list[dict]:
        config = _config('eia_wpsr')
        response = await self.guarded('GET', config['schedule_path'], headers={'Accept': 'text/html'})
        exceptions = parse_wpsr_schedule(response.text)
        # The page always lists the year's holiday rows (past ones included); none parsed means the layout changed
        # and holiday weeks would silently fall back to the standard Wednesday slot.
        self.health['schedule_exceptions'] = len(exceptions)
        if not exceptions:
            self.health.update(status='DEGRADED', error='No holiday rows parsed from the WPSR schedule page')
        source = config['schedule_base_url'] + config['schedule_path']
        return [calendar_row(config['calendar_name'], 'US', config['calendar_category'], release, config['impact'], self.name,
                             source, week_ending=week.isoformat(), time_known=True, event_class='EIA_WPSR',
                             time_source='source' if week in exceptions else 'config',
                             time_basis='holiday alternate (EIA schedule)' if week in exceptions else 'standard Wednesday 10:30 ET',
                             reference_period=f'week ending {week.isoformat()}', attribution=config['attribution'])
                for week, release in self.releases(date.fromisoformat(start), date.fromisoformat(end), exceptions)]


# --- FRED / ALFRED ------------------------------------------------------------------------------------------------------


class FredProvider(OfficialProvider):
    """FRED/ALFRED series observations. Without FRED_API_KEY the adapter is NOT CONFIGURED and never called.
    Capability calendar: the BLS release dates (release/dates) as the fallback of the BLS ICS schedule."""
    fallback_for = 'bls_release_calendar'  # runtime.refresh_calendar calls calendar() only when that source is unusable

    def __init__(self, key: str | None = None):
        config = _config('fred')
        key = key if key is not None else os.environ.get(config['key_env'], '')
        super().__init__('fred', config['base_url'], config['rate_limits'], key, bool(key))

    async def observations(self, series_id: str, *, observation_start: str | None = None,
                           realtime_start: str | None = None, realtime_end: str | None = None,
                           output_type: int | None = None, limit: int | None = None, sort_order: str = 'asc') -> list[dict]:
        """Observations with their realtime (vintage) window. realtime_start == realtime_end == D returns the
        series exactly as it was known on D; output_type=4 returns initial releases only."""
        config = _config('fred')
        meta = config['series'].get(series_id)
        if meta is None:
            raise ValueError('Series not configured for FRED')
        params: dict[str, Any] = {'series_id': series_id, 'api_key': self.key, 'file_type': 'json', 'sort_order': sort_order}
        for name, option in [('observation_start', observation_start), ('realtime_start', realtime_start),
                             ('realtime_end', realtime_end), ('output_type', output_type), ('limit', limit)]:
            if option is not None:
                params[name] = option
        response = await self.guarded('GET', '/fred/series/observations', params=params)
        now = utcnow()
        attribution = config['attribution'] + (f" ({meta['attribution_extra']})" if meta.get('attribution_extra') else '')
        # Without realtime/output_type parameters FRED answers with the current vintage and stamps every row with
        # realtime_start = the request date, which is NOT when the value became public. Only vintage queries
        # (realtime_start given) and initial-release queries (output_type=4) carry a meaningful known_from.
        vintage_query = realtime_start is not None or output_type == 4
        daily = meta['frequency'] == 'daily'
        result = []
        for row in validate_rows(FredObservation, response.json().get('observations'), self.health):
            value = _number(row['value'])  # FRED writes '.' for a missing observation
            if value is None:
                continue
            known = datetime.combine(date.fromisoformat(row['realtime_start']), clock(0), UTC) if vintage_query else None
            if daily:
                as_of, basis = datetime.combine(date.fromisoformat(row['date']), clock(0), UTC), 'observation date'
            elif known is not None:
                as_of, basis = known, 'vintage date (realtime_start)'
            else:
                as_of, basis = now, 'retrieved (current vintage; FRED gives no release timestamp here)'
            result.append(_record(provider='fred', series=series_id, title=meta['title'], value=value, unit=meta['unit'],
                                  period=row['date'], realtime_start=row['realtime_start'],
                                  realtime_end=row['realtime_end'], known_from=known, as_of=as_of, as_of_basis=basis,
                                  delay=meta['delay'], label='LAST CLOSE' if daily else 'OFFICIAL RELEASE',
                                  attribution=attribution, retrieved_at=now))
        return result

    async def calendar(self, start: str, end: str) -> list[dict]:
        """Release dates of the configured BLS releases (include_release_dates_with_no_data returns future dates) at
        the agency's standard release time from config; time_basis says so. Dates outside [start, end] are dropped."""
        config = _config('fred')['release_calendar']
        zone = ZoneInfo(config['timezone'])
        rows = []
        for release in config['releases']:
            response = await self.guarded('GET', config['path'], params={
                'release_id': release['release_id'], 'api_key': self.key, 'file_type': 'json',
                'include_release_dates_with_no_data': 'true', 'realtime_start': start, 'sort_order': 'desc',
                'limit': config['limit']})
            for item in validate_rows(FredReleaseDate, response.json().get('release_dates'), self.health):
                if item['release_id'] != release['release_id'] or not start <= item['date'] <= end:
                    continue
                day = date.fromisoformat(item['date'])
                hour, minute = (int(part) for part in release['time'].split(':'))
                rows.append(calendar_row(release['name'], 'US', 'MACRO', datetime.combine(day, clock(hour, minute), zone),
                                         release['impact'], self.name, config['source_url'].format(release_id=release['release_id']),
                                         event_class=release['event_class'], time_known=True, time_source='config',
                                         time_basis=config['time_basis'].format(agency=release['agency'], time=release['time']),
                                         attribution=config['attribution'], release_id=release['release_id']))
        return sorted(rows, key=lambda r: r['scheduled_at'])

    async def latest(self, series_ids: list[str] | None = None) -> list[dict]:
        """Most recent non-missing observation per configured series (current vintage), with the prior non-missing
        observation as previous_value/previous_period (the reference for a close-to-close change)."""
        lookback = _config('fred')['latest_lookback']
        result = []
        for series_id in series_ids or list(_config('fred')['series']):
            rows = sorted(await self.observations(series_id, sort_order='desc', limit=lookback),
                          key=lambda r: r['period'], reverse=True)
            if rows:
                record = dict(rows[0])
                if len(rows) > 1:
                    record.update(previous_value=rows[1]['value'], previous_period=rows[1]['period'])
                result.append(record)
        return result

    async def as_known_on(self, series_id: str, day: str, observation_start: str | None = None) -> list[dict]:
        """No-lookahead snapshot: the series as published on `day` (ALFRED vintage)."""
        return await self.observations(series_id, observation_start=observation_start, realtime_start=day, realtime_end=day)

    async def first_releases(self, series_id: str, observation_start: str) -> list[dict]:
        """First-print values with the date each became public (for surprise history without revisions)."""
        return await self.observations(series_id, observation_start=observation_start, realtime_start=observation_start,
                                       realtime_end='9999-12-31', output_type=4)


def fred_quote_attribution(meta: dict) -> str:
    """The owner's notice for a quote shown from FRED; it always names FRED as the channel ('via FRED') and never
    claims a licence the owner did not grant."""
    text = meta.get('quote_attribution') or f"Source: {meta['title']} via FRED"
    return text if 'via FRED' in text else f'{text} (via FRED)'


def fred_quotes(records: list[dict]) -> list[dict]:
    """Daily-close quotes for the FRED series mapped in fred.quote_symbols (e.g. VIXCLS -> VIX), stamped at the
    configured US close of the observation date, with the previous observation as previous_close. FRED lags its
    sources by days, so these are never live. The attribution keeps the owner's citation/copyright note."""
    config = _config('fred')
    mapping, zone = config['quote_symbols'], config['quote_record_timezone']
    result = []
    for record in records:
        symbol = mapping.get(record.get('series', ''))
        if record.get('provider') != 'fred' or not symbol or record.get('value') is None or record['value'] <= 0:
            continue
        meta = config['series'][record['series']]
        quote = {'symbol': symbol, 'price': record['value'], 'provider': 'fred', 'daily_close': True,
                 'data_class': 'DAILY_CLOSE', 'label': meta.get('quote_label', meta['title']),
                 'attribution': fred_quote_attribution(meta),
                 'as_of_label': config['quote_as_of_label'].format(series=record['series'], date=label_day(record['period'])),
                 'timestamp': _local(date.fromisoformat(record['period']), config['quote_record_time'], zone)}
        previous = record.get('previous_value')
        if isinstance(previous, (int, float)) and previous > 0 and record.get('previous_period'):
            quote.update(previous_close=previous, change_basis='previous_close',
                         previous_close_at=_local(date.fromisoformat(record['previous_period']), config['quote_record_time'], zone),
                         change_pct=round((record['value'] / previous - 1) * 100, 4))
        result.append(quote)
    return result


# --- central bank state -------------------------------------------------------------------------------------------------


def _latest(records: list[dict] | None, series: str) -> dict | None:
    matches = [r for r in records or [] if r.get('series') == series]
    return max(matches, key=lambda r: (r['period'], r['as_of'])) if matches else None


def central_bank_state(ecb_rates: list[dict] | None = None, ecb_yields: list[dict] | None = None,
                       fred_latest: list[dict] | None = None, us_2y_quote: dict | None = None) -> dict[str, dict]:
    """Per-currency {bank, currency, policy_rate, rate, yield_2y?, as_of, source, verified, basis, missing}.

    Keys a source does not give are left out (never guessed), so analytics.relative_policy returns WAIT for them.
    expected_next_rate is never provided here: no official source in this module publishes it."""
    config = _config('central_bank_state')
    result: dict[str, dict] = {}

    fed = config['fed']
    upper, lower = _latest(fred_latest, fed['upper']), _latest(fred_latest, fed['lower'])
    state: dict[str, Any] = {'bank': fed['bank'], 'currency': fed['currency'], 'missing': []}
    if upper and lower:
        state.update(policy_rate=round((upper['value'] + lower['value']) / 2, 4), policy_rate_range=[lower['value'], upper['value']],
                     as_of=max(upper['as_of'], lower['as_of']), source=upper['attribution'], verified=True, basis=fed['basis'])
        state['rate'] = state['policy_rate']
    else:
        state['missing'] += ['policy_rate', 'rate', 'as_of', 'verified']
        state['reason'] = 'FRED not configured or no target-range observation; the Fed rate is not guessed'
    us_2y = _latest(fred_latest, fed['yield_2y'])
    if us_2y:
        state.update(yield_2y=us_2y['value'], yield_2y_as_of=us_2y['as_of'], yield_2y_source=us_2y['attribution'])
    elif us_2y_quote and us_2y_quote.get('daily_close') and isinstance(us_2y_quote.get('price'), (int, float)):
        state.update(yield_2y=float(us_2y_quote['price']), yield_2y_as_of=us_2y_quote.get('timestamp'),
                     yield_2y_source='Source: U.S. Treasury daily par yield curve')
    else:
        state['missing'].append('yield_2y')
    state['missing'].append('expected_next_rate')
    result[fed['currency']] = state

    ecb = config['ecb']
    rate, eu_2y = _latest(ecb_rates, ecb['rate_series']), _latest(ecb_yields, ecb['yield_2y'])
    state = {'bank': ecb['bank'], 'currency': ecb['currency'], 'missing': []}
    if rate:
        state.update(policy_rate=rate['value'], rate=rate['value'], effective_from=rate['period'], as_of=rate['as_of'],
                     source=rate['attribution'], verified=True, basis=ecb['basis'])
    else:
        state['missing'] += ['policy_rate', 'rate', 'as_of', 'verified']
    if eu_2y:
        state.update(yield_2y=eu_2y['value'], yield_2y_as_of=eu_2y['as_of'], yield_2y_source=eu_2y['attribution'])
    else:
        state['missing'].append('yield_2y')
    state['missing'].append('expected_next_rate')
    result[ecb['currency']] = state
    return result


async def build_central_bank_state(ecb: EcbDataProvider | None, fred: FredProvider | None = None,
                                   us_2y_quote: dict | None = None) -> dict[str, dict]:
    """Fetch and assemble central_bank_state; a failing, unconfigured or switched-off (None) source leaves its fields
    missing."""
    async def attempt(call) -> list[dict]:
        try:
            return await call()
        except (ProviderUnavailable, ValueError, KeyError, TypeError):
            # Same error family as runtime.PROVIDER_ERRORS: a malformed payload leaves the fields missing.
            return []
    fed_config = _config('central_bank_state')['fed']
    fred_rows = []
    if fred is not None and fred.configured:
        fred_rows = await attempt(lambda: fred.latest([fed_config['upper'], fed_config['lower'], fed_config['yield_2y']]))
    if ecb is None:
        return central_bank_state([], [], fred_rows, us_2y_quote)
    return central_bank_state(await attempt(ecb.policy_rates), await attempt(ecb.yields), fred_rows, us_2y_quote)
