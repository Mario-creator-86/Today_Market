import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Quote(StrictModel):
    symbol: str = Field(pattern=r'^[A-Z0-9]{2,16}$')
    price: float = Field(gt=0)
    timestamp: AwareDatetime
    provider: Literal['binance', 'coinbase', 'massive', 'deribit', 'treasury', 'ecb', 'ecb-derived', 'eia', 'fred', 'finnhub',
                      'alpaca', 'massive-stub']
    latency_ms: float = Field(default=0, ge=0)
    stub: bool = False
    daily_close: bool = False
    bid: float | None = Field(default=None, gt=0)
    ask: float | None = Field(default=None, gt=0)
    volume: float | None = Field(default=None, ge=0)
    change_pct: float | None = None
    # Provenance of the change and of the value (docs/MARKET_COVERAGE.md): what it is compared with, what kind of data it is.
    previous_close: float | None = Field(default=None, gt=0)
    previous_close_at: AwareDatetime | None = None
    change_bp: float | None = None
    change_basis: Literal['previous_close', 'previous_fix', 'rolling_24h', 'provider'] | None = None
    data_class: Literal['LIVE', 'DAILY_CLOSE', 'DERIVED_DAILY'] = 'LIVE'
    as_of_label: str | None = Field(default=None, max_length=200)
    label: str | None = Field(default=None, max_length=120)
    attribution: str | None = Field(default=None, max_length=200)
    derived: bool = False
    feed: str | None = Field(default=None, max_length=32)

    @model_validator(mode='after')
    def spread_valid(self):
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError('bid exceeds ask')
        return self


class DocumentInput(StrictModel):
    source_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=5, max_length=1000)
    content: str = Field(max_length=40000)
    url: HttpUrl
    published_at: AwareDatetime
    event_type: str = Field(default='NEWS', max_length=64)
    assets: list[str] = Field(default_factory=list, max_length=30)
    event_id: str | None = None
    relation: Literal['supports', 'contradicts'] = 'supports'
    direct: bool = True
    discovery_only: bool = False


