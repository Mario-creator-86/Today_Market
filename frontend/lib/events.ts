import type {
  CausalPath,
  MarketEvent,
  Reaction,
  ReactionBasket,
  RecordData,
} from "./api";
import { localTime, safeUrl } from "./api";
import { bucharestDay } from "./event-time";
import { humanize, isNum, labelCase, plainText } from "./format";

/*
 * Event titles for ranked topic lists, plus the pure display rules of the
 * event pages (/events, /events/[id], /sources). Pure, with relative imports
 * only, so vitest can collect it.
 */

/** Short words that are names rather than acronyms. */
const WORD_CASE: Record<string, string> = {
  FED: "Fed",
  OIL: "Oil",
  GAS: "Gas",
};

/** An upper-case slug in words: "IRAN_HORMUZ" -> "Iran / Hormuz" (topics), "OFFICIAL_RELEASE" -> "Official Release". */
function slugWords(slug: string, joiner: string): string {
  return slug
    .split("_")
    .filter(Boolean)
    .map(
      (w) =>
        WORD_CASE[w] ?? (w.length <= 3 ? w : w[0] + w.slice(1).toLowerCase()),
    )
    .join(joiner);
}

/** Title for a topic slug: "IRAN_HORMUZ" -> "Iran / Hormuz". */
export function topicLabel(event: MarketEvent): string {
  const slug = event.topic || event.event_type;
  if (!slug) return "Unclassified";
  return slugWords(slug, event.topic ? " / " : " ");
}

/**
 * The event's headline (its verified fact) as plain text: headlines stored
 * from feeds may still carry markup or character references ("S&amp;P").
 */
export function eventHeadline(event: Pick<MarketEvent, "summary_fact">): string {
  return plainText(event.summary_fact);
}

/** First sentence of the verified fact (as plain text). */
export function oneSentence(textValue: string): string {
  const trimmed = plainText(textValue);
  const match = trimmed.match(/^.+?[.!?](?=\s|$)/);
  return match ? match[0] : trimmed;
}

/** At most `max` characters, cut at a word boundary with an ellipsis. */
export function clampText(value: string, max = 140): string {
  const text = value.replace(/\s+/g, " ").trim();
  if (text.length <= max) return text;
  const cut = text.slice(0, max - 1);
  const space = cut.lastIndexOf(" ");
  return `${(space > max * 0.6 ? cut.slice(0, space) : cut).replace(/[\s,;:.-]+$/, "")}…`;
}

export type EventTitle = {
  /** What the list shows as the item title. */
  text: string;
  /** Full text for a tooltip when `text` was clamped. */
  full: string;
  /** True when the title is the event's own headline rather than a topic. */
  headline: boolean;
};

/**
 * Title of an event in a topic list. A topic set by a causal rule is shown as
 * the topic ("Iran / Hormuz"); a topic guessed from entities, or none at all,
 * is not a reliable name, so the event's headline (its verified fact) is shown
 * instead, clamped. Without a headline the topic or event type remains.
 */
export function eventTitle(event: MarketEvent, max = 140): EventTitle {
  const full = eventHeadline(event);
  if ((event.topic_basis === "rule" && event.topic) || !full) {
    const text = topicLabel(event);
    return { text, full: text, headline: false };
  }
  return { text: clampText(full, max), full, headline: true };
}

/** A topic badge is a curated name only when a causal rule set it; entity-joined topics are heuristics. */
export function ruleTopic(event: MarketEvent): string | null {
  return event.topic_basis === "rule" && event.topic ? topicLabel(event) : null;
}

/* --- Event lists (/events, /sources) --- */

/** Events requested per list load: the API maximum, so search and filters see every stored event. */
export const EVENTS_LIMIT = 500;

function time(value: unknown): number | null {
  if (typeof value !== "string" || !value) return null;
  const t = Date.parse(value);
  return Number.isFinite(t) ? t : null;
}

