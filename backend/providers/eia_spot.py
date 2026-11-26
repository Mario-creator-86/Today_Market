"""EIA daily spot prices (keyless): WTI Cushing (RWTC) and Brent Europe (RBRTE) from the weekly-updated summary table
https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm (docs/MARKET_COVERAGE.md).

- Daily closing spot values, published once a week (Wednesday, through Tuesday): the latest value is 1 to 8 days old,
  so these are DAILY_CLOSE quotes stamped at the nominal US close of the observation date, never live.
- The change is vs the previous published observation in the same table (change_basis 'previous_close').
- Rows are located by the series code in the history link (s=RWTC / s=RBRTE), never by the row text; the column dates
  come from the table header. A layout that does not match marks the provider DEGRADED and yields nothing.
- Conditional GET (If-Modified-Since; EIA answers 304 when nothing changed) and a local rate guard (config
  official_data.eia_spot.rate_limits); the runtime polls at most hourly.
- EIA content is public domain (cite EIA); the price source per EIA's table definitions is Refinitiv (LSEG)."""
import html
import re
from datetime import date, datetime

from backend.providers.base import label_day
from backend.providers.official_data import OfficialProvider, _config, _local, _number
from backend.schemas import Quote

HEADER_DATE = re.compile(r'<th[^>]*class=["\']?Series5["\']?[^>]*>\s*(\d{1,2}/\d{1,2}/\d{2})\s*</th>', re.IGNORECASE)
# Row boundaries are the DataRow openings: a stub cell nests its own <table><tr>…</tr></table>, so a lazy
# '<tr>.*?</tr>' match would end inside the stub and lose the price cells.
DATA_ROW = re.compile(r'<tr[^>]*\bclass=["\']?DataRow\b[^>]*>', re.IGNORECASE)
SERIES_LINK = re.compile(r'LeafHandler\.ashx\?[^"\'>]*?\bs=([A-Z0-9_]+)', re.IGNORECASE)
VALUE_CELL = re.compile(r'<td[^>]*class=["\']?(?:DataB|Current2)["\']?[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL)
RELEASE = re.compile(r'Release Date:\s*(\d{1,2}/\d{1,2}/\d{4})', re.IGNORECASE)
NEXT_RELEASE = re.compile(r'Next Release Date:\s*(\d{1,2}/\d{1,2}/\d{4})', re.IGNORECASE)
TAGS = re.compile(r'<[^>]+>')


def _cell(text: str) -> float | None:
    """A published price, or None for a blank / NA / W / '-' cell (never zero)."""
    value = _number(html.unescape(TAGS.sub('', text)).replace('\xa0', ' ').strip())
    return value if value is not None and value > 0 else None


def _us_date(text: str, fmt: str) -> date | None:
    try:
        return datetime.strptime(text, fmt).date()
    except ValueError:
        return None


def parse_spot_table(page: str) -> dict:
    """{'dates': [date...], 'series': {code: [(date, value | None)...]}, 'release_date', 'next_release_date'}.
    A series row whose value count differs from the header date count is left out (columns would be misaligned)."""
    result: dict = {'dates': [], 'series': {}, 'release_date': None, 'next_release_date': None}
    # Each series row belongs to the nearest date header above it (the page may repeat the header per section);
    # without a header row, the page's own header dates (deduplicated, in order) are used.
    def header_dates(text: str) -> list | None:
        found = [_us_date(item, '%m/%d/%y') for item in HEADER_DATE.findall(text)]
        return ([] if None in found else list(dict.fromkeys(found))) if found else None

    starts = [m.start() for m in DATA_ROW.finditer(page)]
    fallback = header_dates(page) or []
    leading = header_dates(page[:starts[0]]) if starts else None
    dates: list = fallback if leading is None else leading
    for begin, end in zip(starts, [*starts[1:], len(page)][:len(starts)], strict=True):
        segment = page[begin:end]
        link = SERIES_LINK.search(segment)
        if link and dates:
            # Price cells precede the history link; a header row for the next section may follow it.
            values = [_cell(cell) for cell in VALUE_CELL.findall(segment[:link.end()])]
            if len(values) == len(dates):
                result['dates'] = result['dates'] or dates
                result['series'].setdefault(link.group(1).upper(), list(zip(dates, values, strict=True)))
        header = header_dates(segment[link.end():] if link else segment)
        if header is not None:
            dates = header
    for key, pattern in (('release_date', RELEASE), ('next_release_date', NEXT_RELEASE)):
        found = pattern.search(page)
        parsed = _us_date(found.group(1), '%m/%d/%Y') if found else None
        result[key] = parsed.isoformat() if parsed else None
    return result


def spot_quotes(table: dict) -> list[dict]:
    """Quote-shaped daily closes for the configured series: the latest published observation, with the previous
    published observation as previous_close. A blank latest cell (holiday, not yet published) is never filled."""
    config = _config('eia_spot')

    def stamp(day: date) -> datetime:
        return _local(day, config['record_time'], config['record_timezone'])

    result = []
    for code, spec in config['series'].items():
        observed = sorted((day, value) for day, value in table['series'].get(code, []) if value is not None)
        if not observed:
            continue
        day, value = observed[-1]
        quote = {'symbol': spec['symbol'], 'price': value, 'timestamp': stamp(day), 'provider': 'eia', 'daily_close': True,
                 'data_class': 'DAILY_CLOSE', 'label': spec['label'], 'attribution': config['attribution'],
                 'as_of_label': config['as_of_label'].format(date=label_day(day))}
        if len(observed) > 1:
            previous_day, previous = observed[-2]
            quote.update(previous_close=previous, previous_close_at=stamp(previous_day), change_basis='previous_close',
                         change_pct=round((value / previous - 1) * 100, 4))
        result.append(Quote.model_validate(quote).model_dump(mode='json'))
    return result


class EIASpotPriceProvider(OfficialProvider):
    """Keyless EIA spot table. Capability market_data (daily closes only)."""

    def __init__(self):
        config = _config('eia_spot')
        super().__init__('eia_spot', config['base_url'], config['rate_limits'])
        self.last_modified: str | None = None
        self.cached: list[dict] = []

    async def quotes(self) -> list[dict]:
        config = _config('eia_spot')
        headers = {'Accept': 'text/html'}
        if self.last_modified and self.cached:
            headers['If-Modified-Since'] = self.last_modified
        response = await self.guarded('GET', config['path'], headers=headers, not_modified_ok=True)
        if response.status_code == 304:
            return list(self.cached)  # unchanged since the last parse: the same observations (ingestion skips them)
        table = parse_spot_table(response.text)
        missing = [code for code in config['series'] if code not in table['series']]
        self.health.update(release_date=table['release_date'], next_release_date=table['next_release_date'])
        if not table['dates'] or missing:
            self.health.update(status='DEGRADED', error=f"EIA spot table layout not recognised (missing: {','.join(missing) or 'dates'})")
        quotes = spot_quotes(table)
        if quotes:
            self.cached = quotes
            self.last_modified = response.headers.get('last-modified')
        return quotes
