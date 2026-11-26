"""Discovery and sanctions adapters (no prices, nothing live).

- GDELT 2.0 raw 15-minute export files: lastupdate.txt -> export CSV zip (checksum verified), filtered by CAMEO root
  codes and energy/geopolitics countries. Output is DISCOVERY-ONLY candidate events with their source URLs plus a
  per-country conflict-intensity summary. Machine-coded news is a lead to verify, never a confirmation.
- SEC EDGAR full-text search (efts.sec.gov): configured queries (tracked companies' Item 2.02, going concern,
  Item 1.03 bankruptcies, bitcoin treasury 8-Ks). Requires SEC_USER_AGENT with a real contact e-mail (SEC fair
  access); NOT CONFIGURED without it. Results are Tier-1 filing documents, matched by keyword only.
- OFAC Recent Actions page and the Sanctions List Service (SLS) delta files: official, public-domain Tier-1 US
  government documents (designations, removals, general licenses) with structured SDN additions/removals.

Every record carries as_of (aware UTC ISO), as_of_basis, a delay label, live=False and the attribution the source's
terms ask for. Vendor rows are validated with strict models; invalid rows are dropped and counted in health.
documents() output matches backend.schemas.DocumentInput exactly (delay and attribution are written into content)."""
import asyncio
import csv
import hashlib
import html
import io
import os
import re
import time
import zipfile
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Annotated, Any, Protocol, runtime_checkable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from defusedxml import ElementTree
from pydantic import Field, HttpUrl, field_validator

from backend.config import rules, rules_view
from backend.database import utcnow
from backend.providers.base import HTTPProvider, ProviderUnavailable
from backend.providers.models import ProviderRow, parse_utc, validate_rows


@runtime_checkable
class DiscoveryProvider(Protocol):
    """Discovery-only leads (never evidence): candidates must be verified against Tier-1/2 sources."""

    async def candidates(self) -> list[dict]: ...


def _config(section: str) -> dict:
    return rules()['discovery'][section]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def day_text(day: str) -> str:
    """'2026-09-17' -> '17 Sep 2026' (document text is read by people, never a raw ISO date)."""
    value = date.fromisoformat(day[:10])
    return f'{value.day} {value:%b %Y}'


def time_text(stamp: str, zone: str) -> str:
    """An aware ISO time as '13:32 EDT on 17 Sep 2026' in the source's own time zone."""
    local = datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(ZoneInfo(zone))
    return f'{local:%H:%M %Z} on {local.day} {local:%b %Y}'


def date_only_day(source_id: str | None, published: datetime | str | None) -> str | None:
    """The publication day ('2026-09-28') of a document whose source publishes a date and no clock time (config
    date_only_documents: OFAC Recent Actions, EDGAR full-text hits), else None. The adapters stamp such a document at
    the start of that day in the source's time zone, so a stamp of exactly 00:00:00 there is the marker; the stamp
    orders and ages the publication and is never a clock time to show."""
    if not source_id or published is None:
        return None
    for rule in rules_view()['discovery'].get('date_only_documents') or []:
        if rule['source_id'] != source_id:
            continue
        try:
            stamp = datetime.fromisoformat(published.replace('Z', '+00:00')) if isinstance(published, str) else published
        except ValueError:
            return None
        local = (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).astimezone(ZoneInfo(rule['timezone']))
        if (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0):
            return local.date().isoformat()
    return None


def topic_labels(topics: list[str]) -> list[str]:
    """Plain names of matched sanctions topics ('SANCTIONS_RUSSIA' -> 'Russia', config program_topic_labels)."""
    labels = rules_view()['discovery'].get('program_topic_labels') or {}
    names = [str(labels.get(t) or t.removeprefix('SANCTIONS_').replace('_', ' ').title()) for t in topics]
    return list(dict.fromkeys(names))


def counted(number: int, action: str) -> str:
    """'1 addition', '2 removals': SLS entity actions counted in words."""
    singular, plural = ACTION_WORDS.get(action, (action, action))
    return f'{number} {singular if number == 1 else plural}'


# Wording of documents stored by earlier versions of these adapters, restated at read time as the current adapters write
# it (the stored rows are never rewritten): topic codes, ISO dates and times, '1 amendments' and bare action codes.
LEGACY_TOPICS = re.compile(r'Programs matched: ([A-Z][A-Z0-9]*_[A-Z0-9_]+(?:, [A-Z][A-Z0-9]*_[A-Z0-9_]+)*)\.')
LEGACY_ACTION_DAY = re.compile(r" on (\d{4}-\d{2}-\d{2})\.(?= Programs matched:| Timing:)")
LEGACY_FILING_DAY = re.compile(r' with the SEC on (\d{4}-\d{2}-\d{2}) \(accession ')
LEGACY_SLS_TIME = re.compile(r'^(OFAC Sanctions List Service publication \d+ \([^)\n]*\)) at '
                             r'(\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:\d{2})):')
LEGACY_SLS_ONE = re.compile(r'(?<![\d.])1 (addition|removal|amendment)s\b')
LEGACY_SLS_LINE = re.compile(r'^(add|remove|update): ', re.MULTILINE)
SLS_PREFIXES = ('OFAC Sanctions List Service publication ', 'OFAC sanctions list update (publication ')