/**
 * When an event was published, for display: "Fri 2 Oct 18:55" (Bucharest),
 * from the publication time, else the first sighting. A publication stamp
 * that is a date without a clock time (`event_time_known: false`, or the
 * event layer's `published_date_only: true`) is shown as
 * that date alone ("Fri 2 Oct"), never with an invented clock time.
 */
export function publishedText(
  event: Pick<MarketEvent, "event_time" | "first_seen_utc" | "event_time_known" | "published_date_only">,
): string {
  const published = time(event.event_time);
  const dateOnly = event.event_time_known === false || event.published_date_only === true;
  if (published !== null && dateOnly) return bucharestDay(published);
  return localTime(event.event_time || event.first_seen_utc);
}

/** Newest publication first (first sighting when the publication time is missing); stable for ties. */
export function sortByPublication<T extends Pick<MarketEvent, "event_time" | "first_seen_utc">>(
  events: T[],
): T[] {
  const key = (e: T) => time(e.event_time) ?? time(e.first_seen_utc) ?? -Infinity;
  return events
    .map((event, index) => ({ event, index, t: key(event) }))
    .sort((a, b) => b.t - a.t || a.index - b.index)
    .map((x) => x.event);
}

/** Source names at most shown on an event row; the rest read "+N". */
export const ROW_SOURCES = 3;

/** Source names from the event's `sources` list (strings or records with a name). */
export function listedSourceNames(event: Pick<MarketEvent, "sources">): string[] {
  const names = (event.sources ?? []).map((s) => {
    const name =
      typeof s === "string"
        ? s
        : s && typeof s === "object"
          ? ((s as RecordData).name ?? (s as RecordData).source)
          : null;
    return typeof name === "string" ? name.trim() : "";
  });
  return [...new Set(names.filter(Boolean))];
}

/** The sources an event row shows ("Fed, BLS, BEA +2"): the first few names and how many more there are. */
export function eventRowSources(event: Pick<MarketEvent, "sources">): { shown: string[]; more: number } {
  const names = listedSourceNames(event);
  return { shown: names.slice(0, ROW_SOURCES), more: Math.max(0, names.length - ROW_SOURCES) };
}

/** The symbols an event row shows: the event's assets, else the extractor's affected assets. */
export function eventRowAssets(event: Pick<MarketEvent, "assets" | "affected_assets">): string[] {
  return event.assets?.length ? event.assets : (event.affected_assets ?? []);
}

/**
 * The text an event row shows, for search: headline, status, symbols, the
 * shown source names and the publication time. Nothing the row hides (event
 * type, topic, ids, further sources) can match.
 */
export function eventSearchFields(event: MarketEvent): unknown[] {
  return [
    eventHeadline(event),
    event.verification_status ? humanize(event.verification_status) : null,
    eventRowAssets(event),
    eventRowSources(event).shown,
    publishedText(event),
  ];
}

/**
 * Honest list count: "308 events, showing 50", "12 of 308 events match,
 * showing 12". A load that hit the API limit says older events are not loaded.
 */
export function eventCountText({
  total,
  matching,
  shown,
  filtered,
  limit = EVENTS_LIMIT,
}: {
  total: number;
  matching: number;
  shown: number;
  filtered: boolean;
  limit?: number;
}): string {
  const noun = (n: number) => `${n} event${n === 1 ? "" : "s"}`;
  const head = filtered
    ? `${matching} of ${noun(total)} match${matching === 1 ? "es" : ""}`
    : noun(total);
  const tail = shown < matching ? `, showing ${shown}` : "";
  const capped = total >= limit ? ` (the newest ${limit} are loaded; older events are not)` : "";
  return `${head}${tail}${capped}`;
}

/* --- Fact confidence --- */

/**
 * One precision everywhere: one decimal, rounded down so 99.58 never reads as
 * a certain "100%". Missing values stay "Unavailable".
 */
