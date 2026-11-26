"""Free and keyed calendar sources (spec §15): Treasury auctions, FOMC meetings, crypto option expiries,
economic releases and earnings (FMP, keyed) and legislation (Congress.gov, keyed).

Every row uses the macro_calendar contract: name, country, category, scheduled_at (aware UTC), impact 0-10,
consensus/previous/actual when known, provider, source_url. Nothing is inferred when a source is silent.

Event-aware extras (docs/EVENT_AWARENESS.md §1, §8): event_class (taxonomy key), time_known, time_basis, time_source
(source | config | rule | none: how the clock time was obtained), reference_period, parent_event_id, sub_event, sep,
attribution, status, kind (DECISION | PRESS_CONFERENCE | MINUTES | SPEECH | TESTIMONY | PROJECTIONS | RELEASE | AUCTION |
EARNINGS | EXPIRY | STATEMENT | OTHER; null when unknown), is_decision, speaker_role / speaker (speeches and testimony)
and session (the session a date-only row is anchored to: earnings BMO / AMC, BoJ TOKYO_DAY). Calendar hygiene lives here
as pure functions: stale_ids (a provider's rows inside its fetch window that a successful refresh no longer lists),
dedupe (the same release from two providers; every metric's values survive on the kept row) and carry_lent (a
re-fetched row keeps the values an earlier dedupe lent to it)."""
import hashlib
import re
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from backend.config import rules
from backend.providers.base import HTTPProvider, ProviderUnavailable
from backend.providers.models import (
    CongressBill,
    DeribitInstrument,
    TreasuryAuction,
    parse_utc,
    validate_rows,
)

MONTHS = {m: i for i, m in enumerate(['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                                      'september', 'october', 'november', 'december'], 1)}
MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})
EXTRAS = ('event_class', 'reference_period', 'parent_event_id', 'sub_event', 'attribution', 'kind', 'speaker_role',
          'speaker', 'session')
# Per-metric values of a release row: kept on the surviving row's metrics, the headline metric lent as its scalars.
VALUE_FIELDS = ('consensus', 'previous', 'actual', 'unit', 'raw_surprise', 'z_score', 'surprise_z_score',
                'surprise_observations', 'trigger', 'actual_ingested_at')
# Bookkeeping of values lent by dedupe (carried over when the kept row is re-fetched without values).
LENT_META = ('values_source', 'lent_fields', 'headline_metric', 'metric', 'metrics', 'duplicates')


class CalendarParseError(ProviderUnavailable):
    """The official page was fetched but nothing could be parsed: raise an alert, keep the previous calendar."""


def _config() -> dict:
    return rules()['provider_routing']['calendars']


def _clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(':'))
    return time(hour, minute)


def calendar_key(row: dict) -> str:
    return hashlib.sha256(f"{row['category']}:{row['scheduled_at']}:{row['name']}:{row.get('country')}".encode()).hexdigest()


def calendar_row(name: str, country: str, category: str, scheduled: datetime, impact: int, provider: str,
                 source_url: str | None = None, **extra) -> dict:
    stamp = scheduled.astimezone(UTC).isoformat()
    row = {'name': name, 'country': country, 'category': category, 'scheduled_at': stamp, 'scheduled_utc': stamp,
           'impact': max(0, min(10, impact)), 'consensus': None, 'previous': None, 'actual': None, 'unit': None,
           'provider': provider, 'source_url': source_url, **extra}
    for field in EXTRAS:
        row.setdefault(field, None)
    row.setdefault('sep', False)
    row['kind'], row['is_decision'] = row_kind(row)
    row['id'] = calendar_key(row)
    return row


def row_kind(row: dict, config: dict | None = None) -> tuple[str | None, bool | None]:
    """(kind, is_decision) per docs/EVENT_AWARENESS.md §8. An explicit kind wins, then the sub-event (press conference,
    minutes, projections, statement), then DECISION for a decision class, then the class and the category mappings.
    None when nothing identifies the row (the event engine derives it); is_decision only for FOMC/ECB/BoE/BoJ decisions."""
    config = config or _config()
    kinds = config['kinds']
    cls, category = row.get('event_class'), row.get('category')
    sub = str(row.get('sub_event') or '').upper()
    kind = row.get('kind')
    if kind not in kinds['values']:
        if sub in kinds['sub_events']:
            kind = sub
        elif cls in config['decision_classes'] and sub in {'', 'DECISION'}:
            kind = 'DECISION'
        else:
            kind = kinds['classes'].get(str(cls)) or kinds['categories'].get(str(category))
    if kind is None:
        return None, None
    return kind, kind == 'DECISION' and cls in config['decision_classes']