class CalendarInput(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    country: str = Field(max_length=10)
    scheduled_at: AwareDatetime
    source_id: str
    impact: int = Field(ge=0, le=10)
    consensus: float | None = None
    previous: float | None = None
    actual: float | None = None
    unit: str = Field(default='', max_length=32)
    historical_surprises: list[float] = Field(default_factory=list, max_length=1000)


class RoleAssignment(StrictModel):
    primary: str | None = None
    backup: str | None = None
    second_opinion: str | None = None


class ResearchPlan(StrictModel):
    queries: list[str] = Field(max_length=3)
    priority: int = Field(ge=0, le=100)
    reason: str = Field(max_length=500)
    domains_preferred: list[str] = Field(max_length=10)

    @model_validator(mode='after')
    def bounded_queries(self):
        if any(not q.strip() or len(q) > 256 for q in self.queries):
            raise ValueError('Each query must be 1-256 characters')
        return self


class Extraction(StrictModel):
    who: list[str]
    what: str
    when: str
    where: str
    direct_claims: list[str]
    new_information: list[str]
    background_information: list[str]
    mentioned_assets: list[str]
    mentioned_entities: list[str]
    event_type: str
    potential_importance: int = Field(ge=0, le=100)


class FactCheck(StrictModel):
    """Backend-computed verification result (status and confidence come from engines.confidence)."""
    status: Literal['CONFIRMED', 'PARTIALLY_CONFIRMED', 'UNCONFIRMED', 'CONTRADICTED', 'FALSE', 'STALE']
    fact_confidence: float = Field(default=0, ge=0, le=100)
    verified_statements: list[str]
    unverified_statements: list[str]
    contradictions: list[str]
    evidence_ids: list[str]
    explanation: str


class EvidenceRelation(StrictModel):
    evidence_id: str
    relation: Literal['supports', 'contradicts', 'unrelated', 'second_hand']
    original_source: str | None = Field(default=None, max_length=200)


class FactCheckProposal(StrictModel):
    """The AI proposes evidence relationships only; it never assigns status or confidence."""
    relations: list[EvidenceRelation] = Field(max_length=100)
    verified_statements: list[str] = Field(max_length=20)
    unverified_statements: list[str] = Field(max_length=20)
    contradictions: list[str] = Field(max_length=20)
    evidence_ids: list[str]
    explanation: str = Field(max_length=1000)


class Hypothesis(StrictModel):
    asset: str
    direction: Literal['positive', 'negative', 'neutral']
    causal_path: list[str]
    evidence_ids: list[str]
    confirmation: str
    invalidation: str


class SpecialistAnalysis(StrictModel):
    hypotheses: list[Hypothesis]
    uncertainties: list[str]


class FXPairView(StrictModel):
    pair: str = Field(pattern=r'^(DXY|[A-Z]{6})$')
    bias: Literal['USD-positive', 'USD-negative', 'neutral-wait']
    base_bank: str
    counter_bank: str
    policy_comparison: str
    yield_differential_view: str
    intervention_risk: str
    causal_path: list[str] = Field(min_length=2)
    evidence_ids: list[str]
    confirmation: str
    invalidation: str

    @model_validator(mode='after')
    def relative(self):
        if self.pair != 'DXY' and self.base_bank.strip().casefold() == self.counter_bank.strip().casefold():
            raise ValueError('FX analysis must compare two central banks')
        return self


class FXAnalysis(StrictModel):
    pairs: list[FXPairView]
    uncertainties: list[str]


class CryptoView(StrictModel):
    asset: Literal['BTC', 'ETH', 'COIN', 'MSTR', 'BMNR']
    direction: Literal['positive', 'negative', 'neutral']
    drivers: list[str]
    positioning_note: str
    causal_path: list[str]
    evidence_ids: list[str]
    confirmation: str
    invalidation: str


FORBIDDEN_CRYPTO_INFERENCES = ('max pain target', 'toward max pain', 'to max pain', 'max pain will', 'calls outnumber puts, so',
                               'more calls than puts, bullish', 'call/put ratio is bullish', 'calls > puts')


class CryptoAnalysis(StrictModel):
    assets: list[CryptoView]
    uncertainties: list[str]

    @model_validator(mode='after')
    def no_positioning_fallacies(self):
        text = self.model_dump_json().casefold()
        if any(phrase in text for phrase in FORBIDDEN_CRYPTO_INFERENCES):
            raise ValueError('Max pain is not a target and calls > puts is not a bullish signal')
        return self


class GeoDevelopment(StrictModel):
    summary: str
    market_relevant: bool
    pathway: list[str] = Field(min_length=2)
    affected_assets: list[str]
    evidence_ids: list[str]


class GeoAnalysis(StrictModel):
    developments: list[GeoDevelopment]
    uncertainties: list[str]

    @model_validator(mode='after')
    def market_relevant_only(self):
        if any(not d.market_relevant for d in self.developments):
            raise ValueError('Report only market-relevant developments')
        return self


class Explanation(StrictModel):
    reasons: list[str] = Field(max_length=5)
    evidence_ids: list[str]
    confirmation: str = Field(max_length=400)
    invalidation: str = Field(max_length=400)

    @model_validator(mode='after')
    def concise(self):
        if any(len(r) > 240 for r in self.reasons):
            raise ValueError('Each reason must be concise (240 characters or fewer)')
        return self


AUDIT_CHECKS = Literal['fresh_prices', 'valid_timestamps', 'market_open', 'sources_attached', 'old_article_as_new',
                       'claim_beyond_source', 'contradictions_visible', 'direction_matches_score', 'probabilities_valid',
                       'invented_numbers', 'timezone_conversion', 'unverified_x_as_fact']


class AuditCheck(StrictModel):
    name: AUDIT_CHECKS
    passed: bool
    detail: str = Field(default='', max_length=500)


class Audit(StrictModel):
    status: Literal['PASS', 'BLOCK']
    reasons: list[str]
    checks: list[AuditCheck] = Field(default_factory=list)

    @model_validator(mode='after')
    def consistent(self):
        if self.status == 'PASS' and any(not c.passed for c in self.checks):
            raise ValueError('PASS is inconsistent with a failed check')
        return self


class SearchResult(StrictModel):
    title: str = Field(default='', max_length=500)
    url: HttpUrl
    content: str = Field(default='', max_length=20000)


# --- market_interpreter (docs/EVENT_AWARENESS.md §5): text only; every number, time and score is backend-owned -------

SURPRISE_ASSESSMENTS = ('HAWKISH_SURPRISE', 'DOVISH_SURPRISE', 'NEUTRAL_PRICED', 'UNDETERMINED', 'NOT_APPLICABLE')
SurpriseAssessment = Literal['HAWKISH_SURPRISE', 'DOVISH_SURPRISE', 'NEUTRAL_PRICED', 'UNDETERMINED', 'NOT_APPLICABLE']
# A sentence ends at . ! or ? followed by whitespace and an upper-case letter, digit or quote ("4.25" and "U.S. dollar"
# do not split). The deterministic fallback caps its text with the same splitter, so it always validates.
SENTENCE_END = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"\'“(\[])')
# The countdown is frontend arithmetic (spec §35): relative times drift, so the model states clock times only.
_SPAN = r'(?:\d+(?:[.,]\d+)?|an?|one|two|three|four|five|six|a few|a couple of|several|half an?)'
COUNTDOWN = re.compile(r'\bin\s+(?:about\s+|approximately\s+|roughly\s+|around\s+|just\s+|~\s*)?\d+(?:[.,]\d+)?\s*'
                       r'(?:minutes?|mins?|m|hours?|hrs?|h)\b|'
                       rf'\bin\s+(?:about\s+|approximately\s+|roughly\s+|around\s+|just\s+|under\s+)?{_SPAN}\s+'
                       r'(?:minutes?|mins?|hours?|hrs?)\b|'
                       r'\bhalf an hour\b|'
                       rf'\b{_SPAN}\s*(?:minutes?|mins?|hours?|hrs?|h)\s+(?:away|left|remaining|from now|to go)\b',
                       re.IGNORECASE)
# Scenario outcomes are possibilities, never certainties (spec §41).
FORBIDDEN_CERTAINTY = ('will certainly', 'certainly will', 'is certain to', 'are certain to', 'definitely will',
                       'will definitely', 'risk-free', 'guarantee', 'will surely', 'surely will', 'is sure to',
                       'are sure to', 'no doubt', 'without doubt', 'cannot fail', "can't fail", 'cannot lose',
                       "can't lose", 'inevitabl', '100% certain', 'a sure thing', 'sure bet')
# "not risk-free", "no guarantee", "nothing is guaranteed" state uncertainty: a negation just before a phrase clears it.
_NEGATED_CERTAINTY = re.compile(r"\b(?:not|no|nothing|never|isn't|aren't|is not|are not|without)\W+(?:\w+\W+){0,2}$")


def certainty_claim(text: str) -> str | None:
    """The first certainty phrase of `text` that is not negated, else None."""
    folded = text.casefold()
    for phrase in FORBIDDEN_CERTAINTY:
        start = folded.find(phrase)
        while start >= 0:
            if not _NEGATED_CERTAINTY.search(folded[max(0, start - 40):start]):
                return phrase
            start = folded.find(phrase, start + 1)
    return None


def sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_END.split(text.strip()) if s.strip()]