export function formatFactConfidence(value: unknown): string {
  if (!isNum(value)) return "Unavailable";
  const floored = Math.floor(value * 10 + 1e-9) / 10;
  return `${floored.toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
}

/* --- Evidence --- */

/** Scheme-, "www."- and trailing-slash-insensitive URL key: the http and https copies of one document match. */
export function evidenceUrlKey(url: unknown): string | null {
  if (typeof url !== "string" || !url.trim()) return null;
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase().replace(/^www\./, "");
    const path = u.pathname.replace(/\/+$/, "");
    return `${host}${path}${u.search}`;
  } catch {
    return null;
  }
}

function contradicts(item: RecordData): boolean {
  const relation = String(item.relation ?? item.stance ?? "").toLowerCase();
  return relation === "contradicts" || relation === "contradicted";
}

export type EvidenceGroup = {
  /** The document shown: its latest version (by ingestion), the https copy when one exists. */
  item: RecordData;
  /** Every received copy of the document, the shown one included. */
  copies: RecordData[];
  /** 1-based reference number used by footnotes. */
  number: number;
};

/** A copy's headline for telling versions apart: plain text, case- and spacing-insensitive. */
function versionKey(item: RecordData): string {
  return plainText(item.title).toLowerCase();
}

/** The copy a group shows: the latest version received, preferring its https copy. */
function shownCopy(copies: RecordData[]): RecordData {
  const at = (c: RecordData) => time(c.ingested_at) ?? -Infinity;
  // Ties (no ingestion time) go to the later copy in API order, which lists copies as they arrived.
  const latest = copies.reduce((a, b) => (at(b) >= at(a) ? b : a));
  const version = versionKey(latest);
  return (
    copies.find((c) => versionKey(c) === version && String(c.url).startsWith("https:")) ?? latest
  );
}

/**
 * Evidence documents with every copy of one address (the same URL over http
 * and https, with or without "www.", or an updated version re-sent under the
 * same URL) collapsed into one numbered entry. Order follows the API;
 * supporting and contradicting copies never merge.
 */
export function groupEvidence(evidence: RecordData[] | null | undefined): EvidenceGroup[] {
  const groups: EvidenceGroup[] = [];
  const byKey = new Map<string, EvidenceGroup>();
  for (const item of evidence ?? []) {
    const url = evidenceUrlKey(item.url);
    const key = url ? `${contradicts(item) ? "against" : "for"}|${url}` : null;
    const found = key ? byKey.get(key) : undefined;
    if (found) {
      found.copies.push(item);
      found.item = shownCopy(found.copies);
      continue;
    }
    const group = { item, copies: [item], number: groups.length + 1 };
    groups.push(group);
    if (key) byKey.set(key, group);
  }
  return groups;
}

/**
 * Why one entry stands for several received copies, in words; null for a
 * single copy. An updated version (same address, another headline) is named
 * as such with the time it arrived; "http/https or www" is said only when the
 * addresses really differ.
 */
export function evidenceCopyNote(group: EvidenceGroup): string | null {
  const n = group.copies.length;
  if (n < 2) return null;
  const versions = new Set(group.copies.map(versionKey)).size;
  const addresses = new Set(group.copies.map((c) => String(c.url ?? "").trim())).size;
  if (versions > 1) {
    const when =
      time(group.item.ingested_at) === null ? "" : ` ${localTime(group.item.ingested_at)} (Bucharest)`;
    const head = versions === 2 ? `Updated version received${when}` : `${versions} versions received, the latest${when}`;
    const variants = addresses > 1 ? ", plus http/https or www copies of the address" : "";
    return `${head}${variants}; the latest is shown, counted once`;
  }
  return addresses > 1
    ? `${n} copies of this address received (http/https or www variants), counted once`
    : `${n} copies of this document received, counted once`;
}

/** Evidence id -> reference number (every copy maps to its group's number). */
export function evidenceNumbers(groups: EvidenceGroup[]): Map<string, number> {
  const numbers = new Map<string, number>();
  for (const g of groups)
    for (const copy of g.copies)
      if (typeof copy.id === "string" || typeof copy.id === "number")
        numbers.set(String(copy.id), g.number);
  return numbers;
}

export type EvidenceSummary = {
  /** Distinct documents (duplicate copies counted once). */
  documents: number;
  supporting: number;
  contradicting: number;
  /** Extra copies of documents already counted. */
  duplicates: number;
  /** Independence groups (the source's group, else its id or name). */
  independentGroups: number;
};

/** Counts over grouped evidence, shared by the event page and /sources so both state the same numbers. */
export function evidenceSummary(groups: EvidenceGroup[]): EvidenceSummary {
  const contradicting = groups.filter((g) => contradicts(g.item)).length;
  const key = (item: RecordData) =>
    [item.group, item.source_id, item.source].find(
      (v): v is string => typeof v === "string" && !!v.trim(),
    );
  return {
    documents: groups.length,
    supporting: groups.length - contradicting,
    contradicting,
    duplicates: groups.reduce((n, g) => n + g.copies.length - 1, 0),
    independentGroups: new Set(groups.map((g) => key(g.item)).filter(Boolean)).size,
  };
}

/**
 * Evidence published further from the event's publication than the
 * same-story window (the API's `same_story_window_hours`, config
 * dedupe.headline_time_window_hours): a recurring release (a monthly table, a
 * daily statement) merged into this event before that rule existed, not
 * another report of it. Null when the API sends no window or a time is
 * missing: nothing is guessed.
 */
export function otherPublication(
  item: RecordData,
  event: Pick<MarketEvent, "event_time"> & { same_story_window_hours?: number | null },
): { gapMs: number; before: boolean } | null {
  const window = event.same_story_window_hours;
  const published = time(item.published_at);
  const eventTime = time(event.event_time);
  if (!isNum(window) || window <= 0 || published === null || eventTime === null) return null;
  const gapMs = Math.abs(published - eventTime);
  return gapMs > window * 3_600_000 ? { gapMs, before: published < eventTime } : null;
}

/** "Published 11 days before this event: probably a separate release, not another report of it". */
export function otherPublicationText(gap: { gapMs: number; before: boolean }): string {
  return `Published ${lagText(gap.gapMs)} ${gap.before ? "before" : "after"} this event: probably a separate release, not another report of it`;
}

/** Upper-case slugs in source text ("SANCTIONS_RUSSIA"): internal topic codes, shown as topic names. */
const SLUG_TOKEN = /\b[A-Z]{2,}(?:_[A-Z]{2,})+\b/g;

/**
 * Evidence text (excerpt, title) as a reader sees it: feed markup and
 * character references removed, internal topic codes as topic names
 * ("Sanctions / Russia") and machine timestamps in Bucharest time.
 */
export function sourceText(value: unknown): string {
  return localizeTimes(plainText(value).replace(SLUG_TOKEN, (slug) => slugWords(slug, " / ")));
}

/* --- Source registry (/sources, Settings) --- */

/**
 * The source's own home: the X account for X sources, else the homepage the
 * registry lists (GET /api/sources `homepage`). Never built from the domain,
 * which is only the host suffix ingestion matches (some have no site at the
 * bare domain), and never the feed URL. Undefined when no home is known.
 */
export function sourceHome(s: {
  x_account?: string | null;
  homepage?: string | null;
}): { href: string; label: string } | undefined {
  const account = s.x_account?.replace(/^@/, "").trim();
  if (account) {
    const href = safeUrl(`https://x.com/${encodeURIComponent(account)}`);
    return href ? { href, label: `@${account} on X` } : undefined;
  }
  const href = typeof s.homepage === "string" ? safeUrl(s.homepage.trim()) : undefined;
  return href ? { href, label: new URL(href).hostname.replace(/^www\./, "") } : undefined;
}

