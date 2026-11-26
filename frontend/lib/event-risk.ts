import type {
  AssetEventRisk,
  BreakItem,
  EventExplanation,
  EventExposure,
  EventObject,
  EventRisk,
  EventRiskMode,
  EventRiskValue,
  EventScenario,
  EventSurprise,
  Interpretation,
  MarketsOpen,
  PostEvent,
  ReactionValue,
  RelevanceLevel,
} from "./api";
import { exactTime, isAnytime, LIVE_PHASES, parseTime } from "./event-time";
import { isNum } from "./format";

/*
 * Presentation of the deterministic event-risk block (docs/EVENT_AWARENESS.md
 * §4, §6). Every number comes from the backend; missing data is shown as
 * missing ("NO CONSENSUS", "UNKNOWN", "EVENT DATA INCOMPLETE"), never filled
 * in. Pure, with relative imports only, so vitest can collect it.
 */

export const NO_CONSENSUS = "NO CONSENSUS";
export const UNKNOWN = "UNKNOWN";
export const DATA_INCOMPLETE = "EVENT DATA INCOMPLETE";
export const DETERMINISTIC_LABEL = "Deterministic summary — AI unavailable";
export const POTENTIAL_LABEL = "POTENTIAL EVENT · UNCONFIRMED";
/** Event data older than this is flagged "may be outdated" (a dead feed must not keep ticking silently). */
export const EVENT_DATA_STALE_SECONDS = 120;
/** Favored / pressured / non-confirming lists show at most this many (never forced). */
export const VIEW_LIST_LIMIT = 5;
/** Reaction windows shown after an event (§16). */
export const REACTION_HORIZONS = ["1m", "5m", "15m", "30m", "1h"] as const;

export type Tone = "positive" | "negative" | "neutral" | "info" | "outline";

const isObject = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);

/** Array or []. */
export function list<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value.filter((x) => x !== null && x !== undefined) as T[]) : [];
}

function isEvent(value: unknown): value is EventObject {
  return isObject(value) && typeof value.event_id === "string" && typeof value.name === "string";
}

/** Well-formed events only. */
export function events(value: unknown): EventObject[] {
  return list<unknown>(value).filter(isEvent);
}

/** The dashboard's event_risk block; null for older backends or a malformed value. */
export function eventRiskOf(dashboard: { event_risk?: unknown } | null | undefined): EventRisk | null {
  const block = dashboard?.event_risk;
  return isObject(block) ? (block as EventRisk) : null;
}

/** Longest backend failure reason shown in the quiet notice. */
const ERROR_TEXT_MAX = 200;

/**
 * Why the event-risk block is missing (§8: an event-risk failure leaves the
 * block null plus `event_risk_error`): "" when the backend flags a failure
 * without text, the trimmed reason when it sends one, null when nothing
 * failed (including older backends without the layer).
 */
export function eventRiskError(
  dashboard: { event_risk?: unknown; event_risk_error?: unknown } | null | undefined,
): string | null {
  if (!dashboard || isObject(dashboard.event_risk)) return null;
  const error = dashboard.event_risk_error;
  if (error === null || error === undefined || error === false) return null;
  const reason = typeof error === "string" ? error.trim() : "";
  return reason.length > ERROR_TEXT_MAX ? `${reason.slice(0, ERROR_TEXT_MAX - 1).trimEnd()}…` : reason;
}

/** The stored interpretation; null when absent or malformed. */
export function interpretationOf(
  dashboard: { interpretation?: unknown } | null | undefined,
): Interpretation | null {
  const value = dashboard?.interpretation;
  return isObject(value) && typeof value.source === "string" ? (value as Interpretation) : null;
}

// --- levels --------------------------------------------------------------------------------