class LenientModel(BaseModel):
    """Base of the market interpreter's output. A key the schema does not know is ignored: a model that adds a field it
    used to write (asset comments, bull and bear cases) must not cost a second request. Every limit still applies."""
    model_config = ConfigDict(extra='ignore', allow_inf_nan=False)


# Limits of the interpreter's narrative (docs/AI_AGENTS.md). The backend owns every structured fact, so the model writes
# only this much; interpretation.sanitize() cuts longer answers to these limits before the schema is applied.
AI_VIEW_SENTENCES = 2
AI_VIEW_WORDS = 60
SUMMARY_SENTENCES = 4
MAX_KEY_RISKS = 3
KEY_RISK_CHARS = 200
MAX_NEXT_EVENTS = 2
# Keys of the stored and served output that the model no longer writes (the deterministic lists carry those facts);
# kept as empty lists so older readers of the dashboard block keep working.
LEGACY_OUTPUT_LISTS = ('favored_assets', 'pressured_assets', 'non_confirming_assets', 'trade_watch')


class InterpretedEvent(LenientModel):
    event_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=320)


class EventExplanation(LenientModel):
    """One sentence on why an event matters; its numbers (time, consensus, pricing, exposure) are backend-owned.
    evidence_ids: the official documents of this event (input official_evidence) its surprise_assessment rests on; a
    classification without them is stored as UNDETERMINED (docs/EVENT_AWARENESS.md §8)."""
    event_id: str = Field(min_length=1, max_length=128)
    why_it_matters: str = Field(min_length=1, max_length=320)
    surprise_assessment: SurpriseAssessment = 'UNDETERMINED'
    evidence_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(default_factory=list, max_length=5)