# --- calendar hygiene (pure) -----------------------------------------------------------------------------------------


def row_zone(row: dict, config: dict | None = None) -> ZoneInfo:
    zones = (config or _config())['country_timezones']
    return ZoneInfo(zones.get(str(row.get('country') or ''), zones['default']))


def local_date(row: dict, config: dict | None = None) -> date | None:
    """Release date in the publisher's own timezone (a 20:00 ET decision is still that New York day)."""
    try:
        return parse_utc(str(row.get('scheduled_at'))).astimezone(row_zone(row, config)).date()
    except (TypeError, ValueError):
        return None


def active(row: dict, config: dict | None = None) -> bool:
    """A scheduled row still expected to happen (suspended, cancelled or postponed rows are not)."""
    return str(row.get('status') or 'SCHEDULED').upper() not in set((config or _config())['inactive_statuses'])


def timed(row: dict, config: dict | None = None) -> bool:
    """Exact clock time known: date-only rows (time_known False) never get minute-level windows. Legacy rows
    without the field keep their previous (timed) behaviour."""
    return row.get('time_known') is not False and active(row, config)


def time_rank(row: dict, config: dict | None = None) -> int:
    """exact time (published by the source or a configured standard time) > rule time > date only."""
    ranks = (config or _config())['dedupe']['time_rank']
    if row.get('time_known') is False:
        return int(ranks['none'])
    return int(ranks.get(str(row.get('time_source') or 'source'), ranks['source']))


def dedupe_key(row: dict, config: dict | None = None) -> tuple | None:
    """(event_class, sub_event, symbol, local date, speaker): one release per publisher day; speeches and testimony are
    also keyed by their speaker, so two appearances on one day stay separate. Rows without a class, stubs and inactive
    rows are never merged."""
    config = config or _config()
    day = local_date(row, config)
    if not row.get('event_class') or row.get('stub') or not active(row, config) or day is None:
        return None
    speaker = ''
    if row.get('kind') in config['dedupe']['speech_kinds']:
        speaker = ' '.join(str(row.get('speaker') or row.get('title') or row.get('name') or '').casefold().split())
    return row['event_class'], row.get('sub_event'), row.get('symbol'), day.isoformat(), speaker


def _priority(row: dict, order: list[str]) -> int:
    provider = str(row.get('provider') or '')
    return order.index(provider) if provider in order else len(order)


def has_values(row: dict) -> bool:
    return row.get('consensus') is not None or row.get('actual') is not None


def _own_values(row: dict) -> bool:
    """The row published a consensus or actual itself (not lent by an earlier dedupe)."""
    lent = set(row.get('lent_fields') or [])
    return any(row.get(k) is not None and k not in lent for k in ('consensus', 'actual'))


def metric_of(row: dict) -> dict:
    """One metric of a release: its name, provider, row id and the value fields it carries."""
    return {'name': str(row.get('name') or ''), 'provider': row.get('provider'), 'row_id': row.get('id'),
            **{k: row[k] for k in VALUE_FIELDS if row.get(k) is not None}}


def headline_metric(event_class: Any, metrics: list[dict], config: dict | None = None) -> dict | None:
    """The metric whose values the kept row carries as scalars: the first configured pattern of the class that names a
    metric, else a lone metric; several unmatched metrics give none (never guessed)."""
    patterns = (config or _config())['dedupe']['headline_metrics'].get(str(event_class), [])
    for pattern in patterns:
        found = next((m for m in metrics if re.search(pattern, m['name'], re.IGNORECASE)), None)
        if found is not None:
            return found
    return metrics[0] if len(metrics) == 1 else None