/** Backend relevance bands: CRITICAL ≥ 90, VERY HIGH ≥ 75, HIGH ≥ 60, MODERATE ≥ 40, else LOW. */
export function levelFromScore(score: unknown): RelevanceLevel | null {
  if (!isNum(score)) return null;
  if (score >= 90) return "CRITICAL";
  if (score >= 75) return "VERY HIGH";
  if (score >= 60) return "HIGH";
  if (score >= 40) return "MODERATE";
  return "LOW";
}

/** "VERY_HIGH" → "VERY HIGH"; the score's band when the level is missing. */
export function levelOf(level: unknown, score?: unknown): string | null {
  if (typeof level === "string" && level.trim()) return level.trim().toUpperCase().replaceAll("_", " ");
  return levelFromScore(score);
}

/** CRITICAL red, VERY HIGH / HIGH amber, MODERATE blue, anything else quiet. */
export function levelTone(level: unknown): Tone {
  const l = levelOf(level);
  if (l === "CRITICAL") return "negative";
  if (l === "VERY HIGH" || l === "HIGH") return "neutral";
  if (l === "MODERATE") return "info";
  return "outline";
}

/** Exposure level wording: "very_high" → "very high". */
export function exposureWord(level: unknown): string | null {
  return typeof level === "string" && level ? level.replaceAll("_", " ").toLowerCase() : null;
}

// --- impact, exposure, expectations --------------------------------------------------------

/** Impact out of 10: the backend's impact_10, the mode's for the same event, else relevance / 10. */
export function impact10(event: EventObject | null | undefined, mode?: EventRiskMode | null): number | null {
  if (!event) return null;
  if (isNum(event.impact_10)) return event.impact_10;
  if (mode && mode.event_id === event.event_id && isNum(mode.impact_10)) return mode.impact_10;
  return isNum(event.relevance_score) ? event.relevance_score / 10 : null;
}

/** "10/10", "9.4/10"; null without a value. */
export function impactText(value: number | null): string | null {
  if (!isNum(value)) return null;
  const n = Math.max(0, Math.min(10, value));
  return `${n.toLocaleString("en-US", { maximumFractionDigits: 1 })}/10`;
}

/**
 * The calendar row's own impact (0-10) carried on the event as `impact`: the
 * number the calendar and "Next important events" show for the same release.
 * Null when the backend sends none (unscheduled news, older backends).
 */
export function rowImpact(event: unknown): number | null {
  const value = isObject(event) ? event.impact : null;
  return isNum(value) ? value : null;
}

export type ImpactView = {
  /** "9/10"; null when neither the row impact nor a relevance-based value exists. */
  text: string | null;
  /** True when `text` is relevance / 10 (impact_10) because the event has no calendar impact. */
  relevanceBased: boolean;
};

/**
 * IMPACT on the dashboard: the calendar row's impact, so one release shows
 * one number everywhere. Only without it the backend's impact_10 (relevance /
 * 10, also the mode's for the same event) is used, flagged as relevance-based.
 * `mode` alone (an event the block does not list) contributes its own
 * `impact` or `impact_10` the same way.
 */
export function impactView(event: EventObject | null | undefined, mode?: EventRiskMode | null): ImpactView {
  const own = rowImpact(event) ?? (mode && (!event || mode.event_id === event.event_id) ? rowImpact(mode) : null);
  if (own !== null) return { text: impactText(own), relevanceBased: false };
  const derived = event ? impact10(event, mode) : isNum(mode?.impact_10) ? mode.impact_10 : null;
  const text = impactText(derived);
  return { text, relevanceBased: text !== null };
}

/** "70/100" from the event's relevance score (0-100), shown apart from its impact; null without one. */
export function relevanceText(event: Pick<EventObject, "relevance_score"> | null | undefined): string | null {
  const score = event?.relevance_score;
  return isNum(score) ? `${Math.round(Math.max(0, Math.min(100, score)))}/100` : null;
}

export type RelevanceView = { text: string | null; level: string | null };