def restate_legacy(text: str) -> str:
    """Document text as the current adapters write it: 'Programs matched: SANCTIONS_RUSSIA' -> 'Russia', an OFAC or
    EDGAR ISO date -> '17 Sep 2026', an SLS publication time -> '13:32 EDT on 17 Sep 2026', '1 amendments' ->
    '1 amendment' and 'add:' -> 'Added:'. Text that is already current (or not from these adapters) is unchanged."""
    if 'Programs matched: ' in text:
        text = LEGACY_TOPICS.sub(lambda m: f"Programs matched: {', '.join(topic_labels(m[1].split(', ')))}.", text)
    if text.startswith("OFAC published '"):
        text = LEGACY_ACTION_DAY.sub(lambda m: f' on {day_text(m[1])}.', text, count=1)
    if ' with the SEC on ' in text:
        text = LEGACY_FILING_DAY.sub(lambda m: f' with the SEC on {day_text(m[1])} (accession ', text, count=1)
    if text.startswith(SLS_PREFIXES):
        zone = rules_view()['discovery']['ofac_sls']['history_timezone']
        text = LEGACY_SLS_TIME.sub(lambda m: f'{m[1]} at {time_text(m[2], zone)}:', text, count=1)
        text = LEGACY_SLS_LINE.sub(lambda m: f'{ACTION_LABELS[m[1]]}: ', LEGACY_SLS_ONE.sub(r'1 \1', text))
    return text


def _program_matches(texts: list[str]) -> tuple[list[str], list[str]]:
    """Configured sanctions-program keywords -> (topics, assets). Nothing is inferred beyond the configured map."""
    topics: set[str] = set()
    assets: set[str] = set()
    upper = ' '.join(texts).upper()
    for rule in rules()['discovery']['program_assets']:
        if rule['match'] in upper:
            topics.add(rule['topic'])
            assets.update(rule['assets'])
    return sorted(topics), sorted(assets)


# --- local rate guard -------------------------------------------------------------------------------------


