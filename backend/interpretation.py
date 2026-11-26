"""AI market interpretation (docs/AI_AGENTS.md, docs/EVENT_AWARENESS.md §5 and §8; spec §8, §9, §12, §23-§25, §32-§34).

The backend owns every structured fact (regime, drivers, lists, events, scores, trade statuses). The market_interpreter
role only writes a short narrative about them: ai_view (1-2 sentences), market_summary (at most 4), at most 3 key risks,
one sentence of reason for the dominant event and one sentence for at most 2 events. It never supplies an event, a
time, a consensus, a probability, a score or a level.

- The model reads a small brief (brief(): at most max_brief_chars characters), not the whole event-risk block.
- An answer is repaired before it is judged (sanitize()): a sentence or item that cites a number, id or event the
  brief does not contain, a countdown, a certainty, an unverified report stated as fact or a trade claim against the
  backend status is removed; a classification the event's phase or documents do not allow becomes UNDETERMINED. Only
  an answer whose ai_view or market_summary has nothing usable left is rejected, and a rejection is never sent again
  blind (ai_runtime.validation: one correction for a schema-shape error only, no backup model after a rejection).
- A stored AI text is served while it still holds (core_hash, bindings): no request is made for a state that did not
  change, for a state whose answer was already rejected (retry_failed_after_seconds) or for a minor change within
  min_ai_interval_seconds. Otherwise the deterministic summary of the current snapshot is served, labelled so.

A central-bank surprise classification rests on the official documents the model received: verified statements,
minutes and press releases of that event (official_evidence, with evidence ids). Each stored run records per event
{assessment, source, evidence_ids, validated}; validated is true only for an AI classification that passed every
check and cites that event's official documents after guidance was published. Nothing here changes a published
decision."""
import asyncio
import hashlib
import importlib
import json
import math
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from backend.ai import numeric_allowlist, prompts
from backend.ai_budget import REFUSAL_MESSAGES
from backend.audit import (
    BASIS_POINTS,
    PERCENT,
    SPELLED_QUANTITY,
    clocks,
    numbers,
    quantities,
    unit_values,
    untraceable,
    value,
)
from backend.config import rules, rules_view
from backend.database import RECORDS, Store, uid, utcnow
from backend.engines import aware, bucharest
from backend.notifications import status_text
from backend.providers.base import provider_label
from backend.regime import day_label
from backend.schemas import (
    AI_VIEW_SENTENCES,
    AI_VIEW_WORDS,
    COUNTDOWN,
    KEY_RISK_CHARS,
    LEGACY_OUTPUT_LISTS,
    MAX_KEY_RISKS,
    MAX_NEXT_EVENTS,
    SUMMARY_SENTENCES,
    SURPRISE_ASSESSMENTS,
    MarketInterpretationOutput,
    certainty_claim,
    sentences,
)
from backend.security import instruction_like, screen_inputs, screen_text

ROLE = 'market_interpreter'
PARENT_ROLE = 'chief_analyst'
TABLE = 'interpretations'
DETERMINISTIC_LABEL = 'Deterministic summary — AI unavailable'
# Inherited from the chief analyst: the models only. A second opinion is never run for this role (AIService).
ASSIGNMENT_KEYS = ('primary', 'backup')
MARKETS = {'us_equities': 'US equities', 'us_rates': 'US rates', 'fx': 'FX', 'commodities': 'commodities'}
PRE_RELEASE = {'SCHEDULED', 'APPROACHING', 'IMMINENT'}
IN_PROGRESS = {'RELEASED', 'IN_PROGRESS'}
FINISHED = {'POST_EVENT', 'RESOLVED'}
# Coarse phase of an event for the text: the narrative says the same while an event is scheduled, approaching or
# imminent, so those three are one stage (a phase step alone never asks for a new AI text).
STAGES = {**dict.fromkeys(PRE_RELEASE, 'PRE'), **dict.fromkeys(IN_PROGRESS, 'LIVE'), 'POST_EVENT': 'POST', 'RESOLVED': 'DONE'}
LISTS = ('favored', 'pressured', 'non_confirming')
# event_risk.market_view marks a list item that rests on the last close alone with this fact: there is no current
# observation of that asset, so it is not 'not confirming now'. A boolean `current_evidence` on the item wins.
HISTORICAL_ONLY = 'Historical context only'
# What the stored AI text was sent for and how it ended (interpretations row `ai_outcome`).
OK, INVALID, REFUSED, UNAVAILABLE = 'ok', 'invalid', 'refused', 'unavailable'
SENT = {OK, INVALID, UNAVAILABLE}  # outcomes of a run whose request reached (or may have reached) the provider
PRICED_HEADLINE_ASSESSMENTS = ('UNDETERMINED', 'NEUTRAL_PRICED')  # spec test 3: never a surprise on the headline alone
PRE_RELEASE_ASSESSMENTS = ('UNDETERMINED', 'NOT_APPLICABLE')  # no outcome exists before release
CLASSIFICATIONS = ('HAWKISH_SURPRISE', 'DOVISH_SURPRISE', 'NEUTRAL_PRICED')  # labels that resolve a scenario
SURPRISES = ('HAWKISH_SURPRISE', 'DOVISH_SURPRISE')
COUNTDOWN_KEYS = ('minutes_until_event', 'minutes_until', 'seconds_until', 'seconds_to_release')
WINDOWS = ((15, 'WITHIN_15_MINUTES'), (60, 'WITHIN_60_MINUTES'))  # contract trigger windows W15 / W60
# Trade claims: (side, pattern), side +1 long, -1 short, 0 either. A claim counts unless it is negated just before it
# ("not a buy", "don't go long") or conditional ("actionable only after the Fed", "a buy if yields fall").
_ENTRY = r'(?:open|enter|take|initiate|establish|put on|add)\s+(?:a\s+|an\s+|the\s+|new\s+|fresh\s+|to\s+)?'
_NOT_ORDER = r'(?:side|signals?|setups?|pressure|programs?|orders?|walls?|volume|flows?|interest)'
CLAIMS = tuple((side, re.compile(pattern, re.IGNORECASE)) for side, pattern in (
    (0, r"\b(?:actionable|tradable|tradeable)\b"),
    (0, r"\bact\s+(?:now|immediately|today|on\s+(?:it|this|that|the\s+(?:setup|signal)))\b"),
    (0, r"\b(?:no need|not necessary|unnecessary|no reason)\s+to\s+wait\b|\bwithout\s+waiting\b|"
        r"\b(?:do not|don't|dont|needn't|need not)\s+wait\b"),
    (0, r"\b(?:enter|execute|trade)\s+(?:it\s+|this\s+|the\s+(?:setup|trade)\s+)?(?:now|immediately|here|today)\b"),
    (0, rf"\b{_ENTRY}(?:position|trade|entry)\b"),
    (0, r"\b(?:pull the trigger|green light|all clear|good entry|entry (?:now|here|point))\b"),
    (1, rf"\bbuys?\b(?!-side|\s+{_NOT_ORDER}\b)"),
    (1, r"\bgo(?:es|ing)?\s+long\b|\bget\s+long\b|\b(?:load(?:ing)?\s+up|accumulat(?:e|ing)|scal(?:e|ing)\s+in)\b"),
    (1, rf"\b{_ENTRY}longs?\b"),
    (1, r"\blong\s+(?:it|this|that|here|now|at|from|above|into)\b"),
    (-1, rf"\bsells?\b(?!-(?:off|side)|\s+(?:off|{_NOT_ORDER})\b)"),
    (-1, r"\bgo(?:es|ing)?\s+short\b|\bget\s+short\b"),
    (-1, rf"\b{_ENTRY}shorts?\b"),
    (-1, r"\bshort\s+(?:it|this|that|here|now|at|from|below|into)\b"),
))
NEGATED = re.compile(r"\b(?:not|no|never|nor|nothing|none|neither|nobody|cannot|can't|isn't|aren't|won't|shouldn't|"
                     r"don't|do not|does not|doesn't|"
                     r"avoid|avoiding|refrain from|rather than|instead of|too early to|premature to)\b[^.;:!?]{0,28}$",
                     re.IGNORECASE)
CONDITION = r'(?:after|once|when|if|unless|following|until|provided|pending|conditional on|subject to)'
CONDITIONAL_AFTER = re.compile(rf"^[^.;:!?]{{0,40}}\b{CONDITION}\b", re.IGNORECASE)
CONDITIONAL_BEFORE = re.compile(rf"\b{CONDITION}\b[^.;:!?]{{0,40}}$", re.IGNORECASE)
CLAUSE = re.compile(r'[,;:–—]|\b(?:while|whereas|but|although|though)\b', re.IGNORECASE)
ACTIONABLE_STATUS = {'LONG': 1, 'STRONG LONG': 1, 'SHORT': -1, 'STRONG SHORT': -1}
# A sentence that mentions an unverified (potential) event must say so.
QUALIFIER = re.compile(r'\b(?:unverified|unconfirmed|under verification|not (?:yet )?(?:confirmed|verified)|'
                       r'awaiting (?:confirmation|verification)|pending verification)\b', re.IGNORECASE)
_STOP = ('that this with from have been were will would could should said says report reports reported reporting '
         'breaking news according sources source market markets price prices today after before over into amid near '
         'about than more less their there what when where which while also just very some other risk event events '
         'possible potential under verification unconfirmed unverified claim claims post posts social users user '
         'following against between during still major latest update').split()
# A probability claim: a percentage next to one of these words must be a value of that event's market_pricing.
PROBABILITY = re.compile(r'\b(?:probabilit\w*|odds|chances?|likelihood|priced|pricing|prices?\s+in|implied|implies)\b',
                         re.IGNORECASE)
UP = {'up', 'higher', 'rise', 'rises', 'positive', 'bullish', 'long', '+', '↑'}
DOWN = {'down', 'lower', 'fall', 'falls', 'negative', 'bearish', 'short', '-', '↓', '−'}
ARROWS = {1: '↑', -1: '↓'}
# Words that tie a stored text to a fact outside the core: it is served again only while that fact is unchanged.
SESSION_WORDS = re.compile(r'\b(?:closed|reopen\w*|sessions?|markets?\s+(?:is|are)\s+open)\b', re.IGNORECASE)
TRADE_WORDS = re.compile(r'\b(?i:trade watch|setups?|actionable|provisional)\b|\bWAIT\b')


def config() -> dict:
    """refresh.interpretation (config/rules.d/refresh.json: active_seconds, idle_seconds, min_interval_seconds, ...)
    over these defaults for the input bounds, staleness and retention, which that section may override. Without the
    section the cadences come from refresh cadences.interpretation."""
    refresh = rules()['refresh']
    cadence = refresh['cadences']['interpretation']
    defaults = {'active_seconds': cadence['seconds'], 'idle_seconds': cadence.get('inactive_seconds', cadence['seconds']),
                'min_interval_seconds': 60, 'stale_factor': 2, 'max_events': 8, 'max_explained_events': 5,
                'max_listed': 5, 'max_trade_watch': 8, 'max_post_events': 3, 'max_unscheduled': 5,
                'max_what_can_break': 8, 'max_providers': 30, 'max_input_chars': 24000, 'retention_days': 30,
                # the model input (brief) and the request policy of refresh()
                'max_brief_chars': 8000, 'max_brief_events': 3, 'min_ai_interval_seconds': 3600,
                'retry_failed_after_seconds': 7200, 'retry_unavailable_after_seconds': 1800, 'memory_rows': 60}
    return {**defaults, **(refresh.get('interpretation') or {})}


def traditional_open(markets_open: dict | None) -> bool:
    """Unknown session state counts as open (the shorter cadence)."""
    return not markets_open or any(bool(markets_open.get(key)) for key in MARKETS)


def cadence_seconds(markets_open: dict | None) -> int:
    cfg = config()
    return int(cfg['active_seconds'] if traditional_open(markets_open) else cfg['idle_seconds'])


# --- role assignment ---------------------------------------------------------------------------------------------------

def _assigned(row: dict | None) -> bool:
    return row is not None and any(row.get(key) for key in ASSIGNMENT_KEYS)


def _decided(store: Store) -> bool:
    """True once the role was inherited or assigned by an administrator (audit_logs role_inherited / role_assignment for
    this role): from then on the stored assignment, even an explicitly cleared one, is the operator's choice."""
    cls = RECORDS['audit_logs']
    query = select(cls.id).where(cls.payload['action'].as_string().in_(('role_inherited', 'role_assignment')),
                                 cls.payload['role'].as_string() == ROLE).limit(1)
    with store.session() as session:
        return session.execute(query).first() is not None


def ensure_role(store: Store) -> None:
    """Startup: a market_interpreter that was never assigned inherits the chief_analyst primary and backup models once,
    audited as role_inherited. The inheritance is not repeated: an administrator who later clears the role keeps it
    cleared across restarts (and AIService uses no fallback model for it)."""
    current = store.get('ai_role_assignments', ROLE)
    if _assigned(current) or _decided(store):
        return
    parent = store.get('ai_role_assignments', PARENT_ROLE)
    if parent is None or not _assigned(parent):
        return
    assignment = {'role': ROLE, **{key: parent.get(key) for key in ASSIGNMENT_KEYS}, 'second_opinion': None,
                  'inherited_from': PARENT_ROLE}
    store.put('ai_role_assignments', ROLE, assignment)
    store.put('audit_logs', uid(), {'action': 'role_inherited', 'role': ROLE, 'from_role': PARENT_ROLE,
                                    'assignment': assignment, 'timestamp': utcnow().isoformat()})


# --- snapshot access ---------------------------------------------------------------------------------------------------