/* --- Extractor and interpreter text --- */

export type TextPart =
  | { kind: "text"; text: string }
  /** A cited evidence document; `number` is null when the event does not list it. */
  | { kind: "evidence"; id: string; number: number | null };

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
/** "(evidence: <uuid>)", "(evidence <uuid>, <uuid>)", "[evidence ids: <uuid>]". */
const EVIDENCE_REF = new RegExp(
  `\\s*[(\\[]\\s*evidence(?:\\s+ids?)?\\s*[:#]?\\s*(${UUID}(?:\\s*(?:,|;|and)\\s*${UUID})*)\\s*[)\\]]`,
  "gi",
);
const BARE_UUID = new RegExp(UUID, "gi");
/** ISO date-times with an explicit zone ("Z", "+00:00", " UTC"); zone-less ones are left as written. */
const ISO_TIME =
  /\b(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(Z|[+-]\d{2}:?\d{2}|\s?UTC\b)/g;

/** Replace machine timestamps with Bucharest time: "2026-09-27T17:30:24+00:00" -> "Sun 27 Sep 20:30 (Bucharest)". */
export function localizeTimes(value: string): string {
  return value.replace(ISO_TIME, (match, date: string, clock: string, zone: string) => {
    const offset = /UTC|Z/i.test(zone)
      ? "Z"
      : zone.includes(":")
        ? zone
        : `${zone.slice(0, 3)}:${zone.slice(3)}`;
    const t = Date.parse(`${date}T${clock}${offset}`);
    return Number.isFinite(t) ? `${localTime(new Date(t).toISOString())} (Bucharest)` : match;
  });
}

/**
 * Extractor/interpreter text as display parts: ISO timestamps in Bucharest
 * time and evidence ids as numbered references to the evidence cards.
 */
export function annotate(value: string, numbers: Map<string, number>): TextPart[] {
  const parts: TextPart[] = [];
  const pushText = (chunk: string) => {
    if (!chunk) return;
    let last = 0;
    for (const m of chunk.matchAll(BARE_UUID)) {
      if (m.index > last) parts.push({ kind: "text", text: localizeTimes(chunk.slice(last, m.index)) });
      const id = m[0].toLowerCase();
      parts.push({ kind: "evidence", id, number: numbers.get(id) ?? numbers.get(m[0]) ?? null });
      last = m.index + m[0].length;
    }
    if (last < chunk.length) parts.push({ kind: "text", text: localizeTimes(chunk.slice(last)) });
  };
  let last = 0;
  for (const m of value.matchAll(EVIDENCE_REF)) {
    pushText(value.slice(last, m.index));
    for (const id of m[1].match(BARE_UUID) ?? []) {
      const key = id.toLowerCase();
      parts.push({ kind: "evidence", id: key, number: numbers.get(key) ?? numbers.get(id) ?? null });
    }
    last = m.index + m[0].length;
  }
  pushText(value.slice(last));
  return parts;
}

/* --- AI interpretation (specialist output: hypotheses, FX pairs, crypto assets or developments) --- */

export type InterpretationCard = {
  title: string;
  /** "positive" / "negative" / "neutral", or an FX bias such as "USD-positive". */
  direction: string | null;
  /** Ordered causal steps. */
  path: string[];
  facts: { label: string; value: string }[];
  lists: { label: string; items: string[] }[];
  confirmation: string | null;
  invalidation: string | null;
  evidenceIds: string[];
};

export type InterpretationModel = {
  /** Heading for the cards: "Hypotheses", "Currency pairs", "Assets", "Developments". */
  heading: string | null;
  cards: InterpretationCard[];
  uncertainties: string[];
  /** Top-level fields no rule above covers (scalars or nested data), shown as a plain list. */
  other: RecordData;
};

const CARD_GROUPS: [key: string, heading: string][] = [
  ["hypotheses", "Hypotheses"],
  ["pairs", "Currency pairs"],
  ["assets", "Assets"],
  ["developments", "Developments"],
];
const TITLE_KEYS = ["asset", "pair", "summary"];
const DIRECTION_KEYS = ["direction", "bias"];
const PATH_KEYS = ["causal_path", "pathway"];
const FACT_LABELS: Record<string, string> = {
  base_bank: "Base bank",
  counter_bank: "Counter bank",
  policy_comparison: "Policy comparison",
  yield_differential_view: "Yield differential",
  intervention_risk: "Intervention risk",
  positioning_note: "Positioning",
};
const LIST_LABELS: Record<string, string> = {
  drivers: "Drivers",
  affected_assets: "Affected assets",
};
/** Fields that are internal flags, not text for a reader. */
const SKIPPED = new Set(["market_relevant"]);

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function strs(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string" && !!v.trim()).map((v) => v.trim())
    : [];
}

function card(raw: RecordData, index: number): InterpretationCard {
  const used = new Set([...TITLE_KEYS, ...DIRECTION_KEYS, ...PATH_KEYS, "confirmation", "invalidation", "evidence_ids"]);
  const facts: InterpretationCard["facts"] = [];
  const lists: InterpretationCard["lists"] = [];
  for (const [key, value] of Object.entries(raw)) {
    if (used.has(key) || SKIPPED.has(key)) continue;
    if (Array.isArray(value)) {
      const items = strs(value);
      if (items.length) lists.push({ label: LIST_LABELS[key] ?? labelCase(key), items });
    } else if (str(value) || isNum(value)) {
      facts.push({ label: FACT_LABELS[key] ?? labelCase(key), value: String(value).trim() });
    }
  }
  return {
    title: TITLE_KEYS.map((k) => str(raw[k])).find(Boolean) ?? `Item ${index + 1}`,
    direction: DIRECTION_KEYS.map((k) => str(raw[k])).find(Boolean) ?? null,
    path: PATH_KEYS.map((k) => strs(raw[k])).find((p) => p.length) ?? [],
    facts,
    lists,
    confirmation: str(raw.confirmation),
    invalidation: str(raw.invalidation),
    evidenceIds: strs(raw.evidence_ids),
  };
}

/** Structured view of a stored interpretation; null when there is none. Never renders raw JSON. */
export function interpretationModel(value: unknown): InterpretationModel | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as RecordData;
  const group = CARD_GROUPS.find(([key]) => Array.isArray(record[key]));
  const cards = group
    ? (record[group[0]] as unknown[])
        .filter((item): item is RecordData => !!item && typeof item === "object" && !Array.isArray(item))
        .map(card)
    : [];
  const other: RecordData = {};
  for (const [key, v] of Object.entries(record))
    if (key !== group?.[0] && key !== "uncertainties" && v !== null && v !== undefined) other[key] = v;
  return { heading: group?.[1] ?? null, cards, uncertainties: strs(record.uncertainties), other };
}