/**
 * Relevance of the EVENT RISK MODE event, "96/100" and "CRITICAL": the
 * mode's own relevance_score / relevance_level (C10), else the listed event's,
 * so the fact stays when the block does not list the mode's event.
 */
export function modeRelevance(mode: EventRiskMode | null | undefined, event: EventObject | null | undefined): RelevanceView {
  const own: Record<string, unknown> = isObject(mode) ? mode : {};
  const score = isNum(own.relevance_score) ? own.relevance_score : event?.relevance_score;
  const level =
    typeof own.relevance_level === "string" && own.relevance_level.trim() ? own.relevance_level : event?.relevance_level;
  return { text: relevanceText({ relevance_score: score }), level: levelOf(level, score) };
}

/**
 * The pre-event adjusted conviction, "42/100": |decision score| (−100..100)
 * scaled by the pre-event penalty. A conviction on the score scale, never a
 * probability, so never shown with "%". Null without a value.
 */
export function convictionText(value: unknown): string | null {
  return isNum(value) ? `${Math.round(Math.max(0, Math.min(100, value)))}/100` : null;
}

/** Most exposed assets first (weight, then level order), at most `limit`. */
export function exposureList(event: EventObject | null | undefined, limit = 8): EventExposure[] {
  const rank: Record<string, number> = { very_high: 4, high: 3, medium: 2, low: 1 };
  return list<unknown>(event?.affected_assets)
    .map((a): EventExposure | null =>
      typeof a === "string" ? { symbol: a } : isObject(a) && typeof a.symbol === "string" ? (a as EventExposure) : null,
    )
    .filter((a): a is EventExposure => !!a)
    .sort(
      (a, b) =>
        (isNum(b.weight) ? b.weight : -1) - (isNum(a.weight) ? a.weight : -1) ||
        (rank[String(b.level ?? "").toLowerCase()] ?? 0) - (rank[String(a.level ?? "").toLowerCase()] ?? 0),
    )
    .slice(0, limit);
}

export type ExpectationLine = { label: string; value: string };
export type Expectation = {
  missing: boolean;
  /** The missing literal, or backend text (a summary or the provider's note). */
  text: string | null;
  lines: ExpectationLine[];
  /** What the numbers are: "Consensus", "Finnhub estimate" (never relabelled). */
  label: string | null;
  /** Where they come from: "Source: finnhub". */
  caption: string | null;
};

/** Keys that are never an expected value: `previous` is last period's actual, not an expectation (§8). */
const NOT_VALUES = new Set([
  "label",
  "source",
  "attribution",
  "basis",
  "unit",
  "previous",
  "note",
  "summary",
  "value",
  "values",
  "name",
]);
const SOURCE_KEYS = ["source", "attribution", "basis"] as const;

const capitalize = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

function keyLabel(key: string): string {
  return capitalize(key.replaceAll("_", " ").trim()) || key;
}

const text = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null);

/** "3.1%", "+25 bp", "1.42 USD": the unit stays attached to its value. */
function withUnit(body: string, unit: string | null): string {
  if (!unit) return body;
  return unit === "%" ? `${body}%` : `${body} ${unit}`;
}

