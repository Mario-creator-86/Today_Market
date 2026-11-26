"""Deterministic event-risk layer (docs/EVENT_AWARENESS.md §2-§4; spec §2-§7, §10-§31, §36-§38).

Calendar rows and news events become EventObjects with phases, sub-events, relevance, dominance and asset exposure. The
snapshot adds EVENT RISK MODE, per-asset event risk, the pre-event overlay (adjusted_confidence, trade_status), favored /
pressured / non-confirming lists, what can break the view, trade-watch cards, scenario templates and post-event reactions.
Every number comes from configuration (config/rules.d/event_risk.json, event_exposure.json), the calendar, the causal
graph or measured ticks; no LLM computes anything here. The overlay sits beside a published decision and never rewrites it.
Missing expectations stay explicit ('NO CONSENSUS', 'UNKNOWN'); a component that cannot be computed is left out of a score
and named, never scored as zero. Unverified events are listed as potential events and never reach a score.
Field shapes follow docs/EVENT_AWARENESS.md §8: a central-bank / Fed-communication surprise is only read back from a
stored AI interpretation that cites an official document of the event (event.surprise); anything else is UNDETERMINED
and never grades a reaction or changes a trade status."""
import math
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from backend import analytics, causal, releases, sessions
from backend.database import Store, utcnow
from backend.engines import aware, bucharest
from backend.regime import OPERATORS, current_change, day_label
from backend.schemas import StrictModel
from backend.scoring import fmt, why_bullets

PHASES = ('SCHEDULED', 'APPROACHING', 'IMMINENT', 'RELEASED', 'IN_PROGRESS', 'POST_EVENT', 'RESOLVED')
UPCOMING = {'SCHEDULED', 'APPROACHING', 'IMMINENT'}
LIVE = {'RELEASED', 'IN_PROGRESS'}
POST = {'RELEASED', 'IN_PROGRESS', 'POST_EVENT'}
ACTIVE = 'ACTIVE'
NO_CONSENSUS, UNKNOWN = 'NO CONSENSUS', 'UNKNOWN'
INCOMPLETE = 'EVENT DATA INCOMPLETE'
POTENTIAL = 'POTENTIAL EVENT · UNCONFIRMED'
UNVERIFIED = {'UNCONFIRMED', 'PARTIALLY_CONFIRMED'}
ARROWS = {1: '↑', -1: '↓'}
KINDS = ('DECISION', 'PRESS_CONFERENCE', 'MINUTES', 'SPEECH', 'TESTIMONY', 'PROJECTIONS', 'RELEASE', 'AUCTION', 'EARNINGS',
         'EXPIRY', 'STATEMENT', 'OTHER')
EventKind = Literal['DECISION', 'PRESS_CONFERENCE', 'MINUTES', 'SPEECH', 'TESTIMONY', 'PROJECTIONS', 'RELEASE', 'AUCTION',
                    'EARNINGS', 'EXPIRY', 'STATEMENT', 'OTHER']
SPEAKER_ROLES = ('CHAIR', 'VICE_CHAIR', 'GOVERNOR')
CONSENSUS_LABEL, FINNHUB_LABEL = 'Consensus', 'Finnhub estimate'
Assessment = Literal['HAWKISH_SURPRISE', 'DOVISH_SURPRISE', 'NEUTRAL_PRICED', 'UNDETERMINED', 'NOT_APPLICABLE']
UNDETERMINED = 'UNDETERMINED'
_UNSET: Any = object()  # snapshot(): 'interpretation not supplied' (None means 'no interpretation')
_validated: str | None = None


def no_surprise() -> dict:
    """event.surprise when no validated, fact-backed AI classification exists (never grades)."""
    return {'assessment': UNDETERMINED, 'source': 'none', 'evidence_ids': [], 'validated': False}


# --- EventObject (spec §2) ---------------------------------------------------------------------------------------------

class Exposure(StrictModel):
    symbol: str
    level: Literal['very_high', 'high', 'medium', 'low']
    weight: float = Field(ge=0, le=1)


class ConsensusValue(StrictModel):
    name: str
    value: float | str
    unit: str | None = None


class Consensus(StrictModel):
    """docs/EVENT_AWARENESS.md §8: {label: 'Consensus' | 'Finnhub estimate', values: [{name, value, unit}], source}.
    'previous' is never an expectation (it stays in known_information)."""
    label: str
    values: list[ConsensusValue] = Field(min_length=1)
    source: str | None = None


class Surprise(StrictModel):
    """§8: only source 'ai', validated, with official-document evidence ids grades or resolves a scenario."""
    assessment: Assessment
    source: Literal['ai', 'none']
    evidence_ids: list[str]
    validated: bool


class OfficialDocument(StrictModel):
    """An official document of the event's own source (statement, press-conference transcript, minutes, speech or
    testimony text) the AI may cite by evidence_id. Its text is never copied here (the interpreter bundle carries the
    screened excerpt); the title is clipped."""
    evidence_id: str
    document_id: str | None = None
    kind: str
    title: str | None = None
    source: str | None = None
    published_at: str | None = None
    ingested_at: str | None = None
    url: str | None = None


class DateWindow(StrictModel):
    """Release window of a date-only row (session, class window or the publisher's local day)."""
    start_utc: str
    end_utc: str
    label: str
    zone: str


class SubEvent(StrictModel):
    sub_event: str
    name: str | None = None
    row_id: str | None = None
    time_utc: str | None = None
    time_bucharest: str | None = None
    time_known: bool
    time_basis: str | None = None
    phase: str


class Scenario(StrictModel):
    id: str
    label: str
    condition: str
    trigger: str | None = None
    expected: dict[str, int]
    unresolved: list[str] = Field(default_factory=list)
    basis: str
    note: str | None = None
    likelihood: Literal['possible'] = 'possible'


class EventObject(StrictModel):
    event_id: str
    row_id: str | None = None
    name: str
    event_class: str
    category: str
    kind: EventKind
    is_decision: bool
    speaker_role: Literal['CHAIR', 'VICE_CHAIR', 'GOVERNOR'] | None = None
    symbol: str | None = None
    status: str
    phase: str
    timing: Literal['SCHEDULED', 'ANYTIME']
    time_utc: str | None = None
    time_bucharest: str | None = None
    date_bucharest: str | None = None
    time_known: bool
    time_basis: str | None = None
    date_window: DateWindow | None = None
    minutes_until_event: int | None = None
    first_seen_utc: str | None = None
    published_utc: str | None = None
    published_date_only: bool | None = None
    importance_score: float | None = None
    expected_volatility_score: float | None = None
    relevance_score: float
    relevance_level: str
    relevance_components: dict[str, float | None]
    relevance_missing: list[str]
    dominance_score: float
    dominance_basis: dict[str, str]
    impact_10: float
    impact: float | None = None
    affected_assets: list[Exposure]
    consensus: Consensus | Literal['NO CONSENSUS']
    market_pricing: dict[str, Any] | Literal['UNKNOWN']
    actual: float | str | None = None
    outcome: dict[str, Any] | None = None
    known_information: list[str]
    possible_surprises: list[str]
    scenarios: list[Scenario]
    consequences: dict[str, int] = Field(default_factory=dict)
    source_ids: list[str]
    source_url: str | None = None
    attribution: str | None = None
    fact_confidence: float | None = None
    sub_events: list[SubEvent] = Field(default_factory=list)
    parent_event_id: str | None = None
    status_note: str | None = None
    data_incomplete: list[str]
    data_incomplete_flag: bool = False
    verification_status: str | None = None
    surprise_basis: dict[str, Any] | None = None
    surprise: Surprise = Field(default_factory=lambda: Surprise.model_validate(no_surprise()))
    official_documents: list[OfficialDocument] = Field(default_factory=list)
    live_assets: list[str] = Field(default_factory=list)
    awaiting_reopen: list[str] = Field(default_factory=list)


# --- configuration ---------------------------------------------------------------------------------------------------

def settings() -> dict:
    """Shared read-only merged rules (never mutate), validated once per configuration content."""
    global _validated
    config = sessions.ruleset()
    if _validated != config['content_hash']:
        validate(config)
        _validated = config['content_hash']
    return config


def _risk() -> dict:
    return settings()['event_risk']


def _exp() -> dict:
    return settings()['event_exposure']


def validate(config: dict) -> None:
    """Load-time checks in the causal.build style: known classes, assets, levels, regimes and triggers, weights summing
    to 1. A malformed fragment raises instead of scoring silently."""
    exp, risk = config['event_exposure'], config['event_risk']
    assets = {a[0] for a in config['assets']}
    classes, levels = set(exp['event_classes']), set(exp['levels'])
    known_triggers = {r['trigger'] for r in config.get('causal_rules', [])}
    regimes = {r['regime'] for r in config.get('regime', {}).get('rules', [])}
    for section in ('relevance', 'dominance'):
        weights = risk[section]['weights']
        if abs(sum(weights.values()) - 1) > 1e-6:
            raise ValueError(f'event_risk.{section}.weights must sum to 1')
    for name, spec in exp['event_classes'].items():
        template = spec.get('scenario_template')
        if template is not None and template not in exp['scenario_templates']:
            raise ValueError(f'{name}: unknown scenario template {template}')
        merged = {**exp['class_defaults'], **spec}
        if merged['kind'] not in KINDS or merged.get('speaker_role') not in (None, *SPEAKER_ROLES):
            raise ValueError(f"{name}: unknown kind {merged['kind']} or speaker role {merged.get('speaker_role')}")
    for mapping in [exp['class_aliases'], *exp['speaker_classes'].values()]:
        if set(mapping.values()) - classes:
            raise ValueError('class_aliases / speaker_classes: unknown event class')
    if set(exp['speaker_classes']) - classes or {r['role'] for r in exp['speaker_roles']} - set(SPEAKER_ROLES) \
            or any(set(m) - set(SPEAKER_ROLES) for m in exp['speaker_classes'].values()):
        raise ValueError('speaker_roles / speaker_classes: unknown class or role')
    documents = risk['surprise']['official_documents']
    for name, spec in documents['classes'].items():
        if name not in classes or set(spec['kinds']) - set(documents['kinds']):
            raise ValueError(f'surprise.official_documents: unknown class or document kind for {name}')
    if set(risk['risk_mode']['live_classes']) - classes or not 0 <= risk['asset_risk']['exposure_floor'] <= 1:
        raise ValueError('risk_mode.live_classes or asset_risk.exposure_floor out of range')
    windows = risk['phases']['date_only_sessions']
    for spec in [*windows['classes'].values(), *windows['sessions'].values()]:
        ZoneInfo(spec['zone'])
        if _clock_minutes(spec['start']) >= _clock_minutes(spec['end']):
            raise ValueError('date_only_sessions: a window must end after it starts')
    if set(windows['classes']) - classes or set(windows['session_patterns']) - set(windows['sessions']):
        raise ValueError('date_only_sessions: unknown class or session')
    for name, mapping in exp['exposure'].items():
        if name not in classes:
            raise ValueError(f'exposure for unknown event class {name}')
        for symbol, level in mapping.items():
            if symbol != '$SELF' and symbol not in assets or level not in levels:
                raise ValueError(f'{name}: unknown asset or level {symbol}={level}')
    for name, overrides in exp['self_overrides'].items():
        for mapping in overrides.values():
            if set(mapping) - assets or set(mapping.values()) - levels:
                raise ValueError(f'{name}: unknown override asset or level')
    for rule in exp['classification'] + exp['unscheduled_rules']:
        if rule['class'] not in classes:
            raise ValueError(f"classification to unknown class {rule['class']}")
    for effect in exp['regime_effects']:
        if set(effect['regimes']) - regimes or (set(effect['favored']) | set(effect['pressured'])) - assets:
            raise ValueError(f"regime effect {effect['id']}: unknown regime or asset")
    sources = {s['id'] for s in config.get('sources', [])}
    documents = risk['release_documents']
    ZoneInfo(documents['series_zone'])
    for name, spec in documents['classes'].items():
        if name not in classes or not spec['sources'] or not spec['phrases'] or (sources and set(spec['sources']) - sources):
            raise ValueError(f'release_documents: unknown class or source, or no phrase, for {name}')
        if spec.get('period_rule') not in (None, *documents['period_rules']):
            raise ValueError(f'release_documents: unknown period_rule for {name}')
        for item in spec.get('series', []):
            if item.get('show') not in releases.SHOWS or not item.get('id') or not item.get('name') \
                    or (item.get('show') == 'yoy' and not isinstance(item.get('periods'), int)):
                raise ValueError(f'release_documents: malformed series for {name}')
    for zone in risk['unscheduled']['date_only_zones']:
        ZoneInfo(zone)
    for name, mapping in exp['expiry_tenor_importance']['classes'].items():
        if name not in classes or set(mapping) != {'daily', 'weekly', 'monthly', 'quarterly'}:
            raise ValueError(f'expiry_tenor_importance: unknown class or tenor for {name}')
    for name, template in exp['scenario_templates'].items():
        for polarity in template.get('surprise_polarity', []):
            re.compile(polarity['pattern'])
            if polarity.get('sign') not in (-1, 1) or 'surprise' not in template:
                raise ValueError(f'{name}: surprise_polarity needs a surprise map and a sign of -1 or 1')
        for spec in template['scenarios']:
            if spec.get('trigger') and spec['trigger'] not in known_triggers:
                raise ValueError(f"{name}/{spec['id']}: trigger {spec['trigger']} has no causal rule")
            if set(spec.get('expected', {})) - assets - {'$SELF'}:
                raise ValueError(f"{name}/{spec['id']}: unknown asset")
    for group in exp['asset_groups'].values():
        if set(group) - assets:
            raise ValueError('asset_groups: unknown asset')


def class_config(event_class: str) -> dict:
    exp = _exp()
    return {**exp['class_defaults'], **exp['event_classes'].get(event_class, exp['event_classes']['OTHER'])}


def _windows(spec: dict) -> dict:
    phases = _risk()['phases']
    return {'approaching_minutes': spec.get('approaching_minutes', phases['approaching_minutes']),
            'imminent_minutes': spec.get('imminent_minutes', phases['imminent_minutes']),
            'released_minutes': phases['released_minutes'], 'in_progress_minutes': spec['in_progress_minutes'],
            'post_event_minutes': spec['post_event_minutes'], 'date_only_lead_hours': phases['date_only_lead_hours']}


def _clock_minutes(value: str) -> int:
    """'HH:MM' -> minutes after midnight ('24:00' is the end of the day)."""
    hours, minutes = str(value).split(':')
    total = int(hours) * 60 + int(minutes)
    if not 0 <= total <= 24 * 60:
        raise ValueError(f'invalid clock time {value}')
    return total


# --- small helpers -------------------------------------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _stamp(value: Any) -> datetime | None:
    try:
        return aware(value) if value else None
    except (TypeError, ValueError):
        return None


def _matches(pattern: str, text: str) -> bool:
    return re.search(pattern, text) is not None


def _word(phrase: str, text: str) -> bool:
    return re.search(rf'(?<!\w){re.escape(phrase.casefold())}(?!\w)', text) is not None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _local(stamp: datetime) -> tuple[str, str]:
    local = bucharest(stamp)
    return f'{local:%H:%M}', local.date().isoformat()


def _minutes(stamp: datetime, now: datetime) -> int:
    return math.floor((stamp - now).total_seconds() / 60)


def _side(label: str | None) -> int:
    """+1/-1 for long/short setup or lean labels, 0 otherwise (engines.category_side on the shared config view)."""
    for band in settings()['categories']['bands']:
        for side, key in ((1, 'long'), (-1, 'short')):
            if label in (band[key]['category'], band[key]['direction']):
                return side
    return 0


def _setup(label: str | None) -> bool:
    return any(label in (band[k]['category'], band[k]['direction']) for band in settings()['categories']['bands']
               if band['setup'] for k in ('long', 'short'))


def _unit(symbol: str) -> str:
    config = settings()
    kind = next((k for s, _, k in config['assets'] if s == symbol), None)
    units = config['market_units']
    return 'bp' if symbol in units['bp_symbols'] or kind in units['bp_asset_classes'] else 'pct'


def _noise(symbol: str) -> float:
    config = settings()
    bands = config['reaction']['tolerances']
    kind = next((k for s, _, k in config['assets'] if s == symbol), '')
    return float((bands['symbols'].get(symbol) or bands['classes'].get(kind) or bands['default'][_unit(symbol)])['noise'])