/** Direction tokens with a fixed reading (FX specialist bias, backend schemas.FXPairView). */
const DIRECTION_LABELS: Record<string, string> = {
  "neutral-wait": "Neutral (wait)",
};

/**
 * "Positive" / "Negative" / "Neutral"; FX biases in words ("USD-positive" ->
 * "USD positive", "neutral-wait" -> "Neutral (wait)"). Free text stays as
 * written; a machine token never reaches the page.
 */
export function directionLabel(value: string): string {
  const trimmed = value.trim();
  const known = DIRECTION_LABELS[trimmed.toLowerCase()];
  if (known) return known;
  return /^[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*$/.test(trimmed) ? labelCase(trimmed) : trimmed;
}

/* --- Reactions --- */

/**
 * Fallback for an API that does not send `max_publication_lag_seconds` with
 * the event: the default of config/rules.d/market.json
 * reaction.max_publication_lag_seconds. The backend never measures an event
 * first seen later than this after publication; the API value always wins.
 */
export const DEFAULT_MAX_PUBLICATION_LAG_SECONDS = 900;

/** "under 1 min", "25 min", "4 h 10 min", "62 h", "12 days" for a lag in milliseconds. */
export function lagText(ms: number): string {
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return "under 1 min";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 10) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  if (hours < 72) return `${hours} h`;
  return `${Math.floor(hours / 24)} days`;
}