class MarketInterpretationOutput(LenientModel):
    """What the market_interpreter model writes: a short narrative only. ai_view: 1-2 sentences (the market state and
    whether today's observations confirm it; the one thing to watch). market_summary: at most 4 sentences. key_risks: at
    most 3 short items. dominant_event: the backend's dominant event and one sentence of reason. next_events: at most 2
    events, one sentence each."""
    ai_view: str = Field(min_length=1, max_length=480)
    market_summary: str = Field(min_length=1, max_length=1000)
    key_risks: list[str] = Field(default_factory=list, max_length=MAX_KEY_RISKS)
    dominant_event: InterpretedEvent | None = None
    next_events: list[EventExplanation] = Field(default_factory=list, max_length=MAX_NEXT_EVENTS)

    @model_validator(mode='after')
    def bounded_and_uncertain(self):
        if not 1 <= len(sentences(self.ai_view)) <= AI_VIEW_SENTENCES:
            raise ValueError(f'ai_view must be 1-{AI_VIEW_SENTENCES} sentences')
        if len(self.ai_view.split()) > AI_VIEW_WORDS:
            raise ValueError(f'ai_view must be at most {AI_VIEW_WORDS} words')
        if len(sentences(self.market_summary)) > SUMMARY_SENTENCES:
            raise ValueError(f'market_summary must be at most {SUMMARY_SENTENCES} sentences')
        if any(not r.strip() or len(r) > KEY_RISK_CHARS for r in self.key_risks):
            raise ValueError(f'Each key risk must be 1-{KEY_RISK_CHARS} characters')
        if len({e.event_id for e in self.next_events}) != len(self.next_events):
            raise ValueError('next_events repeats an event_id')
        text = self.model_dump_json()
        if COUNTDOWN.search(text):
            raise ValueError('State event times as the supplied Bucharest clock time, never as a countdown')
        if certainty_claim(text):
            raise ValueError(f'Scenario outcomes are possibilities, never certainties ({certainty_claim(text)!r})')
        return self


ROLE_SCHEMAS: dict[str, type[BaseModel]] = {
    'research_planner': ResearchPlan, 'extractor': Extraction, 'fact_checker': FactCheckProposal,
    'macro': SpecialistAnalysis, 'fx': FXAnalysis, 'crypto': CryptoAnalysis,
    'equity_credit': SpecialistAnalysis, 'geopolitical_energy': GeoAnalysis,
    'chief_analyst': Explanation, 'auditor': Audit, 'market_interpreter': MarketInterpretationOutput,
}


def iso(value: datetime) -> str:
    from datetime import UTC
    return value.replace(tzinfo=UTC).isoformat() if value.tzinfo is None else value.astimezone(UTC).isoformat()