class PacedGuard:
    """Documented limits live in config: spaces calls by min_interval and refuses beyond a sliding window budget
    (before the vendor has to). Refusal is a ProviderUnavailable with status 429."""

    def __init__(self, name: str, limits: dict, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        self.name, self.clock, self.sleep = name, clock, sleep
        self.min_interval = float(limits.get('min_interval_seconds', 0))
        self.max_calls, self.per_seconds = int(limits['max_calls']), float(limits['per_seconds'])
        self.calls: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            now = self.clock()
            while self.calls and now - self.calls[0] >= self.per_seconds:
                self.calls.popleft()
            if len(self.calls) >= self.max_calls:
                raise ProviderUnavailable(
                    f'{self.name}: local rate guard reached ({self.max_calls} calls per {self.per_seconds:g}s)', 429)
            if self.calls:
                wait = self.min_interval - (now - self.calls[-1])
                if wait > 0:
                    await self.sleep(wait)
                    now = self.clock()
            self.calls.append(now)


class GuardedProvider(HTTPProvider):
    """HTTPProvider (honest headers, health, circuit breaker) plus the configured rate guard."""

    def __init__(self, name: str, section: str, *, configured: bool = True, headers: dict | None = None):
        config = _config(section)
        super().__init__(name, config['base_url'], '', configured, timeout=float(config['timeout_seconds']),
                         headers=headers)
        self.section = section
        self.guard = PacedGuard(name, config['rate_limit'])
        self.health.update(rate_limit=config['rate_limit']['documented'], live=False)

    async def get(self, path: str, **kwargs):
        if not self.configured:
            raise ProviderUnavailable(f'{self.name}: NOT CONFIGURED')
        await self.guard.acquire()
        return await self.request('GET', path, **kwargs)


# --- GDELT 2.0 raw 15-minute export ----------------------------------------------------------------------

# GDELT 2.0 Event export: 61 tab-separated columns (GDELT 2.0 Event Codebook); only the used ones are mapped.
GDELT_COLUMNS = {'global_event_id': 0, 'sql_date': 1, 'actor1_code': 5, 'actor1_name': 6, 'actor1_country': 7,
                 'actor2_code': 15, 'actor2_name': 16, 'actor2_country': 17, 'is_root_event': 25, 'event_code': 26,
                 'event_base_code': 27, 'root_code': 28, 'quad_class': 29, 'goldstein': 30, 'num_mentions': 31,
                 'num_sources': 32, 'num_articles': 33, 'avg_tone': 34, 'actor1_geo_country': 37,
                 'actor2_geo_country': 45, 'action_geo_name': 52, 'action_country': 53, 'action_lat': 56,
                 'action_lon': 57, 'date_added': 59, 'source_url': 60}
GDELT_WIDTH = 61
LASTUPDATE_LINE = re.compile(r'^(?P<size>\d{1,12}) (?P<md5>[0-9a-f]{32}) '
                             r'https?://data\.gdeltproject\.org/gdeltv2/(?P<batch>\d{14})\.export\.CSV\.zip$')


class GdeltEventRow(ProviderRow):
    global_event_id: str = Field(pattern=r'^\d{1,20}$')
    sql_date: str = Field(pattern=r'^\d{8}$')
    actor1_code: str | None = Field(default=None, max_length=64)
    actor1_name: str | None = Field(default=None, max_length=255)
    actor1_country: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    actor2_code: str | None = Field(default=None, max_length=64)
    actor2_name: str | None = Field(default=None, max_length=255)
    actor2_country: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    is_root_event: int = Field(ge=0, le=1)
    event_code: str = Field(pattern=r'^\d{2,4}$')
    event_base_code: str | None = Field(default=None, pattern=r'^\d{2,4}$')
    root_code: str = Field(pattern=r'^\d{2}$')
    quad_class: int = Field(ge=1, le=4)
    goldstein: float = Field(ge=-10, le=10)
    num_mentions: int = Field(ge=0)
    num_sources: int = Field(ge=0)
    num_articles: int = Field(ge=0)
    avg_tone: float = Field(ge=-100, le=100)
    actor1_geo_country: str | None = Field(default=None, pattern=r'^[A-Z]{2}$')
    actor2_geo_country: str | None = Field(default=None, pattern=r'^[A-Z]{2}$')
    action_geo_name: str | None = Field(default=None, max_length=255)
    action_country: str | None = Field(default=None, pattern=r'^[A-Z]{2}$')
    action_lat: float | None = Field(default=None, ge=-90, le=90)
    action_lon: float | None = Field(default=None, ge=-180, le=180)
    date_added: str = Field(pattern=r'^\d{14}$')
    source_url: HttpUrl

    @field_validator('sql_date')
    @classmethod
    def real_day(cls, value: str) -> str:
        datetime.strptime(value, '%Y%m%d')
        return value

    @field_validator('date_added')
    @classmethod
    def real_stamp(cls, value: str) -> str:
        datetime.strptime(value, '%Y%m%d%H%M%S')
        return value


def gdelt_stamp(value: str) -> datetime:
    """GDELT YYYYMMDDHHMMSS stamps are UTC (GDELT 2.0 codebook)."""
    return parse_utc(f'{value[:4]}-{value[4:6]}-{value[6:8]}T{value[8:10]}:{value[10:12]}:{value[12:14]}Z')


def parse_lastupdate(text: str) -> dict:
    """The export line of lastupdate.txt; the host is pinned to data.gdeltproject.org."""
    for line in text.splitlines():
        match = LASTUPDATE_LINE.match(line.strip())
        if match:
            return {'size': int(match['size']), 'md5': match['md5'], 'batch': match['batch'],
                    'path': f"/gdeltv2/{match['batch']}.export.CSV.zip"}
    raise ValueError('GDELT lastupdate.txt has no export line on data.gdeltproject.org')


def gdelt_rows(archive: bytes, batch: str, max_csv_bytes: int) -> list[dict]:
    """Unzip one export batch (single member, bounded size) into column dicts; blanks become None."""
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        members = bundle.infolist()
        if len(members) != 1 or members[0].filename != f'{batch}.export.CSV':
            raise ValueError('Unexpected GDELT archive layout')
        if members[0].file_size > max_csv_bytes:
            raise ValueError('GDELT export exceeds size limit')
        text = bundle.read(members[0]).decode('utf-8', errors='replace')
    rows: list[dict] = []
    for fields in csv.reader(io.StringIO(text), delimiter='\t', quoting=csv.QUOTE_NONE):
        if len(fields) != GDELT_WIDTH:
            rows.append({})  # counted as rejected by validation
            continue
        rows.append({name: (fields[index].strip() or None) for name, index in GDELT_COLUMNS.items()})
    return rows


def gdelt_summary(rows: list[dict], config: dict, batch: str, as_of: datetime) -> dict:
    """Filter validated rows into discovery candidates and a per-country intensity summary."""
    countries = config['countries']
    roots, conflict, coercive = set(config['cameo_root_codes']), set(config['conflict_root_codes']), set(config['coercive_root_codes'])
    labels = config['cameo_root_labels']
    added = gdelt_stamp(batch)
    intensity: dict[str, dict] = {}
    best: dict[str, dict] = {}
    for row in rows:
        event_day = datetime.strptime(row['sql_date'], '%Y%m%d').date()
        age = (gdelt_stamp(row['date_added']).date() - event_day).days
        if not -1 <= age <= config['max_event_age_days']:
            continue  # an old event re-mentioned today is not new
        matched = [c for c in countries if row.get('action_country') == c['fips']
                   or c['fips'] in {row.get('actor1_geo_country'), row.get('actor2_geo_country')}
                   or c['iso3'] in {row.get('actor1_country'), row.get('actor2_country')}]
        if not matched:
            continue
        for country in {c['label']: c for c in matched}.values():
            stats = intensity.setdefault(country['label'], {'events': 0, 'conflict_events': 0, 'coercive_events': 0,
                                                            'mentions': 0, '_goldstein': 0.0, '_tone': 0.0})
            stats['events'] += 1
            stats['conflict_events'] += row['root_code'] in conflict
            stats['coercive_events'] += row['root_code'] in coercive
            stats['mentions'] += row['num_mentions']
            stats['_goldstein'] += row['goldstein']
            stats['_tone'] += row['avg_tone']
        if row['root_code'] not in roots or row['num_mentions'] < config['min_num_mentions']:
            continue
        url = row['source_url']
        actor1 = row.get('actor1_name') or row.get('actor1_code') or 'unidentified actor'
        actor2 = row.get('actor2_name') or row.get('actor2_code')
        root_label = labels.get(row['root_code'], row['root_code'])
        candidate = {
            'candidate_id': f"gdelt:{row['global_event_id']}", 'provider': 'gdelt_events', 'kind': 'GDELT_EVENT',
            'global_event_id': row['global_event_id'], 'event_code': row['event_code'], 'root_code': row['root_code'],
            'root_label': root_label, 'quad_class': row['quad_class'], 'goldstein': row['goldstein'],
            'avg_tone': round(row['avg_tone'], 3), 'num_mentions': row['num_mentions'], 'num_sources': row['num_sources'],
            'num_articles': row['num_articles'], 'actor1': actor1, 'actor2': actor2,
            'action_geo': row.get('action_geo_name'), 'countries': sorted({c['label'] for c in matched}),
            'topics': sorted({t for c in matched for t in c['topics']}),
            'title': f"{root_label}: {actor1}{' -> ' + actor2 if actor2 else ''}"
                     f"{' (' + row['action_geo_name'] + ')' if row.get('action_geo_name') else ''}"[:300],
            'event_date': event_day.isoformat(), 'date_added_utc': _iso(gdelt_stamp(row['date_added'])),
            'batch_id': batch, 'batch_label_utc': _iso(added), 'as_of': _iso(as_of),
            'as_of_basis': 'GDELT export file publication time (Last-Modified), batch label otherwise',
            'source_url': url, 'source_domain': urlsplit(url).hostname or '',
            'discovery_only': True, 'verification_status': 'UNCONFIRMED', 'live': False,
            'delay': config['delay_label'], 'attribution': config['attribution'],
            'note': 'Machine-coded from one news article: a lead to verify against Tier-1/2 sources, never confirmation.'}
        previous = best.get(url)
        if previous is None or (candidate['num_mentions'], abs(candidate['goldstein'])) > \
                (previous['num_mentions'], abs(previous['goldstein'])):
            best[url] = candidate
    for stats in intensity.values():
        count = stats['events']
        stats['goldstein_mean'] = round(stats.pop('_goldstein') / count, 3)
        stats['tone_mean'] = round(stats.pop('_tone') / count, 3)
    candidates = sorted(best.values(), key=lambda c: (-c['num_mentions'], -abs(c['goldstein']), c['global_event_id']))
    return {'candidates': candidates[:config['max_candidates']], 'intensity': dict(sorted(intensity.items()))}


class GdeltEventsProvider(GuardedProvider):
    """Polls the newest GDELT 2.0 export batch (about 20-40 KB zipped). Mentions/GKG files are not fetched."""

    def __init__(self):
        super().__init__('gdelt_events', 'gdelt')
        self.last_batch: str | None = None

    async def batch(self) -> dict:
        """Newest export batch not yet processed. lastupdate.txt sometimes lists a batch whose export file is not
        downloadable yet (HTTP 404); then up to `fallback_batches` earlier 15-minute batches are tried. Only the
        listed batch has a published MD5 (verified); a fallback batch is checked by the zip CRC only, and says so."""
        config = _config('gdelt')
        listing = parse_lastupdate((await self.get(config['lastupdate_path'])).text)
        base = {'provider': self.name, 'listed_batch_id': listing['batch'], 'live': False, 'delay': config['delay_label'],
                'attribution': config['attribution'], 'candidates': [], 'intensity': {}}
        if listing['batch'] == self.last_batch:
            return dict(base, status='UNCHANGED', batch_id=listing['batch'], as_of=None)
        if listing['size'] > config['max_zip_bytes']:
            raise ValueError('GDELT export exceeds size limit')
        label = gdelt_stamp(listing['batch'])
        for behind in range(int(config['fallback_batches']) + 1):
            batch_id = (label - timedelta(minutes=15 * behind)).strftime('%Y%m%d%H%M%S')
            if self.last_batch and batch_id <= self.last_batch:
                # Newer listed batches are not downloadable yet, and this one was already processed.
                self.health['note'] = (f'listed batch {listing["batch"]} not downloadable (HTTP 404); '
                                       f'newest processed batch is still {self.last_batch}')
                return dict(base, status='UNCHANGED', batch_id=self.last_batch, as_of=None)
            try:
                response = await self.get(f'/gdeltv2/{batch_id}.export.CSV.zip')
            except ProviderUnavailable as exc:
                if exc.status_code != 404:
                    raise
                continue
            archive = response.content
            if len(archive) > config['max_zip_bytes']:
                raise ValueError('GDELT export exceeds size limit')
            if behind == 0 and hashlib.md5(archive, usedforsecurity=False).hexdigest() != listing['md5']:
                raise ValueError('GDELT export checksum mismatch')
            as_of = min(gdelt_stamp(batch_id), utcnow())
            modified = response.headers.get('Last-Modified')
            if modified:
                try:
                    as_of = parsedate_to_datetime(modified).astimezone(UTC)
                except (TypeError, ValueError):
                    pass
            rows = validate_rows(GdeltEventRow, gdelt_rows(archive, batch_id, config['max_csv_bytes']), self.health)
            summary = gdelt_summary(rows, config, batch_id, as_of)
            self.last_batch = batch_id
            if behind:
                self.health['note'] = f'listed batch {listing["batch"]} not downloadable (HTTP 404); used {batch_id}'
            else:
                self.health.pop('note', None)
            return dict(base, status='OK' if behind == 0 else 'OK_FALLBACK', batch_id=batch_id, batches_behind=behind,
                        checksum='md5 (lastupdate.txt)' if behind == 0 else 'zip CRC only (batch not in lastupdate.txt)',
                        as_of=_iso(as_of), rows_total=len(rows), **summary)
        self.health['error'] = 'listed GDELT batch (and fallbacks) not downloadable yet (HTTP 404)'
        return dict(base, status='BATCH_NOT_PUBLISHED', batch_id=listing['batch'], as_of=None)

    async def candidates(self) -> list[dict]:
        return (await self.batch())['candidates']


# --- SEC EDGAR full-text search --------------------------------------------------------------------------

ADSH = r'^\d{10}-\d{2}-\d{6}$'
TICKERS = re.compile(r'\(([A-Z0-9.\-]{1,10}(?:,\s*[A-Z0-9.\-]{1,10})*)\)\s*\(CIK')


def sec_contact_configured(user_agent: str) -> bool:
    """SEC fair access requires a declared User-Agent with a contact e-mail; placeholders do not count."""
    return bool(re.search(r'\S+@\S+\.\S+', user_agent)) and 'example.com' not in user_agent.lower()


class EdgarHit(ProviderRow):
    id: str = Field(pattern=r'^\d{10}-\d{2}-\d{6}:[A-Za-z0-9._\-]{1,200}$')
    adsh: str = Field(pattern=ADSH)
    ciks: list[Annotated[str, Field(pattern=r'^\d{10}$')]] = Field(min_length=1)
    display_names: list[str] = Field(default_factory=list)
    form: str = Field(min_length=1, max_length=20)
    file_date: date
    items: list[str] | None = None
    file_type: str | None = Field(default=None, max_length=40)

    @field_validator('items')
    @classmethod
    def item_codes(cls, value: list[str] | None) -> list[str] | None:
        if value and any(not re.fullmatch(r'\d{1,2}\.\d{2}', item) for item in value):
            raise ValueError('Invalid 8-K item code')
        return value


class EdgarFullTextProvider(GuardedProvider):
    """EDGAR full-text search for configured queries. Hits are keyword matches in official filings."""

    def __init__(self, user_agent: str | None = None):
        agent = (user_agent if user_agent is not None else os.environ.get(_config('edgar_fts')['user_agent_env'], '')).strip()
        configured = sec_contact_configured(agent)
        super().__init__('sec_fulltext', 'edgar_fts', configured=configured,
                         headers={'User-Agent': agent, 'Accept': 'application/json'} if configured else None)

    @staticmethod
    def tracked_ciks(config: dict | None = None) -> dict[str, str]:
        watch = (config or rules())['provider_routing']['sec']['cik_watchlist']
        return {cik.zfill(10): ticker for ticker, cik in watch.items()}

    async def search(self, query: dict, start: date, end: date) -> list[dict]:
        config = _config('edgar_fts')
        params: dict[str, str] = {'dateRange': 'custom', 'startdt': start.isoformat(), 'enddt': end.isoformat()}
        if query.get('q'):
            params['q'] = query['q']
        if query.get('forms'):
            params['forms'] = ','.join(query['forms'])
        if query.get('ciks') == 'tracked':
            params['ciks'] = ','.join(sorted(self.tracked_ciks()))
        body = (await self.get(config['search_path'], params=params)).json()
        hits = (body.get('hits') or {}).get('hits') if isinstance(body, dict) else None
        if not isinstance(hits, list):
            raise ValueError('Invalid EDGAR full-text payload')
        total = (body.get('hits') or {}).get('total')
        total = total.get('value') if isinstance(total, dict) else total
        self.health.setdefault('query_totals', {})[query['id']] = total
        rows = [dict(hit['_source'], id=hit.get('_id')) for hit in hits
                if isinstance(hit, dict) and isinstance(hit.get('_source'), dict)]
        required = set(query.get('require_items') or [])
        return [row for row in validate_rows(EdgarHit, rows, self.health)
                if not required or required & set(row.get('items') or [])]

    def record(self, row: dict, query: dict) -> dict:
        config = _config('edgar_fts')
        tracked = self.tracked_ciks()
        cik = next((c for c in row['ciks'] if c in tracked), row['ciks'][0])
        display = next(iter(row['display_names']), '')
        match = TICKERS.search(display)
        tickers = [t.strip() for t in match.group(1).split(',')] if match else []
        company = re.sub(r'\s*\(.*$', '', display).strip() or f'CIK {cik}'
        document = row['id'].split(':', 1)[1]
        folder = f"{config['archive_url']}/Archives/edgar/data/{int(cik)}/{row['adsh'].replace('-', '')}"
        # file_date is an EDGAR (US Eastern) business day; the acceptance time is not in full-text results.
        zone = ZoneInfo(config['filing_timezone'])
        filed = datetime.combine(date.fromisoformat(row['file_date']), datetime.min.time(), zone).astimezone(UTC)
        assets = sorted({tracked[c] for c in row['ciks'] if c in tracked})
        return {'provider': self.name, 'kind': 'SEC_FULLTEXT_HIT', 'query_ids': [query['id']],
                'query_labels': [query['label']], 'event_type': query.get('event_type', 'REGULATORY_FILING'),
                'adsh': row['adsh'], 'cik': cik, 'ciks': row['ciks'], 'company': company, 'tickers': tickers,
                'form': row['form'], 'file_date': row['file_date'], 'items': row.get('items') or [],
                'document': document, 'file_type': row.get('file_type'), 'url': f'{folder}/{document}',
                'index_url': f"{folder}/{row['adsh']}-index.htm", 'assets': assets, 'as_of': _iso(filed),
                'as_of_basis': f"EDGAR file_date (date only, start of day {config['filing_timezone']}; "
                               'acceptance time is not in full-text results)',
                'delay': config['delay_label'], 'live': False, 'attribution': config['attribution']}

    async def records(self, today: date | None = None) -> list[dict]:
        config = _config('edgar_fts')
        end = today or utcnow().date()
        start = end - timedelta(days=int(config['lookback_days']))
        merged: dict[str, dict] = {}
        for query in config['queries']:
            for row in await self.search(query, start, end):
                record = self.record(row, query)
                current = merged.get(row['adsh'])
                if current is None:
                    merged[row['adsh']] = record
                    continue
                for key in ('query_ids', 'query_labels'):
                    current[key] = list(dict.fromkeys(current[key] + record[key]))
                # One document per filing: prefer the primary document (file type equals the form) over exhibits.
                if record['file_type'] == record['form'] and current['file_type'] != current['form']:
                    current.update({k: record[k] for k in ('document', 'file_type', 'url')})
        return sorted(merged.values(), key=lambda r: (r['file_date'], r['adsh']), reverse=True)

    @staticmethod
    def document(record: dict) -> dict:
        tickers = f" ({', '.join(record['tickers'][:3])})" if record['tickers'] else ''
        items = f" (8-K items {', '.join(record['items'])})" if record['items'] else ''
        return {'source_id': 'sec-filings',
                'title': f"{record['company']}{tickers} filed {record['form']}{items} - EDGAR full-text match: "
                         f"{'; '.join(record['query_labels'])}"[:1000],
                'content': (f"{record['company']} filed form {record['form']}{items} with the SEC on "
                            f"{day_text(record['file_date'])} "
                            f"(accession {record['adsh']}). EDGAR full-text search matched: {'; '.join(record['query_labels'])}. "
                            f"This is a keyword match in the filing text, not an interpretation of it. "
                            f"Timing: {record['delay']}. {record['attribution']}.")[:40000],
                'url': record['url'], 'published_at': record['as_of'], 'event_type': record['event_type'],
                'assets': record['assets'], 'direct': True, 'discovery_only': False}

    async def documents(self) -> list[dict]:
        return [self.document(record) for record in await self.records()]


# --- OFAC Recent Actions ---------------------------------------------------------------------------------

ACTION_LINK = re.compile(r'<a href="/recent-actions/(?P<slug>\d{8}(?:[_-]\d{1,4})?)"[^>]*>(?P<title>[^<]{1,1200})</a>')
ACTION_DATE = re.compile(r'(?P<date>(?:January|February|March|April|May|June|July|August|September|October|November|'
                         r'December) \d{1,2}, \d{4})')
ACTION_CATEGORY = re.compile(r'<a href="/recent-actions/(?P<slug>[a-z][a-z0-9-]{1,80})"[^>]*>(?P<name>[^<]{1,200})</a>')


class OfacAction(ProviderRow):
    slug: str = Field(pattern=r'^\d{8}(?:[_-]\d{1,4})?$')
    title: str = Field(min_length=5, max_length=1000)
    date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    category: str | None = Field(default=None, max_length=200)


def parse_recent_actions(page: str, limit: int) -> list[dict]:
    """Entries of the OFAC Recent Actions list: one 'views-row' block per action (link, date, category)."""
    rows: list[dict] = []
    for block in page.split('views-row')[1:]:
        link = ACTION_LINK.search(block)
        when = ACTION_DATE.search(block)
        if not link or not when:
            rows.append({})  # counted as rejected
            continue
        category = ACTION_CATEGORY.search(block[link.end():])
        rows.append({'slug': link['slug'], 'title': ' '.join(html.unescape(link['title']).split()),
                     'date': datetime.strptime(when['date'], '%B %d, %Y').date().isoformat(),
                     'category': html.unescape(category['name']).strip() if category else None})
        if len(rows) >= limit:
            break
    return rows


class OfacRecentActionsProvider(GuardedProvider):
    """ofac.treasury.gov/recent-actions (slow site: long timeout). rss.xml is NOT used: it lists program pages."""

    def __init__(self):
        super().__init__('ofac_actions', 'ofac_actions')

    async def actions(self) -> list[dict]:
        config = _config('ofac_actions')
        response = await self.get(config['path'])
        if len(response.content) > config['max_page_bytes']:
            raise ValueError('OFAC page exceeds size limit')
        zone = ZoneInfo(config['publication_timezone'])
        result = []
        for row in validate_rows(OfacAction, parse_recent_actions(response.text, config['max_actions']), self.health):
            day = date.fromisoformat(row['date'])
            published = datetime.combine(day, datetime.min.time(), zone).astimezone(UTC)
            topics, assets = _program_matches([row['title']])
            result.append({'provider': self.name, 'kind': 'OFAC_RECENT_ACTION', 'action_id': row['slug'],
                           'title': row['title'], 'date': row['date'], 'category': row.get('category'),
                           'url': f"{config['base_url']}{config['path']}/{row['slug']}", 'topics': topics,
                           'assets': assets, 'published_at': _iso(published), 'as_of': _iso(published),
                           'as_of_basis': f"publication date only (start of day, {config['publication_timezone']})",
                           'delay': config['delay_label'], 'live': False, 'attribution': config['attribution']})
        return result

    @staticmethod
    def document(record: dict) -> dict:
        category = f" in {record['category']}" if record['category'] else ''
        topics = f" Programs matched: {', '.join(topic_labels(record['topics']))}." if record['topics'] else ''
        return {'source_id': 'ofac', 'title': f"OFAC: {record['title']}"[:1000],
                'content': (f"OFAC published '{record['title']}'{category} on {day_text(record['date'])}.{topics} "
                            f"Timing: {record['delay']}. {record['attribution']}.")[:40000],
                'url': record['url'], 'published_at': record['published_at'], 'event_type': 'SANCTIONS',
                'assets': record['assets'], 'direct': True, 'discovery_only': False}

    async def documents(self) -> list[dict]:
        return [self.document(record) for record in await self.actions()]


# --- OFAC Sanctions List Service deltas ------------------------------------------------------------------


class SlsPublication(ProviderRow):
    publicationID: int = Field(ge=1)
    datePublished: str = Field(min_length=10, max_length=40)

    @field_validator('datePublished')
    @classmethod
    def parseable(cls, value: str) -> str:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value


class SlsEntity(ProviderRow):
    entity_id: str = Field(pattern=r'^\d{1,12}$')
    action: str = Field(pattern=r'^[a-z]{2,20}$')
    entity_type: str | None = Field(default=None, max_length=60)
    name: str | None = Field(default=None, max_length=500)
    programs: list[str] = Field(default_factory=list)
    lists: list[str] = Field(default_factory=list)
    digital_currencies: list[str] = Field(default_factory=list)


def _texts(node, path: str) -> list[str]:
    return [(child.text or '').strip() for child in node.findall(path) if (child.text or '').strip()]


def parse_delta(content: bytes) -> tuple[dict, list[dict]]:
    """SLS DeltaFile 1.0 XML -> publication info and one row per entity action (add/remove/update)."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError('SLS delta is not well-formed XML') from exc
    info = {'date_published': root.findtext('{*}publicationInfo/{*}datePublished'),
            'publication_type': root.findtext('{*}publicationInfo/{*}publicationType')}
    rows = []
    for entity in root.findall('{*}entities/{*}entity'):
        name = None
        for candidate in entity.findall('{*}names/{*}name'):
            if (candidate.findtext('{*}isPrimary') or '').strip() == 'true':
                translation = next((t for t in candidate.findall('{*}translations/{*}translation')
                                    if (t.findtext('{*}isPrimary') or '').strip() == 'true'), None)
                if translation is not None:
                    name = (translation.findtext('{*}formattedFullName') or '').strip() or None
                break
        currencies = [text.rsplit('-', 1)[-1].strip() for text in _texts(entity, '{*}features/{*}feature/{*}type')
                      if text.startswith('Digital Currency Address')]
        # An entity without an action attribute is an amendment of some of its parts (addresses, names, ...).
        rows.append({'entity_id': entity.get('id'), 'action': entity.get('action') or 'update',
                     'entity_type': entity.findtext('{*}generalInfo/{*}entityType'), 'name': name,
                     'programs': _texts(entity, '{*}sanctionsPrograms/{*}sanctionsProgram'),
                     'lists': _texts(entity, '{*}sanctionsLists/{*}sanctionsList'), 'digital_currencies': currencies})
    return info, rows


ACTION_WORDS = {'add': ('addition', 'additions'), 'remove': ('removal', 'removals'), 'update': ('amendment', 'amendments')}
ACTION_LABELS = {'add': 'Added', 'remove': 'Removed', 'update': 'Amended'}


class OfacSanctionsListProvider(GuardedProvider):
    """SLS publication history (JSON) and per-publication delta XML. The delta endpoint answers with a redirect to
    OFAC's own short-lived pre-signed S3 URL; only the configured OFAC hosts are accepted."""

    def __init__(self):
        super().__init__('ofac_sls', 'ofac_sls')
        self.last_publication_id: int | None = None

    async def publications(self, year: int) -> list[dict]:
        config = _config('ofac_sls')
        body = (await self.get(config['history_path'].format(year=int(year)))).json()
        zone = ZoneInfo(config['history_timezone'])
        result = []
        for row in validate_rows(SlsPublication, body, self.health):
            stamp = datetime.fromisoformat(row['datePublished'].replace('Z', '+00:00'))
            # SLS history times carry no offset; they match US Eastern (checked against the delta files' S3 time).
            stamp = stamp.replace(tzinfo=zone) if stamp.tzinfo is None else stamp
            result.append({'publication_id': row['publicationID'], 'published_at': _iso(stamp)})
        return sorted(result, key=lambda p: p['publication_id'])

    async def delta(self, publication: dict) -> dict:
        config = _config('ofac_sls')
        response = await self.get(config['delta_path'].format(publication_id=int(publication['publication_id'])),
                                  follow_redirects=True)
        hosts = {urlsplit(str(hop.url)).hostname for hop in [*response.history, response]}
        if not hosts <= set(config['delta_hosts']):
            raise ValueError('SLS delta served from an unexpected host')
        if len(response.content) > config['max_delta_bytes']:
            raise ValueError('SLS delta exceeds size limit')
        info, raw = parse_delta(response.content)
        entities = validate_rows(SlsEntity, raw, self.health)
        programs = sorted({p for e in entities for p in e['programs']})
        topics, assets = _program_matches(programs)
        coins: dict[str, int] = {}
        for entity in entities:
            for code in entity['digital_currencies']:
                coins[code] = coins.get(code, 0) + 1
        mapped = config['digital_currency_assets']
        counts: dict[str, int] = {}
        for entity in entities:
            counts[entity['action']] = counts.get(entity['action'], 0) + 1
        return {'provider': self.name, 'kind': 'OFAC_SLS_DELTA', 'publication_id': publication['publication_id'],
                'published_at': publication['published_at'], 'as_of': publication['published_at'],
                'as_of_basis': 'SLS publication history time (US Eastern, converted to UTC)',
                'delta_date': (info['date_published'] or '')[:10] or None, 'publication_type': info['publication_type'],
                'counts': counts, 'programs': programs, 'topics': topics,
                'assets': sorted(set(assets) | {mapped[c] for c in coins if c in mapped}),
                'digital_currency_addresses': coins, 'entities': entities,
                'url': f"{config['base_url']}{config['delta_path'].format(publication_id=int(publication['publication_id']))}",
                'delay': config['delay_label'], 'live': False, 'attribution': config['attribution']}

    async def updates(self, year: int | None = None) -> list[dict]:
        """New publications since the last poll (oldest first); the first poll takes only the latest few."""
        config = _config('ofac_sls')
        now = utcnow()
        year = year or now.year
        publications = await self.publications(year)
        if now.month == 1 and len(publications) < config['initial_publications']:
            publications = await self.publications(year - 1) + publications
        if self.last_publication_id is None:
            fresh = publications[-int(config['initial_publications']):]
        else:
            fresh = [p for p in publications if p['publication_id'] > self.last_publication_id]
        result: list[dict] = []
        for publication in fresh[:int(config['max_publications_per_poll'])]:
            try:
                update = await self.delta(publication)
            except (ProviderUnavailable, ValueError) as exc:
                if not result:
                    raise
                # Keep what was fetched (last_publication_id already points at it); retry the rest next poll.
                self.health.update(error=f"publication {publication['publication_id']} could not be read; retried next poll",
                                   error_type=type(exc).__name__)
                break
            result.append(update)
            self.last_publication_id = publication['publication_id']
        return result

    @staticmethod
    def document(record: dict, limit: int = 25) -> dict:
        counts = ', '.join(counted(n, a) for a, n in sorted(record['counts'].items())) or 'no entity changes'
        programs = ', '.join(record['programs'][:8]) or 'no program listed'
        lines = [f"{ACTION_LABELS.get(e['action'], e['action'])}: {e.get('name') or 'entity ' + e['entity_id']} "
                 f"({e.get('entity_type') or 'type n/a'}; "
                 f"{', '.join(e['programs']) or 'no program'})" for e in record['entities'][:limit]]
        more = len(record['entities']) - len(lines)
        coins = (' Digital currency addresses listed: '
                 + ', '.join(f'{k} x{v}' for k, v in sorted(record['digital_currency_addresses'].items())) + '.'
                 if record['digital_currency_addresses'] else '')
        return {'source_id': 'ofac-sls',
                'title': f"OFAC sanctions list update (publication {record['publication_id']}): {counts} [{programs}]"[:1000],
                'content': (f"OFAC Sanctions List Service publication {record['publication_id']} "
                            f"({record.get('publication_type') or 'type n/a'}) at "
                            f"{time_text(record['published_at'], _config('ofac_sls')['history_timezone'])}: {counts}.{coins}\n"
                            + '\n'.join(lines) + (f'\n... and {more} more entities.' if more > 0 else '')
                            + f"\nTiming: {record['delay']}. {record['attribution']}.")[:40000],
                'url': record['url'], 'published_at': record['published_at'], 'event_type': 'SANCTIONS',
                'assets': record['assets'], 'direct': True, 'discovery_only': False}

    async def documents(self) -> list[dict]:
        limit = int(_config('ofac_sls')['max_entities_in_document'])
        return [self.document(record, limit) for record in await self.updates()]