/** Event fields the reaction timing reads; the two lag fields come from the event detail API when it sends them. */
export type ReactionTimingInput = Pick<MarketEvent, "event_time" | "first_seen_utc"> & {
  reactions?: Reaction[];
  /** Seconds from publication to first sighting, as the backend computed it. */
  publication_lag_seconds?: number | null;
  /** The configured limit above which the backend does not measure reactions. */
  max_publication_lag_seconds?: number | null;
};

export type ReactionTiming = {
  /** Time of the baseline prices (the latest baseline tick), ISO; null without measured reactions. */
  measuredFrom: string | null;
  /** How long after publication the baseline prices were taken; null when either time is missing. */
  baselineLagMs: number | null;
  /** How long after publication the event was first seen; null without a publication time. */
  publicationLagMs: number | null;
  /** The configured publication lag limit (the API value, else the documented default). */
  maxLagMs: number;
  /** First seen after the limit: measured moves are not the release reaction. */
  late: boolean;
};

/**
 * When the reaction baseline was taken relative to publication, and whether
 * the event was first seen too late for its moves to be the release reaction.
 * Same rule as the backend (analytics._eligible): publication lag (first seen
 * minus publication time) above `max_publication_lag_seconds`.
 */
export function reactionTiming(event: ReactionTimingInput): ReactionTiming {
  const baselines = (event.reactions ?? [])
    .map((r) => time(r.baseline_timestamp))
    .filter((t): t is number => t !== null);
  const published = time(event.event_time);
  const firstSeen = time(event.first_seen_utc);
  const publicationLagMs = isNum(event.publication_lag_seconds)
    ? event.publication_lag_seconds * 1000
    : published !== null && firstSeen !== null
      ? firstSeen - published
      : null;
  const limit =
    isNum(event.max_publication_lag_seconds) && event.max_publication_lag_seconds >= 0
      ? event.max_publication_lag_seconds
      : DEFAULT_MAX_PUBLICATION_LAG_SECONDS;
  const maxLagMs = limit * 1000;
  const from = baselines.length ? Math.max(...baselines) : null;
  return {
    measuredFrom: from === null ? null : new Date(from).toISOString(),
    baselineLagMs: from === null || published === null ? null : from - published,
    publicationLagMs,
    maxLagMs,
    late: publicationLagMs !== null && publicationLagMs > maxLagMs,
  };
}