function valueText(key: string, value: unknown, unitValue?: unknown): string | null {
  if (value === null || value === undefined) return null;
  const unit = text(unitValue);
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null;
    // Probabilities without a unit arrive as 0..1 fractions; shown as percent, nothing else changes.
    if (!unit && /prob/i.test(key) && value >= 0 && value <= 1)
      return `${(value * 100).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
    const body =
      Math.abs(value) >= 1e6
        ? value.toLocaleString("en-US", { notation: "compact", maximumFractionDigits: 2 })
        : value.toLocaleString("en-US", { maximumFractionDigits: 4 });
    return withUnit(body, unit);
  }
  if (typeof value === "string") return value.trim() ? withUnit(value.trim(), unit) : null;
  if (isObject(value)) return valueText(key, value.value, value.unit);
  return null;
}

/**
 * Consensus or market pricing as display lines. The §8 shape is
 * {label, values: [{name, value, unit}], source}; older flat dicts are read
 * the same way. `label` ("Consensus", "Finnhub estimate") is kept as sent,
 * units stay with their values, and `previous` is never an expectation. A
 * missing value, an empty dict or the backend literal gives `missing` with
 * that literal ("NO CONSENSUS", "UNKNOWN") as text.
 */
export function expectation(value: unknown, missingLiteral: string): Expectation {
  const none: Expectation = { missing: true, text: missingLiteral, lines: [], label: null, caption: null };
  if (typeof value === "string") {
    const v = value.trim();
    return !v || v.toUpperCase() === missingLiteral
      ? none
      : { missing: false, text: v, lines: [], label: null, caption: null };
  }
  if (!isObject(value)) return none;
  const rawLabel = text(value.label);
  const label = rawLabel ? capitalize(rawLabel) : null;
  const source = SOURCE_KEYS.map((k) => text(value[k])).find((s) => s) ?? null;
  const lines: ExpectationLine[] = [];
  if (Array.isArray(value.values)) {
    for (const item of value.values) {
      if (!isObject(item)) continue;
      const name = text(item.name) ?? "Value";
      const shown = valueText(name, item.value, item.unit);
      if (shown !== null) lines.push({ label: name, value: shown });
    }
  } else {
    // Older flat shapes: {value, unit, previous, label, source} or {eps, revenue, note, label, source}.
    const headline = valueText("value", value.value, value.unit);
    if (headline !== null) lines.push({ label: text(value.name) ?? "Headline", value: headline });
    for (const [key, raw] of Object.entries(value)) {
      if (NOT_VALUES.has(key)) continue;
      const shown = valueText(key, raw);
      if (shown !== null) lines.push({ label: keyLabel(key), value: shown });
    }
  }
  const notes = [text(value.summary), text(value.note)].filter((n): n is string => !!n);
  if (!lines.length && !notes.length) return none;
  return {
    missing: false,
    text: notes.length ? notes.join(" ") : null,
    lines,
    label,
    caption: source ? `Source: ${source}` : null,
  };
}

/** The event's own data-quality reasons (shown quietly unless the backend flags the event). */
export function incompleteReasons(event: EventObject | null | undefined): string[] {
  return list<unknown>(event?.data_incomplete).map((r) => String(r).replaceAll("_", " "));
}

/**
 * Whether an event is flagged EVENT DATA INCOMPLETE. The flag is the
 * backend's: an entry for the event in the block's `data_incomplete`, which
 * the backend fills only at its configured levels (CRITICAL / VERY HIGH).
 * The event's own reasons are returned either way, to be shown quietly when
 * it is not flagged.
 */
export function incompleteFor(
  block: EventRisk | null | undefined,
  event: EventObject | null | undefined,
): { flagged: boolean; reasons: string[] } {
  if (!event) return { flagged: false, reasons: [] };
  const reasons = incompleteReasons(event);
  let flagged = false;
  for (const entry of list<unknown>(block?.data_incomplete)) {
    if (entry === event.event_id) flagged = true;
    else if (isObject(entry) && entry.event_id === event.event_id) {
      flagged = true;
      for (const r of list<unknown>(entry.reasons)) reasons.push(String(r).replaceAll("_", " "));
    }
  }
  return { flagged, reasons: [...new Set(reasons)] };
}

// --- event lookup --------------------------------------------------------------------------

/** Every event the block carries, de-duplicated by id. */
export function allEvents(block: EventRisk | null | undefined): EventObject[] {
  if (!block) return [];
  const seen = new Map<string, EventObject>();
  for (const e of [
    block.next_event,
    block.dominant_event,
    ...events(block.events),
    ...events(block.active_events),
  ])
    if (isEvent(e) && !seen.has(e.event_id)) seen.set(e.event_id, e);
  return [...seen.values()];
}

export function findEvent(block: EventRisk | null | undefined, id: unknown): EventObject | null {
  if (typeof id !== "string" || !id) return null;
  return allEvents(block).find((e) => e.event_id === id || e.row_id === id) ?? null;
}

/**
 * The dominant event when it is not the next event (e.g. a CRITICAL FOMC
 * tonight behind a HIGH release this afternoon), so it can be surfaced at the
 * top; null when there is none or it is the next event itself.
 */
export function dominantBeyondNext(block: EventRisk | null | undefined): EventObject | null {
  const dominant = block?.dominant_event;
  if (!isEvent(dominant)) return null;
  const next = block?.next_event;
  return isEvent(next) && next.event_id === dominant.event_id ? null : dominant;
}

/**
 * Target time of the risk-mode event: the mode's time_utc (§8), else the
 * event's, else generated_at + minutes_until (all backend values).
 */
export function modeTarget(block: EventRisk | null | undefined): number | null {
  const mode = block?.mode;
  if (!mode?.active) return null;
  const event = findEvent(block, mode.event_id);
  if (event?.time_known === false || isAnytime(event)) return null;
  const own = parseTime(mode.time_utc);
  if (own !== null) return own;
  const t = exactTime(event);
  if (t !== null) return t;
  const base = parseTime(block?.generated_at);
  return base !== null && isNum(mode.minutes_until) ? base + mode.minutes_until * 60_000 : null;
}

/** True when the event data is older than `seconds` (a stalled feed). */
export function eventDataStale(
  block: EventRisk | null | undefined,
  now: number,
  seconds = EVENT_DATA_STALE_SECONDS,
): boolean {
  const t = parseTime(block?.generated_at);
  return t !== null && now - t > seconds * 1000;
}

// --- per-asset risk ------------------------------------------------------------------------

/** Score of an event-risk value (a number or {score}). */
export function riskScore(value: EventRiskValue | undefined): number | null {
  if (isNum(value)) return value;
  return isObject(value) && isNum(value.score) ? value.score : null;
}

export type RiskBadge = { score: number | null; level: string | null; event: EventObject | null };

/**
 * An asset's event risk: the item's own value, else the block's per-asset
 * entry, with the event that drives it.
 */
export function riskBadge(
  block: EventRisk | null | undefined,
  symbol: string,
  value?: EventRiskValue,
  eventId?: string | null,
): RiskBadge | null {
  const own = isObject(value) ? (value as AssetEventRisk) : null;
  const perAsset = isObject(block?.asset_event_risk) ? block.asset_event_risk[symbol] : undefined;
  const score = riskScore(value) ?? riskScore(perAsset ?? null);
  const level = levelOf(own?.level ?? (riskScore(value) === null ? perAsset?.level : null), score);
  const event = findEvent(block, eventId ?? own?.event_id ?? perAsset?.event_id);
  if (score === null && !level) return null;
  return { score, level, event };
}

/** Short event name for badges: "FOMC rate decision" stays; only the first 28 chars. */
export function shortName(event: EventObject | null | undefined): string | null {
  if (!event?.name) return null;
  const name = event.name.trim();
  return name.length > 28 ? `${name.slice(0, 27).trimEnd()}…` : name;
}

// --- market view ---------------------------------------------------------------------------

/** Items with a symbol, at most `limit`; an empty list stays empty (never forced). */
export function viewItems<T extends { symbol?: unknown }>(value: unknown, limit = VIEW_LIST_LIMIT): T[] {
  return list<T>(value)
    .filter((x) => isObject(x) && typeof x.symbol === "string" && !!x.symbol)
    .slice(0, limit);
}

/** Text of a "what can break this view" item. */
export function breakText(item: BreakItem | null | undefined): string | null {
  if (typeof item === "string") return item.trim() || null;
  return isObject(item) && typeof item.text === "string" && item.text.trim() ? item.text.trim() : null;
}

const sameText = (a: string, b: string) =>
  a.trim().replace(/\s+/g, " ").toLowerCase() === b.trim().replace(/\s+/g, " ").toLowerCase();

/**
 * Key risks that do not repeat an item of "What can break this view" (the
 * deterministic fallback builds its key risks from that list, which the
 * dashboard already shows right above).
 */
export function distinctRisks(risks: unknown, breaks: BreakItem[] | null | undefined): string[] {
  const shown = list<BreakItem>(breaks)
    .map(breakText)
    .filter((t): t is string => !!t);
  const seen: string[] = [];
  for (const r of list<unknown>(risks)) {
    if (typeof r !== "string" || !r.trim()) continue;
    const risk = r.trim();
    if (shown.some((b) => sameText(b, risk)) || seen.some((s) => sameText(s, risk))) continue;
    seen.push(risk);
  }
  return seen;
}

/** A scenario's condition, unless it only repeats the scenario's label ("Below consensus" twice). */
export function scenarioCondition(scenario: Pick<EventScenario, "label" | "condition"> | null | undefined): string | null {
  const condition = typeof scenario?.condition === "string" ? scenario.condition.trim() : "";
  if (!condition) return null;
  const label = typeof scenario?.label === "string" ? scenario.label : "";
  return label && sameText(label, condition) ? null : condition;
}

/**
 * Exposed assets a scenario leaves without a direction because their causal
 * paths compete (the scenario's `unresolved` list): unique symbols, never one
 * that also carries an expected direction. Empty on a backend without the list.
 */
export function unresolvedSymbols(scenario: Pick<EventScenario, "expected" | "unresolved"> | null | undefined): string[] {
  const expected = isObject(scenario?.expected) ? scenario.expected : {};
  const out: string[] = [];
  for (const symbol of list<unknown>(scenario?.unresolved)) {
    if (typeof symbol !== "string" || !symbol.trim() || out.includes(symbol.trim())) continue;
    const sign = direction(expected[symbol.trim()])?.sign;
    if (sign === 1 || sign === -1) continue;
    out.push(symbol.trim());
  }
  return out;
}

/** AI comment for a symbol (text only). */
export function commentFor(value: unknown, symbol: string): string | null {
  const hit = list<{ symbol?: unknown; comment?: unknown }>(value).find(
    (c) => isObject(c) && c.symbol === symbol,
  );
  return typeof hit?.comment === "string" && hit.comment.trim() ? hit.comment.trim() : null;
}

const MARKET_NAMES: [keyof MarketsOpen, string][] = [
  ["us_equities", "US equities"],
  ["us_rates", "US rates"],
  ["fx", "FX"],
  ["commodities", "Commodities"],
];

/** Traditional markets reported closed (crypto is not listed here). */
export function closedMarkets(open: MarketsOpen | null | undefined): string[] {
  if (!isObject(open)) return [];
  return MARKET_NAMES.filter(([key]) => open[key] === false).map(([, name]) => name);
}

/**
 * The counterpart of closedMarkets: crypto first, then traditional markets
 * explicitly reported open. A missing session state stays unknown.
 */
export function openMarkets(open: MarketsOpen | null | undefined): string[] {
  if (!isObject(open)) return [];
  return [
    ...(open.crypto === true ? ["Crypto"] : []),
    ...MARKET_NAMES.filter(([key]) => open[key] === true).map(([, name]) => name),
  ];
}

/** Open sessions only; their state does not establish fresh observations. */
export function liveMarketsNote(open: MarketsOpen | null | undefined): string | null {
  const names = openMarkets(open);
  if (!names.length) return null;
  const joined = names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
  return `${joined} ${names.length === 1 ? "market is" : "markets are"} open.`;
}

// --- interpretation ------------------------------------------------------------------------

export type SourceLabel = { label: string; deterministic: boolean };

/** "AI · <model>" or the deterministic label. */
export function interpretationSource(value: Interpretation | null | undefined): SourceLabel {
  if (value?.source === "ai")
    return { label: value.model ? `AI · ${value.model}` : "AI", deterministic: false };
  return { label: value?.label?.trim() || DETERMINISTIC_LABEL, deterministic: true };
}

/** Prefix of a per-asset interpretation comment: "AI note" only when a model wrote it. */
export function commentLabel(value: Interpretation | null | undefined): string {
  return value?.source === "ai" ? "AI note" : "Deterministic note";
}

/**
 * The age-based STALE badge: honor the backend flag and expire a cached fresh
 * response against its supplied deadline. Without that deadline, the browser
 * adds no threshold. Inputs are not compared; text states the limit when sent,
 * else the measured age.
 */
export function staleText(value: Interpretation | null | undefined, now: number): string | null {
  if (!value) return null;
  const limit = isNum(value.stale_after_seconds) && value.stale_after_seconds >= 0 ? value.stale_after_seconds * 1000 : null;
  const generated = parseTime(value.generated_at);
  const age = generated !== null ? now - generated : isNum(value.age_seconds) ? value.age_seconds * 1000 : null;
  if (!value.stale && !(limit !== null && generated !== null && now - generated > limit)) return null;
  const ms = limit ?? age;
  if (ms === null || ms < 60_000) return "STALE";
  return `STALE · older than ${Math.floor(ms / 60_000)} min`;
}

/** "HAWKISH_SURPRISE" → "HAWKISH SURPRISE" with its tone; null when absent or not applicable. */
export function surpriseView(value: unknown): { text: string; tone: Tone } | null {
  if (typeof value !== "string" || !value || value === "NOT_APPLICABLE") return null;
  const text = value.replaceAll("_", " ");
  const tone: Tone =
    value === "HAWKISH_SURPRISE" ? "negative" : value === "DOVISH_SURPRISE" ? "positive" : "outline";
  return { text, tone };
}

const DIRECTIONAL = new Set(["HAWKISH_SURPRISE", "DOVISH_SURPRISE"]);

/**
 * The surprise badge for an event: the backend's validated, evidence-backed
 * classification wins; a hawkish/dovish call from the AI text that the backend
 * did not validate is shown as "not validated", never as a finding.
 */
export function eventSurpriseView(
  event: { surprise?: EventSurprise | null } | null | undefined,
  aiAssessment: unknown,
): { text: string; tone: Tone } | null {
  const backend = event?.surprise;
  if (backend?.validated === true && typeof backend.assessment === "string") return surpriseView(backend.assessment);
  if (typeof aiAssessment === "string" && DIRECTIONAL.has(aiAssessment))
    return { text: `${aiAssessment.replaceAll("_", " ")} · not validated`, tone: "outline" };
  return surpriseView(aiAssessment === "NEUTRAL_PRICED" || aiAssessment === "UNDETERMINED" ? aiAssessment : null);
}

export type ScenarioSet = {
  event: EventObject;
  explanation: EventExplanation | null;
  scenarios: EventScenario[];
};

/**
 * Per-event scenarios: deterministic templates from the event plus the AI's
 * explanation for the same event id. Dominant event first, then the next
 * event, then the rest (scheduled, then active unscheduled events) with
 * either. AI explanations for unknown ids are dropped; unverified (potential)
 * events are never explained.
 */
export function scenarioSets(
  block: EventRisk | null | undefined,
  interpretation: Interpretation | null | undefined,
): ScenarioSet[] {
  const explanations = list<EventExplanation>(interpretation?.output?.next_events).filter(
    (x) => isObject(x) && typeof x.event_id === "string",
  );
  const ordered = [
    block?.dominant_event,
    block?.next_event,
    ...events(block?.events),
    ...events(block?.active_events),
  ].filter(isEvent);
  const seen = new Set<string>();
  const out: ScenarioSet[] = [];
  for (const event of ordered) {
    if (seen.has(event.event_id)) continue;
    seen.add(event.event_id);
    const explanation = explanations.find((x) => x.event_id === event.event_id) ?? null;
    const scenarios = list<EventScenario>(event.scenarios).filter(isObject);
    if (explanation || scenarios.length) out.push({ event, explanation, scenarios });
  }
  return out;
}

// --- directions and reactions --------------------------------------------------------------

export type Direction = { arrow: string; word: string; sign: number };

/** +1 / -1 / 0 or "up" / "down" / "flat"; null when unknown. */
export function direction(value: unknown): Direction | null {
  const sign = isNum(value)
    ? Math.sign(value)
    : typeof value === "string"
      ? /^(up|higher|\+)/i.test(value)
        ? 1
        : /^(down|lower|-)/i.test(value)
          ? -1
          : /^(flat|neutral|0)/i.test(value)
            ? 0
            : NaN
      : NaN;
  if (!Number.isFinite(sign)) return null;
  return sign > 0
    ? { arrow: "↑", word: "up", sign: 1 }
    : sign < 0
      ? { arrow: "↓", word: "down", sign: -1 }
      : { arrow: "→", word: "flat", sign: 0 };
}

/** Post-event entries whose phase is RELEASED, IN_PROGRESS or POST_EVENT. */
export function livePostEvents(block: EventRisk | null | undefined): PostEvent[] {
  return list<PostEvent>(block?.post_event).filter(
    (p) => isObject(p) && typeof p.event_id === "string" && LIVE_PHASES.has(String(p.phase ?? "").toUpperCase()),
  );
}

export type ReactionCell = { text: string; sign: number | null };

/** "bp" | "pct" | null from a unit word ("%" counts as pct). */
function reactionUnit(value: unknown): "bp" | "pct" | null {
  const u = typeof value === "string" ? value.trim().toLowerCase() : "";
  return u === "bp" ? "bp" : u === "pct" || u === "%" ? "pct" : null;
}

/**
 * "+0.70%", "+11 bp"; "Pending" when not measured yet. The unit is the cell's
 * own ({change, unit, status}, §8), else `unit` (the entry's `units` map for
 * bare numbers). Yields arrive in bp and are never shown as percent; a number
 * whose unit is not reported gets no unit rather than a guessed one.
 */
export function reactionCell(value: ReactionValue | undefined, unit?: string | null): ReactionCell {
  let amount: number | null = null;
  let resolved = reactionUnit(unit);
  if (isNum(value)) amount = value;
  else if (isObject(value)) {
    resolved = reactionUnit(value.unit) ?? resolved;
    if (isNum(value.change)) amount = value.change;
    else if (isNum(value.change_pct)) {
      amount = value.change_pct;
      resolved = "pct";
    } else if (typeof value.status === "string" && value.status)
      return { text: value.status.replaceAll("_", " "), sign: null };
  }
  if (amount === null) return { text: "Pending", sign: null };
  const bp = resolved === "bp";
  const body = Math.abs(amount).toLocaleString("en-US", {
    minimumFractionDigits: bp ? 0 : 2,
    maximumFractionDigits: bp ? 1 : 2,
  });
  const sign = amount > 0 ? "+" : amount < 0 ? "-" : "";
  const suffix = bp ? " bp" : resolved === "pct" ? "%" : "";
  return { text: `${sign}${body}${suffix}`, sign: Math.sign(amount) };
}

/** The unit of a post-event entry's symbol from its `units` map (bare-number cells). */
export function reactionUnitFor(entry: PostEvent | null | undefined, symbol: string): string | null {
  const units = entry?.units;
  return isObject(units) && typeof units[symbol] === "string" ? units[symbol] : null;
}