def _merge(best: dict, others: list[dict], config: dict, order: list[str]) -> dict:
    """best plus the per-metric values of the other valued rows of its release (and of an earlier merge). Every metric is
    listed in `metrics` and the headline named in headline_metric; when best published no values itself, the headline
    metric's values are lent to it (values_source, lent_fields, metric = the headline's name). A structured vendor
    estimate (Finnhub `estimates`) is kept: only the actual is lent."""
    lent = set(best.get('lent_fields') or [])
    result = {k: v for k, v in best.items() if k not in LENT_META}
    result.update({k: None for k in lent})
    metrics: dict[str, dict] = {str(m.get('name') or '').casefold(): m for m in best.get('metrics') or []
                                if isinstance(m, dict) and m.get('row_id') != best.get('id')}
    for row in sorted(others, key=lambda r: (_priority(r, order), str(r.get('name')), str(r.get('id'))), reverse=True):
        metrics[str(row.get('name') or '').casefold()] = metric_of(row)  # the higher priority is written last
    if _own_values(result):
        metrics[str(result.get('name') or '').casefold()] = metric_of(result)
    listed = sorted(metrics.values(), key=lambda m: (_priority(m, order), m['name']))
    head = headline_metric(result.get('event_class'), listed, config)
    if head is not None:
        listed = [head, *(m for m in listed if m is not head)]
    if listed:
        result['metrics'] = listed
    if head is not None:
        result['headline_metric'] = head['name']
    if head is not None and not _own_values(result) and head.get('row_id') != result.get('id'):
        fields = config['dedupe']['structured_estimate_lendable'] if result.get('estimates') else VALUE_FIELDS
        lending = [k for k in fields if head.get(k) is not None and result.get(k) is None]
        if {'consensus', 'actual'} & set(lending):
            result.update({k: head[k] for k in lending}, values_source=head.get('provider'), lent_fields=lending,
                          metric=head['name'])
    if best.get('duplicates'):
        result['duplicates'] = best['duplicates']
    return result