const REACTION_LABELS: Record<string, string> = {
  NOT_EXPECTED: "NO EXPECTATION",
  NO_REACTION: "NO REACTION",
};

/** Badge text for a reaction status; NOT_EXPECTED reads "no expectation", never a verdict. */
export function reactionStatusLabel(status: unknown): string {
  const value = typeof status === "string" ? status : "";
  return REACTION_LABELS[value] ?? humanize(value || "Unavailable");
}

export type ConfirmationState = "measured" | "nothing-expected" | "no-data";

/**
 * Whether a basket percentage is meaningful: with no asset expected to move
 * (no causal rule applies) confirmation is not applicable, which is different
 * from expected assets that were not measured.
 */
export function confirmationState(basket: ReactionBasket): ConfirmationState {
  if (isNum(basket.confirmation_pct)) return "measured";
  const expected = isNum(basket.expected)
    ? basket.expected
    : [basket.confirming, basket.partial, basket.diverging, basket.missing, basket.no_reaction].reduce(
        (n, list) => n + (list?.length ?? 0),
        0,
      );
  return expected === 0 ? "nothing-expected" : "no-data";
}

/* --- Causal paths --- */

export type PathDetail = CausalPath & {
  /** The rule's configured name, e.g. "Tighter crypto regulation". */
  label?: string | null;
  half_life_hours?: number | null;
  /** The rule's configured strength; `strength` is it after regime and exception adjustment. */
  strength_prior?: number | null;
  regime_multiplier?: number | null;
  exceptions_fired?: string[];
};

/** Rule or exception slug as a name: "crypto-regulation" -> "Crypto regulation". */
export function ruleLabel(slug: string | null | undefined, fallback = "Causal rule"): string {
  return slug && slug.trim() ? labelCase(slug) : fallback;
}

/** A path's name: the rule's configured label, else its id in words, else "Path n". */
export function pathName(path: PathDetail, index: number): string {
  const label = typeof path.label === "string" ? path.label.trim() : "";
  return label || ruleLabel(path.rule_id, `Path ${index + 1}`);
}