def _arrows(expected: dict[str, int], limit: int = 6) -> str:
    return ', '.join(f'{s} {ARROWS[v]}' for s, v in list(expected.items())[:limit] if v in ARROWS)


def level_for(score: float | None) -> str:
    risk = _risk()
    if score is None:
        return risk['below_level']
    return next((band['level'] for band in risk['levels'] if score >= band['min']), risk['below_level'])


def piecewise(points: list[list[float]], x: float) -> float:
    if x <= points[0][0]:
        return float(points[0][1])
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x <= x1:
            return float(y1 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return float(points[-1][1])


# --- classification and exposure (§2) -----------------------------------------------------------------------------------

def classify_row(row: dict) -> str:
    """Taxonomy key of a calendar row: its own event_class (or a known class / event_type alias), else the first
    matching configured rule (category / country / provider / name regex), else OTHER. A speech or testimony then moves
    to its speaker's class (speaker_classes): only the Chair uses the Chair-level classes."""
    exp = _exp()
    classes = exp['event_classes']
    given = row.get('event_class') or exp['event_type_aliases'].get(str(row.get('event_type') or ''), row.get('event_type'))
    given = exp['class_aliases'].get(str(given or ''), given)
    if given in classes:
        return _speaker_class(str(given), row)
    name = str(row.get('name') or '').casefold()
    for rule in exp['classification']:
        if rule.get('categories') and row.get('category') not in rule['categories']:
            continue
        if rule.get('countries') and row.get('country') not in rule['countries']:
            continue
        patterns, providers = rule.get('patterns', []), rule.get('providers', [])
        if (patterns or providers) and row.get('provider') not in providers and not any(_matches(p, name) for p in patterns):
            continue
        if any(_matches(p, name) for p in rule.get('exclude', [])):
            continue
        return _speaker_class(str(rule['class']), row)
    return 'OTHER'


def speaker_role(row: dict, event_class: str) -> str | None:
    """CHAIR | VICE_CHAIR | GOVERNOR for speeches and testimonies: the row's own speaker_role, else the first configured
    title pattern (Vice Chair before Chair), else the class default; None for every other kind."""
    spec = class_config(event_class)
    if spec['kind'] not in {'SPEECH', 'TESTIMONY'}:
        return None
    given = str(row.get('speaker_role') or '').upper()
    if given in SPEAKER_ROLES:
        return given
    name = str(row.get('name') or '').casefold()
    found = next((r['role'] for r in _exp()['speaker_roles'] if _matches(r['pattern'], name)), None)
    return found or spec.get('speaker_role')


def _speaker_class(event_class: str, row: dict) -> str:
    role = speaker_role(row, event_class)
    return str(_exp()['speaker_classes'].get(event_class, {}).get(role or '', event_class))


def sub_event_kind(row: dict) -> str | None:
    """PRESS_CONFERENCE / PROJECTIONS / ... for a row that belongs to a main event; None for a main event."""
    value = row.get('sub_event')
    if value:
        return None if str(value).upper() in {'DECISION', 'MAIN', 'STATEMENT'} else str(value).upper()
    name = str(row.get('name') or '').casefold()
    return next((kind for kind, patterns in _exp()['sub_event_patterns'].items() if any(p in name for p in patterns)), None)


def time_known(row: dict) -> bool:
    """Explicit time_known wins; a row without it is timed unless its time_basis says it is date-only."""
    if isinstance(row.get('time_known'), bool):
        return bool(row['time_known'])
    return 'date only' not in str(row.get('time_basis') or '').casefold()


def row_symbol(row: dict, event_class: str) -> str | None:
    known = {a[0] for a in settings()['assets']}
    for value in (row.get('symbol'), row.get('currency')):
        if value and str(value).upper() in known:
            return str(value).upper()
    if event_class == 'EARNINGS':
        first = str(row.get('name') or '').split(' ')[0].upper()
        return first if first in known else None
    return None


def exposure(event_class: str, symbol: str | None = None, extra: Iterable[str] = (),
             causal_weights: dict[str, float] | None = None) -> list[dict]:
    """[{symbol, level, weight}] from the class mapping ('$SELF' = the event's own symbol, plus per-symbol overrides).
    `extra` symbols (an event's own affected assets) not in the mapping get a level from their causal transmission
    weight, else 'medium'. Sorted by weight, then configuration order."""
    exp, config = _exp(), settings()
    order = [a[0] for a in config['assets']]
    levels = exp['levels']
    mapping = {s: v for s, v in exp['exposure'].get(event_class, {}).items() if s != '$SELF'}
    self_level = exp['exposure'].get(event_class, {}).get('$SELF')
    if self_level and symbol in order:
        mapping[symbol] = self_level
    if symbol:
        mapping.update(exp['self_overrides'].get(event_class, {}).get(symbol, {}))
    for asset in extra:
        if asset in order and asset not in mapping:
            weight = (causal_weights or {}).get(asset)
            mapping[asset] = 'medium' if weight is None else next(
                (band['level'] for band in exp['causal_weight_levels'] if weight >= band['min']), 'low')
    items = [{'symbol': s, 'level': level, 'weight': levels[level]} for s, level in mapping.items() if s in order]
    return sorted(items, key=lambda i: (-i['weight'], order.index(i['symbol'])))


def _causal_weights(trigger: str | None) -> dict[str, float]:
    weights: dict[str, float] = {}
    for path in causal.paths(trigger) if trigger else []:
        for symbol, weight in path['weights'].items():
            weights[symbol] = max(weights.get(symbol, 0.0), weight)
    return weights


# --- phases (§3) -------------------------------------------------------------------------------------------------------

def _session_key(row: dict) -> str | None:
    """bmo / amc / dmh of a date-only row: its 'session' field, else a configured time_basis pattern."""
    config = _risk()['phases']['date_only_sessions']
    given = str(row.get('session') or '').lower()
    if given in config['sessions']:
        return given
    basis = str(row.get('time_basis') or '').casefold()
    return next((key for key, patterns in config['session_patterns'].items() if any(_word(p, basis) for p in patterns)), None)


def whole_day_label(zone_name: str | None) -> str:
    """'whole day, New York time': the label of a publisher's-day window (date_only_sessions default_label, the zone's
    name from zone_names, else its city)."""
    config = _risk()['phases']['date_only_sessions']
    zone = str(zone_name or 'UTC')
    name = (config.get('zone_names') or {}).get(zone) or zone.rsplit('/', 1)[-1].replace('_', ' ')
    return str(config['default_label']).format(zone=name)


def date_window(row: dict, event_class: str, t0: datetime) -> dict:
    """Release window of a date-only row (docs/EVENT_AWARENESS.md §8): the class window (BoJ: statement day in Tokyo),
    else the row's session (before the US open until 09:30 ET, after the US close from 16:00 ET, ...), else the
    publisher's local day ('whole day, New York time'). {start, end, label, zone}; the day is the row's own date in
    that zone."""
    config = _risk()['phases']['date_only_sessions']
    spec = config['classes'].get(event_class) or config['sessions'].get(_session_key(row) or '')
    if spec:
        zone_name, label = spec['zone'], spec['label']
        start_minutes, end_minutes = _clock_minutes(spec['start']), _clock_minutes(spec['end'])
    else:
        zones = settings().get('provider_routing', {}).get('calendars', {}).get('country_timezones') or {}
        zone_name = zones.get(str(row.get('country') or ''), zones.get('default', 'UTC'))
        label, start_minutes, end_minutes = whole_day_label(zone_name), 0, 24 * 60
    zone = ZoneInfo(zone_name)
    day = aware(t0).astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    # Aware + timedelta is wall-clock arithmetic in the row's zone: a DST change keeps the configured clock times.
    start, end = day + timedelta(minutes=start_minutes), day + timedelta(minutes=end_minutes)
    return {'start': start, 'end': end, 'label': label, 'zone': zone_name}


def timeline(t0: datetime, windows: dict, sub_times: Iterable[datetime] = (), known: bool = True,
             window: tuple[datetime, datetime] | None = None) -> dict[str, datetime]:
    """Phase boundaries. A timed event stays IN_PROGRESS until its last sub-event (the press conference) plus
    in_progress_minutes, so a central-bank event never resolves before its press conference is over. A date-only row has
    no minute-precise boundary: it is APPROACHING ('time TBA') from lead hours before its release window until the
    window ends (date_window; without one, the 24 h after t0), then POST_EVENT for post_event_minutes."""
    t0 = aware(t0)
    if not known:
        start, end = window or (t0, t0 + timedelta(days=1))
        return {'approaching': aware(start) - timedelta(hours=windows['date_only_lead_hours']), 'window_start': aware(start),
                'window_end': aware(end), 'post_start': aware(end),
                'resolved': aware(end) + timedelta(minutes=windows['post_event_minutes'])}
    last = max([t0, *(aware(t) for t in sub_times)])
    released_end = t0 + timedelta(minutes=windows['released_minutes'])
    in_progress_end = max(released_end, last + timedelta(minutes=windows['in_progress_minutes']))
    return {'approaching': t0 - timedelta(minutes=windows['approaching_minutes']),
            'imminent': t0 - timedelta(minutes=windows['imminent_minutes']), 'released': t0, 'released_end': released_end,
            'in_progress_end': in_progress_end, 'post_start': in_progress_end,
            'resolved': in_progress_end + timedelta(minutes=windows['post_event_minutes'])}


def phase_at(now: datetime, marks: dict[str, datetime], known: bool = True) -> str:
    """SCHEDULED -> APPROACHING -> IMMINENT -> RELEASED -> IN_PROGRESS -> POST_EVENT -> RESOLVED; a date-only row only
    ever reaches SCHEDULED, APPROACHING, POST_EVENT (after its release window) and RESOLVED (never IMMINENT, RELEASED,
    IN_PROGRESS or EVENT RISK MODE)."""
    now = aware(now)
    if not known:
        for name, key in (('SCHEDULED', 'approaching'), ('APPROACHING', 'window_end'), ('POST_EVENT', 'resolved')):
            if now < marks[key]:
                return name
        return 'RESOLVED'
    for name, key in (('SCHEDULED', 'approaching'), ('APPROACHING', 'imminent'), ('IMMINENT', 'released'),
                      ('RELEASED', 'released_end'), ('IN_PROGRESS', 'in_progress_end'), ('POST_EVENT', 'resolved')):
        if now < marks[key]:
            return name
    return 'RESOLVED'


def phase(now: datetime, time_utc: datetime | str, *, event_class: str = 'OTHER', known: bool = True,
          sub_event_times: Iterable[datetime] = (), row: dict | None = None) -> str:
    t0 = aware(time_utc)
    window = None
    if not known:
        found = date_window(row or {}, event_class, t0)
        window = (found['start'], found['end'])
    return phase_at(now, timeline(t0, _windows(class_config(event_class)), sub_event_times, known, window), known)


# --- scores (§3) -------------------------------------------------------------------------------------------------------

def proximity(event_phase: str, minutes: float | None, *, known: bool = True, share: float | None = None) -> float:
    """Configurable curve on minutes until the event; RELEASED/IN_PROGRESS count fully, POST_EVENT decays over its window,
    an active unscheduled event decays over the active window, and a date-only row is capped (no minute precision)."""
    config = _risk()['relevance']
    if event_phase in LIVE:
        return float(config['released_proximity'])
    if event_phase == 'POST_EVENT':
        span = config['post_event_proximity']
        return span['start'] + (span['end'] - span['start']) * _clamp(share or 0.0, 0.0, 1.0)
    if event_phase == ACTIVE:
        span = config['active_proximity']
        return span['start'] + (span['end'] - span['start']) * _clamp(share or 0.0, 0.0, 1.0)
    if event_phase == 'RESOLVED' or minutes is None:
        return 0.0
    value = piecewise(config['proximity_curve'], max(0.0, minutes))
    return min(value, float(config['date_only_proximity_cap'])) if not known else value


def regime_sensitivity(weights: dict[str, float], regime: dict | None) -> tuple[float, str]:
    """Exposure of the event on the regime drivers (Rates, USD, Oil, Risk appetite: the event's highest exposure among
    each driver's inputs). The static mean over every driver is an event property; drivers that move AND are exposed
    raise it toward 100 (moving_exposed_boost x their exposure share). A moving driver the event is not exposed to never
    lowers it, so an unrelated move (oil before the Fed) cannot flip the event's level."""
    config = _risk()['relevance']
    moving = {d.get('driver'): d.get('direction') for d in (regime or {}).get('drivers') or [] if isinstance(d, dict)}
    drivers = settings()['regime']['drivers']
    if not drivers:
        return 0.0, 'no regime drivers configured'
    exposures, moving_exposure, names = [], 0.0, []
    for driver in drivers:
        exposed = max((weights.get(i['feature'], 0.0) for i in driver['inputs']), default=0.0)
        exposures.append(exposed)
        if exposed > 0 and moving.get(driver['name']) in {'up', 'down'}:
            moving_exposure += exposed
            names.append(driver['name'])
    static = sum(exposures) / len(exposures)
    value = 100 * min(1.0, static + config['moving_exposed_boost'] * (1 - static) * moving_exposure / len(exposures))
    basis = f"exposed drivers moving: {', '.join(names)}" if names else 'no exposed driver moving (static exposure mean)'
    return round(value, 1), basis


def breadth(weights: dict[str, float]) -> float:
    return round(_clamp(100 * sum(weights.values()) / _risk()['relevance']['breadth_full_weight']), 1)


def probabilities(pricing: Any) -> list[float]:
    """Explicit outcome probabilities (keys ending in 'probability', 0..1) of a market-pricing dict."""
    values = [_num(v) for k, v in (pricing.items() if isinstance(pricing, dict) else []) if str(k).endswith('probability')]
    return [v for v in values if v is not None and 0 <= v <= 1]


def pricing_uncertainty(pricing: Any) -> tuple[float, str]:
    """100 x (1 - max outcome probability) / (1 - 1/n) from explicit *_probability values; the configured default when
    market pricing is UNKNOWN."""
    found = probabilities(pricing)
    if len(found) < 2:
        return float(_risk()['relevance']['pricing_uncertainty_default']), 'configured default (market pricing UNKNOWN)'
    return round(_clamp(100 * (1 - max(found)) / (1 - 1 / len(found))), 1), 'market pricing'


def weighted(components: dict[str, float | None], weights: dict[str, float]) -> tuple[float, list[str]]:
    """Weighted mean over the measured components (weights renormalised); the missing ones are named."""
    measured = {k: v for k, v in components.items() if v is not None}
    total = sum(weights[k] for k in measured)
    value = sum(weights[k] * v for k, v in measured.items()) / total if total else 0.0
    return round(_clamp(value), 1), [k for k, v in components.items() if v is None]


def relevance(components: dict[str, float | None], cap: float | None = None) -> dict:
    """Weighted relevance; `cap` bounds an event that exposes no tracked asset (unmapped.relevance_cap)."""
    value, missing = weighted(components, _risk()['relevance']['weights'])
    value = min(value, float(cap)) if cap is not None else value
    return {'value': value, 'level': level_for(value), 'components': components, 'missing': missing}


def dominance(components: dict[str, float | None], cap: float | None = None) -> float:
    value = weighted(components, _risk()['dominance']['weights'])[0]
    return min(value, float(cap)) if cap is not None else value


def unmapped_cap(affected: list[dict]) -> float | None:
    """relevance/dominance cap of an event that exposes no tracked asset (no exposure mapping for its class)."""
    return None if affected else float(_risk()['unmapped']['relevance_cap'])


def history(store: Store) -> dict[str, list[float]]:
    """Stored per-class impact of past scheduled events (record_history), for the dominance historical input."""
    rows = analytics.stored_relationships(store, _risk()['dominance']['history_prefix'])
    found: dict[str, list[float]] = {}
    for row in rows.values():
        if row.get('event_class') and _num(row.get('impact')) is not None:
            found.setdefault(str(row['event_class']), []).append(float(row['impact']))
    return found


def historical_impact(event_class: str, spec: dict, stored: dict[str, list[float]] | None) -> tuple[float | None, str]:
    values = (stored or {}).get(event_class, [])
    minimum = _risk()['dominance']['min_history_events']
    if len(values) >= minimum:
        return round(sum(values) / len(values), 1), f'stored reactions (n={len(values)})'
    prior = _num(spec.get('expected_volatility'))
    return prior, f'configured prior (stored history insufficient: n={len(values)} < {minimum})'


# --- expectations and data quality (§2, §38) -----------------------------------------------------------------------------

CONSENSUS_META = {'label', 'source', 'note', 'previous', 'unit', 'values', 'provider'}
VALUE_NAMES = {'eps': 'EPS', 'eps_estimate': 'EPS', 'revenue': 'Revenue', 'revenue_estimate': 'Revenue'}


def _consensus_label(label: Any, source: Any = None) -> str:
    """'Consensus' for a (street) consensus, 'Finnhub estimate' for Finnhub's estimates; any other vendor label is kept
    as given (never relabelled as consensus)."""
    text = str(label or '').strip()
    if 'finnhub' in text.casefold() or (not text and 'finnhub' in str(source or '').casefold()):
        return FINNHUB_LABEL
    return CONSENSUS_LABEL if not text or text.casefold() == 'consensus' else text


def _value_items(mapping: dict) -> list[dict]:
    unit = mapping.get('unit')
    items = []
    for key, value in mapping.items():
        if key in CONSENSUS_META or value is None or isinstance(value, (dict, list, bool)) or value == '':
            continue
        if isinstance(value, (int, float)) and not math.isfinite(value):
            continue
        items.append({'name': VALUE_NAMES.get(key, key), 'value': float(value) if isinstance(value, (int, float)) else str(value),
                      'unit': unit if isinstance(unit, str) and unit else None})
    return items


def consensus_of(row: dict) -> dict | str:
    """docs/EVENT_AWARENESS.md §8: 'NO CONSENSUS' or {label: 'Consensus' | 'Finnhub estimate', values: [{name, value,
    unit}], source}. Vendor estimates keep their own label, never 'Street consensus'; 'previous' is not an expectation
    (it stays in known_information)."""
    value = row.get('consensus')
    source = row.get('consensus_source') or row.get('values_source') or row.get('provider')
    if isinstance(value, dict) and value:
        given = value.get('values')
        items = [{'name': str(v.get('name')), 'value': v.get('value'), 'unit': v.get('unit')} for v in given
                 if isinstance(v, dict) and v.get('name') and v.get('value') is not None] if isinstance(given, list) \
            else _value_items(value)
        label = _consensus_label(value.get('label'), value.get('source') or source)
        return {'label': label, 'values': items, 'source': value.get('source') or source} if items else NO_CONSENSUS
    if value is None or value == '':
        estimates = row.get('estimates') if isinstance(row.get('estimates'), dict) else None
        if estimates:  # e.g. Finnhub EPS / revenue, labelled by the provider
            items = _value_items(estimates)
            label = _consensus_label(estimates.get('label'), row.get('provider'))
            return {'label': label, 'values': items, 'source': row.get('provider')} if items else NO_CONSENSUS
        items = _value_items({k: row[k] for k in ('eps_estimate', 'revenue_estimate') if row.get(k) is not None})
        label = _consensus_label(row.get('consensus_label') or 'vendor estimate', row.get('provider'))
        return {'label': label, 'values': items, 'source': source} if items else NO_CONSENSUS
    unit = row.get('unit') if isinstance(row.get('unit'), str) and row.get('unit') else None
    name = 'EPS' if unit == 'EPS' else str(row.get('metric') or row.get('name') or 'consensus')
    number = _num(value)
    return {'label': _consensus_label(row.get('consensus_label'), source),
            'values': [{'name': name, 'value': number if number is not None else str(value), 'unit': None if unit == 'EPS' else unit}],
            'source': source}


def consensus_value(consensus: Any) -> float | None:
    """The number a published actual is compared with: the first numeric expectation (the headline; EPS for an
    earnings estimate)."""
    if not isinstance(consensus, dict):
        return None
    for item in consensus.get('values') or []:
        if isinstance(item, dict) and (number := _num(item.get('value'))) is not None:
            return number
    return next((v for k in ('value', 'eps', 'eps_estimate') if (v := _num(consensus.get(k))) is not None), None)


def headline_metric(consensus: Any) -> str:
    """Name of the metric the headline numbers belong to (the first numeric expectation's name), '' when unknown."""
    if not isinstance(consensus, dict):
        return ''
    for item in consensus.get('values') or []:
        if isinstance(item, dict) and _num(item.get('value')) is not None:
            return str(item.get('name') or '')
    return ''


def surprise_polarity(template: dict, metric: str) -> int:
    """+1, or -1 when the template marks the metric as inverted (surprise_polarity: first matching pattern)."""
    folded = metric.casefold()
    return next((int(p['sign']) for p in template.get('surprise_polarity') or [] if _matches(p['pattern'], folded)), 1)


def consensus_words(consensus: Any) -> str:
    """'consensus' / 'Finnhub estimate' for resolution texts."""
    label = consensus.get('label') if isinstance(consensus, dict) else None
    return 'consensus' if not label or label == CONSENSUS_LABEL else str(label)


def pricing_of(row: dict) -> dict | str:
    value = row.get('market_pricing')
    return dict(value) if isinstance(value, dict) and value else UNKNOWN


def incomplete_reasons(row: dict, spec: dict, consensus: Any, pricing: Any, known: bool) -> list[str]:
    reasons = []
    if spec.get('expects_consensus') and consensus == NO_CONSENSUS:
        reasons.append(NO_CONSENSUS)
    if spec.get('expects_pricing') and pricing == UNKNOWN:
        reasons.append('MARKET PRICING UNKNOWN')
    if not known:
        reasons.append('EXACT TIME UNKNOWN')
    if row.get('provisional'):
        reasons.append('PROVISIONAL DATE')
    return reasons


def surprise_basis(event: dict, now: datetime, guidance_end: datetime | None, guidance: str = 'press conference') -> dict:
    """Central-bank / Fed-communication surprise basis for the AI (§3, §8): headline_priced true when the published
    headline equals consensus or the market pricing of the most likely outcome reaches priced_threshold, false when the
    headline differs from consensus or pricing is below it, 'UNKNOWN' otherwise; guidance_pending until the press
    conference (or the speech / testimony) ends. A HAWKISH / DOVISH call also needs an official document of the event
    (event.official_documents): without one only UNDETERMINED (or NEUTRAL_PRICED for a priced headline) is allowed."""
    config = _risk()['post_event']
    actual, expected = _num(event.get('actual')), consensus_value(event['consensus'])
    facts = []
    priced: bool | str = UNKNOWN
    if actual is not None and expected is not None:
        priced = abs(actual - expected) < 1e-9
        facts.append(f"headline {actual:g} vs consensus {expected:g}")
    else:
        found = probabilities(event['market_pricing'])
        if found:
            priced = max(found) >= config['priced_threshold']
            facts.append(f"most likely outcome priced at {max(found):g} (threshold {config['priced_threshold']:g})")
        else:
            facts.append('market pricing UNKNOWN')
    t0 = _stamp(event.get('time_utc'))
    pending = now < guidance_end if guidance_end else (t0 is None or now < t0)
    facts.append(f'guidance pending until the {guidance} ends' if pending and guidance_end else
                 'statement not published yet' if pending else 'guidance published')
    documents = event.get('official_documents') or []
    kinds = sorted({str(d.get('kind')).replace('_', ' ').lower() for d in documents})
    facts.append(f"official documents received: {', '.join(kinds)}" if documents else
                 'no official document of this event received yet (a surprise call needs one)')
    if event.get('phase') in UPCOMING:
        allowed = ['UNDETERMINED', 'NOT_APPLICABLE']
    elif pending and priced is not False:
        allowed = ['UNDETERMINED', 'NEUTRAL_PRICED']
    elif not documents:
        allowed = ['UNDETERMINED', 'NEUTRAL_PRICED'] if priced is True else ['UNDETERMINED']
    else:
        allowed = ['HAWKISH_SURPRISE', 'DOVISH_SURPRISE', 'NEUTRAL_PRICED', 'UNDETERMINED']
    return {'headline_priced': priced, 'guidance_pending': pending, 'priced_threshold': config['priced_threshold'],
            'allowed_assessments': allowed, 'official_document_ids': [d['evidence_id'] for d in documents], 'basis': facts}


# --- scenarios (§3, spec §11, §14) -------------------------------------------------------------------------------------

def causal_directions(paths: list[dict], symbols: Iterable[str] | None = None) -> tuple[dict[str, int], list[str]]:
    """(expected sign per symbol, unresolved symbols) of causal paths: the netted sign of every symbol the paths move
    (or of `symbols`), except those whose competing paths are conflicting (causal.conflict), which get no direction."""
    net = analytics.expected_effects({'causal_paths': paths})
    reached = [s for s in (list(net) if symbols is None else symbols) if s in net]
    # The conflict test is the causal engine's own (whatever sign the netting gives a nearly cancelled asset).
    unresolved = [s for s in reached if causal.conflict(paths, s)['conflicting']]
    return {s: net[s] for s in reached if net[s] and s not in unresolved}, unresolved


def scenarios(event: dict, regime: dict | None = None) -> list[dict]:
    """Deterministic templates per class, each labelled 'possible'. A scenario naming a trigger takes its direction per
    exposed asset from the causal graph (competing paths netted, regime exceptions applied). An asset whose competing
    paths the causal engine itself calls conflicting (causal.conflict: |net| / gross below conflict_net_ratio, the rule
    the decision engine gates on) gets NO direction and is listed in `unresolved` (EVR-1: equities and crypto after a
    labour release, where the rates path and the growth path nearly cancel, were 'down' in both branches). A configured
    direction is labelled as such; anything else has no direction."""
    template = _exp()['scenario_templates'].get(class_config(event['event_class']).get('scenario_template') or '')
    if not template:
        return []
    exposed = [a['symbol'] for a in event.get('affected_assets', [])]
    context = causal.build_context(regime) if regime else {}
    result = []
    for spec in template['scenarios']:
        unresolved: list[str] = []
        if spec.get('trigger'):
            paths = causal.paths(spec['trigger'], 0.0, context)
            expected, unresolved = causal_directions(paths, exposed or None)
            basis = 'causal graph: ' + ', '.join(p['rule_id'] for p in paths if p['active'])
        elif spec.get('expected'):
            symbol = event.get('symbol')
            expected = {str(symbol if s == '$SELF' else s): int(v) for s, v in spec['expected'].items() if s != '$SELF' or symbol}
            basis = 'configured template'
        else:
            expected, basis = {}, 'no directional model'
        result.append({'id': spec['id'], 'label': spec['label'], 'condition': spec['label'], 'trigger': spec.get('trigger'),
                       'expected': expected, 'unresolved': unresolved, 'basis': basis, 'note': spec.get('note'),
                       'likelihood': 'possible'})
    return result


def _template(event_class: str) -> dict:
    return _exp()['scenario_templates'].get(class_config(event_class).get('scenario_template') or '') or {}


def assessed(event_class: str) -> bool:
    """Classes whose surprise direction comes from an AI classification of an official document (central-bank
    decisions, minutes, Chair / Vice Chair / Fed speeches and testimony), not from consensus."""
    return 'assessments' in _template(event_class)


def document_kind(text: str) -> str | None:
    """STATEMENT / PRESS_CONFERENCE_TRANSCRIPT / MINUTES / SPEECH / TESTIMONY from a document title (first configured
    kind whose phrase appears)."""
    folded = text.casefold()
    kinds = _risk()['surprise']['official_documents']['kinds']
    return next((kind for kind, phrases in kinds.items() if any(_word(p, folded) for p in phrases)), None)


def official_documents(event_class: str, t0: datetime, news: Iterable[dict], window_end: datetime | None = None) -> list[dict]:
    """Official documents of a scheduled central-bank / Fed-communication event: direct, official evidence of the
    bank's own source (config surprise.official_documents.classes) on CONFIRMED news events, published from
    match_before_minutes before t0 (a date-only event: its window start) to match_after_hours after t0 (or after its
    window end), of a kind allowed for the class. These are what the AI may classify and cite (evidence_id); nothing
    else can back a surprise call."""
    config = _risk()['surprise']['official_documents']
    spec = config['classes'].get(event_class)
    if not spec:
        return []
    start = t0 - timedelta(minutes=config['match_before_minutes'])
    end = (window_end or t0) + timedelta(hours=config['match_after_hours'])
    found: dict[str, dict] = {}
    for item in news:
        if item.get('verification_status') != 'CONFIRMED':
            continue
        for evidence in item.get('evidence') or []:
            if not isinstance(evidence, dict) or not evidence.get('id') or not evidence.get('official') \
                    or evidence.get('direct') is False or evidence.get('relation') == 'contradicts' \
                    or evidence.get('source_id') not in spec['sources']:
                continue
            stamp = _stamp(evidence.get('published_at')) or _stamp(item.get('event_time') or item.get('first_seen_utc'))
            kind = document_kind(f"{evidence.get('title') or ''} {item.get('summary_fact') or ''}")
            if stamp is None or not start <= stamp <= end or kind not in spec['kinds']:
                continue
            title = ' '.join(str(evidence.get('title') or '').split())[:config['title_chars']] or None
            found.setdefault(str(evidence['id']), {
                'evidence_id': str(evidence['id']), 'document_id': evidence.get('document_id'), 'kind': kind,
                'title': title, 'source': evidence.get('source'), 'published_at': stamp.isoformat(),
                'ingested_at': evidence.get('ingested_at'), 'url': evidence.get('url')})
    return sorted(found.values(), key=lambda d: (d['published_at'], d['evidence_id']))[:config['max_per_event']]


def interpretation_rows(store: Store, now: datetime) -> list[dict]:
    """Stored interpretation runs of the grading source ('ai') generated within surprise.history_hours, newest first.
    Deterministic fallbacks are never read as a classification."""
    config = _risk()['surprise']
    try:
        rows = store.list('interpretations', config['history_rows'])
    except (KeyError, SQLAlchemyError):  # migration 0005 not applied
        return []
    oldest = aware(now) - timedelta(hours=config['history_hours'])
    kept = [r for r in rows if r.get('source') == config['grading_source'] and (s := _stamp(r.get('generated_at'))) and s >= oldest]
    return sorted(kept, key=lambda r: str(r.get('generated_at')), reverse=True)


SURPRISE_RECORD_KEYS = ('assessments', 'surprises', 'surprise_records')


def _row_assessments(row: dict, event_id: str) -> list[tuple[str, list[str]]]:
    """(assessment, cited evidence ids) an interpretation run gives for one event: its per-event surprise record
    ({assessment, source, evidence_ids, validated}, stored by the interpreter; only a validated 'ai' record counts), else
    its next_events item (surprise_assessment + evidence_ids, unless marked validated false)."""
    for key in SURPRISE_RECORD_KEYS:
        value = row.get(key)
        records = value if isinstance(value, dict) else {str(r.get('event_id')): r for r in value or []
                                                          if isinstance(r, dict)} if isinstance(value, list) else None
        if records is None:
            continue
        record = records.get(event_id)
        if not isinstance(record, dict):
            return []
        valid = record.get('validated') is True and record.get('source', row.get('source')) == 'ai'
        return [(str(record.get('assessment')), [str(i) for i in record.get('evidence_ids') or []])] if valid else []
    output = row.get('output')
    items = output.get('next_events') if isinstance(output, dict) else None
    return [(str(item.get('surprise_assessment')), [str(i) for i in item.get('evidence_ids') or []])
            for item in items or [] if isinstance(item, dict) and item.get('event_id') == event_id
            and item.get('validated') is not False]


def classify_surprise(event: dict, rows: Iterable[dict], guidance_end: datetime | None,
                      released: datetime | None = None) -> dict:
    """event.surprise (docs/EVENT_AWARENESS.md §8): the latest stored interpretation of source 'ai' whose next_events item
    for this event names a mapped assessment and cites at least one of the event's official documents that was ingested
    before the interpretation ran, made after the release and - unless the headline is known not to be priced - after
    the guidance ended. A later run without the event, or a deterministic fallback, never removes it. Anything else is
    UNDETERMINED with source 'none' (never grades)."""
    mapping = _template(event['event_class']).get('assessments') or {}
    documents = {d['evidence_id']: d for d in event.get('official_documents') or []}
    t0 = aware(released) if released else _stamp(event.get('time_utc'))
    if not mapping or not documents or t0 is None or event.get('phase') not in POST:
        return no_surprise()
    priced = (event.get('surprise_basis') or {}).get('headline_priced')
    grading = _risk()['surprise']['grading_source']
    best: tuple[datetime, str, list[str]] | None = None
    for row in rows:
        at = _stamp(row.get('generated_at'))
        if row.get('source') != grading or at is None or at < t0 or (guidance_end and at < guidance_end and priced is not False):
            continue
        for assessment, cited_ids in _row_assessments(row, event['event_id']):
            cited = [i for i in cited_ids if i in documents
                     and (seen := _stamp(documents[i].get('ingested_at') or documents[i].get('published_at'))) and seen <= at]
            if assessment in mapping and cited and (best is None or at > best[0]):
                best = (at, assessment, list(dict.fromkeys(cited)))
    if best is None:
        return no_surprise()
    return {'assessment': best[1], 'source': 'ai', 'evidence_ids': best[2], 'validated': True}


def resolved_scenario(event: dict, *_: Any) -> tuple[str | None, str]:
    """Scenario the release resolved to, when a surprise direction is known: a macro surprise needs a consensus (or a
    labelled vendor estimate); a central bank / Fed communication needs event.surprise from a validated AI
    classification backed by an official document (never a deterministic fallback, an uncited label or the measured
    reactions), and never while a priced headline still waits for guidance."""
    template = _template(event['event_class'])
    if not template:
        return None, 'no scenario template'
    if 'surprise' in template:
        actual, expected = _num(event.get('actual')), consensus_value(event['consensus'])
        words = consensus_words(event['consensus'])
        if expected is None:
            return None, 'NO CONSENSUS: surprise direction unknown'
        if actual is None:
            return None, 'actual not published yet'
        diff, tolerance = actual - expected, _risk()['post_event']['in_line_tolerance']
        side = 'above' if diff > tolerance else 'below' if diff < -tolerance else None
        if side is None:
            neutral = next((s['id'] for s in template['scenarios'] if not s.get('trigger') and not s.get('expected')), None)
            return neutral, f'actual {actual:g} in line with {words} {expected:g}'
        # EVR-9: the surprise map is written for the class headline (payrolls: above = stronger). A lent headline
        # metric of the opposite polarity (the unemployment rate: above = weaker) resolves to the opposite branch.
        metric = headline_metric(event['consensus'])
        inverted = surprise_polarity(template, metric) < 0
        branch = template['surprise'][{'above': 'below', 'below': 'above'}[side] if inverted else side]
        note = f' ({metric}: a higher value is the weaker outcome)' if inverted else ''
        return branch, f'actual {actual:g} {side} {words} {expected:g}{note}'
    if 'assessments' in template:
        basis = event.get('surprise_basis') or {}
        if basis.get('guidance_pending') and basis.get('headline_priced') is not False:
            return None, 'guidance pending: a priced headline alone is not a surprise'
        found = event.get('surprise') or {}
        if not (found.get('source') == 'ai' and found.get('validated') and found.get('evidence_ids')):
            return None, 'surprise UNDETERMINED: no validated AI classification backed by an official document of the event'
        kinds = sorted({str(d.get('kind')).replace('_', ' ').lower() for d in event.get('official_documents') or []
                        if d.get('evidence_id') in found['evidence_ids']})
        scenario = template['assessments'].get(found['assessment'])
        return scenario, f"AI classification {found['assessment']} (validated; cites the official {', '.join(kinds)})"
    return None, 'no surprise rule for this class'


# --- event objects -------------------------------------------------------------------------------------------------------

def _discriminator(item: dict) -> str:
    """Part of the dedupe key beside (class, UTC date): the event's own symbol; the name for OTHER; the title and clock
    time for speech / testimony classes (group_by_name: two speeches on one day are two events)."""
    name = str(item['row'].get('name') or '').casefold()
    if item['symbol']:
        return str(item['symbol'])
    if item['class'] == 'OTHER':
        return name
    return f"{name}|{item['stamp']:%H:%M}" if class_config(item['class']).get('group_by_name') else ''


def _group(rows: list[tuple[datetime, dict]]) -> list[dict]:
    """Main rows deduplicated by (class, UTC date, own symbol or name), preferring an exact time over a rule/date-only
    one; sub-event rows (press conference, projections) attached to their main row by parent_event_id, else to the
    closest earlier main row of the same class within sub_event_max_offset_minutes (else they stand alone). A second
    row of the same sub-event (a vendor 'ECB Press Conference' beside the bank's own) within sub_event_dedupe_minutes is
    a duplicate of the first (linked rows first, then exact times)."""
    offset = timedelta(minutes=_risk()['phases']['sub_event_max_offset_minutes'])
    same = timedelta(minutes=_risk()['phases']['sub_event_dedupe_minutes'])
    items: list[dict[str, Any]] = [{'stamp': stamp, 'row': row, 'class': classify_row(row), 'sub': sub_event_kind(row)}
                                   for stamp, row in rows]
    for item in items:
        item['symbol'] = row_symbol(item['row'], item['class'])
    mains: dict[tuple, dict] = {}
    def rank(item: dict) -> tuple:
        row = item['row']
        rule = row.get('time_source') == 'rule' or 'rule' in str(row.get('time_basis') or '')
        return not time_known(row), rule, -(_num(row.get('impact')) or 0), str(row.get('id'))
    for item in sorted((i for i in items if not i['sub']), key=rank):
        key = (item['class'], item['stamp'].date(), _discriminator(item))
        if key in mains:
            mains[key]['duplicates'].append(item['row'])
        else:
            mains[key] = dict(item, duplicates=[], subs=[])
    groups = list(mains.values())
    ids = {str(r.get('id')): g for g in groups for r in [g['row'], *g['duplicates']] if r.get('id')}
    subs = sorted((i for i in items if i['sub']), key=lambda i: (not i['row'].get('parent_event_id'), *rank(i)))
    for item in subs:
        parent = ids.get(str(item['row'].get('parent_event_id') or ''))
        if parent is None:
            candidates = [g for g in groups if g['class'] == item['class'] and timedelta(0) <= item['stamp'] - g['stamp'] <= offset]
            parent = max(candidates, key=lambda g: g['stamp'], default=None)
        if parent is not None and timedelta(0) <= item['stamp'] - parent['stamp'] <= offset:
            twin = next((s for s in parent['subs'] if s['sub'] == item['sub'] and abs(s['stamp'] - item['stamp']) <= same), None)
            if twin is None:
                parent['subs'].append(item)
            else:
                parent['duplicates'].append(item['row'])
        else:
            groups.append(dict(item, duplicates=[], subs=[]))
    return sorted(groups, key=lambda g: (g['stamp'], str(g['row'].get('id'))))


def _display_ids(groups: list[dict]) -> list[str]:
    """FED-20261028-FOMC style ids; a clash on the same day gets the stored id prefix appended (stable across calls)."""
    base = []
    for group in groups:
        spec = class_config(group['class'])
        middle = f"-{group['symbol']}" if group['symbol'] and group['class'] in {'EARNINGS', 'CRYPTO_EXPIRY'} else ''
        base.append(f"{spec['id_prefix']}-{group['stamp']:%Y%m%d}{middle}-{group['class']}")
    counts = {b: base.count(b) for b in base}
    return [b if counts[b] == 1 else f"{b}-{str(g['row'].get('id') or '')[:6]}" for b, g in zip(base, groups, strict=True)]


def short_label(event: dict) -> str:
    spec = class_config(event['event_class'])
    label = str(spec.get('short_label') or event['event_class'])
    return f"{event['symbol']} {label}" if event.get('symbol') and event['event_class'] in {'EARNINGS', 'CRYPTO_EXPIRY'} else label


def _market_live(symbols: Iterable[str], now: datetime) -> dict[str, bool]:
    kinds = {s: k for s, _, k in settings()['assets']}
    return {s: sessions.market_state(kinds.get(s, 'unknown'), now, s) == 'LIVE' for s in symbols}


def _scheduled(group: dict, event_id: str, now: datetime, regime: dict | None, news: list[dict],
               rows: Callable[[], list[dict]], stored: dict[str, list[float]] | None) -> dict:
    row, cls, symbol, t0 = group['row'], group['class'], group['symbol'], group['stamp']
    spec, known = class_config(cls), time_known(row)
    if not known:
        # EVR-5: a date-only decision whose official statement is already ingested is no longer 'time TBA': the
        # statement's own publication time is the release time, and the event follows the normal timed timeline.
        statement = published_statement(row, cls, t0, now, news)
        if statement is not None:
            t0, known = statement, True
            row = dict(row, time_known=True, time_basis=f'official statement published at {_local(t0)[0]} Bucharest '
                                                         f'(source timestamp; the calendar row carried the date only)')
    windows = _windows(spec)
    subs = [{'kind': s['sub'], 'stamp': s['stamp'], 'row': s['row'], 'known': time_known(s['row'])} for s in group['subs']]
    if spec['has_press_conference'] and known and spec.get('press_conference_offset_minutes') is not None \
            and not group.get('sub') and not any(s['kind'] == 'PRESS_CONFERENCE' for s in subs):
        subs.append({'kind': 'PRESS_CONFERENCE', 'stamp': t0 + timedelta(minutes=spec['press_conference_offset_minutes']),
                     'row': None, 'known': True})
    subs.sort(key=lambda s: s['stamp'])
    window = None if known else date_window(row, cls, t0)
    marks = timeline(t0, windows, [s['stamp'] for s in subs if s['known']], known,
                     (window['start'], window['end']) if window else None)
    current = phase_at(now, marks, known)
    sub_windows = dict(windows, in_progress_minutes=_risk()['phases']['press_conference_minutes'])
    sub_events = []
    for sub in subs:
        local = _local(sub['stamp'])
        basis = (sub['row'] or {}).get('time_basis') if sub['row'] else \
            f"configured offset +{spec['press_conference_offset_minutes']} min after the decision (no press-conference row in the calendar)"
        span = None if sub['known'] else date_window(sub['row'] or {}, cls, sub['stamp'])
        sub_events.append({'sub_event': sub['kind'], 'name': (sub['row'] or {}).get('name'), 'row_id': (sub['row'] or {}).get('id'),
                           'time_utc': sub['stamp'].isoformat(), 'time_bucharest': local[0] if sub['known'] else None,
                           'time_known': sub['known'], 'time_basis': basis,
                           'phase': phase_at(now, timeline(sub['stamp'], sub_windows, (), sub['known'],
                                                           (span['start'], span['end']) if span else None), sub['known'])})
    press = next((s for s in subs if s['kind'] == 'PRESS_CONFERENCE' and s['known']), None)
    pc_end = press['stamp'] + timedelta(minutes=_risk()['phases']['press_conference_minutes']) if press else None
    affected = exposure(cls, symbol, row.get('affected_assets') or [])
    weights = {a['symbol']: a['weight'] for a in affected}
    consensus, pricing = consensus_of(row), pricing_of(row)
    impact = _num(row.get('impact'))  # the calendar row's own 0-10 impact, the number the calendar shows
    importance = _num(row.get('importance_score')) or _num(_exp()['symbol_importance'].get(cls, {}).get(symbol or '')) \
        or tenor_importance(cls, t0) or _num(spec.get('importance')) or (10 * impact if impact is not None else None)
    volatility = _num(spec.get('expected_volatility'))
    share = None
    if current == 'POST_EVENT':
        length = (marks['resolved'] - marks['post_start']).total_seconds()
        share = (now - marks['post_start']).total_seconds() / length if length else 1.0
    raw_minutes = _minutes(t0, now)
    near = proximity(current, raw_minutes if known else _minutes(marks['window_start'], now), known=known, share=share)
    sensitivity, sensitivity_basis = regime_sensitivity(weights, regime)
    uncertainty, uncertainty_basis = pricing_uncertainty(pricing)
    cap = unmapped_cap(affected)
    scored = relevance({'importance': importance, 'expected_volatility': volatility, 'proximity': round(near, 1),
                        'regime_sensitivity': sensitivity, 'exposure_breadth': breadth(weights), 'pricing_uncertainty': uncertainty},
                       cap)
    past, past_basis = historical_impact(cls, spec, stored)
    count = round(_clamp(100 * len(affected) / _risk()['dominance']['affected_full_count']), 1)
    local = _local(t0)
    kind = group['sub'] if group.get('sub') in KINDS else spec['kind']
    reasons = incomplete_reasons(row, spec, consensus, pricing, known)
    event: dict[str, Any] = {
        'event_id': event_id, 'row_id': row.get('id'), 'name': str(row.get('name') or spec['label']), 'event_class': cls,
        'category': spec['category'], 'kind': kind, 'is_decision': kind == 'DECISION', 'speaker_role': speaker_role(row, cls),
        'symbol': symbol, 'status': current, 'phase': current, 'timing': 'SCHEDULED',
        'time_utc': t0.isoformat(), 'time_bucharest': local[0] if known else None, 'date_bucharest': local[1],
        'time_known': known, 'time_basis': row.get('time_basis'),
        'date_window': {'start_utc': aware(window['start']).isoformat(), 'end_utc': aware(window['end']).isoformat(),
                        'label': window['label'], 'zone': window['zone']} if window else None,
        'minutes_until_event': raw_minutes if known else None,
        'importance_score': importance, 'expected_volatility_score': volatility, 'relevance_score': scored['value'],
        'relevance_level': scored['level'], 'relevance_components': scored['components'], 'relevance_missing': scored['missing'],
        'dominance_score': dominance({'importance': importance, 'expected_volatility': volatility, 'proximity': near,
                                      'affected_count': count, 'pricing_uncertainty': uncertainty, 'historical_impact': past,
                                      'regime_sensitivity': sensitivity}, cap),
        'dominance_basis': {'historical_impact': past_basis, 'regime_sensitivity': sensitivity_basis,
                            'pricing_uncertainty': uncertainty_basis,
                            **({'exposure': f'no tracked asset exposed: relevance and dominance capped at {cap:g}'}
                               if cap is not None else {})},
        'impact_10': round(scored['value'] / 10, 1), 'impact': impact, 'affected_assets': affected,
        'consensus': consensus, 'market_pricing': pricing, 'actual': row.get('actual'),
        'outcome': releases.event_outcome(row),
        'source_ids': list(dict.fromkeys([*(str(r.get('provider')) for r in [row, *group['duplicates']] if r.get('provider')),
                                          *(str(p) for p in row.get('duplicates') or [])])),
        'source_url': row.get('source_url'), 'attribution': row.get('attribution'),
        'fact_confidence': _num(row.get('fact_confidence')) or _num(_risk()['calendar_fact_confidence']['providers'].get(
            str(row.get('provider')), _risk()['calendar_fact_confidence']['default'])),
        'sub_events': sub_events, 'parent_event_id': row.get('parent_event_id'),
        'data_incomplete': reasons,
        'data_incomplete_flag': bool(reasons) and scored['level'] in _risk()['incomplete_flag_levels'],
        'verification_status': None, 'surprise': no_surprise(), 'official_documents': []}
    if cls in _exp()['central_bank_classes'] or assessed(cls):
        released = t0 if known else aware(marks['window_start'])
        if current in POST:
            event['official_documents'] = official_documents(cls, released, news, None if known else aware(marks['window_end']))
        # Guidance: the press conference; a speech / testimony / minutes until it is over; a date-only decision until
        # its release window ends.
        guidance_end = pc_end or (aware(marks['window_end']) if not known else
                                  marks['in_progress_end'] if cls not in _exp()['central_bank_classes'] else None)
        words = 'press conference' if pc_end else 'release window' if not known else kind.lower().replace('_', ' ')
        event['surprise_basis'] = surprise_basis(event, now, guidance_end, words)
        if event['official_documents']:
            event['surprise'] = classify_surprise(event, rows(), guidance_end, released)
    event['scenarios'] = scenarios(event, regime)
    event['possible_surprises'] = [s['id'] for s in event['scenarios']]
    event['known_information'] = _known(event, row, group)
    event['status_note'] = _status_note(event, short_label(event))
    return event


def published_statement(row: dict, event_class: str, t0: datetime, now: datetime, news: list[dict]) -> datetime | None:
    """Publication time of the official statement of a date-only decision (classes of surprise.official_documents), once
    its release window has opened and the document is ingested (CONFIRMED official direct evidence of the bank's own
    source, published inside the window); None otherwise, and the row stays 'time TBA'."""
    if event_class not in _risk()['surprise']['official_documents']['classes']:
        return None
    window = date_window(row, event_class, t0)
    start, end = aware(window['start']), aware(window['end'])
    if aware(now) < start:
        return None
    stamps = [s for d in official_documents(event_class, start, news, end) if (s := _stamp(d.get('published_at')))]
    found = min((s for s in stamps if start <= s <= min(end, aware(now))), default=None)
    return found


def expiry_tenor(stamp: datetime) -> str:
    """quarterly / monthly / weekly / daily for an option expiry date: the last Friday of a quarter-end month, the last
    Friday of any other month, any other Friday, any other day."""
    day = aware(stamp).date()
    if day.weekday() != 4:
        return 'daily'
    if (day + timedelta(days=7)).month == day.month:
        return 'weekly'
    return 'quarterly' if day.month % 3 == 0 else 'monthly'


def tenor_importance(event_class: str, stamp: datetime) -> float | None:
    """Class importance by expiry tenor (event_exposure.expiry_tenor_importance), None for a class without tenors.
    EVR-8: a routine daily options expiry is not scored like a monthly or quarterly one."""
    mapping = _exp()['expiry_tenor_importance']['classes'].get(event_class)
    return _num(mapping.get(expiry_tenor(stamp))) if mapping else None


def _source_fact(event: dict, row: dict) -> str:
    """'Source: U.S. Bureau of Economic Analysis': the row's own attribution (the publisher's name, never the internal
    provider ids beside it); only a row without one names its providers, in words."""
    attribution = ' '.join(str(row.get('attribution') or '').split())
    if attribution:
        return attribution if attribution.casefold().startswith('source') else f'Source: {attribution}'
    names = [s.replace('_', ' ') for s in event['source_ids']]
    return f"Source: {', '.join(names)}" if names else 'Source: not recorded'


def _known(event: dict, row: dict, group: dict) -> list[str]:
    """Backend facts only (sources, time basis, flags, published values)."""
    facts = [_source_fact(event, row)]
    if row.get('time_basis'):
        facts.append(f"Time basis: {row['time_basis']}")
    if row.get('sep') or row.get('dot_plot'):
        facts.append('Summary of Economic Projections meeting (dot plot)')
    if row.get('reference_period'):
        facts.append(f"Reference period: {row['reference_period']}")
    estimates = row.get('estimates')
    if isinstance(event['consensus'], dict) and isinstance(estimates, dict) and estimates.get('note'):
        facts.append(f"{event['consensus']['label']}: {estimates['note']}")
    outcome = event.get('outcome') or {}
    if outcome.get('text'):
        # EVR-4: the outcome read from the official series states every headline number with its previous reading.
        facts.append(f"Published: {outcome['text']}" + (f" (source: {outcome['source']})" if outcome.get('source') else ''))
    else:
        if row.get('previous') is not None:
            facts.append(f"Previous: {row['previous']}" + (f" {row['unit']}" if row.get('unit') else ''))
        if row.get('actual') is not None:
            facts.append(f"Actual: {row['actual']}" + (f" {row['unit']}" if row.get('unit') else ''))
    if row.get('provisional'):
        facts.append('Date provisional on the official page')
    if group['duplicates']:
        facts.append(f"Also listed by {len(group['duplicates'])} other calendar row(s)")
    return facts


def _status_note(event: dict, label: str) -> str | None:
    """'FED decision published; next risk: press conference at 21:30 Bucharest' (clock times, never a countdown)."""
    pending = next((s for s in event['sub_events'] if s['phase'] in UPCOMING), None)
    running = next((s for s in event['sub_events'] if s['phase'] in LIVE), None)

    def words(sub: dict) -> str:
        return str(sub['sub_event']).replace('_', ' ').lower()
    window = event.get('date_window') or {}
    # A session or class window is worth naming; the publisher's whole day adds nothing to 'on <day> (time TBA)'.
    whole_day = not window.get('label') or window['label'] == whole_day_label(window.get('zone'))
    session = '' if whole_day else f", {window['label']}"
    if event['phase'] in LIVE and running:
        return f'{label} event in progress: {words(running)} under way'
    if event['phase'] in LIVE and pending:
        when = f" at {pending['time_bucharest']} Bucharest" if pending['time_bucharest'] else ''
        return f"{label} {'decision published' if event.get('is_decision') else 'released'}; next risk: {words(pending)}{when}"
    if event['phase'] in LIVE:
        return f'{label} released; reaction in progress'
    day = day_label(event['date_bucharest']) or event['date_bucharest']  # 'Wed 30 Sep', like the rest of the page
    if not event['time_known'] and event['phase'] in UPCOMING:
        return f"{label} on {day}{session} (time TBA)"
    if not event['time_known'] and event['phase'] == 'POST_EVENT':
        return f"{label} release window over ({day if whole_day else window['label']}); exact time not published"
    return None


def classify_news(event: dict) -> tuple[str, dict] | None:
    """Unscheduled class of a news event (config unscheduled_rules, first match): trigger, event type or a keyword of
    its summary."""
    text = str(event.get('summary_fact') or '').casefold()
    for rule in _exp()['unscheduled_rules']:
        if event.get('trigger') in rule.get('triggers', []) or event.get('event_type') in rule.get('event_types', []) \
                or any(_word(k, text) for k in rule.get('keywords', [])):
            return str(rule['class']), rule
    return None


def own_assets(item: dict) -> list[str]:
    """Tracked assets the news event itself was mapped to (affected_assets and assets)."""
    return list(dict.fromkeys(str(a) for a in [*(item.get('affected_assets') or []), *(item.get('assets') or [])]))


def news_exposure(item: dict, cls: str, rule: dict) -> set[str] | None:
    """Which assets of the class exposure a news event may expose (EVR-6). An event matched through a causal trigger
    exposes the class mapping (None: no restriction). One matched only by its event type or a keyword exposes only the
    assets it was itself mapped to that the class also exposes; an empty set means it is not a market risk for any
    tracked asset (it stays in the news stream)."""
    if item.get('trigger') in rule.get('triggers', []) or causal.event_trigger(item) is not None:
        return None
    mapped = {s for s in _exp()['exposure'].get(cls, {}) if s != '$SELF'}
    return set(own_assets(item)) & mapped


def date_only_stamp(stamp: datetime) -> bool:
    """A publication stamp that is a date without a clock time: exact midnight in one of the configured publisher zones
    (unscheduled.date_only_zones; an OFAC notice dated '2 Oct' is stored as midnight New York)."""
    return any((local := aware(stamp).astimezone(ZoneInfo(zone))).hour == 0 and local.minute == 0 and local.second == 0
               and local.microsecond == 0 for zone in _risk()['unscheduled']['date_only_zones'])


def _publisher(item: dict) -> frozenset[str]:
    """Independence groups of the official sources behind a news event (its publisher); empty when none is official."""
    groups = {s['id']: s.get('independence_group') or s['id'] for s in settings()['sources']}
    return frozenset(str(groups.get(e.get('source_id'), e.get('source_id'))) for e in item.get('evidence') or []
                     if isinstance(e, dict) and e.get('official') and e.get('source_id'))


def _merge_publisher_day(events: list[tuple[dict, frozenset[str]]]) -> list[dict]:
    """One active event per class, official publisher and publication day (EVR-6: an OFAC designation arrives through
    the recent-actions page and through the sanctions list service): the more relevant one is kept and says how many
    notices it stands for. Events without an official publisher are never merged."""
    kept: dict[tuple, dict] = {}
    result: list[dict] = []
    for event, publisher in sorted(events, key=lambda pair: -pair[0]['relevance_score']):
        day = str(event.get('published_utc') or event.get('first_seen_utc') or '')[:10]
        key = (event['event_class'], publisher, day)
        if publisher and key in kept:
            first = kept[key]
            first['source_ids'] = list(dict.fromkeys([*first['source_ids'], *event['source_ids']]))
            first['merged_notices'] = first.get('merged_notices', 0) + 1
            continue
        if publisher:
            kept[key] = event
        result.append(event)
    for event in result:
        count = event.pop('merged_notices', 0)
        if count:
            event['known_information'].append(f"Same publisher and day: {count} other notice{'s' if count != 1 else ''} merged")
    return result


def _unscheduled(news: list[dict], now: datetime, regime: dict | None, scheduled: list[dict]) -> tuple[list[dict], list[dict]]:
    """Verified GEO / SANCTIONS / REGULATION / statement events within active_window_hours -> ACTIVE EVENT RISK (timing
    ANYTIME, UNRESOLVED or DE-ESCALATING); unverified ones within potential_window_hours -> potential events, which never
    reach a score (§3, spec §28-§29)."""
    config, exp = _risk()['unscheduled'], _exp()
    banks = [aware(e['time_utc']) for e in scheduled if e['event_class'] in exp['central_bank_classes']]
    match = timedelta(hours=config['cb_statement_match_hours'])
    active: list[dict] = []
    potential: list[dict] = []
    deescalations: dict[str, list[datetime]] = {}
    for item in news:
        if item.get('verification_status') == 'CONFIRMED':
            for cls, triggers in exp['deescalation_triggers'].items():
                if item.get('trigger') in triggers and (stamp := _stamp(item.get('event_time') or item.get('first_seen_utc'))):
                    deescalations.setdefault(cls, []).append(stamp)
    context = causal.build_context(regime) if regime else {}
    found_active: list[tuple[dict, frozenset[str]]] = []
    for item in news:
        found = classify_news(item)
        published = _stamp(item.get('event_time') or item.get('first_seen_utc'))
        seen = _stamp(item.get('first_seen_utc')) or published
        if found is None or published is None or seen is None:
            continue
        cls, rule = found
        if rule.get('unscheduled_only') and any(abs(published - b) <= match for b in banks):
            continue  # the release of a scheduled meeting, not an unscheduled statement
        only = news_exposure(item, cls, rule)
        if only is not None and not only:
            continue  # no causal trigger and no own asset the class exposes: news, not a risk for a tracked asset
        # The active window runs from the publication; a date-only stamp (midnight of the publisher's day) is not a
        # publication time, so the window then starts when the app first saw the event.
        stamp = seen if date_only_stamp(published) else published
        status = item.get('verification_status')
        hours = (now - stamp).total_seconds() / 3600
        if status == 'CONFIRMED' and 0 <= hours <= config['active_window_hours']:
            if item.get('trigger') in exp['deescalation_triggers'].get(cls, []):
                continue  # a de-escalation marks the risk it relieves; it is not a new risk
            found_active.append((_news_event(item, cls, stamp, now, regime, context, hours / config['active_window_hours'],
                                             any(d > stamp for d in deescalations.get(cls, [])), only=only, seen=seen,
                                             published=published), _publisher(item)))
        elif status in UNVERIFIED and 0 <= hours <= config['potential_window_hours']:
            potential.append(_news_event(item, cls, stamp, now, regime, context, None, False, only=only, seen=seen,
                                         published=published))
    active = _merge_publisher_day(found_active)
    return sorted(active, key=lambda e: -e['relevance_score']), sorted(potential, key=lambda e: e['first_seen_utc'] or '', reverse=True)


def _news_event(item: dict, cls: str, stamp: datetime, now: datetime, regime: dict | None, context: dict,
                share: float | None, relieved: bool, *, only: set[str] | None = None, seen: datetime | None = None,
                published: datetime | None = None) -> dict:
    """EventObject of an unscheduled news event. `only` restricts its exposure to those assets (news_exposure);
    first_seen_utc is when the app first saw it (`seen`), published_utc its publication stamp."""
    spec = class_config(cls)
    trigger = causal.event_trigger(item)
    assets = list(dict.fromkeys([*(item.get('affected_assets') or causal.affected_assets(trigger)), *(item.get('assets') or [])]))
    affected = [a for a in exposure(cls, None, assets, _causal_weights(trigger)) if only is None or a['symbol'] in only]
    weights = {a['symbol']: a['weight'] for a in affected}
    verified = share is not None
    near = proximity(ACTIVE, None, share=share) if verified else 0.0
    sensitivity, sensitivity_basis = regime_sensitivity(weights, regime)
    uncertainty, uncertainty_basis = pricing_uncertainty(UNKNOWN)
    importance, volatility = _num(spec.get('importance')), _num(spec.get('expected_volatility'))
    cap = unmapped_cap(affected)
    scored = relevance({'importance': importance, 'expected_volatility': volatility, 'proximity': round(near, 1),
                        'regime_sensitivity': sensitivity, 'exposure_breadth': breadth(weights), 'pricing_uncertainty': uncertainty},
                       cap)
    past, past_basis = historical_impact(cls, spec, None)
    count = round(_clamp(100 * len(affected) / _risk()['dominance']['affected_full_count']), 1)
    # Conflicting competing paths give no consequence direction (same rule as the scenarios, EVR-1).
    consequences = causal_directions(causal.event_paths(item, now, context))[0] if trigger else {}
    live = _market_live(weights, now)
    event: dict[str, Any] = {
        'event_id': f"{spec['id_prefix']}-{stamp:%Y%m%d}-{str(item['id'])[:8]}", 'row_id': item['id'],
        'name': str(item.get('summary_fact') or spec['label']), 'event_class': cls, 'category': spec['category'],
        'kind': spec['kind'], 'is_decision': False, 'speaker_role': None,
        'surprise': no_surprise(), 'official_documents': [], 'data_incomplete_flag': False,
        'status': ('DE-ESCALATING' if relieved else 'UNRESOLVED') if verified else POTENTIAL,
        'phase': ACTIVE if verified else 'UNDER_VERIFICATION', 'timing': 'ANYTIME', 'time_utc': None, 'time_known': False,
        'time_basis': 'unscheduled (timing ANYTIME)', 'first_seen_utc': (seen or stamp).isoformat(),
        'published_utc': (published or stamp).isoformat(), 'published_date_only': date_only_stamp(published or stamp),
        'importance_score': importance, 'expected_volatility_score': volatility, 'relevance_score': scored['value'],
        'relevance_level': scored['level'], 'relevance_components': scored['components'], 'relevance_missing': scored['missing'],
        'dominance_score': dominance({'importance': importance, 'expected_volatility': volatility, 'proximity': near,
                                      'affected_count': count, 'pricing_uncertainty': uncertainty, 'historical_impact': past,
                                      'regime_sensitivity': sensitivity}, cap) if verified else 0.0,
        'dominance_basis': {'historical_impact': past_basis, 'regime_sensitivity': sensitivity_basis,
                            'pricing_uncertainty': uncertainty_basis},
        'impact_10': round(scored['value'] / 10, 1), 'impact': None, 'affected_assets': affected,
        'consensus': NO_CONSENSUS, 'market_pricing': UNKNOWN, 'known_information': [f"Verification: {item.get('verification_status')}",
                                                         *([f"Causal chain: {causal.summary(trigger)}"] if trigger else [])],
        'consequences': {s: v for s, v in consequences.items() if v and s in weights} if verified else {},
        'source_ids': [str(s) for s in item.get('sources') or []], 'fact_confidence': _num(item.get('fact_confidence')),
        'data_incomplete': [], 'verification_status': item.get('verification_status'),
        'live_assets': [s for s, v in live.items() if v], 'awaiting_reopen': [s for s, v in live.items() if not v],
        'status_note': None if verified else 'Possible market-moving event under verification; no effect on scores'}
    event['scenarios'] = scenarios(event, regime)
    event['possible_surprises'] = [s['id'] for s in event['scenarios']]
    return event


# --- released today (EVR-4) ----------------------------------------------------------------------------------------------

SeriesLoader = releases.SeriesLoader
series_rows = releases.series_rows


def release_documents(event_class: str, start: datetime, end: datetime, news: Iterable[dict]) -> list[dict]:
    """Official documents that published a scheduled release (release_documents.classes: the publisher's own source and
    a title phrase; central-bank classes: surprise.official_documents sources and kinds): CONFIRMED news events with
    direct official evidence published between `start` and `end`, earliest first. Each names the news event carrying
    it, so the released event links to what was published."""
    config = _risk()['release_documents']
    spec = config['classes'].get(event_class)
    bank = _risk()['surprise']['official_documents']['classes'].get(event_class)
    if not spec and not bank:
        return []
    sources = set((spec or bank)['sources'])
    found: dict[str, dict] = {}
    for item in news:
        if item.get('verification_status') != 'CONFIRMED':
            continue
        for evidence in item.get('evidence') or []:
            if not isinstance(evidence, dict) or not evidence.get('id') or not evidence.get('official') \
                    or evidence.get('direct') is False or evidence.get('relation') == 'contradicts' \
                    or evidence.get('source_id') not in sources:
                continue
            stamp = _stamp(evidence.get('published_at')) or _stamp(item.get('event_time') or item.get('first_seen_utc'))
            if stamp is None or not start <= stamp <= end:
                continue
            text = f"{evidence.get('title') or ''} {item.get('summary_fact') or ''}"
            matched = any(_word(phrase, text.casefold()) for phrase in spec['phrases']) if spec \
                else document_kind(text) in bank['kinds']
            if not matched:
                continue
            found.setdefault(str(evidence['id']), {
                'evidence_id': str(evidence['id']), 'event_id': item.get('id'), 'source': evidence.get('source'),
                'title': ' '.join(str(evidence.get('title') or '').split())[:config['title_chars']] or None,
                'summary': ' '.join(str(item.get('summary_fact') or '').split())[:config['summary_chars']] or None,
                'published_at': stamp.isoformat(), 'url': evidence.get('url')})
    return sorted(found.values(), key=lambda d: (d['published_at'], d['evidence_id']))


def release_outcome(row: dict, event_class: str, t0: datetime, loader: SeriesLoader | None) -> dict | None:
    """What the official series say the release published (backend/releases.py): the outcome the runtime attached to
    the calendar row, else one read from the stored series now (a snapshot built before the attach ran)."""
    held = row.get('release_outcome')
    if isinstance(held, dict) and held.get('text'):
        return held
    if loader is None:
        return None
    return releases.outcome(event_class, t0, loader, row)


def _released(groups: list[dict], ids: list[str], now: datetime, news: list[dict], loader: SeriesLoader | None) -> list[dict]:
    """Scheduled events released today (Europe/Bucharest day) for which the app holds what was published (EVR-4): the
    official document of the release, the calendar row's actual, or the official series values. They stay listed
    after the event resolves and leaves `events`, so the day's key release never vanishes from the opening screen.
    Only events of class importance >= release_documents.min_importance that expose tracked assets; newest first."""
    config = _risk()['release_documents']
    today = bucharest(now).date()
    before, after = timedelta(minutes=config['match_before_minutes']), timedelta(hours=config['match_after_hours'])
    result = []
    for group, event_id in zip(groups, ids, strict=True):
        row, cls, symbol, t0 = group['row'], group['class'], group['symbol'], group['stamp']
        if group.get('sub'):
            continue
        spec, known = class_config(cls), time_known(row)
        window = None if known else date_window(row, cls, t0)
        opens, closes = (t0, t0) if window is None else (aware(window['start']), aware(window['end']))
        if now < opens:
            continue
        importance = _num(row.get('importance_score')) or _num(_exp()['symbol_importance'].get(cls, {}).get(symbol or '')) \
            or tenor_importance(cls, t0) or _num(spec.get('importance'))
        affected = exposure(cls, symbol, row.get('affected_assets') or [])
        if importance is None or importance < config['min_importance'] or not affected:
            continue
        documents = release_documents(cls, opens - before, min(closes + after, now), news)
        outcome = release_outcome(row, cls, t0, loader) if known else None
        actual = row.get('actual')
        if not documents and actual is None and outcome is None:
            continue  # nothing published is held: the app does not claim a release it cannot show
        document = documents[0] if documents else None
        released = _stamp((document or {}).get('published_at')) if not known else t0
        released = released or opens
        if bucharest(released).date() != today:
            continue
        unit = f" {row['unit']}" if row.get('unit') else ''
        consensus = consensus_value(consensus_of(row))
        vendor = actual is not None and outcome is None
        actual_text = f"Actual {actual}{unit}" + (f' vs consensus {consensus:g}' if consensus is not None else '') \
            if vendor else None
        values = [{k: m.get(k) for k in ('name', 'series', 'value', 'previous', 'unit', 'period', 'text')}
                  for m in (outcome or {}).get('metrics') or [] if isinstance(m, dict)]
        values_text = str(outcome['text']) if outcome else None
        # The outcome's numbers first (every headline figure with its previous reading), else the publisher's own
        # summary, else the vendor's actual: the opening screen states what was published, not that it was.
        summary = values_text or (document or {}).get('summary') or actual_text
        basis = 'official series' if values_text else 'official document' if (document or {}).get('summary') else 'calendar actual'
        result.append({
            'event_id': event_id, 'row_id': row.get('id'), 'name': str(row.get('name') or spec['label']), 'event_class': cls,
            'time_utc': t0.isoformat(), 'time_known': known, 'released_utc': released.isoformat(),
            'summary': summary, 'summary_basis': basis, 'actual': actual, 'values': values, 'values_text': values_text,
            'outcome': releases.event_outcome(dict(row, release_outcome=outcome)) if outcome else None,
            'reaction': None, 'document': document, 'news_event_id': (document or {}).get('event_id'),
            'importance_score': importance, 'affected_assets': affected, 'phase': phase_at(now, timeline(
                t0, _windows(spec), (), known, (window['start'], window['end']) if window else None), known)})
    result.sort(key=lambda e: e['released_utc'], reverse=True)
    return result[:config['max_items']]


InterpretationRows = Iterable[dict] | Callable[[], list[dict]] | None


def _rows_source(interpretation: dict | None, interpretations: InterpretationRows) -> Callable[[], list[dict]]:
    """Lazy list of interpretation runs to read surprise classifications from: the stored history (a list or a loader,
    read at most once and only when an assessed event is live or post-event) plus a supplied block (e.g. the latest)."""
    cache: list[list[dict]] = []

    def load() -> list[dict]:
        if not cache:
            found = interpretations() if callable(interpretations) else list(interpretations or [])
            extra = [interpretation] if isinstance(interpretation, dict) else []
            cache.append([r for r in [*extra, *found] if isinstance(r, dict)])
        return cache[0]
    return load


def build_events(calendar_rows: Iterable[dict], news_events: Iterable[dict], now: datetime, *, regime: dict | None = None,
                 interpretation: dict | None = None, stored_history: dict[str, list[float]] | None = None,
                 interpretations: InterpretationRows = None, official_series: SeriesLoader | None = None) -> dict[str, list[dict]]:
    """{'scheduled': EventObjects not yet RESOLVED (time order), 'active': verified unscheduled, 'potential': unverified,
    'released': today's released scheduled events with what was published (_released; resolved ones included)}.
    Pure: time is injected, nothing is read from a provider or a model (interpretation runs and the official-series
    loader are supplied)."""
    now = aware(now)
    risk = _risk()
    start, end = now - timedelta(hours=risk['recent_hours']), now + timedelta(hours=risk['horizon_hours'])
    hidden = set(risk['phases']['hidden_statuses'])
    rows = [(stamp, row) for row in calendar_rows if (stamp := _stamp(row.get('scheduled_at') or row.get('scheduled_utc')))
            and start <= stamp <= end and str(row.get('status') or '').upper() not in hidden]
    groups = _group(rows)
    news = list(news_events)
    runs = _rows_source(interpretation, interpretations)
    scheduled = []
    ids = _display_ids(groups)
    for group, event_id in zip(groups, ids, strict=True):
        event = _scheduled(group, event_id, now, regime, news, runs, stored_history)
        if event['phase'] != 'RESOLVED':
            scheduled.append(EventObject.model_validate(event).model_dump())
    active, potential = _unscheduled(news, now, regime, scheduled)
    return {'scheduled': scheduled, 'active': [EventObject.model_validate(e).model_dump() for e in active],
            'potential': [EventObject.model_validate(e).model_dump() for e in potential],
            'released': _released(groups, ids, now, news, official_series)}


# --- post-event (§3, spec §16-§17) -------------------------------------------------------------------------------------

def post_event(store: Store, event: dict, now: datetime) -> dict:
    """Reactions T+1m..T+1h of a timed scheduled event from the price just before time_utc, market confirmation against
    the resolved scenario (when a surprise direction is known) and MARKET DIVERGENCE when most measured exposed assets
    move against it. Reaction cells are {change, unit: 'bp' | 'pct', status} (§8): yields always in bp."""
    config = _risk()['post_event']
    t0 = aware(event['time_utc'])
    scenario_id, basis = resolved_scenario(event)
    scenario = next((s for s in event['scenarios'] if s['id'] == scenario_id), None)
    symbols = [a['symbol'] for a in event['affected_assets']]
    expected = {s: v for s, v in (scenario or {}).get('expected', {}).items() if s in symbols}
    rows = analytics.scheduled_reactions(store, event['event_id'], t0, symbols, expected, now, labels=config['windows'],
                                         baseline_seconds=config['baseline_max_age_seconds'],
                                         gap_seconds=config['window_max_gap_seconds'])
    reactions: dict[str, dict[str, dict]] = {}
    for row in rows:
        reactions.setdefault(row['symbol'], {})[row['horizon']] = {'change': round(float(row['change']), 4),
                                                                    'unit': row['unit'], 'status': row['status']}
    graded = [label for label in config['windows'] if any(r['horizon'] == label and r['expected_direction'] for r in rows)]
    horizon = graded[-1] if graded else next((label for label in reversed(config['windows'])
                                              if any(r['horizon'] == label for r in rows)), None)
    at = [r for r in rows if r['horizon'] == horizon and r['expected_direction']]
    against = [r['symbol'] for r in at if r['status'] in analytics.AGAINST]
    enough = len(at) >= config['min_measured']
    confirmation = analytics.basket_confirmation(event['event_id'], horizon, rows, expected)['confirmation_pct'] \
        if enough and horizon else None
    divergence = enough and len(against) / len(at) > config['divergence_share']
    label = short_label(event)
    facts = [f"{label}: {basis}"]
    if scenario:
        facts.append(f"Expected under scenario {scenario['id']} ({scenario['label']}): {_arrows(expected) or 'no direction'}")
    if horizon:
        facts.append(f'Measured at T+{horizon}: {_moves(reactions, horizon)}')
    if divergence:
        facts.append(f"MARKET DIVERGENCE: {len(against)} of {len(at)} measured exposed assets moved against the expected "
                     f"direction ({', '.join(against)}); no automatic trade in the event direction")
    elif enough and confirmation is not None:
        facts.append(f'Market confirmation {confirmation:.0f}/100 over {len(at)} measured exposed assets')
    elif not expected:
        facts.append('Market confirmation not graded: surprise direction unknown')
    closed = [s for s, live in _market_live(symbols, now).items() if not live]
    if closed:
        facts.append(f"Cannot be confirmed until markets reopen: {', '.join(closed)}")
    return {'event_id': event['event_id'], 'name': event['name'], 'event_class': event['event_class'], 'phase': event['phase'],
            'awaiting_reopen': closed,
            'time_utc': event['time_utc'], 'resolved_scenario': scenario_id, 'resolution_basis': basis,
            'surprise': event.get('surprise') or no_surprise(),
            'expected': expected, 'reactions': reactions, 'horizon': horizon,
            'measured': len(at), 'confirmation': confirmation, 'divergence': divergence, 'diverging': against,
            'exposed': symbols, 'research_recommended': divergence, 'summary_facts': facts}


def _moves(reactions: dict[str, dict[str, dict]], horizon: str) -> str:
    """'US2Y +3.10 bp, DXY +0.21%' at one horizon, in the order the reactions were measured."""
    return ', '.join(f"{s} {cell['change']:+.2f}{' bp' if cell['unit'] == 'bp' else '%'}" for s in reactions
                     if (cell := reactions[s].get(horizon)))


def released_reaction(store: Store, item: dict, now: datetime, post: list[dict]) -> dict | None:
    """The measured reaction of a released event for the opening screen (EVR-4): {horizon, text} at the last horizon
    with a measurement, from the post-event block while the event is live, else measured now from the stored ticks
    (a resolved event keeps its reaction until the Bucharest day ends). None before any window is measured."""
    config = _risk()['post_event']
    entry = next((p for p in post if p.get('event_id') == item.get('event_id')), None)
    if entry is None and item.get('time_known') and item.get('time_utc'):
        symbols = [a['symbol'] for a in item.get('affected_assets') or []]
        rows = analytics.scheduled_reactions(store, item['event_id'], aware(item['time_utc']), symbols, {}, now,
                                             labels=config['windows'], baseline_seconds=config['baseline_max_age_seconds'],
                                             gap_seconds=config['window_max_gap_seconds'])
        reactions: dict[str, dict[str, dict]] = {}
        for row in rows:
            reactions.setdefault(row['symbol'], {})[row['horizon']] = {'change': round(float(row['change']), 4), 'unit': row['unit']}
        horizon = next((label for label in reversed(config['windows']) if any(r['horizon'] == label for r in rows)), None)
    else:
        reactions, horizon = (entry or {}).get('reactions') or {}, (entry or {}).get('horizon')
    if not horizon:
        return None
    moved = _moves(reactions, horizon)
    return {'horizon': horizon, 'text': f'T+{horizon}: {moved}'} if moved else None


def record_history(store: Store, snapshot: dict) -> int:
    """Stores the T+1h impact of finished post-event measurements (share of measured exposed assets moving beyond their
    confirm tolerance, 0-100) under sched-history:<event_id>, for the dominance historical input. Idempotent."""
    config, prefix = _risk()['post_event'], _risk()['dominance']['history_prefix']
    last = config['windows'][-1]
    config_bands = settings()['reaction']['tolerances']
    kinds = {s: k for s, _, k in settings()['assets']}
    written = {}
    for item in snapshot.get('post_event') or []:
        moves = {s: float(cell['change'] if isinstance(cell, dict) else cell) for s, w in item['reactions'].items()
                 if (cell := w.get(last)) is not None}
        if item['phase'] != 'POST_EVENT' or len(moves) < config['min_measured']:
            continue
        beyond = sum(abs(v) >= (config_bands['symbols'].get(s) or config_bands['classes'].get(kinds.get(s, ''))
                                or config_bands['default'][_unit(s)])['confirm'] for s, v in moves.items())
        written[f"{prefix}{item['event_id']}"] = {'event_id': item['event_id'], 'event_class': item['event_class'],
                                                  'impact': round(100 * beyond / len(moves), 1), 'measured': len(moves),
                                                  'recorded_at': snapshot.get('generated_at')}
    store.put_many('event_relationships', written)
    return len(written)


# --- per-asset risk and the pre-event overlay (§3) ----------------------------------------------------------------------

def _counts(event: dict, now: datetime, horizon: float) -> bool:
    if event['phase'] in POST or event['phase'] == ACTIVE:
        return True
    if event['phase'] not in UPCOMING or not event.get('time_utc'):
        return False
    return _minutes(aware(event['time_utc']), now) <= horizon


def exposure_factor(weight: float) -> float:
    """floor + (1 - floor) x exposure weight for an exposed asset (asset_risk.exposure_floor); 0 when unexposed."""
    floor = float(_risk()['asset_risk']['exposure_floor'])
    return floor + (1 - floor) * weight if weight > 0 else 0.0


def asset_risks(symbol: str, events: Iterable[dict], now: datetime) -> list[dict]:
    """relevance x (floor + (1 - floor) x exposure weight) of every active or near-term event exposing the symbol
    (upcoming = within asset_risk_horizon_minutes), highest first. Potential (unverified) events are never passed in."""
    horizon = _risk()['asset_risk_horizon_minutes']
    found = []
    for event in events:
        weight = next((a['weight'] for a in event['affected_assets'] if a['symbol'] == symbol), 0.0)
        if weight and _counts(event, now, horizon):
            found.append({'score': round(event['relevance_score'] * exposure_factor(weight), 1), 'event': event, 'weight': weight})
    return sorted(found, key=lambda r: -r['score'])


def asset_event_risk(symbols: Iterable[str], events: list[dict], now: datetime) -> dict[str, dict]:
    result = {}
    for symbol in symbols:
        top = next(iter(asset_risks(symbol, events, now)), None)
        event = top['event'] if top else {}
        score = top['score'] if top else 0.0
        result[symbol] = {'score': score, 'level': level_for(score), 'event_id': event.get('event_id'),
                          'minutes_until': event.get('minutes_until_event'), 'phase': event.get('phase'),
                          'event_name': event.get('name')}
    return result


def trade_status(symbol: str, decision: dict | None, events: list[dict], now: datetime, *, session_state: str | None = None,
                 divergent: dict[str, str] | None = None) -> dict:
    """Overlay on the published decision (never changed): WAIT FOR <EVENT> when a side exists and an exposing event is
    within wait_window_minutes at event risk >= wait_threshold, or is RELEASED/IN_PROGRESS; CONDITIONAL LONG/SHORT when the
    confirmation is weak (a lean, market confirmation below threshold, a market divergence) or event risk >=
    conditional_threshold; LONG/SHORT for an actionable setup with low event risk; WAIT without a side.
    adjusted_confidence = |decision score| x (1 - penalty), penalty = clamp((risk - start) / (100 - start)) x max_penalty."""
    config = _risk()['adjustment']
    risks = asset_risks(symbol, events, now)
    top = risks[0] if risks else None
    score = top['score'] if top else 0.0
    decision = decision or {}
    category = decision.get('category') or decision.get('direction')
    side = _side(category)
    base = abs(value) if (value := _num(decision.get('score'))) is not None else None
    penalty = _clamp((score - config['penalty_start']) / (100 - config['penalty_start']), 0.0, 1.0) * config['max_penalty']
    reasons: list[str] = []
    status = 'WAIT'
    if side:
        label = 'LONG' if side > 0 else 'SHORT'
        waiting = None
        for item in risks:
            event, minutes = item['event'], item['event'].get('minutes_until_event')
            if event['timing'] == 'SCHEDULED' and event['phase'] in LIVE and item['score'] >= config['in_progress_min_risk']:
                waiting = (event, f"{event['name']} in progress (event risk {item['score']:.0f})")
            elif event['time_known'] and minutes is not None and 0 <= minutes <= config['wait_window_minutes'] \
                    and item['score'] >= config['wait_threshold']:
                waiting = (event, f"{event['name']} within {config['wait_window_minutes']} min (event risk {item['score']:.0f})")
            if waiting:
                break
        if waiting:
            status = f'WAIT FOR {short_label(waiting[0])}'
            reasons.append(waiting[1])
        else:
            if not _setup(category):
                reasons.append(f'{category} is a lean, not a setup')
            confirmation = _num(decision.get('market_confirmation'))
            if confirmation is not None and confirmation < config['confirmation_threshold']:
                reasons.append(f"market confirmation {confirmation:.0f} < {config['confirmation_threshold']}")
            elif confirmation is None and config['unmeasured_confirmation_is_weak']:
                reasons.append('market confirmation not measured')
            if top and score >= config['conditional_threshold']:
                reasons.append(f"event risk {score:.0f} from {top['event']['name']}")
            if (divergent or {}).get(symbol):
                reasons.append(f"MARKET DIVERGENCE after {(divergent or {})[symbol]}")
            status = f'CONDITIONAL {label}' if reasons else label
    event = top['event'] if top else {}
    return {'score': score, 'level': level_for(score), 'event_id': event.get('event_id'),
            'minutes_until': event.get('minutes_until_event'), 'phase': event.get('phase'), 'event_name': event.get('name'),
            'trade_status': status, 'adjusted_confidence': round(base * (1 - penalty), 1) if base is not None else None,
            'base_confidence': base, 'penalty': round(penalty, 4), 'confidence_basis': config['base_confidence_basis'],
            'awaiting_reopen': session_state is not None and session_state != 'LIVE',
            'status_reason': '; '.join(reasons) or None}


def _session_state(asset: dict) -> str | None:
    return ((asset.get('quote') or {}).get('session') or asset.get('session') or {}).get('state')


def _divergent(post: list[dict]) -> dict[str, str]:
    """Exposed symbol -> name of a post-event divergence it belongs to."""
    return {s: p['name'] for p in post if p['divergence'] for s in p['exposed']}


def overlays(assets: list[dict], events: list[dict], post: list[dict], now: datetime) -> dict[str, dict]:
    divergent = _divergent(post)
    return {a['symbol']: trade_status(a['symbol'], a.get('decision'), events, now, session_state=_session_state(a),
                                      divergent=divergent) for a in assets}


def apply_assets(assets: list[dict], per_asset: dict[str, dict]) -> None:
    """Adds the per-asset event_risk block beside each published decision (the decision itself is left untouched)."""
    for asset in assets:
        asset['event_risk'] = per_asset.get(asset['symbol'])


# --- regime cause and market view (§2, spec §18-§19, §23-§24) ------------------------------------------------------------

def regime_cause(regime: dict | None) -> dict | None:
    """First configured cause whose regimes are all active (e.g. RISK_OFF + GROWTH_SCARE before plain RISK_OFF)."""
    if not regime or regime.get('posture') in {None, 'INSUFFICIENT_DATA'}:
        return None
    active = {r for r in regime.get('regimes') or [] if r != 'MIXED'}
    return next((dict(e) for e in _exp()['regime_effects'] if set(e['regimes']) <= active), None)


def change_basis(source: dict, live: bool) -> str:
    """How a change was measured, from a regime feature source or a quote: '24h via PAXG' (a proxy), 'live 24h'
    (rolling 24h), 'last close Fri 25 Sep' (', stale' for a STALE close), 'vs previous fix', 'live intraday' or
    'latest quote'."""
    rolling = source.get('change_basis') == 'rolling_24h'
    if source.get('via'):
        return f"{'24h ' if rolling else ''}via {source['via']}"
    if rolling:
        return 'live 24h' if live else '24h, not live'
    if source.get('basis') == 'last_close' or source.get('close_date') or source.get('close_status'):
        day = day_label(source.get('close_date'))
        return f"last close{f' {day}' if day else ''}" + (', stale' if source.get('close_status') == 'STALE' else '')
    if source.get('change_basis') == 'previous_fix':
        return 'vs previous fix'
    return 'live intraday' if live else 'latest quote'


def _change(symbol: str, asset: dict | None, regime: dict | None) -> tuple[float | None, str, str | None]:
    """Prefer a fresh quote or approved direction proxy, then historical regime inputs.

    Return the change, unit and its own observation basis. The regime-wide label is
    only a compatibility fallback for older payloads without per-feature sources.
    """
    unit = _unit(symbol)
    field = 'change_bp' if unit == 'bp' else 'change_pct'
    current, current_source = current_change(asset) if asset else ({}, {})
    if field in current:
        return current[field], unit, change_basis(current_source, True)
    regime = regime or {}
    value = _num((regime.get('features') or {}).get(symbol, {}).get(field))
    if value is not None:
        source = (regime.get('feature_sources') or {}).get(symbol)
        basis = change_basis(source, source.get('basis') == 'live') if source else regime.get('basis_label')
        return value, unit, basis
    quote = (asset or {}).get('quote') or {}
    value = _num(quote.get(field))
    return value, unit, change_basis(quote, quote.get('freshness_status') == 'FRESH') if value is not None else None


def _display(symbol: str, direction: int) -> str:
    text = f'{symbol} {ARROWS[direction]}'
    return text + (f" (yield; Treasuries {ARROWS[-direction]})" if _unit(symbol) == 'bp' and symbol.startswith('US') else '')


# EVR-10: only a current move confirms. A decision side is listed as context and never confirms, so the closing reason
# names what is missing (it used to deny an aligned decision side the list had just shown).
NOT_CONFIRMING = 'current price change does not confirm (no move beyond noise in the implied direction)'


def market_view(assets: list[dict], regime: dict | None, per_asset: dict[str, dict]) -> dict[str, list[dict]]:
    """Favored / pressured from the active regime cause (config regime_effects), each kept only when the asset's own
    current change exceeds noise in the implied direction; otherwise it is non-confirming (NOT_CONFIRMING). A decision
    side is context: it is listed among the facts and never confirms. At most max_items per list; nothing is forced."""
    empty: dict[str, list[dict]] = {'favored': [], 'pressured': [], 'non_confirming': []}
    cause = regime_cause(regime)
    if cause is None:
        return empty
    limit = _risk()['lists']['max_items']
    by_symbol = {a['symbol']: a for a in assets}
    result: dict[str, list[dict]] = {'favored': [], 'pressured': [], 'non_confirming': []}
    for key, stance in (('favored', 'FAVORED'), ('pressured', 'PRESSURED')):
        for symbol, expected in cause[key].items():
            asset = by_symbol.get(symbol)
            decision = (asset or {}).get('decision') or {}
            category = decision.get('category') or decision.get('direction')
            side = _side(category)
            change, unit, basis = _change(symbol, asset, regime)
            moved = 0 if change is None or abs(change) < _noise(symbol) else (1 if change > 0 else -1)
            facts = [f"{cause['label']}: {_display(symbol, expected)}"]
            if change is not None:
                facts.append(f"{symbol} {change:+.2f}{' bp' if unit == 'bp' else '%'} ({basis or 'latest quote'})")
            if side:
                facts.append(f"Decision {category}" + (f" ({score:+.0f})" if (score := _num(decision.get('score'))) is not None else ''))
            current, _ = current_change(asset) if asset else ({}, {})
            historical_only = not current
            # A saved decision or historical close cannot replace current direction evidence.
            agrees = not historical_only and moved == expected
            if historical_only:
                facts.append('Historical context only; current-session confirmation is unavailable')
            risk = per_asset.get(symbol) or {}
            item = {'symbol': symbol, 'stance': stance if agrees else 'NOT CONFIRMING', 'implied': stance,
                    'expected_direction': expected, 'display': _display(symbol, expected), 'regime_cause': cause['id'],
                    'event_risk': {'score': risk.get('score'), 'level': risk.get('level'), 'minutes_until': risk.get('minutes_until')},
                    'event_id': risk.get('event_id'), 'awaiting_reopen': risk.get('awaiting_reopen'),
                    'reason_facts': facts if agrees else [*facts, NOT_CONFIRMING]}
            result[key if agrees else 'non_confirming'].append(item)
    return {k: v[:limit] for k, v in result.items()}


# --- what can break, trade watch, mode (§3, spec §6, §20-§21, §25, §30) --------------------------------------------------

def _adverse(event: dict, view: list[tuple[str, int]]) -> dict | None:
    """The scenario of an event that most opposes the given (symbol, direction) view."""
    best, best_score = None, 0
    for scenario in event.get('scenarios') or []:
        score = sum(-scenario['expected'].get(s, 0) * d for s, d in view)
        if score > best_score:
            best, best_score = scenario, score
    return best


def what_can_break(dominant: dict | None, lists: dict[str, list[dict]], regime: dict | None, watch: list[dict]) -> list[dict]:
    """Deterministic list: the dominant event's scenario adverse to the current view, reversals of the moving regime
    drivers, and the invalidation levels of the trade-watch setups. A reversal is a FUTURE condition: when the current
    observations already show it (regime.divergences: the current value opposes the historical input the active
    regime rests on), the item says so with kind 'observed' instead of naming it as a risk still ahead (RI-1)."""
    items: list[dict] = []
    view = [(i['symbol'], i['expected_direction']) for i in lists['favored'] + lists['pressured']]
    view = view or [(c['asset'], c['side']) for c in watch if c.get('side')]
    if dominant:
        scenario = _adverse(dominant, view)
        if scenario:
            items.append({'text': f"{short_label(dominant)} {scenario['label']} (possible): {_arrows(scenario['expected'])}",
                          'kind': 'event', 'source': dominant['event_id']})
    regime = regime or {}
    active = set(regime.get('regimes') or [])
    observed = regime.get('features') or {}
    reversals: dict[str, set[str]] = {}
    for rule in settings()['regime']['rules']:
        if rule['regime'] not in active:
            continue
        for condition in rule['required'] + rule['supporting']:
            value = _num(observed.get(condition['feature'], {}).get(condition.get('field', 'change_pct')))
            if value is not None and OPERATORS[condition['op']](value, condition['value']):
                reversals.setdefault(condition['feature'], set()).add(
                    'lower' if condition['op'] in {'>', '>='} else 'higher')
    observed_now = {d['feature']: d for d in regime.get('divergences') or [] if isinstance(d, dict) and d.get('feature')}
    for driver in (regime or {}).get('drivers') or []:
        for item in driver.get('inputs') or []:
            directions = reversals.get(item['feature'], set())
            if len(directions) != 1:
                continue
            direction = next(iter(directions))
            day = day_label(item.get('close_date'))
            stale = f" (stale close{f' {day}' if day else ''})" if item.get('close_status') == 'STALE' else ''
            current = _num((observed_now.get(item['feature']) or {}).get('current_value'))
            if current is not None and (current < 0) == (direction == 'lower') and current != 0:
                detail = str(observed_now[item['feature']].get('text') or '').split(';')[0].strip()
                items.append({'text': f"{item['feature']} reversal {direction} already under way"
                              + (f": {detail}" if detail else ''), 'kind': 'observed', 'source': driver['driver']})
                continue
            items.append({'text': f"{item['feature']} reversal {direction}{stale}",
                          'kind': 'driver', 'source': driver['driver']})
    for card in watch:
        level = _num(card.get('invalidation_level'))
        if card.get('side') and level is not None:
            verb = 'breaks below' if card['side'] > 0 else 'reclaims'
            items.append({'text': f"{card['asset']} {verb} {fmt(level)}", 'kind': 'level', 'source': card['asset']})
    unique = list({i['text']: i for i in items}.values())
    return unique[:_risk()['lists']['what_can_break_max']]


def _event_brief(event: dict | None) -> dict | None:
    keys = ('event_id', 'name', 'event_class', 'time_utc', 'time_bucharest', 'date_bucharest', 'time_known',
            'minutes_until_event', 'phase', 'relevance_level', 'relevance_score', 'timing')
    return {k: event.get(k) for k in keys} if event else None


def _clause(event: dict | None, symbol: str, side: int) -> tuple[str | None, str | None]:
    """Event-conditional confirmation / invalidation from the event's scenarios for this asset's side."""
    if not event or not side:
        return None, None
    favorable = next((s for s in event['scenarios'] if s['expected'].get(symbol) == side), None)
    adverse = next((s for s in event['scenarios'] if s['expected'].get(symbol) == -side), None)

    def text(scenario: dict | None) -> str | None:
        if not scenario:
            return None
        others = {s: v for s, v in scenario['expected'].items() if s != symbol}
        return f"after {short_label(event)}: {scenario['label']} reaction" + (f" ({_arrows(others, 3)})" if others else '')
    return text(favorable), text(adverse)


def trade_watch(assets: list[dict], per_asset: dict[str, dict], lists: dict[str, list[dict]], events: list[dict],
                regime: dict | None) -> list[dict]:
    """At most trade_watch_max event-aware cards (spec §20-§21): setups and leans by |score|, then listed assets."""
    config = _risk()['lists']
    cause = regime_cause(regime)
    implied = {s: d for key in ('favored', 'pressured') for s, d in (cause or {}).get(key, {}).items()}
    by_symbol = {a['symbol']: a for a in assets}
    sided = sorted((a for a in assets if _side(((a.get('decision') or {}).get('category')))),
                   key=lambda a: -abs(_num((a.get('decision') or {}).get('score')) or 0))
    listed = [by_symbol[i['symbol']] for key in ('favored', 'pressured') for i in lists[key] if i['symbol'] in by_symbol]
    horizon = settings().get('autolearn', {}).get('report_horizon') or settings()['decision']['report_horizon_fallback']
    shown = _risk()['dashboard_min_relevance']
    confidence = _num((regime or {}).get('confidence'))
    moving = [f"{d['driver']} {d.get('arrow') or ''}".strip() for d in (regime or {}).get('drivers') or []
              if d.get('direction') in {'up', 'down'}]
    cards: list[dict] = []
    for asset in list({a['symbol']: a for a in sided + listed}.values())[:config['trade_watch_max']]:
        symbol, decision = asset['symbol'], asset.get('decision') or {}
        overlay = per_asset.get(symbol) or {}
        side = _side(decision.get('category'))
        direction = side or implied.get(symbol, 0)
        favorability = None
        if symbol in implied and direction and confidence is not None:
            favorability = round(confidence if implied[symbol] == direction else -confidence, 1)
        driving = next((e for e in events if e['event_id'] == overlay.get('event_id')), None)
        upcoming = driving or next((e for e in events if e['phase'] in UPCOMING and e['relevance_score'] >= shown
                                    and any(a['symbol'] == symbol for a in e['affected_assets'])), None)
        confirm, invalidate = _clause(upcoming, symbol, side)
        probability = decision.get('historical_probability') or {}
        calibrated = isinstance(probability, dict) and _num(probability.get('p')) is not None
        cards.append({
            'asset': symbol, 'symbol': symbol, 'trade_status': overlay.get('trade_status', 'WAIT'), 'side': side,
            'awaiting_reopen': overlay.get('awaiting_reopen'), 'status_reason': overlay.get('status_reason'),
            'decision_category': decision.get('category'), 'decision_score': _num(decision.get('score')),
            'regime_favorability': favorability,
            'regime_favorability_basis': (f"{(cause or {}).get('label')}: {_display(symbol, implied[symbol])}; rule coverage {confidence:g}"
                                          + (f"; drivers {', '.join(moving)}" if moving else '')) if favorability is not None else
            'not in the regime effects of the active cause' if cause else 'no regime cause active',
            'evidence_confidence': _num(decision.get('evidence_confidence')),
            'market_confirmation': _num(decision.get('market_confirmation')),
            'event_risk': {'score': overlay.get('score'), 'level': overlay.get('level'), 'event_id': overlay.get('event_id')},
            'adjusted_confidence': overlay.get('adjusted_confidence'),
            'historical_probability': probability if calibrated else None,
            'historical_probability_reason': None if calibrated else (probability.get('reason') if isinstance(probability, dict)
                                                                      else None) or 'not calibrated',
            'horizon': horizon, 'why': list(dict.fromkeys([
                *(decision.get('blocks') or []), *(decision.get('why') or []),
                *why_bullets(symbol, decision.get('component_detail') or {})]))[:config['why_max']],
            'but': [f"{upcoming['name']} may materially change {', '.join(a['symbol'] for a in upcoming['affected_assets'][:3])}"]
            if upcoming else [],
            'upcoming_event': _event_brief(upcoming),
            'confirmation': ' AND '.join(t for t in (decision.get('confirmation'), confirm) if t) or None,
            'invalidation': ' OR '.join(t for t in (decision.get('invalidation'), invalidate) if t) or None,
            'invalidation_level': _num(decision.get('invalidation_level'))})
    return cards


def markets_open(now: datetime) -> dict[str, bool]:
    return {key: sessions.market_state(kind, now) == 'LIVE' for key, kind in _risk()['markets_open_classes'].items()}


def risk_mode(scheduled: list[dict], active: list[dict], now: datetime | None = None) -> dict:
    """EVENT RISK MODE: a timed event within risk_mode minutes (or RELEASED / IN_PROGRESS) that is CRITICAL or whose
    class importance reaches importance_min (so it does not hinge on a composite sitting at the CRITICAL cutoff); a
    RELEASED / IN_PROGRESS central-bank decision at a live level (a well-priced FOMC is VERY HIGH, yet its press
    conference is the risk); or a verified UNRESOLVED unscheduled event that is CRITICAL or, like a scheduled one,
    whose class importance reaches importance_min while it exposes tracked assets and was first seen at most
    active_minutes ago (EVR-3: no unscheduled class could reach the CRITICAL composite, so a verified Hormuz closure
    never switched the mode on). Date-only rows never qualify."""
    config = _risk()['risk_mode']
    now = aware(now) if now else utcnow()

    def fresh(event: dict) -> bool:
        seen = _stamp(event.get('first_seen_utc'))
        return seen is not None and 0 <= (now - seen).total_seconds() / 60 <= config['active_minutes']

    def unscheduled(event: dict) -> bool:
        if event.get('status') != 'UNRESOLVED':
            return False
        prior = _num(class_config(event['event_class']).get('importance')) or 0
        return event['relevance_level'] == config['level'] or (
            prior >= config['importance_min'] and bool(event['affected_assets']) and fresh(event))

    def near(event: dict) -> bool:
        minutes = event['minutes_until_event']
        return event['phase'] in LIVE or (minutes is not None and 0 <= minutes <= config['minutes'])

    def qualifies(event: dict) -> bool:
        # The configured class importance (never a vendor impact fallback) of an event that exposes tracked assets.
        prior = _num(class_config(event['event_class']).get('importance')) or 0
        important = event['relevance_level'] == config['level'] or (prior >= config['importance_min'] and bool(event['affected_assets']))
        live_decision = event['phase'] in LIVE and event['event_class'] in config['live_classes'] \
            and event['relevance_level'] in config['live_levels']
        return important or live_decision
    candidates = [e for e in scheduled if e['time_known'] and near(e) and qualifies(e)]
    candidates += [e for e in active if unscheduled(e)]
    if not candidates:
        return {'active': False, 'event_id': None, 'name': None, 'time_bucharest': None, 'time_utc': None, 'minutes_until': None,
                'impact_10': None, 'impact': None, 'relevance_score': None, 'relevance_level': None, 'affected_groups': [],
                'phase': None}
    event = max(candidates, key=lambda e: e['relevance_score'])
    symbols = {a['symbol'] for a in event['affected_assets']}
    groups = [name for name, members in _exp()['asset_groups'].items() if symbols & set(members)]
    # impact: the calendar row's own 0-10 impact (as the calendar shows it); impact_10 is relevance / 10.
    return {'active': True, 'event_id': event['event_id'], 'name': event['name'], 'time_bucharest': event.get('time_bucharest'),
            'time_utc': event.get('time_utc'), 'minutes_until': event.get('minutes_until_event'), 'impact_10': event['impact_10'],
            'impact': event.get('impact'), 'relevance_score': event['relevance_score'],
            'relevance_level': event['relevance_level'], 'affected_groups': groups, 'phase': event['phase']}


def _next(scheduled: list[dict]) -> dict | None:
    config = _risk()
    pending = [e for e in scheduled if e['phase'] in UPCOMING | LIVE]
    for minimum in (config['next_event_min_relevance'], config['dashboard_min_relevance']):
        found = [e for e in pending if e['relevance_score'] >= minimum]
        if found:
            return min(found, key=lambda e: (e['time_utc'] or '', -e['relevance_score']))
    return None


def _regime_block(regime: dict | None) -> dict:
    regime = regime or {}
    cause = regime_cause(regime)
    keys = ('posture', 'regimes', 'confidence', 'coverage_pct', 'basis', 'basis_label', 'missing_required', 'method', 'as_of',
            'current_drivers', 'divergences')
    # Driver inputs keep only what labels a stale close (the interpreter names it); values stay in 'input'.
    return dict({k: regime.get(k) for k in keys},
                drivers=[dict({k: d.get(k) for k in ('driver', 'direction', 'arrow', 'input', 'missing')},
                              inputs=[{k: i.get(k) for k in ('feature', 'close_status', 'close_date')}
                                      for i in d.get('inputs') or [] if isinstance(i, dict)])
                         for d in regime.get('drivers') or []],
                evaluated=[{k: e.get(k) for k in ('regime', 'state', 'confidence')} for e in regime.get('evaluated') or []],
                cause={'id': cause['id'], 'label': cause['label']} if cause else None)


# --- entry points ------------------------------------------------------------------------------------------------------

def calendar_context(rows: list[dict], now: datetime, *, regime: dict | None = None, news: Iterable[dict] | None = None,
                     stored_history: dict[str, list[float]] | None = None) -> list[dict]:
    """Expose the existing conditional templates on the calendar, without inventing release values.

    Numeric bands already supplied by a provider are preserved. Templates carry
    no numeric bounds, so the UI cannot mistake them for a realised surprise.
    regime / news / stored_history are the inputs the dashboard's event layer uses (EVR-7): with them a row shows the
    same relevance, level and phase as the same event on the dashboard; without them the regime sensitivity is the
    static exposure mean.
    """
    built = build_events(rows, news or [], now, regime=regime, stored_history=stored_history)
    by_row = {e['row_id']: e for e in built['scheduled'] if e.get('row_id')}
    result = []
    for row in rows:
        event = by_row.get(row.get('id'))
        if not event:
            result.append(row)
            continue
        templates = [dict(s, implications=s['expected'], interpretation='Conditional scenario, not a forecast'
                          + (f". {s['note']}" if s.get('note') else '')) for s in event['scenarios']]
        result.append(dict(row, scenarios=row.get('scenarios') or templates,
                           phase=event['phase'], relevance_score=event['relevance_score'],
                           relevance_level=event['relevance_level']))
    return result


def stored_regime(store: Store, now: datetime) -> dict | None:
    """Latest regime stored by the analysis cycle, while younger than stored_regime_max_age_seconds (cheap read for the
    per-asset overlay; the dashboard passes its own freshly computed regime)."""
    rows = store.list('market_regimes', 1)
    stamp = _stamp(rows[0].get('as_of')) if rows else None
    if stamp is None or not 0 <= (aware(now) - stamp).total_seconds() <= _risk()['stored_regime_max_age_seconds']:
        return None
    return rows[0]


def frame(store: Store, now: datetime, *, assets: list[dict], news: list[dict], calendar: list[dict], regime: dict | None,
          interpretation: dict | None) -> dict:
    """Events, post-event measurements and the per-asset overlay: everything that does not need the regime lists.
    Surprise classifications come from the stored AI interpretation runs (read only when an assessed event is live or
    post-event) plus the supplied interpretation block."""
    built = build_events(calendar, news, now, regime=regime, interpretation=interpretation, stored_history=history(store),
                         interpretations=lambda: interpretation_rows(store, now),
                         official_series=lambda key: series_rows(store, key))
    shown = _risk()['dashboard_min_relevance']
    post = [post_event(store, e, now) for e in built['scheduled']
            if e['phase'] in POST and e['time_known'] and e['relevance_score'] >= shown]
    scoring_events = built['scheduled'] + built['active']
    per_asset = overlays(assets, scoring_events, post, now)
    for item in built['released']:
        # The outcome text carries the measured reaction once a window is measured ('... · reaction T+30m: ...').
        reaction = released_reaction(store, item, now, post)
        item['reaction'] = reaction
        if reaction and item.get('summary'):
            item['summary'] = f"{item['summary']} · reaction {reaction['text']}"
    return dict(built, post=post, per_asset=per_asset)


def _inputs(hub: Any, now: datetime, assets: list[dict] | None, events: list[dict] | None, calendar: list[dict] | None,
            regime: dict | None) -> tuple[list[dict], list[dict], list[dict], dict | None]:
    assets = hub.assets(now, event_overlay=False) if assets is None else assets
    events = hub.events(_risk()['news_events_limit']) if events is None else events
    calendar = hub.calendar() if calendar is None else calendar
    regime = hub.regime(assets, events, last_close=True) if regime is None else regime
    return assets, events, calendar, regime


def snapshot(hub: Any, now: datetime | None = None, *, assets: list[dict] | None = None, events: list[dict] | None = None,
             calendar: list[dict] | None = None, regime: dict | None = None, interpretation: Any = _UNSET) -> dict:
    """The dashboard 'event_risk' block (docs/EVENT_AWARENESS.md §4), computed once. Inputs the caller already holds
    (published assets without the overlay, news events, calendar rows, display regime, None included) are reused;
    missing ones are read from the hub. Surprise classifications come from the stored AI runs; a supplied
    interpretation block (e.g. the latest) is considered as well."""
    now = aware(now) if now else utcnow()
    assets, events, calendar, regime = _inputs(hub, now, assets, events, calendar, regime)
    interpretation = None if interpretation is _UNSET else interpretation
    built = frame(hub.store, now, assets=assets, news=events, calendar=calendar, regime=regime, interpretation=interpretation)
    scheduled, active, per_asset = built['scheduled'], built['active'], built['per_asset']
    config = _risk()
    lists = market_view(assets, regime, per_asset)
    watch = trade_watch(assets, per_asset, lists, scheduled + active, regime)
    ranked = [e for e in scheduled + active if e['phase'] != 'RESOLVED']
    dominant = max(ranked, key=lambda e: e['dominance_score'], default=None)
    dominant = dominant if dominant and dominant['dominance_score'] > config['dominance']['threshold'] else None
    shown = [e for e in scheduled if e['relevance_score'] >= config['dashboard_min_relevance']]
    incomplete = [{'event_id': e['event_id'], 'name': e['name'], 'level': e['relevance_level'], 'flag': INCOMPLETE,
                   'reasons': e['data_incomplete']} for e in shown + active if e['data_incomplete_flag']]
    return {'generated_at': now.isoformat(), 'timezone': 'Europe/Bucharest', 'markets_open': markets_open(now),
            'mode': risk_mode(scheduled, active, now), 'next_event': _next(scheduled), 'dominant_event': dominant,
            'events': shown, 'active_events': active, 'potential_events': built['potential'],
            'asset_event_risk': per_asset, **lists, 'what_can_break': what_can_break(dominant, lists, regime, watch),
            'trade_watch': watch,
            'post_event': built['post'], 'released_today': built['released'], 'data_incomplete': incomplete,
            'regime': _regime_block(regime),
            'versions': {'event_risk': settings().get('event_risk_version'),
                         'event_exposure': settings().get('event_exposure_version')}}


def overlay(hub: Any, assets: list[dict], now: datetime | None = None) -> None:
    """Per-asset event_risk blocks for /api/assets: news events, calendar rows and the latest stored cycle regime are read
    once; no per-asset query."""
    now = aware(now) if now else utcnow()
    store = hub.store
    built = frame(store, now, assets=assets, news=hub.events(_risk()['news_events_limit']), calendar=hub.calendar(),
                  regime=stored_regime(store, now), interpretation=None)
    apply_assets(assets, built['per_asset'])


def upcoming(hub: Any, now: datetime | None = None, min_relevance: float = 0.0, **inputs: Any) -> list[dict]:
    """GET /api/events/upcoming: every scheduled event not yet RESOLVED (including those hidden from the dashboard below
    dashboard_min_relevance), in time order."""
    now = aware(now) if now else utcnow()
    assets, events, calendar, regime = _inputs(hub, now, inputs.get('assets'), inputs.get('events'), inputs.get('calendar'),
                                               inputs.get('regime'))
    built = build_events(calendar, events, now, regime=regime, interpretation=inputs.get('interpretation'),
                         stored_history=history(hub.store), interpretations=lambda: interpretation_rows(hub.store, now))
    return [e for e in built['scheduled'] if e['relevance_score'] >= min_relevance]


def trigger_state(snapshot: dict) -> dict:
    """Comparable state for refresh.detect_triggers: phases (sub-events as '<event_id>:<SUB_EVENT>'), 60/15-minute window
    buckets of important timed events, risk mode, dominant event, regime posture and CRITICAL ids; plus post-event
    divergences and markets_open for the CROSS_ASSET_DIVERGENCE / MARKET_OPEN_CLOSE triggers."""
    config = _risk()
    events = [e for e in (snapshot.get('events') or []) + (snapshot.get('active_events') or []) if isinstance(e, dict)]
    phases: dict[str, str] = {}
    windows: dict[str, str | None] = {}
    buckets = sorted(config['trigger_windows'].items(), key=lambda item: item[1])
    for event in events:
        phases[event['event_id']] = event['phase']
        for sub in event.get('sub_events') or []:
            phases[f"{event['event_id']}:{sub['sub_event']}"] = sub['phase']
        minutes = event.get('minutes_until_event')
        important = event.get('relevance_level') in config['trigger_levels']
        windows[event['event_id']] = next((name for name, limit in buckets if event.get('time_known') and important
                                           and minutes is not None and 0 <= minutes <= limit), None)
    return {'phases': phases, 'windows': windows, 'risk_mode': bool((snapshot.get('mode') or {}).get('active')),
            'dominant_event_id': (snapshot.get('dominant_event') or {}).get('event_id'),
            'posture': (snapshot.get('regime') or {}).get('posture'),
            'critical_ids': sorted(e['event_id'] for e in events if e.get('relevance_level') == 'CRITICAL'),
            'divergences': sorted(p['event_id'] for p in snapshot.get('post_event') or [] if p.get('divergence')),
            'markets_open': dict(snapshot.get('markets_open') or {})}