def dedupe(rows: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """(kept, dropped). Within one key the best row wins: exact time over a rule time over a date, then the configured
    provider priority (official first). Rows of the winning provider are all kept; rows of other providers are dropped.
    No value is lost: when other rows of the release carry a consensus or an actual (FMP CPI MoM / YoY / core next to the
    BLS schedule), every metric goes to the kept row's `metrics` and the headline metric is lent as its scalars."""
    config = _config()
    order = list(config['dedupe']['provider_priority'])
    groups: dict[tuple, list[dict]] = {}
    kept: list[dict] = []
    for row in rows:
        key = dedupe_key(row, config)
        if key is None:
            kept.append(row)
        else:
            groups.setdefault(key, []).append(row)
    dropped: list[dict] = []
    for group in groups.values():
        best = min(group, key=lambda r: (-time_rank(r, config), _priority(r, order), str(r.get('id'))))
        winners = [r for r in group if r.get('provider') == best.get('provider')]
        losers = [r for r in group if r.get('provider') != best.get('provider')]
        others = [r for r in group if r is not best and has_values(r)]
        if others:
            merged = _merge(best, others, config, order)
            shared = {k: merged[k] for k in ('metrics', 'headline_metric') if k in merged}
            winners = [merged if r is best else dict(r, **shared) for r in winners]
        if losers:
            winners = [dict(r, duplicates=sorted({str(x.get('provider')) for x in losers})) for r in winners]
        kept += winners
        dropped += losers
    return kept, dropped


def carry_lent(previous: dict | None, row: dict) -> dict:
    """A re-fetched row that has no values of its own keeps what an earlier dedupe lent to it (headline values, metrics,
    duplicates): the vendor rows those values came from are no longer stored."""
    if not previous or has_values(row) or not (previous.get('values_source') or previous.get('metrics')):
        return row
    carried = {k: previous[k] for k in previous.get('lent_fields') or [] if previous.get(k) is not None}
    carried.update({k: previous[k] for k in LENT_META if previous.get(k) is not None and row.get(k) is None})
    return dict(row, **carried)


def stale_ids(stored: Iterable[dict], provider: str, fetched_ids: set[str], start: date, end: date,
              scope: Callable[[dict], bool] | None = None) -> list[str]:
    """Rows a successful fetch of `provider` replaces: its stored rows strictly inside the fetch window (the edge days
    are skipped: a provider filters by local date, the store by UTC date) that the fetch no longer lists. Rows with an
    actual are history and are kept."""
    result = []
    for row in stored:
        if row.get('provider') != provider or str(row.get('id')) in fetched_ids or row.get('actual') is not None:
            continue
        try:
            day = parse_utc(str(row.get('scheduled_at'))).date()
        except (TypeError, ValueError):
            continue
        if start < day < end and (scope is None or scope(row)):
            result.append(str(row['id']))
    return result


def classify(name: str, country: str | None) -> dict[str, Any]:
    """event_class (and sub_event) of a vendor row from configured name patterns; {} when nothing matches."""
    for rule in _config()['event_class_patterns']:
        if rule.get('country') and rule['country'] != country:
            continue
        if re.search(rule['pattern'], name, re.IGNORECASE):
            return {'event_class': rule['event_class'], 'sub_event': rule.get('sub_event')}
    return {}


def auction_class(term: str, security_type: str, flags: Iterable[Any] = ()) -> str | None:
    """'10-Year' or a reopening '9-Year 10-Month' Note -> UST_AUCTION_10Y (next benchmark tenor); bills, TIPS and FRNs
    have no class."""
    config = _config()['auction_classes']
    if security_type not in config['types'] or any(str(f).strip().lower() in {'yes', 'y', 'true'} for f in flags):
        return None
    match = re.match(r'\s*(\d+)-Year(?:\s+(\d+)-Month)?', term)
    if not match:
        return None
    years = int(match.group(1)) + (1 if match.group(2) and int(match.group(2)) > 0 else 0)
    tenor = next((t for t in config['benchmarks'] if t >= years), None)
    return config['class_format'].format(tenor=tenor) if tenor is not None else None


class TreasuryAuctionCalendar(HTTPProvider):
    """TreasuryDirect upcoming auctions (free, official). Auction time is published per security; date only here."""

    def __init__(self):
        super().__init__('treasury_auctions', 'https://www.treasurydirect.gov')

    async def calendar(self, start: str, end: str) -> list[dict]:
        response = await self.request('GET', '/TA_WS/securities/upcoming', params={'format': 'json'})
        impacts = _config()['auction_impact']
        rows = []
        for auction in validate_rows(TreasuryAuction, response.json(), self.health):
            day = date.fromisoformat(auction['auctionDate'][:10])
            if not start <= day.isoformat() <= end:
                continue
            closing = auction.get('closingTimeCompetitive') or ''
            known = bool(re.fullmatch(r'\d{1,2}:\d{2} [AP]M', closing))
            # Competitive bidding close as published by TreasuryDirect; without it only the date is known.
            clock = datetime.strptime(closing, '%I:%M %p').time() if known else time(0)
            scheduled = datetime.combine(day, clock, ZoneInfo('America/New_York'))
            flags = (auction.get('type') if auction.get('type') in {'TIPS', 'FRN', 'CMB'} else '', auction.get('tips'),
                     auction.get('floatingRate'))
            rows.append(calendar_row(f"{auction['securityTerm']} {auction['securityType']} auction", 'US', 'TREASURY_AUCTION',
                                     scheduled, impacts.get(auction['securityType'], 3), self.name,
                                     'https://www.treasurydirect.gov/auctions/upcoming/', cusip=auction['cusip'],
                                     time_known=known, time_source='source' if known else 'none',
                                     time_basis='competitive close (TreasuryDirect)' if known else 'date only',
                                     event_class=auction_class(auction['securityTerm'], auction['securityType'],
                                                               [f for f in flags if f]),
                                     attribution='Source: TreasuryDirect (U.S. Department of the Treasury)'))
        return rows


def parse_fomc_meetings(html: str, year: int) -> list[dict]:
    """Meetings of one year from the Federal Reserve FOMC calendar page: {decision_day (last meeting day), meeting_days,
    sep}. A trailing '*' marks a meeting with a Summary of Economic Projections (dot plot)."""
    start = html.find(f'{year} FOMC Meetings')
    if start < 0:
        return []
    following = html.find('FOMC Meetings', start + 20)
    segment = html[start:following if following > 0 else None]
    cells = re.findall(r'fomc-meeting__(month|date)[^>]*>\s*(?:<strong>)?\s*([^<]+)', segment)
    meetings = []
    for (kind_a, month), (kind_b, dates) in zip(cells[::2], cells[1::2], strict=False):
        if kind_a != 'month' or kind_b != 'date':
            continue
        names = [part.strip().casefold()[:3] for part in month.strip().split('/')]
        numbers = [int(n) for n in re.findall(r'\d+', dates)]
        if not numbers or any(name not in MONTHS for name in names):
            continue
        # 'Apr/May' with '30-1': the first day belongs to the first month, the decision day to the last one.
        first_month = MONTHS[names[0]] if len(numbers) > 1 and numbers[0] > numbers[-1] else MONTHS[names[-1]]
        try:
            days = [date(year, first_month, numbers[0]), date(year, MONTHS[names[-1]], numbers[-1])] \
                if len(numbers) > 1 else [date(year, MONTHS[names[-1]], numbers[-1])]
        except ValueError:
            continue
        meetings.append({'decision_day': days[-1], 'meeting_days': days, 'sep': '*' in dates})
    return meetings


def parse_fomc(html: str, year: int) -> list[date]:
    """Decision days (last meeting day) for one year from the Federal Reserve FOMC calendar page."""
    return [meeting['decision_day'] for meeting in parse_fomc_meetings(html, year)]


class FOMCCalendar(HTTPProvider):
    """fomccalendars.htm stays the authority for decision dates. Each meeting gives the decision row (sep flag for
    projection meetings) and the Chair's press conference as its sub-event (parent_event_id = decision row id)."""

    def __init__(self):
        url = _config()['fomc_url']
        base, _, path = url.partition('/monetarypolicy')
        super().__init__('fomc_calendar', base)
        self.path = '/monetarypolicy' + path

    async def calendar(self, start: str, end: str) -> list[dict]:
        html = (await self.request('GET', self.path)).text
        config = _config()
        zone = ZoneInfo(config['fomc_timezone'])
        rows = []
        for year in sorted({int(start[:4]), int(end[:4])}):
            for meeting in parse_fomc_meetings(html, year):
                day = meeting['decision_day']
                if not start <= day.isoformat() <= end:
                    continue
                common = {'bank': 'fed', 'event_class': 'FOMC', 'time_known': True, 'time_source': 'config',
                          'attribution': config['fomc_attribution'], 'sep': meeting['sep'],
                          'meeting_days': [d.isoformat() for d in meeting['meeting_days']]}
                decision = calendar_row('FOMC rate decision', 'US', 'CENTRAL_BANK', datetime.combine(day, _clock(config['fomc_decision_time']), zone),
                                        config['fomc_impact'], self.name, config['fomc_url'], sub_event='DECISION',
                                        time_basis=config['fomc_time_basis'], **common)
                rows.append(decision)
                if config.get('fomc_press_conference_time'):
                    rows.append(calendar_row('FOMC press conference', 'US', 'CENTRAL_BANK',
                                             datetime.combine(day, _clock(config['fomc_press_conference_time']), zone),
                                             config['fomc_press_conference_impact'], self.name, config['fomc_url'],
                                             sub_event='PRESS_CONFERENCE', parent_event_id=decision['id'],
                                             time_basis=config['fomc_press_conference_time_basis'], **common))
        if not any(parse_fomc_meetings(html, year) for year in {int(start[:4]), int(end[:4])}):
            # Alert-worthy: the page answered but no meeting parsed; the stored rows are kept (no replacement).
            self.health.update(status='DEGRADED', error='parser found no FOMC meetings (page layout changed?)', alert=True)
            raise CalendarParseError(f'{self.name}: parser found no meetings')
        self.health['alert'] = False
        # Other central banks: configured meeting dates only (no guessing), each with its official source link.
        for meeting in config['central_bank_meetings']:
            scheduled = parse_utc(meeting['scheduled_at'])
            if start <= scheduled.date().isoformat() <= end:
                rows.append(calendar_row(meeting['name'], meeting['country'], 'CENTRAL_BANK', scheduled,
                                         meeting.get('impact', 9), 'configured', meeting.get('source_url')))
        return rows


def expiry_tenor(scheduled: datetime) -> str:
    """quarterly / monthly / weekly / daily for an options expiry date (config option_expiry_importance): the last
    Friday of a quarter-end month, the last Friday of any other month, any other Friday, any other day."""
    day = scheduled.astimezone(UTC).date()
    if day.weekday() != 4:
        return 'daily'
    if (day + timedelta(days=7)).month == day.month:
        return 'weekly'
    return 'quarterly' if day.month % 3 == 0 else 'monthly'


class CryptoExpiryCalendar(HTTPProvider):
    """Deribit option expiries (free public API). Size of each expiry comes from positioning, not from here. Each row
    carries its tenor and the configured importance_score of that tenor (option_expiry_importance): Deribit lists an
    expiry every day, and a routine daily expiry must not be scored like the monthly or quarterly one (EVR-8)."""

    def __init__(self):
        super().__init__('crypto_expiries', 'https://www.deribit.com/api/v2')

    async def calendar(self, start: str, end: str) -> list[dict]:
        rows = []
        for currency in rules()['provider_routing']['deribit']['currencies']:
            response = await self.request('GET', '/public/get_instruments', params={'currency': currency, 'kind': 'option', 'expired': 'false'})
            instruments = validate_rows(DeribitInstrument, response.json().get('result'), self.health)
            expiries: dict[int, int] = {}
            for item in instruments:
                expiries[item['expiration_timestamp']] = expiries.get(item['expiration_timestamp'], 0) + 1
            for stamp, count in sorted(expiries.items()):
                scheduled = datetime.fromtimestamp(stamp / 1000, UTC)
                if start <= scheduled.date().isoformat() <= end:
                    rows.append(expiry_row(currency, scheduled, count, self.name))
        return rows


def expiry_row(currency: str, scheduled: datetime, instruments: int, provider: str = 'crypto_expiries') -> dict:
    """One options-expiry calendar row with its tenor and the importance of that tenor (config
    option_expiry_importance; the event layer reads a row's importance_score before the class value)."""
    tenor = expiry_tenor(scheduled)
    importance = _config()['option_expiry_importance'][tenor]
    return calendar_row(f'{currency} options expiry', 'CRYPTO', 'OPTION_EXPIRY', scheduled, _config()['option_expiry_impact'],
                        provider, 'https://www.deribit.com/', instruments=instruments, currency=currency,
                        event_class='CRYPTO_EXPIRY', tenor=tenor, importance_score=importance, time_known=True,
                        time_source='source', time_basis='instrument expiration timestamp (Deribit)', attribution='Source: Deribit')


def fmp_economic_rows(rows: list[dict], provider: str = 'calendar') -> list[dict]:
    """FMP economic calendar rows (documented as UTC) -> contract rows with raw surprise when both values exist."""
    impact = {'High': 10, 'Medium': 5, 'Low': 1}
    result = []
    for row in rows:
        stamp = datetime.fromisoformat(row['date'].replace('Z', '+00:00'))
        stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp
        item = calendar_row(row['event'], row.get('country') or '', 'MACRO', stamp, impact.get(str(row.get('impact')), 0), provider,
                            time_known=True, time_source='source', time_basis='vendor timestamp (FMP, UTC)',
                            attribution='Source: Financial Modeling Prep', **classify(row['event'], row.get('country')))
        item.update(consensus=row.get('estimate'), previous=row.get('previous'), actual=row.get('actual'), unit=row.get('unit'))
        if isinstance(item['actual'], (int, float)) and isinstance(item['consensus'], (int, float)):
            item['raw_surprise'] = item['actual'] - item['consensus']
        result.append(item)
    return result


def fmp_earnings_rows(rows: list[dict], watch: set[str], provider: str = 'calendar') -> list[dict]:
    """Earnings of tracked/systemic companies only; FMP gives the date, the session time is not assumed."""
    result = []
    for row in rows:
        if row['symbol'] not in watch:
            continue
        day = date.fromisoformat(row['date'][:10])
        # Date only: stamped 00:00 New York so the publisher's local date is the reported date.
        item = calendar_row(f"{row['symbol']} earnings", 'US', 'EARNINGS', datetime.combine(day, time(0), ZoneInfo('America/New_York')),
                            _config()['earnings_impact'], provider, time_basis='date only; session not published',
                            symbol=row['symbol'], event_class='EARNINGS', time_known=False, time_source='none')
        item.update(consensus=row.get('epsEstimated'), actual=row.get('epsActual'), unit='EPS')
        result.append(item)
    return result


class CongressCalendar(HTTPProvider):
    """Congress.gov API (free api.data.gov key): recently actioned bills as legislative events."""

    def __init__(self, key: str, name: str = 'congress_api'):
        super().__init__(name, 'https://api.congress.gov/v3', key, bool(key))

    async def calendar(self, start: str, end: str) -> list[dict]:
        response = await self.request('GET', '/bill', params={'api_key': self.key, 'format': 'json', 'limit': 50,
                                                              'fromDateTime': f'{start}T00:00:00Z', 'toDateTime': f'{end}T23:59:59Z'})
        rows = []
        for bill in validate_rows(CongressBill, response.json().get('bills', []), self.health):
            action = bill.get('latestAction') or {}
            when = action.get('actionDate')
            if not when:
                continue
            rows.append(calendar_row(f"{bill['type']}{bill['number']}: {bill['title'][:160]}", 'US', 'LEGISLATION',
                                     datetime.combine(date.fromisoformat(when), time(0), UTC), _config()['legislation_impact'],
                                     self.name, f"https://www.congress.gov/bill/{bill['congress']}th-congress/",
                                     latest_action=action.get('text'), time_basis='action date only', time_known=False,
                                     time_source='none', attribution='Source: Congress.gov'))
        return rows