/** Why the strength differs from the rule's: the regime multiplier, fired exceptions, or both. */
function adjustment(path: PathDetail): string {
  const regime = isNum(path.regime_multiplier) && Math.abs(path.regime_multiplier - 1) > 1e-9;
  const exceptions = !!path.exceptions_fired?.length;
  return regime && exceptions
    ? "adjusted for the current regime and exceptions"
    : exceptions
      ? "adjusted by exceptions"
      : "adjusted for the current regime";
}

/**
 * "Strength 78 (rule 60, adjusted for the current regime) · 8% of the initial
 * effect remains (half-life 24 h)". `strength` is the adjusted value; the
 * rule's configured strength is named when it differs.
 */
export function pathStrengthText(path: PathDetail): string | null {
  if (!isNum(path.strength)) return null;
  const strength = Math.round(path.strength);
  const prior = isNum(path.strength_prior) ? Math.round(path.strength_prior) : null;
  const parts = [
    prior !== null && prior !== strength
      ? `Strength ${strength} (rule ${prior}, ${adjustment(path)})`
      : `Strength ${strength}`,
  ];
  if (isNum(path.decay)) {
    const pct = path.decay * 100;
    const remaining =
      pct >= 99.95
        ? "full effect"
        : pct < 0.1
          ? "under 0.1% of the initial effect remains"
          : `${pct.toLocaleString("en-US", { maximumFractionDigits: pct < 10 ? 1 : 0 })}% of the initial effect remains`;
    const half = isNum(path.half_life_hours)
      ? ` (half-life ${path.half_life_hours.toLocaleString("en-US", { maximumFractionDigits: 1 })} h)`
      : "";
    parts.push(`${remaining}${half}`);
  }
  return parts.join(" · ");
}

/** Why a path does not count: suppressed by a named exception, or faded with time. Null for an active path. */
export function inactiveReason(path: PathDetail): string | null {
  if (path.active !== false) return null;
  if (path.suppressed_by) return `Suppressed by the exception "${ruleLabel(path.suppressed_by)}".`;
  if (isNum(path.decay) && path.decay < 1)
    return "Inactive: the effect has faded with time since publication.";
  return "Inactive: below the causal graph's activity threshold.";
}

/**
 * Qualifier for the fact panel's affected assets when the event has causal
 * paths but none of them counts; null when a path is active or there are none.
 */
export function inactivePathsNote(paths: PathDetail[] | null | undefined): string | null {
  const list = paths ?? [];
  if (!list.length || list.some((p) => p.active !== false)) return null;
  if (list.every((p) => p.suppressed_by)) return "no active causal path: suppressed by an exception";
  if (list.every((p) => !p.suppressed_by && isNum(p.decay) && p.decay < 1))
    return "no active causal path: the effect has faded";
  return "no active causal path";
}

/* --- Fact panel captions --- */

/** Plain-English caption of what the importance number is derived from. */
export function importanceBasisText(basis: string | null | undefined): string | null {
  const value = (basis ?? "").trim();
  if (!value) return null;
  const lower = value.toLowerCase();
  if (lower === "source tier") return "source trust tier only; not assessed by the extractor";
  if (lower.includes("extractor") && lower.includes("tier"))
    return "source trust tier × importance assessed by the AI extractor";
  return value.replaceAll("_", " ");
}

const TRIGGER_BASIS: Record<string, string> = {
  rule: "matched a trigger rule",
  macro_surprise: "macro surprise",
  market_move: "market move",
};

/** Caption for how the trigger was set; null when none was set. */
export function triggerBasisText(basis: string | null | undefined): string | null {
  if (!basis || basis === "none") return null;
  return TRIGGER_BASIS[basis] ?? labelCase(basis).toLowerCase();
}

/**
 * A reaction stored before its event was classified (event.classified_on_read): it carries no expected direction
 * (NOT_EXPECTED), although a causal rule now expects the asset to move, so it was never graded against it.
 */
export function ungradedReaction(
  event: object,
  status: string | null | undefined,
  expectedSign: number | null | undefined,
): boolean {
  const legacy = (event as { classified_on_read?: unknown }).classified_on_read === true;
  return legacy && status === "NOT_EXPECTED" && !!expectedSign;
}