def _parts(snapshot: dict) -> tuple[dict, dict]:
    """(event_risk block, regime) of an interpretation bundle, or of a bare event_risk block."""
    if isinstance(snapshot.get('event_risk'), dict):
        return snapshot['event_risk'], snapshot.get('regime') or {}
    return snapshot, snapshot.get('regime') or {}


def _dicts(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _event(value: Any) -> dict | None:
    return value if isinstance(value, dict) and value.get('event_id') else None


def _phase(item: dict) -> str:
    return str(item.get('phase') or item.get('status') or '').upper()


def _symbol(item: dict) -> str:
    return str(item.get('symbol') or item.get('asset') or '')


def _status(item: dict) -> str:
    return str(item.get('trade_status') or '').strip()


def _stage(item: dict) -> str:
    """PRE (scheduled, approaching, imminent), LIVE (released, in progress), POST or DONE; another phase as given."""
    phase = _phase(item)
    return STAGES.get(phase, phase)


def _focus(er: dict) -> list[tuple[str, dict]]:
    """(role, event) of the dominant and of the next event; one entry when they are the same event."""
    dominant, following = _event(er.get('dominant_event')), _event(er.get('next_event'))
    found: list[tuple[str, dict]] = [('dominant', dominant)] if dominant else []
    if following and (not dominant or following['event_id'] != dominant['event_id']):
        found.append(('next', following))
    return found


def _dominant_id(er: dict) -> str | None:
    """The backend's dominant event: the block's dominant_event, or in a brief the event whose role is dominant."""
    dominant = _event(er.get('dominant_event'))
    if dominant is None:
        dominant = next((e for e in _dicts(er.get('events')) if e.get('role') == 'dominant' and e.get('event_id')), None)
    return str(dominant['event_id']) if dominant else None


def events_index(er: dict, potential: bool = True) -> dict[str, dict]:
    """Every event object of the block by event_id: next and dominant first, then scheduled, active, post-event
    (and potential, unverified ones unless potential=False)."""
    found = [er.get('next_event'), er.get('dominant_event'), *_dicts(er.get('events')), *_dicts(er.get('active_events'))]
    found += _dicts(er.get('potential_events')) if potential else []
    index: dict[str, dict] = {}
    for item in found:
        if (event := _event(item)) is not None:
            index.setdefault(str(event['event_id']), event)
    for item in _dicts(er.get('post_event')):
        if item.get('event_id'):
            index.setdefault(str(item['event_id']), item)
    return index


def surprise_basis(event: dict) -> tuple[Any, Any]:
    """(headline_priced, guidance_pending) from the event's surprise_basis (or top-level fields). Without an explicit
    guidance_pending, a press-conference sub-event that has not finished keeps guidance pending."""
    found = event.get('surprise_basis')
    basis: dict = found if isinstance(found, dict) else {}
    priced = basis.get('headline_priced', event.get('headline_priced'))
    pending = basis.get('guidance_pending', event.get('guidance_pending'))
    if pending is None:
        subs = [s for s in _dicts(event.get('sub_events')) if str(s.get('sub_event', '')).upper() == 'PRESS_CONFERENCE']
        pending = any(_phase(s) not in FINISHED for s in subs) if subs else None
    return priced, pending


def allowed_assessments(event: dict) -> tuple[str, ...]:
    if _phase(event) in PRE_RELEASE:
        return PRE_RELEASE_ASSESSMENTS
    priced, pending = surprise_basis(event)
    # A priced (or unknown-pricing) headline is never a surprise while guidance is pending; only a headline known
    # NOT to be priced may be called one before the press conference ends.
    if pending is True and priced is not False:
        return PRICED_HEADLINE_ASSESSMENTS
    return SURPRISE_ASSESSMENTS


def provisional(event: dict) -> bool:
    """A priced (or unknown-pricing) headline while guidance is pending: any label is pre-guidance and never final."""
    priced, pending = surprise_basis(event)
    return pending is True and priced is not False


# --- official evidence (the documents a central-bank classification rests on) -----------------------------------------

def settings() -> dict:
    """market_interpreter inputs and validation settings (config/prompts.json 'market_interpreter', prompts_version)."""
    return prompts()['market_interpreter']


def _stamp(value: Any) -> datetime | None:
    try:
        return aware(value) if value else None
    except (TypeError, ValueError):
        return None


def _official(doc: dict, kinds: set[str], groups: set[str]) -> bool:
    """A direct, supporting document of an official source of the event's central bank (never discovery-only)."""
    return (doc.get('official') is True and doc.get('kind') in kinds and doc.get('group') in groups and bool(doc.get('id'))
            and doc.get('relation', 'supports') == 'supports' and doc.get('direct', True) is not False
            and not doc.get('discovery_only'))


def official_evidence(er: dict, news: Iterable[dict], now: datetime) -> tuple[list[dict], list[str]]:
    """Verified official documents (statement, minutes, press release, press-conference text) for each released event
    of a configured central-bank class: evidence of CONFIRMED news events from that bank's official sources, published
    from lead_minutes before the event time up to window_hours after it (and not after `now`). When the event already
    lists its official_documents (the event-risk layer, which grades only on those ids), only those documents are used.
    Excerpts are screened (instruction-like sentences removed) and size-bounded. Returns (documents, findings)."""
    cfg = settings()['official_evidence']
    kinds, by_class = set(cfg['source_kinds']), cfg['groups_by_class']
    confirmed = [item for item in news if isinstance(item, dict) and item.get('verification_status') == 'CONFIRMED']
    documents: list[dict] = []
    findings: list[str] = []
    for event in events_index(er, potential=False).values():
        cls = event.get('event_class')
        t0 = _stamp(event.get('time_utc') or event.get('first_seen_utc'))
        if cls not in by_class or t0 is None or _phase(event) in PRE_RELEASE:
            continue
        listed = event.get('official_documents')
        own = {str(d.get('evidence_id')) for d in _dicts(listed)} if isinstance(listed, list) else None
        start = t0 - timedelta(minutes=cfg['lead_minutes'])
        end = min(aware(now), t0 + timedelta(hours=cfg['window_hours']))
        groups = set(by_class[cls])
        found: dict[str, tuple[float, datetime, dict]] = {}
        for item in confirmed:
            for doc in _dicts(item.get('evidence')):
                published = _stamp(doc.get('published_at'))
                if published is None or published > aware(now) or not _official(doc, kinds, groups):
                    continue
                if (str(doc['id']) in own) if own is not None else start <= published <= end:
                    found.setdefault(str(doc['id']), (abs((published - t0).total_seconds()), published, doc))
        closest = sorted(found.values(), key=lambda row: (row[0], str(row[2]['id'])))[:cfg['max_per_event']]
        for _, published, doc in closest:
            excerpt, screened = screen_text(str(doc.get('excerpt') or doc.get('content') or ''), redact=True)
            title, titled = screen_text(str(doc.get('title') or ''), redact=True)
            findings += [f"official_evidence {doc['id']}: {f}" for f in dict.fromkeys(screened + titled)]
            documents.append({'evidence_id': str(doc['id']), 'event_id': str(event['event_id']),
                              'source': doc.get('source') or doc.get('source_id'), 'kind': doc.get('kind'),
                              'published_at': published.isoformat(), 'title': _clip(title, 300),
                              'excerpt': _clip(excerpt, cfg['max_excerpt_chars'])})
    return documents[:cfg['max_documents']], findings


def documents_by_event(data: dict) -> dict[str, set[str]]:
    """{event_id: evidence ids of its official documents} of an input bundle."""
    result: dict[str, set[str]] = {}
    for doc in _dicts(data.get('official_evidence')):
        result.setdefault(str(doc.get('event_id')), set()).add(str(doc.get('evidence_id')))
    return result


def _unverified(event: dict) -> dict:
    """A potential event reaches the model only as an unverified report: no scores, exposures or consequences."""
    return {'event_id': str(event['event_id']), 'name': event.get('name'), 'event_class': event.get('event_class'),
            'category': event.get('category'), 'status': 'UNVERIFIED',
            'verification_status': event.get('verification_status') or 'UNCONFIRMED',
            'first_seen_utc': event.get('first_seen_utc'),
            'note': 'Unverified report, not a fact: mention it only as unverified or unconfirmed; no effect on scores.'}


# --- AI input bundle ---------------------------------------------------------------------------------------------------

def _window(item: dict) -> str | None:
    minutes = next((item[key] for key in COUNTDOWN_KEYS[:2] if isinstance(item.get(key), (int, float))), None)
    if item.get('time_known') is False or minutes is None or minutes < 0:
        return None
    return next((label for limit, label in WINDOWS if minutes <= limit), None)


def _local(value: Any) -> datetime | None:
    try:
        return bucharest(aware(value)) if value else None
    except (TypeError, ValueError):
        return None


def _prepared(value: Any) -> Any:
    """Countdown fields become window labels (the countdown is frontend arithmetic); sub-events and events with a
    known UTC time get their Bucharest clock time from the backend, so the AI can quote it, and every Bucharest date
    its day label ('Wed 30 Sep', day_bucharest), the way dates are written for the user."""
    if isinstance(value, list):
        return [_prepared(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _prepared(item) for key, item in value.items() if key not in COUNTDOWN_KEYS}
    if any(key in value for key in COUNTDOWN_KEYS):
        result['window'] = _window(value)
    if value.get('time_utc') and value.get('time_known') is not False and not value.get('time_bucharest'):
        local = _local(value['time_utc'])
        if local is not None:
            result.update(time_bucharest=f'{local:%H:%M}', date_bucharest=local.date().isoformat())
    if isinstance(result.get('date_bucharest'), str) and (day := day_label(result['date_bucharest'])):
        result['day_bucharest'] = day
    return result


# Fields that never help the model explain (ids, URLs, score breakdowns); dropped to keep the prompt small, since a slow
# model on a 50k-character prompt exceeds the chat timeout. Every value the model may cite stays in the input.
DROPPED_EVENT_KEYS = {'row_id', 'source_url', 'relevance_components', 'dominance_basis', 'relevance_missing',
                      'impact_10', 'impact', 'timing', 'status'}
# Unscheduled (active) events keep their status (UNRESOLVED, DE-ESCALATING) and timing ANYTIME: that is what they are.
DROPPED_ACTIVE_KEYS = DROPPED_EVENT_KEYS - {'timing', 'status'}
# EVENT RISK MODE keeps its event, time, relevance and groups. The calendar impact and the relevance-based impact_10 are
# page labels, left out of the model's input as on every event, so the model explains importance by relevance only.
DROPPED_MODE_KEYS = {'impact', 'impact_10'}
DROPPED_CARD_KEYS = {'regime_favorability_basis', 'historical_probability_reason', 'confidence_basis', 'symbol'}
# Provider states worth telling the model (they explain missing data); ONLINE/PENDING rows are noise.
REPORTED = {'OFFLINE', 'AUTH_ERROR', 'RATE_LIMITED', 'DEGRADED', 'NOT CONFIGURED', 'DISABLED'}


def _lean(value: Any, dropped: frozenset[str] | set[str] = frozenset()) -> Any:
    """Recursively drops empty lists/dicts and the listed keys (None stays: 'unknown' is information)."""
    if isinstance(value, list):
        return [_lean(item, dropped) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: _lean(item, dropped) for key, item in value.items()
            if key not in dropped and item != [] and item != {}}


def _compact(er: dict, cfg: dict) -> dict:
    """The event-risk block as the model sees it: lean events, active events and risk mode (scenarios only for the first
    max_explained_events, affected assets without weights, no impact numbers), per-asset risk only where it is not zero,
    lean trade-watch cards, no duplicated regime."""
    result = {key: value for key, value in er.items() if key != 'regime'}

    def event(item: Any, with_scenarios: bool, dropped: set[str] = DROPPED_EVENT_KEYS) -> Any:
        if not isinstance(item, dict):
            return item
        lean = _lean(item, dropped)
        if isinstance(lean.get('affected_assets'), list):
            lean['affected_assets'] = [{k: a.get(k) for k in ('symbol', 'level')} for a in _dicts(lean['affected_assets'])]
        if not with_scenarios:
            lean.pop('scenarios', None)
        return lean

    events = [e for e in er.get('events') or [] if isinstance(e, dict)][:cfg['max_events']]
    result['events'] = [event(e, i < cfg['max_explained_events']) for i, e in enumerate(events)]
    for key in ('next_event', 'dominant_event'):
        if isinstance(er.get(key), dict):
            result[key] = event(er[key], True)
    if isinstance(er.get('active_events'), list):
        result['active_events'] = [event(e, True, DROPPED_ACTIVE_KEYS) for e in er['active_events']]
    if isinstance(er.get('mode'), dict):
        result['mode'] = _lean(er['mode'], DROPPED_MODE_KEYS)
    risk = er.get('asset_event_risk')
    if isinstance(risk, dict):
        result['asset_event_risk'] = {sym: {k: v.get(k) for k in ('score', 'level', 'event_id', 'trade_status', 'window')
                                            if v.get(k) is not None}
                                      for sym, v in risk.items() if isinstance(v, dict) and (v.get('score') or 0) > 0}
    cards = []
    for card in _dicts(er.get('trade_watch')):
        lean = _lean(card, DROPPED_CARD_KEYS)
        upcoming = lean.get('upcoming_event')
        if isinstance(upcoming, dict):
            lean['upcoming_event'] = {k: upcoming.get(k) for k in ('event_id', 'name', 'time_bucharest', 'date_bucharest',
                                                                  'day_bucharest', 'phase', 'window')
                                      if upcoming.get(k) is not None}
        cards.append(lean)
    result['trade_watch'] = cards
    return _lean(result)


def stale_closes(driver: dict) -> list[str]:
    """'VIX close of Tue 22 Sep' for each input of a regime driver that is a STALE daily close (it still votes while its
    market is closed, regime last_close.max_age_days)."""
    found = []
    for item in _dicts(driver.get('inputs')):
        if item.get('close_status') == 'STALE' and item.get('feature'):
            day = day_label(item.get('close_date'))
            found.append(f"{item['feature']} close" + (f' of {day}' if day else ''))
    return found


def _regime(regime: dict | None) -> dict:
    if not regime:
        return {'posture': 'UNKNOWN', 'regimes': []}
    keep = ('posture', 'regimes', 'confidence', 'coverage_pct', 'basis', 'basis_label', 'missing_required', 'method',
            'regime_cause', 'cause', 'provisional', 'divergences', 'current_drivers')
    result = {key: regime[key] for key in keep if key in regime}
    result['drivers'] = []
    for driver in _dicts(regime.get('drivers')):
        item = {key: driver.get(key) for key in ('driver', 'direction', 'arrow', 'input', 'missing')}
        if 'inputs' in driver:
            item['inputs'] = driver['inputs']
        if stale := stale_closes(driver):
            item['stale_closes'] = stale
        result['drivers'].append(item)
    result['evaluated'] = [{key: e.get(key) for key in ('regime', 'state', 'confidence')}
                           for e in _dicts(regime.get('evaluated'))]
    return result


def _providers(health: Iterable[dict] | None, limit: int) -> list[dict]:
    """Only providers that are not OK, by their human label and plain-language status ('Finnhub (US equities)', 'rate
    limited'), as the page names them: the model repeats what it is given, so it never sees a provider id or a status
    code. Errors can carry URLs; they are never forwarded."""
    rows = [{'name': h.get('label') or provider_label(str(h['name'])), 'status': status_text(h['status'])}
            for h in health or [] if isinstance(h, dict) and h.get('name') and h.get('status') in REPORTED]
    return sorted(rows, key=lambda row: str(row['name']))[:limit]


def _whole(number: float) -> int:
    """Half away from zero, like the page's number formatting (toLocaleString / toFixed)."""
    return int(math.copysign(math.floor(abs(number) + 0.5), number))


def _numeric(item: Any) -> bool:
    return isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item)


def as_shown(value: Any, keys: frozenset[str] | set[str]) -> Any:
    """Scores as the page shows them: numbers under the configured display keys (market_interpreter.display_numbers
    whole_number_keys) become whole numbers, so the model writes 'relevance 70/100' like the page, never 70.2."""
    if isinstance(value, list):
        return [as_shown(item, keys) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: _whole(item) if key in keys and _numeric(item) else as_shown(item, keys) for key, item in value.items()}


def _keyed_values(value: Any, keys: frozenset[str] | set[str], found: set[float] | None = None) -> set[float]:
    """Magnitudes of the numbers directly under `keys`, anywhere in `value`."""
    found = found if found is not None else set()
    if isinstance(value, list):
        for item in value:
            _keyed_values(item, keys, found)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in keys and _numeric(item):
                found.add(round(abs(float(item)), 6))
            else:
                _keyed_values(item, keys, found)
    return found


def prepare(snapshot: dict, regime: dict | None = None, providers: Iterable[dict] | None = None,
            now: datetime | None = None, news: Iterable[dict] | None = None) -> tuple[dict, list[str]]:
    """The market_interpreter input and its screening findings: the size-bounded event-risk block (spec §4), the regime
    (posture, drivers, basis, coverage), provider health and, for released central-bank events, their verified official
    documents with evidence ids. Every number the model may cite is in here, scores as the page shows them (whole
    numbers, as_shown) and providers by label and plain status. Potential events are passed only as
    unverified reports (dropped when they carry instruction-like text) and instruction-like sentences are removed from
    every string, so the model never receives an injected instruction."""
    cfg = config()
    er, own_regime = _parts(snapshot)
    er = dict(er)
    limits = {'events': cfg['max_events'], 'active_events': cfg['max_unscheduled'],
              'potential_events': cfg['max_unscheduled'], 'favored': cfg['max_listed'], 'pressured': cfg['max_listed'],
              'non_confirming': cfg['max_listed'], 'trade_watch': cfg['max_trade_watch'],
              'post_event': cfg['max_post_events'], 'what_can_break': cfg['max_what_can_break']}
    for key, limit in limits.items():
        if isinstance(er.get(key), list):
            er[key] = er[key][:limit]
    findings: list[str] = []
    potential = []
    for event in _dicts(er.get('potential_events')):
        if _event(event) is None:
            continue
        texts = [event.get('name'), event.get('status_note'), *(event.get('known_information') or [])]
        if any(instruction_like(str(text)) for text in texts if text):
            findings.append(f"potential_event_dropped:{event['event_id']}:instruction_like_text")
            continue
        potential.append(_unverified(event))
    if 'potential_events' in er:
        er['potential_events'] = potential
    stamp = _local(er.get('generated_at') or now or utcnow())
    documents, found = official_evidence(er, news or [], aware(now) if now else _stamp(er.get('generated_at')) or utcnow())
    findings += found
    whole = frozenset(settings()['display_numbers']['whole_number_keys'])
    compact = as_shown(_compact(_prepared(er), cfg), whole)
    # 'Mon 5 Oct 2026 07:22': the unpadded day every other date of the input (day_bucharest) and the page use.
    as_of = f'{stamp:%a} {stamp.day} {stamp:%b %Y %H:%M} Europe/Bucharest' if stamp else None
    data: dict[str, Any] = {'as_of_bucharest': as_of,
            'regime': as_shown(_regime(regime if regime is not None else own_regime), whole), 'event_risk': compact,
            'official_evidence': documents, 'evidence_ids': sorted({d['evidence_id'] for d in documents}),
            'provider_health': _providers(providers, cfg['max_providers'])}
    events = compact.get('events')
    while len(json.dumps(data, default=str)) > cfg['max_input_chars'] and isinstance(events, list) and len(events) > 1:
        events.pop()
    screened, found = screen_inputs(data, redact=True)
    return screened, list(dict.fromkeys(findings + found))


def bundle(snapshot: dict, regime: dict | None = None, providers: Iterable[dict] | None = None,
           now: datetime | None = None, news: Iterable[dict] | None = None) -> dict:
    """The market_interpreter input (prepare() without the screening findings)."""
    return prepare(snapshot, regime, providers, now, news)[0]


def core(data: dict) -> dict:
    """What an interpretation is about: the regime (posture, regimes, basis), the dominant and the next event with
    their stage, time, expectations, sub-events and surprise basis, the risk mode, post-event divergence and the
    official documents. When one of these changes, a stored AI text is out of date and a new one is asked for at once."""
    er, regime = _parts(data)
    mode = er.get('mode') or {}
    focus = [{'role': role, 'id': str(e['event_id']), 'stage': _stage(e), 'time': e.get('time_utc'),
              'time_known': e.get('time_known'), 'consensus': e.get('consensus'), 'pricing': e.get('market_pricing'),
              'actual': e.get('actual'), 'surprise': surprise_basis(e),
              'sub_events': [(s.get('sub_event'), _stage(s)) for s in _dicts(e.get('sub_events'))]}
             for role, e in _focus(er)]
    return {'regime': {'posture': regime.get('posture'), 'regimes': sorted(str(r) for r in regime.get('regimes') or []),
                       'basis': regime.get('basis')},
            'focus': focus, 'mode': (bool(mode.get('active')), mode.get('event_id')),
            'post_event': sorted((str(p.get('event_id')), bool(p.get('divergence'))) for p in _dicts(er.get('post_event'))),
            'official_evidence': sorted((str(d.get('event_id')), str(d.get('evidence_id')))
                                        for d in _dicts(data.get('official_evidence')))}


def _members(er: dict) -> dict[str, list[str]]:
    return {key: sorted({_symbol(i) for i in _dicts(er.get(key)) if _symbol(i)}) for key in LISTS}


def _driver_directions(regime: dict) -> list[list[str]]:
    """[driver, direction] of the regime drivers and, as 'now:<driver>', of the current observations next to a
    last-close regime."""
    rows = [[str(d.get('driver')), str(d.get('direction'))] for d in _dicts(regime.get('drivers'))]
    return rows + [[f"now:{d.get('driver')}", str(d.get('direction'))] for d in _dicts(regime.get('current_drivers'))]


def _open_risks(er: dict) -> list[list[str]]:
    return sorted([str(e['event_id']), str(e.get('status') or _phase(e))] for e in _dicts(er.get('active_events')) if _event(e))


def _potential_ids(er: dict) -> list[str]:
    return sorted(str(e['event_id']) for e in _dicts(er.get('potential_events')) if _event(e))


def _statuses(er: dict) -> list[list[str]]:
    return sorted([_symbol(i), _status(i)] for i in _dicts(er.get('trade_watch')) if _symbol(i))


def basis(data: dict) -> dict:
    """The material state of the market for the interpretation: the core plus the driver directions, the lists'
    membership, the open unscheduled and unverified events, the sessions and the trade statuses. Live percentages,
    countdowns, windows, relevance levels, continuous scores and the what-can-break texts (they carry price levels)
    are left out, so an unchanged market does not look new on every tick."""
    er, regime = _parts(data)
    return {'core': core(data), 'drivers': _driver_directions(regime),
            'divergences': sorted(str(d.get('feature')) for d in _dicts(regime.get('divergences'))),
            'lists': _members(er), 'active': _open_risks(er), 'potential': _potential_ids(er),
            'markets_open': er.get('markets_open'), 'trade_watch': _statuses(er)}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def basis_hash(data: dict) -> str:
    return _digest(basis(data))


def core_hash(data: dict) -> str:
    return _digest(core(data))


def _observations(regime: dict) -> dict:
    """The regime observations a quoted move can come from, the same whether the regime is the event-risk block's or
    the full one: each driver by its input text ('DXY +0.49%'), else by its input values."""
    def rows(key: str) -> list:
        return [[d.get('driver'), d.get('direction'), d.get('input') or
                 [[i.get('feature'), i.get('field'), i.get('value')] for i in _dicts(d.get('inputs'))]]
                for d in _dicts(regime.get(key))]
    return {'posture': regime.get('posture'), 'regimes': regime.get('regimes'), 'basis_label': regime.get('basis_label'),
            'drivers': rows('drivers'), 'current_drivers': rows('current_drivers'),
            'divergences': [d.get('text') for d in _dicts(regime.get('divergences'))]}


def prose_hash(data: dict) -> str:
    """Binds a text that quotes percentage or basis-point moves to the observations it quoted (cites_moves)."""
    er, regime = _parts(data)
    evidence = {'regime': _observations(regime), 'lists': {
        key: [{k: item.get(k) for k in ('symbol', 'asset', 'stance', 'reason_facts')}
              for item in _dicts(er.get(key))] for key in LISTS},
        'watch': [{k: item.get(k) for k in ('asset', 'decision_score', 'why', 'confirmation', 'invalidation',
                                          'evidence_confidence', 'market_confirmation')}
                  for item in _dicts(er.get('trade_watch'))]}
    return _digest(evidence)


def bindings(output: dict, data: dict) -> dict:
    """The facts outside the core that a text depends on, read from the current snapshot: the driver directions and
    the open unscheduled events always; the list of each asset the text names; the sessions, the trade statuses and
    the unverified reports only when the text speaks of them. A stored AI text is served while these are unchanged, so
    a list change for an asset it never mentions does not discard it, and a fact that flips back restores it."""
    er, regime = _parts(data)
    text = ' '.join(t for _, t in _free_texts(output) if t)
    member = {symbol: key for key, symbols in _members(er).items() for symbol in symbols}
    return {'drivers': _driver_directions(regime), 'active': _open_risks(er),
            'assets': {symbol: member.get(symbol) for symbol in sorted(_assets_in(text, _asset_names(er)))},
            'sessions': er.get('markets_open') if SESSION_WORDS.search(text) else None,
            'trade': _statuses(er) if TRADE_WORDS.search(text) else None,
            'unverified': _potential_ids(er) if QUALIFIER.search(text) else None}


def cites_moves(output: dict) -> bool:
    """True when a text quotes a percentage or basis-point value (a live observation that can move)."""
    return any(PERCENT.search(text) or BASIS_POINTS.search(text) for _, text in _free_texts(output))


# --- the model input (brief) --------------------------------------------------------------------------------------------

def current_evidence(item: dict) -> bool:
    """False for a list item that rests on the last close alone (no current observation of the asset)."""
    flag = item.get('current_evidence')
    if isinstance(flag, bool):
        return flag
    return not any(HISTORICAL_ONLY in str(fact) for fact in item.get('reason_facts') or [])


def _scenario_line(scenario: dict, limit: int = 6) -> str | None:
    """'DXY ↑, US2Y ↑, BTC ↓' for the directional effects of a scenario template."""
    signs = [(symbol, sign) for symbol, sign in _directions(scenario).items() if sign][:limit]
    return ', '.join(f'{symbol} {ARROWS[sign]}' for symbol, sign in signs) or None


def _brief_event(event: dict, role: str, er: dict) -> dict:
    keep = ('event_id', 'name', 'phase', 'status', 'timing', 'time_bucharest', 'day_bucharest',
            'time_known', 'relevance_level', 'relevance_score', 'dominance_score', 'consensus', 'market_pricing',
            'actual', 'verification_status', 'surprise_basis')
    item: dict[str, Any] = {'role': role, **{k: event[k] for k in keep if event.get(k) not in (None, '', [], {})}}
    item['name'] = _name(event)
    if exposed := _exposed(event):
        item['most_exposed'] = exposed
    scenarios = [{'label': s.get('label') or s.get('id'), 'expected': line}
                 for s in _dicts(event.get('scenarios'))[:3] if (line := _scenario_line(s))]
    if scenarios:
        item['scenarios'] = scenarios
    subs = [{k: sub[k] for k in ('sub_event', 'phase', 'time_bucharest') if sub.get(k)} for sub in _dicts(event.get('sub_events'))]
    if subs:
        item['sub_events'] = subs
    if _flagged(event, er) and event.get('data_incomplete'):
        item['data_incomplete'] = [_words(r) for r in event['data_incomplete'] if r]
    if _phase(event) not in PRE_RELEASE:
        facts = [_clip(str(f), 160) for f in (event.get('known_information') or [])[:2] if f]
        if facts:
            item['known_information'] = facts
    return item


def _brief_regime(regime: dict) -> dict:
    """The regime as the model reads it: its plain-language summary (the codes RISK_OFF, USD_STRENGTH are never
    passed, so they cannot be echoed), basis, confidence and the drivers that have a direction."""
    keep = ('posture', 'basis', 'basis_label', 'confidence', 'coverage_pct', 'provisional')
    result: dict[str, Any] = {'summary': _regime_phrase(regime) or 'insufficient data'}
    result.update({k: regime[k] for k in keep if regime.get(k) not in (None, '', [], {})})
    cause = regime.get('cause')
    if isinstance(cause, dict) and cause.get('label'):
        result['cause'] = cause['label']

    def rows(key: str) -> list[dict]:
        found = []
        for driver in _dicts(regime.get(key)):
            if not driver.get('driver') or driver.get('direction') in (None, 'unavailable'):
                continue
            row = {'driver': driver['driver'], 'direction': driver['direction']}
            if driver.get('input'):
                row['input'] = driver['input']
            if stale := driver.get('stale_closes') or stale_closes(driver):
                row['stale_closes'] = stale
            found.append(row)
        return found
    result['drivers'] = rows('drivers')
    if 'current_drivers' in regime:
        result['current_drivers'] = rows('current_drivers')
        missing = [str(d['driver']) for d in _dicts(regime.get('current_drivers'))
                   if d.get('driver') and d.get('direction') in (None, 'unavailable')]
        if missing:
            result['no_current_observation'] = missing
    opposed = [_clip(d['text'], 200) for d in _dicts(regime.get('divergences')) if isinstance(d.get('text'), str)][:3]
    if opposed:
        result['divergences'] = opposed
    return result


def brief(data: dict) -> dict:
    """The market_interpreter input, cut from the prepared bundle (prepare()): the regime summary and its drivers, at
    most max_brief_events events (the dominant one, the next one, then released ones: with official documents first), the lists
    as symbols only (non-confirming split into assets whose current move disagrees and assets without a current
    observation), trade statuses, what can break, the closed markets and the official documents of those events. It
    keeps the shape of the bundle ({regime, event_risk, official_evidence, ...}), so the same checks read both, and it
    stays within max_brief_chars: optional parts are dropped in a fixed order until it fits."""
    cfg = config()
    er, regime = _parts(data)
    documents = documents_by_event(data)
    index = events_index(er, potential=False)
    chosen = _focus(er)
    taken = {str(event['event_id']) for _, event in chosen}
    # Then what is happening now: a released event with official documents to classify, then any event in progress.
    live = [event_id for event_id, event in index.items() if _stage(event) == 'LIVE' and event_id not in documents]
    chosen += [('released', index[event_id]) for event_id in [*documents, *live] if event_id in index and event_id not in taken]
    events = [_brief_event(event, role, er) for role, event in chosen[:int(cfg['max_brief_events'])]]
    ids = {str(event['event_id']) for event in events}
    limits = settings()['official_evidence']
    evidence = [dict(doc, excerpt=_clip(str(doc.get('excerpt') or ''), int(limits.get('max_brief_excerpt_chars', 900))))
                for doc in _dicts(data.get('official_evidence')) if str(doc.get('event_id')) in ids]
    evidence = evidence[:int(limits.get('max_brief_documents', 3))]
    listed = int(cfg['max_listed'])
    against = [i for i in _dicts(er.get('non_confirming')) if _symbol(i)]
    markets_open = er.get('markets_open') if isinstance(er.get('markets_open'), dict) else None
    mode = er.get('mode') or {}
    risk: dict[str, Any] = {
        'events': events,
        'risk_mode': {'event_id': mode.get('event_id'), 'name': mode.get('name')} if mode.get('active') else None,
        'active_events': [{'event_id': str(e['event_id']), 'name': _name(e), 'status': e.get('status') or _phase(e),
                           'relevance_level': e.get('relevance_level')}
                          for e in _dicts(er.get('active_events')) if _event(e) and str(e['event_id']) not in ids][:2],
        'potential_events': [{k: e.get(k) for k in ('event_id', 'name', 'status', 'note')}
                             for e in _dicts(er.get('potential_events')) if _event(e)][:2],
        'favored': [_symbol(i) for i in _dicts(er.get('favored')) if _symbol(i)][:listed],
        'pressured': [_symbol(i) for i in _dicts(er.get('pressured')) if _symbol(i)][:listed],
        'not_confirming': [_symbol(i) for i in against if current_evidence(i)][:listed],
        'no_current_evidence': [_symbol(i) for i in against if not current_evidence(i)][:listed],
        'post_event': [{'event_id': str(p.get('event_id')), 'name': _name(index.get(str(p.get('event_id')), p)),
                        'divergence': bool(p.get('divergence')), 'confirmation': p.get('confirmation')}
                       for p in _dicts(er.get('post_event'))][:1],
        'closed_markets': _closed(markets_open),
        'trade_watch': [{'asset': _symbol(i), 'trade_status': _status(i)}
                        for i in _dicts(er.get('trade_watch')) if _symbol(i)][:int(cfg['max_trade_watch'])],
        'what_can_break': [_clip(text, 120) for text in _break_texts(er)][:3]}
    result: dict[str, Any] = {
        'as_of_bucharest': data.get('as_of_bucharest'), 'regime': _brief_regime(regime),
        'event_risk': {key: value for key, value in risk.items() if value not in (None, [], {})},
        'official_evidence': evidence, 'evidence_ids': sorted({str(d['evidence_id']) for d in evidence}),
        # Only providers that are failing now explain missing data; one that was never configured is not news.
        'provider_health': [row for row in _dicts(data.get('provider_health'))
                            if row.get('status') not in {status_text('NOT CONFIGURED'), status_text('DISABLED')}][:4]}

    def size() -> int:
        return len(json.dumps(result, ensure_ascii=False, default=str))

    def drop(*path: str) -> None:
        holder = result
        for key in path[:-1]:
            holder = holder.get(key) or {}
        holder.pop(path[-1], None)

    limit = int(cfg['max_brief_chars'])
    for path in (('provider_health',), ('event_risk', 'what_can_break'), ('event_risk', 'active_events'),
                 ('regime', 'divergences'), ('regime', 'current_drivers'), ('event_risk', 'potential_events')):
        if size() <= limit:
            break
        drop(*path)
    for event in reversed(events):  # scenario lines, the secondary events first
        if size() <= limit:
            break
        event.pop('scenarios', None)
    while size() > limit and any(len(str(doc.get('excerpt') or '')) > 200 for doc in evidence):
        for doc in evidence:
            doc['excerpt'] = _clip(str(doc.get('excerpt') or ''), max(200, len(str(doc.get('excerpt') or '')) // 2))
    return result


# --- validation --------------------------------------------------------------------------------------------------------

def _texts(output: dict) -> list[tuple[str | None, str | None, str]]:
    """Every free-text sentence of an output as (event_id, symbol, sentence): the dominant event's reason and the
    event explanations carry their event. symbol is always None (the model writes no per-asset comment)."""
    dominant = output.get('dominant_event') or {}
    units: list[tuple[str | None, str | None, str]] = []
    for text in (output.get('ai_view') or '', output.get('market_summary') or '', *(output.get('key_risks') or [])):
        units += [(None, None, piece) for piece in sentences(str(text))]
    units += [(dominant.get('event_id'), None, piece) for piece in sentences(str(dominant.get('reason') or ''))]
    for item in _dicts(output.get('next_events')):
        units += [(str(item.get('event_id')), None, piece) for piece in sentences(str(item.get('why_it_matters') or ''))]
    return units


@lru_cache(maxsize=512)
def _name_pattern(name: str) -> re.Pattern:
    return re.compile(rf'(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])', re.IGNORECASE)


def _asset_names(er: dict) -> dict[str, tuple[str, ...]]:
    """{symbol: names that count as a mention}: the symbol (any case), its configured name and aliases; ordinary words
    listed in ignored_asset_names never count. Symbols of the block that are not configured assets match by symbol."""
    cfg = settings()
    ignored = {str(n).casefold() for n in cfg.get('ignored_asset_names', [])}
    aliases = cfg.get('asset_aliases', {})
    names: dict[str, tuple[str, ...]] = {}
    for symbol, name, _ in rules()['assets']:
        found = [symbol, *(aliases.get(symbol) or []), *([name] if name and name.casefold() not in ignored else [])]
        names[symbol] = tuple(dict.fromkeys(str(n) for n in found if n))
    for key in ('trade_watch', 'favored', 'pressured', 'non_confirming'):
        for item in _dicts(er.get(key)):
            if _symbol(item) and _symbol(item) not in names:
                names[_symbol(item)] = (_symbol(item),)
    return names


def _assets_in(text: str, names: dict[str, tuple[str, ...]]) -> list[str]:
    return [symbol for symbol, found in names.items() if any(_name_pattern(n).search(text) for n in found)]


_NOUN_AFTER = r'(?:setups?|positions?|trades?|ideas?|signals?|bias|views?|thesis|case|status|exposure|squeeze|side|term)'


def trade_claims(text: str, aliases: Iterable[str] = ()) -> list[tuple[int, int, int]]:
    """(side, start, end) of the trade claims of a sentence that are neither negated ("not a buy", "don't go long")
    nor conditional ("actionable only after the Fed", "a buy if yields fall"); side +1 long, -1 short, 0 either.
    `aliases` adds "long <asset>" / "short <asset>" (not "long BTC setup")."""
    found = [(side, m.start(), m.end()) for side, pattern in CLAIMS for m in pattern.finditer(text)]
    for alias in aliases:
        for m in re.finditer(rf'\b(long|short)\s+{re.escape(alias)}(?![A-Za-z0-9])(?!\s+{_NOUN_AFTER}\b)', text,
                             re.IGNORECASE):
            found.append((1 if m.group(1).lower() == 'long' else -1, m.start(), m.end()))
    return sorted({(side, start, end) for side, start, end in found
                   if not NEGATED.search(text[:start]) and not CONDITIONAL_BEFORE.search(text[:start])
                   and not CONDITIONAL_AFTER.search(text[end:])}, key=lambda claim: claim[1])


def _clause(text: str, start: int, end: int) -> tuple[int, int]:
    """Bounds of the clause of `text` around [start, end) (split at , ; : dashes, while, whereas, but, although)."""
    cuts = list(CLAUSE.finditer(text))
    left = max((m.end() for m in cuts if m.end() <= start), default=0)
    right = min((m.start() for m in cuts if m.start() >= end), default=len(text))
    return left, right


def _trade_problems(output: dict, er: dict, names: dict[str, tuple[str, ...]]) -> list[str]:
    """Every sentence is checked against the backend trade_status of the assets it names (symbol, name or alias, any
    case; a claim without an asset falls back to the whole sentence): only a LONG or SHORT status may be described as
    actionable, and only on its own side. A claim naming no asset at all contradicts any trade_watch status that is
    not actionable."""
    statuses = {_symbol(i): _status(i) for i in _dicts(er.get('trade_watch')) if _symbol(i)}
    passive = [f"{s} {status or 'WAIT'}" for s, status in statuses.items() if status.upper() not in ACTIONABLE_STATUS]
    everyone = [alias for found in names.values() for alias in found]
    problems: list[str] = []
    for _, symbol, text in _texts(output):
        for side, start, end in trade_claims(text, everyone):
            left, right = _clause(text, start, end)
            targets = _assets_in(text[left:right], names) or _assets_in(text, names) or ([symbol] if symbol else [])
            if not targets and (passive or not statuses):
                problems.append('a general trade claim contradicts the backend trade statuses ('
                                + (', '.join(passive) if passive else 'no trade_watch item is actionable') + ')')
            for target in targets:
                status = statuses.get(target, '')
                wanted = ACTIONABLE_STATUS.get(status.upper())
                if wanted is None:
                    problems.append(f'{target} is {status}: it cannot be described as actionable' if status else
                                    f'{target} has no actionable backend trade status: it cannot be described as '
                                    'actionable')
                elif side not in (0, wanted):
                    problems.append(f"{target} is {status}: it cannot be described as a {'buy' if side > 0 else 'sell'}")
    return list(dict.fromkeys(problems))


def _stem(word: str) -> str:
    """Crude stem, so 'close', 'closes', 'closed' and 'closing' compare equal."""
    word = word.casefold().replace('’', "'").removesuffix("'s")
    for suffix in ('ing', 'ed', 'es', 's'):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            word = word[:-len(suffix)]
            break
    return word[:-1] if len(word) > 4 and word.endswith('e') else word


STOP = frozenset(_stem(w) for w in _STOP)


def _tokens(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[A-Za-z][A-Za-z'’-]*", text) if len(w) >= 4} - STOP


def _unverified_problems(output: dict, data: dict, names: dict[str, tuple[str, ...]]) -> list[str]:
    """A sentence that names an unverified (potential) event, by its id or by the distinctive words of its report
    (words shared with verified events and asset names do not count), must call it unverified or unconfirmed."""
    er, _ = _parts(data)
    potential = [e for e in _dicts(er.get('potential_events')) if _event(e)]
    if not potential:
        return []
    verified = ' '.join(str(e.get('name') or '') for e in events_index(er, potential=False).values())
    common = _tokens(verified) | {_stem(w) for found in names.values() for n in found for w in str(n).split()}
    texts = _texts(output)
    problems = []
    for event in potential:
        signature = _tokens(str(event.get('name') or '')) - common
        need = min(2, len(signature))
        for _, _, text in texts:
            named = str(event['event_id']) in text or (need > 0 and len(_tokens(text) & signature) >= need)
            if named and not QUALIFIER.search(text):
                problems.append(f"unverified event {event['event_id']} is stated without an unverified/unconfirmed "
                                f'qualifier: {_clip(text, 80)!r}')
                break
    return problems


def _without_potential(data: dict) -> dict:
    """The bundle without its unverified reports: their numbers are claims, never input facts."""
    if isinstance(data.get('event_risk'), dict):
        return {**data, 'event_risk': {k: v for k, v in data['event_risk'].items() if k != 'potential_events'}}
    return {k: v for k, v in data.items() if k != 'potential_events'}


def _values(tokens: Iterable[str]) -> set[float]:
    found = set()
    for token in tokens:
        try:
            found.add(value(token))
        except ValueError:
            continue
    return found


def _pricing(er: dict) -> dict[str, set[float]]:
    """{event_id: numbers of its market_pricing} for events whose pricing is known."""
    result = {}
    for event_id, event in events_index(er, potential=False).items():
        pricing = event.get('market_pricing')
        if pricing in (None, '', {}, []) or (isinstance(pricing, str) and pricing.strip().upper() == 'UNKNOWN'):
            continue
        result[event_id] = _values(numbers(json.dumps(pricing, ensure_ascii=False, default=str)))
    return result


def _bucharest_times(data: dict) -> set[str]:
    """The supplied Bucharest clock times: every time_bucharest of the block and the as_of time."""
    found = clocks(str(data.get('as_of_bucharest') or ''))

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key == 'time_bucharest' and isinstance(child, str):
                    found.update(clocks(child))
                else:
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
    walk(_parts(_without_potential(data))[0])
    return found


def _probability(text: str, start: int, end: int) -> bool:
    """A percentage whose clause puts a probability word right next to it ("100% priced", "50% odds of a cut")."""
    left, right = _clause(text, start, end)
    return PROBABILITY.search(text[max(left, start - 25):min(right, end + 25)]) is not None


def _number_problems(output: dict, data: dict) -> list[str]:
    """Numbers in context (the plain numbers check runs in AIService): a percentage or basis-point value must be an
    input value in that unit, a probability a value of that event's market_pricing (any event's for general text), a
    clock time a supplied Bucharest time, and a spelled-out quantity must appear in the input as written. Scores the page
    shows with a % sign (display_numbers percent_keys, such as the regime confidence) count as percent values."""
    er, regime = _parts(data)
    view = _without_potential(data)
    text = json.dumps(view, ensure_ascii=False, default=str)
    written, tagged = quantities(text), unit_values(view)
    shown = _keyed_values(view, frozenset(settings()['display_numbers']['percent_keys']))
    percents, bps = written['pct'] | tagged['pct'] | shown, written['bp'] | tagged['bp']
    for driver in _dicts(regime.get('drivers')) + _dicts(regime.get('current_drivers')):
        for item in _dicts(driver.get('inputs')):
            number = item.get('value')
            if isinstance(number, (int, float)) and _numeric(number) and item.get('field') in {'change_pct', 'change_bp'}:
                (percents if item['field'] == 'change_pct' else bps).add(float(number))
    times, pricing = _bucharest_times(data), _pricing(er)
    every = set().union(*pricing.values()) if pricing else set()
    folded = text.casefold()
    problems: list[str] = []
    for event_id, _, sentence in _texts(output):
        problems += [f'clock time {t} is not a supplied Bucharest time (time_bucharest)'
                     for t in sorted(clocks(sentence) - times)]
        for m in PERCENT.finditer(sentence):
            number, claim = value(m.group(1)), m.group(0).strip()
            if _probability(sentence, m.start(), m.end()):
                scope = pricing.get(event_id, set()) if event_id else every
                if number not in scope and round(number / 100, 6) not in scope:
                    where = f'the market_pricing of {event_id}' if event_id else "any event's market_pricing"
                    problems.append(f'probability {claim} is not in {where}')
            elif number not in percents:
                problems.append(f'{claim} is not a percent value of the input')
        problems += [f'{m.group(0).strip()} is not a basis-point value of the input'
                     for m in BASIS_POINTS.finditer(sentence) if value(m.group(1)) not in bps]
        problems += [f'spelled-out quantity {m.group(0)!r} is not in the input: repeat input values as written'
                     for m in SPELLED_QUANTITY.finditer(sentence) if m.group(0).casefold() not in folded]
    return list(dict.fromkeys(problems))


def violations(output: dict, data: dict) -> list[str]:
    """Input-dependent checks of a schema-valid output (the plain numbers check runs in AIService for NUMERIC_ROLES):
    event ids and the dominant event, surprise assessments and their official evidence, trade statuses, unverified
    events, numbers in context and plain language. `data` is what the model read (the brief) or the prepared bundle.
    sanitize() removes the parts that would fail here, so for a repaired answer this is the final guard."""
    er, _ = _parts(data)
    problems = []
    dominant = _dominant_id(er)
    chosen = (output.get('dominant_event') or {}).get('event_id')
    if chosen is not None and chosen != dominant:
        problems.append(f"dominant_event {chosen} is not the backend dominant_event ({dominant or 'none'})")
    explainable = events_index(er, potential=False)
    documents = documents_by_event(data)
    for item in _dicts(output.get('next_events')):
        event = explainable.get(str(item.get('event_id')))
        if event is None:
            problems.append(f"Unknown event_id {item.get('event_id')}")
            continue
        assessment = item.get('surprise_assessment', 'UNDETERMINED')
        if assessment not in (allowed := allowed_assessments(event)):
            problems.append(f"surprise_assessment for {item['event_id']} must be one of {list(allowed)} "
                            f"(phase {_phase(event) or 'unknown'}, headline_priced/guidance_pending {surprise_basis(event)})")
        cited = [str(e) for e in item.get('evidence_ids') or []]
        foreign = [e for e in cited if e not in documents.get(str(item['event_id']), set())]
        if foreign:
            problems.append(f"evidence_ids {foreign} of {item['event_id']} are not official documents of that event "
                            '(official_evidence)')
        elif assessment in SURPRISES and not cited:
            problems.append(f"{assessment} for {item['event_id']} needs evidence_ids of that event's official documents "
                            '(official_evidence); without them the assessment is UNDETERMINED')
    names = _asset_names(er)
    problems += _trade_problems(output, er, names)
    problems += _unverified_problems(output, data, names)
    problems += _number_problems(output, data)
    problems += _plain_language_problems(output, data)
    return problems


# Internal names must not leak into what the user reads (lower-case snake_case identifiers, 'backend', 'input data').
JARGON = re.compile(r'\b(?:[a-z]+_[a-z_]+|backend|snapshot|input data)\b')
# Codes the page never shows: upper-case statuses and classes (RATE_LIMITED, US_NFP, HAWKISH_SURPRISE) and event ids
# (BLS-20261002-US_NFP, GEO-20261028-a3f1c2d4), also ones that are not in the input.
CODES = re.compile(r'\b[A-Z][A-Z0-9]*_[A-Z0-9_]*[A-Z0-9]\b|\b[A-Z]{2,}-\d{8}-[\w-]*\w')


def _free_texts(output: dict) -> list[tuple[str, str]]:
    """(field, text) of everything the user reads in an output."""
    texts = [('ai_view', str(output.get('ai_view') or '')), ('market_summary', str(output.get('market_summary') or ''))]
    texts += [('key_risks', r) for r in output.get('key_risks') or [] if isinstance(r, str)]
    texts += [('next_events', str(item.get('why_it_matters') or '')) for item in _dicts(output.get('next_events'))]
    dominant = output.get('dominant_event')
    if isinstance(dominant, dict):
        texts.append(('dominant_event', str(dominant.get('reason') or '')))
    return texts


@lru_cache(maxsize=4)
def _registry_ids(ignored: tuple[str, ...], content: str) -> re.Pattern[str] | None:
    """Provider and source ids of the configuration ('finnhub', 'gdelt_events', 'x-federalreserve') as written (the page
    says 'Finnhub (US equities)'), except ids that are also ordinary words (plain_language ignored_provider_ids).
    `content` is the configuration hash, so an edited registry is read again."""
    config = rules_view()
    names = set((config.get('provider_labels') or {}).get('names') or {})
    names |= {str(s['id']) for s in config.get('sources') or [] if isinstance(s, dict) and s.get('id')}
    ids = sorted(names - set(ignored), key=len, reverse=True)
    return re.compile(rf"(?<![\w-])(?:{'|'.join(map(re.escape, ids))})(?![\w-])") if ids else None


def _internal_ids(output: dict, data: dict | None) -> list[str]:
    """Ids and codes the output's texts cite: event and evidence ids of the input, event-id and status-code shapes and
    provider ids of the configuration."""
    known: set[str] = set()
    if data is not None:
        known = set(events_index(_parts(data)[0])) | {str(d['evidence_id']) for d in _dicts(data.get('official_evidence'))
                                                      if d.get('evidence_id')}
    ignored = tuple(sorted(settings()['plain_language']['ignored_provider_ids']))
    providers = _registry_ids(ignored, str(rules_view().get('content_hash') or ''))
    cited: set[str] = set()
    for _, text in _free_texts(output):
        cited |= set(CODES.findall(text)) | (set(providers.findall(text)) if providers else set())
        cited |= {key for key in known if re.search(rf'(?<![\w-]){re.escape(key)}(?![\w-])', text)}
    return sorted(cited)


def _plain_language_problems(output: dict, data: dict | None = None) -> list[str]:
    """The user reads these texts: no internal identifiers (field names, event and evidence ids, status and class
    codes, provider ids)."""
    problems = [f'{field} uses internal names {sorted(set(JARGON.findall(text)))[:5]}: write plain language'
                for field, text in _free_texts(output) if JARGON.search(text)]
    if cited := _internal_ids(output, data):
        problems.append(f'the texts cite ids or codes {cited[:5]}: name the event, write a provider by its name and '
                        'status as given, and write statuses and classes in plain words')
    return problems


def _folded(text: str) -> str:
    return ' '.join(text.split()).casefold()


def _break_texts(er: dict) -> list[str]:
    """The what-can-break texts as written (items are {text, kind, source} or plain strings)."""
    items = [i.get('text') if isinstance(i, dict) else i for i in er.get('what_can_break') or []]
    return [i for i in items if isinstance(i, str) and i.strip()]


def _breaks(er: dict) -> set[str]:
    """The what-can-break texts, whitespace- and case-folded."""
    return {_folded(text) for text in _break_texts(er)}


def _allowed_numbers() -> set[str]:
    """Bare numbers the role may write without an input source (the same set AIService applies)."""
    return set(numeric_allowlist(ROLE)) | numbers(json.dumps(rules()['assets']))


def _unit(field: str, text: str, event_id: str | None = None) -> dict:
    """An output holding one text, so the whole-output checks can judge a single sentence or item."""
    if field == 'next_events':
        return {'next_events': [{'event_id': event_id, 'why_it_matters': text, 'surprise_assessment': 'UNDETERMINED'}]}
    if field == 'dominant_event':
        return {'dominant_event': {'event_id': event_id, 'reason': text}}
    return {'key_risks': [text]} if field == 'key_risks' else {field: text}


def text_problems(field: str, text: str, data: dict, event_id: str | None = None,
                  names: dict[str, tuple[str, ...]] | None = None, allowed: set[str] | None = None) -> list[str]:
    """Why one sentence or item of an answer cannot be shown: a countdown, a certainty, a number that is not in the
    input (or not in that unit, pricing or clock time), a trade claim against the backend status, an unverified report
    stated as fact, or an internal name, id or code."""
    er, _ = _parts(data)
    names = names if names is not None else _asset_names(er)
    unit = _unit(field, text, event_id)
    problems = []
    if COUNTDOWN.search(text):
        problems.append('a countdown instead of the Bucharest clock time')
    if phrase := certainty_claim(text):
        problems.append(f'a certainty ({phrase!r}); outcomes are possibilities')
    if invented := untraceable({'text': text}, data, allowed if allowed is not None else _allowed_numbers()):
        problems.append(f'numbers that are not in the input: {sorted(invented)[:5]}')
    return (problems + _trade_problems(unit, er, names) + _unverified_problems(unit, data, names)
            + _number_problems(unit, data) + _plain_language_problems(unit, data))


def sanitize(raw: Any, data: dict) -> tuple[Any, list[str]]:
    """Repairs a parsed model answer against what the model read (`data`, the brief) before the schema is applied,
    and returns (answer, notes of what was removed). A valid short answer must not fail because one sentence is wrong:

    - ai_view and market_summary keep their usable sentences, at most AI_VIEW_SENTENCES and SUMMARY_SENTENCES;
    - a key risk that is too long, repeats what can break or has a problem is removed; at most MAX_KEY_RISKS stay;
    - dominant_event is kept only for the backend's dominant event with a usable one-sentence reason (finalize() then
      writes the deterministic reason);
    - next_events keep known events with a usable one-sentence explanation, at most MAX_NEXT_EVENTS (classified ones
      first); a surprise_assessment the event's phase or cited documents do not allow becomes UNDETERMINED.

    Raises ValueError when no sentence of ai_view or of market_summary can be shown: the answer is rejected (a content
    failure, never corrected by a second request)."""
    if not isinstance(raw, dict):
        return raw, []
    er, _ = _parts(data)
    names, allowed, notes = _asset_names(er), _allowed_numbers(), []
    result = dict(raw)

    def usable(field: str, text: str, event_id: str | None = None) -> bool:
        problems = text_problems(field, text, data, event_id, names, allowed)
        if problems:
            notes.append(f'{field}: removed {_clip(text, 80)!r} ({problems[0]})')
        return not problems

    rejected = []
    for field, limit in (('ai_view', AI_VIEW_SENTENCES), ('market_summary', SUMMARY_SENTENCES)):
        text = raw.get(field)
        if not isinstance(text, str):
            continue
        parts = sentences(text)
        kept = [part for part in parts if usable(field, part)]
        if len(kept) > limit:
            notes.append(f'{field}: cut to {limit} sentences')
        if parts and not kept:
            rejected.append(next(note for note in reversed(notes) if note.startswith(f'{field}: removed')))
        result[field] = ' '.join(kept[:limit])
    if rejected:
        raise ValueError(_clip('Nothing usable left. ' + '; '.join(rejected), 480))

    risks, breaks = raw.get('key_risks'), _breaks(er)
    if isinstance(risks, list):
        kept = []
        for risk in risks:
            text = ' '.join(risk.split()) if isinstance(risk, str) else ''
            if not text or _folded(text) in breaks:
                continue
            if len(text) > KEY_RISK_CHARS:
                notes.append(f'key_risks: removed an item longer than {KEY_RISK_CHARS} characters')
            elif usable('key_risks', text):
                kept.append(text)
        result['key_risks'] = kept[:MAX_KEY_RISKS]

    chosen, dominant = raw.get('dominant_event'), _dominant_id(er)
    if isinstance(chosen, dict):
        reason = sentences(chosen['reason'])[:1] if isinstance(chosen.get('reason'), str) else []
        if dominant is None or str(chosen.get('event_id')) != dominant:
            notes.append(f"dominant_event: removed {chosen.get('event_id')!r} (the dominant event is {dominant or 'none'})")
            result['dominant_event'] = None
        elif not reason or len(reason[0]) > 320 or not usable('dominant_event', reason[0], dominant):
            result['dominant_event'] = None
        else:
            result['dominant_event'] = {'event_id': dominant, 'reason': reason[0]}

    items = raw.get('next_events')
    if isinstance(items, list):
        explainable, documents = events_index(er, potential=False), documents_by_event(data)
        explained: dict[str, dict] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            event_id = str(item.get('event_id'))
            event = explainable.get(event_id)
            if event is None or event_id in explained:
                notes.append(f'next_events: removed {event_id!r} ' + ('(repeated)' if event else '(not an event of the input)'))
                continue
            why = sentences(item['why_it_matters'])[:1] if isinstance(item.get('why_it_matters'), str) else []
            if not why or len(why[0]) > 320 or not usable('next_events', why[0], event_id):
                continue
            label = item.get('surprise_assessment')
            label = label if label in SURPRISE_ASSESSMENTS else 'UNDETERMINED'
            cited = [e for e in dict.fromkeys(str(x) for x in item.get('evidence_ids') or [] if isinstance(x, str))
                     if e in documents.get(event_id, set())][:5]
            if label not in allowed_assessments(event) or (label in SURPRISES and not cited):
                notes.append(f'next_events: {label} for {event_id} is not supported (phase {_phase(event) or "unknown"}, '
                             f'{len(cited)} official documents cited): stored as UNDETERMINED')
                label = 'UNDETERMINED'
            explained[event_id] = {'event_id': event_id, 'why_it_matters': why[0], 'surprise_assessment': label,
                                   'evidence_ids': cited}
        ordered = sorted(explained.values(), key=lambda entry: entry['surprise_assessment'] not in CLASSIFICATIONS)
        result['next_events'] = ordered[:MAX_NEXT_EVENTS]
    return result, notes


def sanitizer(data: dict) -> Callable[[Any], tuple[Any, list[str]]]:
    def repair(raw: Any) -> tuple[Any, list[str]]:
        return sanitize(raw, data)
    return repair


def finalize(output: dict, data: dict, source: str, generated_at: str) -> tuple[dict, dict[str, dict]]:
    """The stored output and its per-event assessments {assessment, source 'ai'|'none', evidence_ids, validated,
    guidance_pending, phase, generated_at} (docs/EVENT_AWARENESS.md §8, read by the event-risk layer as event.surprise).
    A key risk that only repeats a what-can-break item (shown in its own panel) is dropped, whatever the source. An AI
    output without a dominant_event gets the deterministic reason of the backend's dominant event. A classification
    (HAWKISH, DOVISH, NEUTRAL_PRICED) that is not an AI output citing official documents of that event is stored as
    UNDETERMINED. validated is true only for an AI classification of a released event that cites them and is not a
    pre-guidance label (a priced headline while guidance is pending); a deterministic fallback is never validated.
    The stored output also carries the keys the model no longer writes (LEGACY_OUTPUT_LISTS) as empty lists."""
    er, _ = _parts(data)
    explainable = events_index(er, potential=False)
    documents = documents_by_event(data)
    result = json.loads(json.dumps(output, default=str))
    assessments: dict[str, dict] = {}
    for item in _dicts(result.get('next_events')):
        event_id = str(item['event_id'])
        event = explainable.get(event_id) or {}
        cited = [e for e in dict.fromkeys(str(x) for x in item.get('evidence_ids') or [])
                 if e in documents.get(event_id, set())]
        label = item.get('surprise_assessment', 'UNDETERMINED')
        if label in CLASSIFICATIONS and (source != 'ai' or not cited):
            label = 'UNDETERMINED'
        pending = provisional(event)
        validated = (source == 'ai' and label in CLASSIFICATIONS and bool(cited) and not pending
                     and _phase(event) not in PRE_RELEASE)
        item.update(surprise_assessment=label, evidence_ids=cited)
        assessments[event_id] = {'assessment': label, 'source': 'ai' if source == 'ai' else 'none',
                                 'evidence_ids': cited, 'validated': validated, 'guidance_pending': pending,
                                 'phase': _phase(event) or None, 'generated_at': generated_at}
    if isinstance(result.get('key_risks'), list):
        breaks = _breaks(er)
        risks = [r for r in result['key_risks'] if not isinstance(r, str) or _folded(r) not in breaks]
        result['key_risks'] = risks[:MAX_KEY_RISKS]
    dominant = _event(er.get('dominant_event'))
    if source == 'ai' and not result.get('dominant_event') and dominant is not None:
        result['dominant_event'] = _dominant(dominant)
    stored = MarketInterpretationOutput.model_validate(result).model_dump(mode='json')
    return {**stored, **{key: [] for key in LEGACY_OUTPUT_LISTS}}, assessments


def checker(data: dict) -> Callable[[dict], None]:
    def check(output: dict) -> None:
        problems = violations(output, data)
        if problems:
            raise ValueError('; '.join(problems)[:500])
    return check


# --- deterministic fallback --------------------------------------------------------------------------------------------

def _clip(text: str, limit: int) -> str:
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rsplit(' ', 1)[0] + '…'


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in '.!?' else text + '.'


def _words(value: Any) -> str:
    return str(value).replace('_', ' ')


def _render(value: Any) -> str:
    """Snapshot values rendered as given (numbers keep their stored representation; nothing is rounded)."""
    if isinstance(value, dict):
        return ', '.join(f'{_words(k)} {_render(v)}' for k, v in value.items() if v not in (None, '', [], {}))
    if isinstance(value, list):
        return ', '.join(_render(v) for v in value if v not in (None, '', [], {}))
    return str(value)


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ', '.join(items[:-1]) + ' and ' + items[-1] if items else ''


def _regime_phrase(regime: dict) -> str | None:
    posture = str(regime.get('posture') or '').upper()
    if posture in {'', 'UNKNOWN', 'INSUFFICIENT_DATA'}:
        return None
    names = [str(n) for n in regime.get('regimes') or [] if n != 'MIXED']
    main = next((n for n in names if n in {'RISK_ON', 'RISK_OFF'}), None)
    causes = [n for n in names if n != main]
    label = {'RISK_ON': 'Risk-on', 'RISK_OFF': 'Risk-off', None: 'Mixed'}[main]
    cause = f" driven by {_join([_words(n).lower().replace('usd', 'USD') for n in causes])}" if causes else ''
    # 'Mixed conditions (mixed posture)' says the same thing twice: the posture is named only when it adds something.
    stance = '' if posture == label.upper() else f" ({_words(posture).lower()} posture)"
    return f'{label} conditions{cause}{stance}'


def _when(event: dict, today: str | None) -> str:
    if str(event.get('timing') or '').upper() == 'ANYTIME' or (not event.get('time_utc') and event.get('verification_status')):
        return 'with timing ANYTIME'
    date = event.get('date_bucharest')
    day = event.get('day_bucharest') or day_label(date) or date  # 'Wed 30 Sep', never the ISO date
    if event.get('time_known') is False or not event.get('time_bucharest'):
        return f'on {day} (time TBA)' if day else '(time TBA)'
    return f"at {event['time_bucharest']} Bucharest" + (f' on {day}' if day and str(date) != today else '')


def _name(event: dict) -> str:
    """Event names of unscheduled events are headlines: clipped so a sentence stays readable. An entry without a name
    (a post-event entry whose event left the calendar) is named by its class, never by its id."""
    return _clip(str(event.get('name') or _words(event.get('event_class') or '') or 'an unnamed event'), 120)


def _exposed(event: dict, limit: int = 5) -> list[str]:
    assets = [a for a in _dicts(event.get('affected_assets')) if a.get('symbol')]
    ranked = sorted(assets, key=lambda a: -(a['weight'] if isinstance(a.get('weight'), (int, float)) else 0))
    return [str(a['symbol']) for a in ranked[:limit]]


def _watch_sentence(event: dict, dominant: bool) -> str:
    """Sentence 2 of the market view: the one thing to watch, named once. Its time, countdown, relevance and the risk
    mode are on the event card and in the banner, so they are not repeated here."""
    name, phase = _name(event), _phase(event)
    if phase in IN_PROGRESS:
        subs = _dicts(event.get('sub_events'))
        upcoming = next((s for s in subs if _phase(s) not in FINISHED | IN_PROGRESS), None)
        running = next((s for s in subs if _phase(s) in IN_PROGRESS), None)
        if upcoming:
            return f"{name} is in progress: the {_words(upcoming.get('sub_event') or 'sub-event').lower()} is still ahead."
        if running:
            return f"{name} is in progress: the {_words(running.get('sub_event') or 'sub-event').lower()} is under way."
        return f'{name} is out and the market reaction is still forming.'
    if phase == 'POST_EVENT':
        return f'{name} is out and its market reaction is being measured.'
    return f"The {'dominant' if dominant else 'next'} event to watch is {name}."


def _closed(markets_open: dict | None) -> list[str]:
    return [label for key, label in MARKETS.items() if markets_open and markets_open.get(key) is False]


def _weekend(markets_open: dict | None) -> str | None:
    """The closed traditional markets by name ('Traditional markets are closed' only when all of them are), and the
    open sessions. Session state does not establish fresh observations."""
    closed = _closed(markets_open)
    if not closed:
        return None
    opened = [label for key, label in MARKETS.items() if (markets_open or {}).get(key) is True]
    opened += ['crypto'] if (markets_open or {}).get('crypto') else []
    sessions = f"; {_join(opened)} {'market is' if len(opened) == 1 else 'markets are'} open" if opened else ''
    if len(closed) == len(MARKETS):
        return (f"Traditional markets are closed ({', '.join(closed)}), so event implications for them cannot be "
                f'confirmed until they reopen{sessions}.')
    reopen = 'they reopen' if len(closed) > 1 else 'that market reopens'
    names = _join(closed)
    return f'{names[:1].upper()}{names[1:]} closed: event implications there cannot be confirmed until {reopen}{sessions}.'


def _sign(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return (value > 0) - (value < 0)
    if isinstance(value, dict):
        return _sign(value.get('direction', value.get('sign', value.get('expected'))))
    text = str(value).strip().lower()
    return 1 if text in UP else -1 if text in DOWN else 0


def _directions(scenario: dict) -> dict[str, int]:
    raw: Any = next((scenario[k] for k in ('directions', 'expected', 'effects', 'assets') if scenario.get(k)), {})
    if isinstance(raw, list):
        raw = {_symbol(i): i for i in _dicts(raw) if _symbol(i)}
    return {str(s): _sign(v) for s, v in raw.items()} if isinstance(raw, dict) else {}


def _expectations(event: dict) -> str:
    consensus = event.get('consensus')
    pricing = event.get('market_pricing')
    return (f"consensus: {_render(consensus) if consensus not in (None, '', {}) else 'NO CONSENSUS'}; "
            f"market pricing: {_render(pricing) if pricing not in (None, '', {}) else 'UNKNOWN'}")


def _known(value: Any, missing: str) -> bool:
    return value not in (None, '', {}, []) and str(value).strip().upper() != missing


_BRACKETED = re.compile(r'\s*\([^()]*\)\s*$')  # a trailing '(…)' of a scenario label


def _explain(event: dict) -> dict:
    """One sentence for an event, from its backend scenario templates: what each outcome would move (possibilities,
    never certainties). A template never classifies an outcome (docs/EVENT_AWARENESS.md §8): UNDETERMINED for
    scheduled events and while guidance is pending, NOT_APPLICABLE otherwise; stored with validated false."""
    # 'Hawkish (higher rate path)' is named 'Hawkish' here: the directions follow in brackets of their own.
    outcomes = [f"{_BRACKETED.sub('', str(s.get('label') or s.get('name') or s.get('id') or 'scenario')) or 'scenario'} ({line})"
                for s in _dicts(event.get('scenarios')) if (line := _scenario_line(s, 4))][:2]
    if outcomes:
        why = f"Possible outcomes: {' or '.join(outcomes)}."
    elif isinstance(event.get('consequences'), dict) and (line := _scenario_line({'expected': event['consequences']}, 4)):
        why = f'Possible consequences: {line}.'  # unscheduled events
    else:
        why = 'No expected direction is defined for this event; its effect is read from the market reaction after it.'
    _, pending = surprise_basis(event)
    scheduled = _phase(event) in PRE_RELEASE | IN_PROGRESS | FINISHED or pending is True
    return {'event_id': str(event['event_id']), 'why_it_matters': _clip(why, 320),
            'surprise_assessment': 'UNDETERMINED' if scheduled else 'NOT_APPLICABLE', 'evidence_ids': []}


def _fit(pieces: list[str], limit: int, max_chars: int) -> str:
    """Whole sentences in priority order while the text stays within `limit` sentences (same splitter as the schema)
    and `max_chars` characters."""
    if not pieces:
        return ''
    text = ''
    for piece in pieces:
        candidate = f'{text} {piece}'.strip()
        if len(sentences(candidate)) <= limit and len(candidate) <= max_chars:
            text = candidate
    return text or _clip(' '.join(sentences(pieces[0])[:limit]), max_chars)


DIRECTIONS = {'up', 'down', 'flat'}  # a driver direction that was measured ('unavailable' was not)


def _today(regime: dict, lists: dict[str, list[str]]) -> tuple[bool, bool]:
    """(something observed today agrees with the regime, something observed today disagrees with it).

    Assets: a favored or pressured asset agrees; an asset that is not confirming disagrees only when it has a current
    observation (lists['not_confirming']; one known from the last close alone says nothing about today). A last-close
    regime also has the regime's own current observations: a driver whose direction today equals its direction at the
    close agrees, another measured direction disagrees, and so does every divergence the regime reports."""
    agree = bool(lists['favored'] or lists['pressured'])
    disagree = bool(lists['not_confirming'])
    if regime.get('basis') == 'last_close':
        before = {str(d.get('driver')): d.get('direction') for d in _dicts(regime.get('drivers'))}
        for driver in _dicts(regime.get('current_drivers')):
            then, now = before.get(str(driver.get('driver'))), driver.get('direction')
            if then in DIRECTIONS and now in DIRECTIONS:
                agree, disagree = agree or now == then, disagree or now != then
        disagree = disagree or bool(_dicts(regime.get('divergences')))
    return agree, disagree


def _regime_sentence(regime: dict, lists: dict[str, list[str]]) -> tuple[str | None, str]:
    """Sentence 1 of the market view: the regime, its basis and whether today's observations confirm it (_today). No
    asset is named: the positioning lists, the driver arrows and the conflict line of the page name them, each once."""
    phrase = _regime_phrase(regime)
    if phrase is None:
        return None, 'Regime data is insufficient for a market view.'
    agree, disagree = _today(regime, lists)
    historical = regime.get('basis') == 'last_close'
    lead = f'{phrase} at the last close (historical context)' if historical else f'{phrase} on live data'
    if agree and disagree:
        return phrase, f"{lead}; today's observations only partly confirm it."
    if agree:
        return phrase, f"{lead}; today's observations confirm it."
    if disagree:
        return phrase, f"{lead}; today's observations are not confirming it."
    if historical:
        return phrase, f'{lead}; there is no current observation to confirm it yet.'
    return phrase, f'{lead}; no asset has enough evidence to confirm it yet.'


def _driver_inputs(driver: dict) -> str:
    """' (SP500 +0.51%, VIX −4.44%; VIX close of Tue 22 Sep is stale)': the driver's inputs, naming a stale close."""
    stale = [str(s) for s in driver.get('stale_closes') or [] if s]
    note = f"{_join(stale)} {'is' if len(stale) == 1 else 'are'} stale" if stale else ''
    text = '; '.join(t for t in (driver.get('input'), note) if t)
    return f' ({text})' if text else ''


def _flagged(event: dict, er: dict) -> bool:
    """EVENT DATA INCOMPLETE is said only for a flagged event (data_incomplete_flag, set at the configured levels), as
    on the event card and in the key risks; a block without the flag falls back to the snapshot's flagged list."""
    if 'data_incomplete_flag' in event:
        return event['data_incomplete_flag'] is True
    return str(event.get('event_id')) in {str(i.get('event_id')) for i in _dicts(er.get('data_incomplete'))}


def _summary(er: dict, regime: dict, focus: dict | None, today: str | None) -> list[str]:
    """market_summary sentences in priority order, each fact once and none that the market view already says or the
    regime banner shows as it is (basis label, driver arrows): current moves that oppose a last-close regime, a stale
    close that still votes, what the market expects from the event (only when a consensus or a pricing exists), a
    post-event reaction, whether setups wait for the event, an open unscheduled risk, the risk mode of another event
    and the closed markets."""
    index, mode = events_index(er), er.get('mode') or {}
    summary: list[str] = []
    opposed = [f"{d['feature']} (via {via})" if (via := (d.get('source') or {}).get('via')) else str(d['feature'])
               for d in _dicts(regime.get('divergences')) if d.get('feature')]
    if opposed:
        named = _join(opposed) if len(opposed) <= 3 else ', '.join(opposed[:3]) + ' and others'
        summary.append(f"Today's moves oppose the last-close inputs for {named}.")
    for driver in _dicts(regime.get('drivers')):
        # Only a close the backend marks STALE is called stale (stale_closes reads close_status).
        if driver.get('driver') and (stale := driver.get('stale_closes') or stale_closes(driver)):
            arrow = driver.get('arrow') or _words(driver.get('direction'))
            summary.append(f"Stale input still voting: {driver['driver']} {arrow}{_driver_inputs(dict(driver, stale_closes=stale))}.")
    if focus and (_known(focus.get('consensus'), 'NO CONSENSUS') or _known(focus.get('market_pricing'), 'UNKNOWN')):
        summary.append(_sentence(f'Market expectation for {_name(focus)} — {_expectations(focus)}'))
    for post in _dicts(er.get('post_event'))[:1]:
        name = _name(index.get(str(post.get('event_id')), post))
        summary.append(f'MARKET DIVERGENCE after {name}: measured reactions contradict the expected direction; risk '
                       'assets are resisting the impulse, so there is no automatic short.' if post.get('divergence') else
                       f"Post-event {name}: market confirmation {_render(post.get('confirmation', 'pending'))}.")
    waiting = [i for i in _dicts(er.get('trade_watch')) if _status(i).upper().startswith('WAIT')]
    if focus and waiting and _stage(focus) in {'PRE', 'LIVE'}:
        summary.append(f'Setups exposed to {_name(focus)} stay provisional until it resolves.')
    shown = (focus or {}).get('event_id')
    for active in [e for e in _dicts(er.get('active_events')) if _event(e) and e['event_id'] != shown][:1]:
        status = _words(active.get('status') or _phase(active) or 'UNRESOLVED')
        summary.append(f'ACTIVE EVENT RISK: {_name(active)} (timing ANYTIME, {status}).')
    if mode.get('active') and focus and mode.get('event_id') != focus.get('event_id') and mode.get('name'):
        summary.append(f"EVENT RISK MODE: {mode['name']} {_when(mode, today)}.")
    markets_open = er.get('markets_open') if isinstance(er.get('markets_open'), dict) else None
    if weekend := _weekend(markets_open):
        summary.append(weekend)
    return summary


def _risks(er: dict) -> list[str]:
    """Flagged incomplete event data and unconfirmed reports. The what-can-break items have their own panel next to the
    summary, so they are not repeated here."""
    index = events_index(er)
    risks: list[str] = []
    for item in er.get('data_incomplete') or []:
        if isinstance(item, dict):
            reasons = ', '.join(_words(r) for r in item.get('reasons') or [])
            risks.append(f"EVENT DATA INCOMPLETE: {_name(index.get(str(item.get('event_id')), item))} ({reasons})")
        else:
            risks.append(f'EVENT DATA INCOMPLETE: {item}')
    risks += [f'Unconfirmed: {_name(e)} (under verification)' for e in _dicts(er.get('potential_events')) if _event(e)]
    return list(dict.fromkeys(_clip(r, KEY_RISK_CHARS) for r in risks if r))


def _dominant(event: dict) -> dict:
    score, level = event.get('dominance_score'), event.get('relevance_level')
    reason = (f'Dominance score {_render(score)}/100' if score is not None else 'The dominant event') + (
        f' with {_words(level)} relevance' if level else '') + (
        '; it can matter more than the current regime for the next directional move.')
    return {'event_id': str(event['event_id']), 'reason': _clip(reason, 320)}


NOTHING_ELSE = 'Nothing else is flagged for the current snapshot.'


def deterministic_output(snapshot: dict) -> dict:
    """Template summary of the same snapshot (source 'deterministic'), as short as the AI narrative and with each fact
    once. This is what the user reads most of the time, so the market view is two short sentences: the regime, its
    basis and whether today's observations confirm it, then the one event to watch by name (no time, no list of
    assets, no session sentence: the event card, the positioning lists and the summary say those). Then a summary of
    at most SUMMARY_SENTENCES sentences that adds what the page does not already show, at most MAX_KEY_RISKS key
    risks, the dominant event's reason and one sentence for the dominant and the next event.
    It quotes only numbers of its input as given (and the bare numbers the role may write, such as the 100 of a score
    out of 100); a sentence that would carry another number is dropped."""
    if isinstance(snapshot.get('event_risk'), dict):
        data = {**snapshot, 'event_risk': _prepared(snapshot['event_risk'])}
    else:
        data = bundle(snapshot)
    er, regime = _parts(data)
    cfg = config()
    known = numbers(json.dumps(data, default=str, ensure_ascii=False)) | set(numeric_allowlist(ROLE))

    def traceable(text: Any) -> bool:
        return bool(text) and numbers(str(text)) <= known and not COUNTDOWN.search(str(text))

    local = _local(er.get('generated_at'))
    today = local.date().isoformat() if local else None
    focus_events = [event for _, event in _focus(er)]
    dominant = _event(er.get('dominant_event'))
    focus = focus_events[0] if focus_events else None
    valid = re.compile(r'[A-Z0-9]{2,16}')
    listed = int(cfg['max_listed'])
    against = [i for i in _dicts(er.get('non_confirming')) if valid.fullmatch(_symbol(i))]
    lists = {'favored': [_symbol(i) for i in _dicts(er.get('favored')) if valid.fullmatch(_symbol(i))][:listed],
             'pressured': [_symbol(i) for i in _dicts(er.get('pressured')) if valid.fullmatch(_symbol(i))][:listed],
             'not_confirming': [_symbol(i) for i in against if current_evidence(i)][:listed]}

    _, regime_text = _regime_sentence(regime, lists)
    event_text = _watch_sentence(focus, focus is dominant) if focus else 'No high-relevance scheduled event is on the calendar.'
    view = [t for t in (regime_text, event_text) if t and traceable(t)] or ['Deterministic market summary.']
    if len(' '.join(view).split()) > AI_VIEW_WORDS:
        view = view[:1]
    summary = [text for text in _summary(er, regime, focus, today) if traceable(text)]
    explained = [item for item in (_explain(event) for event in focus_events if event.get('name'))
                 if traceable(item['why_it_matters'])]
    dominant_item = _dominant(dominant) if dominant else None
    output: dict[str, Any] = {
        'ai_view': _fit(view, AI_VIEW_SENTENCES, 480),
        'market_summary': _fit(summary, SUMMARY_SENTENCES, 1000) or NOTHING_ELSE,
        'key_risks': [r for r in _risks(er) if traceable(r)][:MAX_KEY_RISKS],
        'dominant_event': dominant_item if dominant_item and traceable(dominant_item['reason']) else None,
        'next_events': explained[:MAX_NEXT_EVENTS]}
    legacy: dict[str, Any] = {key: [] for key in LEGACY_OUTPUT_LISTS}
    try:
        return {**MarketInterpretationOutput.model_validate(output).model_dump(mode='json'), **legacy}
    except ValidationError:  # last resort: the first sentence of the market view, built from backend labels
        first = _clip(' '.join(sentences(output['ai_view'])[:1]), 300)
        for view_text in (first, 'Deterministic market summary unavailable for this snapshot.'):
            try:
                minimal = MarketInterpretationOutput.model_validate({'ai_view': view_text, 'market_summary': NOTHING_ELSE})
                return {**minimal.model_dump(mode='json'), **legacy}
            except ValidationError:
                continue
        raise


# --- storage and refresh -----------------------------------------------------------------------------------------------

def _recent_rows(store: Store, limit: int = 1) -> list[dict]:
    """The newest stored runs, newest first ([] on a database from before migration 0005)."""
    try:
        return store.list(TABLE, limit)
    except SQLAlchemyError:
        return []


def _latest_row(store: Store) -> dict | None:
    rows = _recent_rows(store)
    return rows[0] if rows else None


def _fresh_ai_row(store: Store, now: datetime) -> dict | None:
    """The newest stored AI interpretation that is still within unchanged_max_age_seconds, if any."""
    cls = RECORDS[TABLE]
    cutoff = utcnow() - timedelta(seconds=float(config().get('unchanged_max_age_seconds') or 0))
    query = (select(cls).where(cls.payload['source'].as_string() == 'ai', cls.created_at >= cutoff)
             .order_by(cls.created_at.desc()).limit(1))
    try:
        with store.session() as session:
            row: Any = session.scalars(query).first()
            found = dict(row.payload, id=row.id) if row is not None else None
    except SQLAlchemyError:
        return None
    return found if found and 0 <= _age(found, now) <= float(config().get('unchanged_max_age_seconds') or 0) else None


def _age(row: dict, now: datetime) -> float:
    return (aware(now) - aware(row['generated_at'])).total_seconds()


def carry_validated(assessments: dict[str, dict], previous: dict | None, data: dict) -> dict[str, dict]:
    """A validated classification outlives a later run that does not validate one (a deterministic fallback, an
    UNDETERMINED read, or an event dropped from next_events) while the event is still in the snapshot; a newer validated
    classification replaces it. The kept record is marked carried."""
    current = events_index(_parts(data)[0], potential=False)
    result = dict(assessments)
    for event_id, item in (previous or {}).items():
        if (isinstance(item, dict) and item.get('validated') is True and event_id in current
                and not (result.get(event_id) or {}).get('validated')):
            result[event_id] = dict(item, carried=True)
    return result


def _stale_limit(source: str, cadence: float | None = None) -> float:
    cfg = config()
    limit = cfg['stale_factor'] * (cadence or cfg['active_seconds'])
    if source == 'ai':  # refresh keeps an unchanged AI basis this long (for_snapshot checks the basis)
        limit = max(limit, float(cfg.get('unchanged_max_age_seconds') or 0))
    return limit


def _dashboard(row: dict, now: datetime) -> dict:
    age = _age(row, now)
    limit = _stale_limit(row['source'], row.get('cadence_seconds'))
    return {'source': row['source'], 'model': row.get('model'), 'generated_at': row['generated_at'],
            'stale': age < 0 or age > limit, 'stale_after_seconds': limit,
            'basis_hash': row['basis_hash'], 'core_hash': row.get('core_hash'), 'prose_hash': row.get('prose_hash'),
            'bindings': row.get('bindings'), 'output': row['output'],
            'assessments': row.get('assessments') or {}, 'label': row.get('label'), 'run_id': row.get('run_id'),
            'reasons': row.get('reasons', []), 'age_seconds': round(age, 1)}


def latest(store: Store, now: datetime | None = None) -> dict | None:
    """The dashboard 'interpretation' block from the stored runs: the newest AI interpretation that is still within
    unchanged_max_age_seconds, else the newest run (for_snapshot() then decides whether that text still holds for the
    snapshot; a newer deterministic row, stored while the AI was not asked, must not hide an AI text that is true
    again, unless that row says the role was switched off). Stale when older than stale_factor x the cadence that was active when it was generated (an AI text:
    at least unchanged_max_age_seconds). 'assessments' holds that run's per-event surprise assessments
    {event_id: {assessment, source 'ai'|'none', evidence_ids, validated, ...}} (empty for runs stored before them)."""
    now = now or utcnow()
    row = _latest_row(store)
    if row is None:
        return None
    if row.get('source') != 'ai' and _outcome(row) != 'role_unavailable':
        row = _fresh_ai_row(store, now) or row
    return _dashboard(row, now)


def _same(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(right, sort_keys=True, default=str)


def servable(stored: dict | None, data: dict) -> bool:
    """Whether a stored interpretation (a dashboard block or a stored row with `stale` set) still holds for the
    prepared bundle `data`: an AI text that is not stale, whose core is unchanged, whose bindings (driver directions,
    open unscheduled events, the lists of the assets it names, and the sessions, trade statuses or unverified reports
    it speaks of) are unchanged and whose numbers, times and probabilities are still input values; a text that quotes
    percentage or basis-point moves also needs the same observations (prose_hash). A row from before the core hash
    is served only for an identical basis."""
    if not stored or stored.get('source') != 'ai' or stored.get('stale'):
        return False
    output = stored.get('output') or {}
    if stored.get('core_hash'):
        if stored['core_hash'] != core_hash(data) or not _same(stored.get('bindings'), bindings(output, data)):
            return False
        if cites_moves(output) and stored.get('prose_hash') != prose_hash(data):
            return False
    else:
        if stored.get('basis_hash') != basis_hash(data):
            return False
        if stored.get('prose_hash') != prose_hash(data) and (
                stored.get('prose_hash') or any(re.search(r'\d', text) for _, _, text in _texts(output))):
            return False
    return not _number_problems(output, data)


def for_snapshot(stored: dict | None, snapshot: dict, regime: dict | None, providers: Iterable[dict], now: datetime,
                 news: Iterable[dict] | None = None) -> dict:
    """Serve coherent text from the current snapshot without inference, provider requests or storage writes.

    A stored AI text is retained while it still holds for this snapshot (servable()). Deterministic text is cheap to
    rebuild and must not freeze while the AI role is waiting. Validated event assessments keep their provenance when
    the prose is replaced.
    """
    data = bundle(snapshot, regime, providers, now, news)
    if servable(stored, data):
        assert stored is not None
        return stored
    return {'source': 'deterministic', 'model': None, 'generated_at': now.isoformat(), 'stale': False,
            'age_seconds': 0.0,
            'stale_after_seconds': _stale_limit('deterministic', cadence_seconds(snapshot.get('markets_open') or None)),
            'basis_hash': basis_hash(data), 'core_hash': core_hash(data), 'output': deterministic_output(data),
            'assessments': (stored or {}).get('assessments') or {},
            'label': 'Deterministic summary — current snapshot', 'run_id': None,
            'previous_generated_at': (stored or {}).get('generated_at')}


def _collect(hub: Any, now: datetime) -> tuple[dict, dict | None, list[dict], list[dict]]:
    """Deterministic inputs: the event-risk snapshot, the display regime (unless the snapshot carries it), provider
    health and the news events it was built from (their official documents are the evidence of released central-bank
    events)."""
    module = importlib.import_module('backend.event_risk')
    loader = getattr(hub, 'events', None)
    news = loader(int(rules()['event_risk']['news_events_limit'])) if callable(loader) else None
    snapshot = module.snapshot(hub, now) if news is None else module.snapshot(hub, now, events=news)
    regime = snapshot.get('regime')
    if not isinstance(regime, dict) or 'posture' not in regime:
        regime = hub.regime(last_close=True)
    return snapshot, regime, hub.health(), list(news or [])


def prune(store: Store) -> int:
    """Retention: rows stored more than retention_days ago (by the storage clock, like created_at)."""
    cls = RECORDS[TABLE]
    cutoff = utcnow() - timedelta(days=config()['retention_days'])
    with store.session() as session:
        result = session.execute(delete(cls).where(cls.created_at < cutoff))
        session.commit()
        return getattr(result, 'rowcount', 0) or 0


def _outcome(row: dict) -> str | None:
    """How the AI part of a stored run ended (`ai_outcome`): 'ok', 'invalid' (sent, answer rejected), 'refused' (the
    budget sent nothing), 'unavailable' (the provider did not answer), 'reused', or the reason no request was made
    ('role_unavailable', 'known_failed', 'provider_backoff', 'deferred'). Rows stored before the field: 'ok' for an AI
    text, 'refused' for a budget refusal, 'invalid' for another run that reached a model."""
    if row.get('ai_outcome'):
        return str(row['ai_outcome'])
    if row.get('source') == 'ai':
        return OK
    if str(row.get('fallback_reason') or '') in set(REFUSAL_MESSAGES.values()):
        return REFUSED
    return INVALID if row.get('run_id') else None


WAITING = {
    'role_unavailable': f'{ROLE} role unavailable',
    'known_failed': 'AI output was rejected for this market state; it is not sent again until the state changes',
    'provider_backoff': 'AI provider did not answer; waiting before the next request',
    'deferred': 'Minor change since the last AI request; the next one waits for the minimum interval',
}


def _decide(rows: list[dict], data: dict, digest: str, core_digest: str, now: datetime, available: bool,
            allowed: Callable[[], bool]) -> tuple[str, Any]:
    """What one refresh does with the stored runs (newest first) and the current state:

    ('keep', row)     nothing is stored and nothing is asked: `row` is served (an AI text that still holds, or the
                      deterministic run of the same state);
    ('wait', outcome) the deterministic summary is stored for a new state without a request (WAITING[outcome]);
    ('ask', None)     one AI request is made.

    A request is made only when no stored AI text holds for the state (or one holds but the basis changed and
    min_ai_interval_seconds passed since the last request), the role is available, this state was not rejected within
    retry_failed_after_seconds, the provider did not fail within retry_unavailable_after_seconds, the last request is
    older than min_ai_interval_seconds or the core changed since, and the budget did not refuse this same state."""
    cfg = config()
    recent = [row for row in rows if _age(row, now) >= 0]
    last = rows[0] if rows else None
    request = next((row for row in recent if _outcome(row) in SENT), None)
    since_request = _age(request, now) if request else None
    interval = float(cfg['min_ai_interval_seconds'])
    if available:
        keep_for = float(cfg.get('unchanged_max_age_seconds') or cfg['active_seconds'])
        held = next((row for row in recent if row.get('source') == 'ai' and _age(row, now) < keep_for
                     and servable(dict(row, stale=False), data)), None)
        if held is not None and (held.get('basis_hash') == digest
                                 or (since_request is not None and since_request < interval)):
            return 'keep', held
    same = last is not None and last.get('source') != 'ai' and last.get('basis_hash') == digest and _age(last, now) >= 0
    if same and last is not None and _age(last, now) < float(cfg['active_seconds']):
        return 'keep', last

    def wait(outcome: str) -> tuple[str, Any]:
        return ('keep', last) if same else ('wait', outcome)
    if not available:
        return wait('role_unavailable')
    if any(row.get('basis_hash') == digest and _outcome(row) == INVALID
           and _age(row, now) < float(cfg['retry_failed_after_seconds']) for row in recent):
        return wait('known_failed')
    if request is not None and since_request is not None:
        if _outcome(request) == UNAVAILABLE and since_request < float(cfg['retry_unavailable_after_seconds']):
            return wait('provider_backoff')
        if since_request < interval and request.get('core_hash') == core_digest:
            return wait('deferred')
    if same and last is not None and _outcome(last) == REFUSED and not allowed():
        return 'keep', last  # the budget still refuses: no new attempt, no new refusal row
    return 'ask', None


def _row_id(now: datetime) -> str:
    return f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{uid()[:8]}"


async def refresh(hub: Any, reasons: list[dict], now: datetime | None = None) -> dict:
    """One interpretation run (see _decide for when a request is made). The model reads the brief and its answer is
    repaired (sanitize), checked (violations) and finalized; without a valid answer the deterministic summary of the
    same snapshot is stored instead, with the reason and `ai_outcome`, so the state is remembered across restarts.
    The returned block has skipped=True when nothing was stored. Either way the stored row carries the per-event
    assessments (finalize)."""
    now = aware(now) if now else utcnow()
    snapshot, regime, health, news = await asyncio.to_thread(_collect, hub, now)
    data, findings = prepare(snapshot, regime, health, now, news)
    digest, core_digest, cfg = basis_hash(data), core_hash(data), config()
    rows = _recent_rows(hub.store, int(cfg['memory_rows']))
    action, detail = _decide(rows, data, digest, core_digest, now, hub.ai.available(ROLE), lambda: hub.ai.allowed(ROLE))
    if action == 'keep':
        kept = detail
        newest_ai = next((row for row in rows if row.get('source') == 'ai'), None)
        if kept.get('source') == 'ai' and rows[0] is not kept and (newest_ai is not kept
                                                                   or _outcome(rows[0]) == 'role_unavailable'):
            # An older AI text holds again (the state flipped back): stored as the newest run, so it is served.
            kept = dict({key: value for key, value in kept.items() if key != 'id'}, ai_outcome='reused', reused_from=kept.get('id'))
            hub.store.put(TABLE, _row_id(now), kept)
        return dict(_dashboard(kept, now), skipped=True)
    output, run_id, model, size = None, None, None, None
    if action == 'ask':
        slim = brief(data)
        size = len(json.dumps(slim, ensure_ascii=False, default=str))
        try:
            output, run_id = await hub.ai.run_with_id(ROLE, slim, check=checker(slim), repair=sanitizer(slim))
            run = (hub.store.get('ai_runs', run_id) or {}) if run_id else {}
            if output:
                outcome, fallback, model = OK, None, run.get('model')
            elif run.get('budget_refusal'):  # the daily AI budget sent nothing (ai_runtime budget, docs/AI_AGENTS.md)
                outcome, fallback = REFUSED, run.get('error')
            elif run.get('error') == 'ProviderUnavailable':
                outcome, fallback = UNAVAILABLE, 'AI provider did not answer'
            elif run:
                outcome, fallback = INVALID, 'AI output rejected'
            else:
                outcome, fallback = 'role_unavailable', 'No model available'
        except ValueError as exc:  # the brief exceeds the application input limit
            outcome, fallback = 'error', str(exc)[:200]
    else:
        outcome, fallback = detail, WAITING[detail]
    source = 'ai' if output else 'deterministic'
    stored, assessments = finalize(output or deterministic_output(data), data, source, now.isoformat())
    assessments = carry_validated(assessments, (rows[0] if rows else {}).get('assessments'), data)
    row = {'source': source, 'model': model, 'generated_at': now.isoformat(), 'basis_hash': digest,
           'core_hash': core_digest, 'prose_hash': prose_hash(data),
           'bindings': bindings(stored, data) if output else None, 'output': stored,
           'assessments': assessments, 'run_id': run_id, 'label': None if output else DETERMINISTIC_LABEL,
           'fallback_reason': fallback, 'ai_outcome': outcome, 'brief_chars': size, 'input_findings': findings[:20],
           'reasons': [{k: r.get(k) for k in ('trigger', 'id') if k in r} for r in reasons if isinstance(r, dict)],
           'cadence_seconds': cadence_seconds(data['event_risk'].get('markets_open') or None),
           'refresh_version': rules().get('refresh_version'), 'prompts_version': prompts().get('prompts_version')}
    hub.store.put(TABLE, _row_id(now), row)
    prune(hub.store)
    return dict(_dashboard(row, now), skipped=False)
