import { localDay, localHourMinute, safeUrl, type Asset, type Quote } from "../../lib/api";
import { expectation, levelOf, NO_CONSENSUS } from "../../lib/event-risk";
import { humanize, isNum, labelCase } from "../../lib/format";
import { APP_ZONE, asOfLine, calendarDay, shortDate, sourceName, zonedTime } from "../../lib/market-data";
import type {
  AlertItem,
  CalendarItem,
  FxRow,
  MetricSummary,
  PerformanceReport,
} from "./types";

/*
 * View logic of the section pages (/fx, /calendar, /performance, /alerts).
 * Every value comes from the API; missing inputs stay missing and are
 * labelled. Pure, with relative imports only, so vitest can collect it.
 */

// --- numbers and words ---------------------------------------------------------------------------

/**
 * Format a measured number. Missing values render `missing` (default
 * "Unavailable") and are never shown as a measured zero.
 */
export function fmt(
  value: unknown,
  {
    digits = 2,
    suffix = "",
    signed = false,
    missing = "Unavailable",
  }: { digits?: number; suffix?: string; signed?: boolean; missing?: string } = {},
): string {
  if (!isNum(value)) {
    if (typeof value === "string" && value.trim() !== "") return value;
    return missing;
  }
  const body = value.toLocaleString("en-US", {
    maximumFractionDigits: digits,
    minimumFractionDigits: 0,
  });
  return `${signed && value > 0 ? "+" : ""}${body}${suffix}`;
}

/** A 0..1 ratio as a percentage. */
export function fmtRatio(value: unknown, missing = "Not enough data"): string {
  return isNum(value) ? `${(value * 100).toFixed(1)}%` : missing;
}

/** Yes/No for booleans, "Unavailable" otherwise. */
export function yesNo(value: unknown): string {
  return value === true ? "Yes" : value === false ? "No" : "Unavailable";
}

/**
 * "LEVEL_BREAK" -> "Level break", "USD_STRENGTH" -> "USD strength": enum ids as
 * plain words, with known acronyms (USD, US, ECB, CPI, ...) and "Fed" kept.
 */
export function sentence(value: string): string {
  return labelCase(value);
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/**
 * Case-insensitive search over the text a row shows. Never the whole record:
 * ids, keys and hidden fields must not match.
 */
export function matchesText(values: unknown[], query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return values.some(
    (v) => (typeof v === "string" || isNum(v)) && String(v).toLowerCase().includes(q),
  );
}

// --- foreign exchange (/fx) ----------------------------------------------------------------------

/** Spec order (§30): majors first, DXY last; unknown pairs sit just before DXY. */
const FX_ORDER = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "DXY"];

export function fxKey(row: FxRow): string {
  return String(row.symbol ?? row.pair ?? "").replace("/", "").toUpperCase();
}

/** "EURUSD" -> "EUR/USD"; DXY and anything else stays as-is. */
export function pairLabel(symbol: string): string {
  return /^[A-Z]{6}$/.test(symbol) ? `${symbol.slice(0, 3)}/${symbol.slice(3)}` : symbol;
}

export function sortFx(rows: FxRow[]): FxRow[] {
  const rank = (r: FxRow) => {
    const i = FX_ORDER.indexOf(fxKey(r));
    return i === -1 ? FX_ORDER.length - 1.5 : i;
  };
  return [...rows].sort((a, b) => rank(a) - rank(b) || fxKey(a).localeCompare(fxKey(b)));
}

/**
 * A basket such as DXY: any symbol that is not a six-letter currency pair (the rule /api/fx applies). It has no single
 * counterpart central bank, so the counterpart rate and both differentials do not apply to it.
 */
export function isBasket(row: FxRow): boolean {
  const key = fxKey(row);
  return key !== "" && !/^[A-Z]{6}$/.test(key);
}

/** Shown where a value does not apply to the row (never "Unavailable", which reads as missing data). */
export const NOT_APPLICABLE = "Not applicable";

/** A base-minus-quote differential in percentage points; "Not applicable" for a basket. */
export function fxDifferentialText(row: FxRow, value: unknown): string {
  return isBasket(row) ? NOT_APPLICABLE : fmt(value, { suffix: " pp", signed: true, digits: 3 });
}

/** Current key first, legacy/alternate key second. */
export function yieldDiff(row: FxRow): number | null | undefined {
  return row.yield_2y_differential ?? row.yield_differential;
}

/**
 * Relative policy cannot be compared for the pair: a differential is missing, or
 * the backend sent repricing_differential empty (a rate expectation is missing),
 * which is what blocks an FX decision (decision.fx_policy_block).
 */
export function policyIncomplete(row: FxRow): boolean {
  if (!isNum(row.policy_differential) || !isNum(yieldDiff(row))) return true;
  return "repricing_differential" in row && !isNum(row.repricing_differential);
}

/** Display names of the central-bank ids of config decision.fx_banks (the API sends the id). */
const BANK_NAMES: Record<string, string> = {
  fed: "Fed",
  ecb: "ECB",
  boe: "BoE",
  boj: "BoJ",
  rba: "RBA",
  boc: "BoC",
  snb: "SNB",
  rbnz: "RBNZ",
};

/** The backend's display label, else the name of a known bank id, else the id in capitals. */
export function bankName(id: unknown, label?: unknown): string | null {
  const given = text(label);
  if (given) return given;
  const key = text(id);
  if (!key) return null;
  return BANK_NAMES[key.toLowerCase()] ?? key.toUpperCase();
}

/**
 * "3.75–4.00% (midpoint 3.875%)" with the target range, else the rate itself
 * ("3.875%"), else "rate unavailable" (shown after "Fed", like "BoE rate unavailable").
 */
export function fedRateText(row: FxRow): string {
  const mid = isNum(row.fed_rate) ? fmt(row.fed_rate, { digits: 3, suffix: "%" }) : null;
  const [lower, upper] = Array.isArray(row.fed_rate_range) ? row.fed_rate_range : [];
  if (isNum(lower) && isNum(upper))
    return `${lower.toFixed(2)}–${upper.toFixed(2)}%${mid ? ` (midpoint ${mid})` : ""}`;
  return mid ?? "rate unavailable";
}

/** The pair's next rate decisions: every bank of the pair when the API lists them, else the soonest one. */
export function fxMeetings(row: FxRow): unknown {
  return Array.isArray(row.next_meetings) && row.next_meetings.length
    ? row.next_meetings
    : (row.next_meeting_expectation ?? row.next_meeting);
}

/**
 * "ECB 2%", "BoE rate unavailable", "Basket, no single counterpart bank" for DXY, or "Unavailable" when the
 * bank of a pair is unknown too.
 */
export function otherRateText(row: FxRow): string {
  const bank = bankName(row.other_bank, row.other_bank_name);
  if (isNum(row.other_rate)) return `${bank ? `${bank} ` : ""}${fmt(row.other_rate, { digits: 3, suffix: "%" })}`;
  if (bank) return `${bank} rate unavailable`;
  return isBasket(row) ? "Basket, no single counterpart bank" : "Unavailable";
}

/** Stored as a date (00:00 UTC or a bare YYYY-MM-DD): the time part is not an observation time. */
export function isDateOnly(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}(T00:00(:00(\.0+)?)?(Z|[+-]00:?00)?)?$/.test(value.trim());
}

/** "27 Sep" for a date-only value, "Sun 27 Sep 14:15 Bucharest" otherwise; null when missing. */
export function asOfText(value: unknown): string | null {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) return null;
  return isDateOnly(value) ? shortDate(value) : zonedTime(value);
}

export type MeetingView = {
  /** One line per scheduled rate decision, soonest first. */
  lines: string[];
  /** True when at least one line carries market pricing. */
  priced: boolean;
  /** Backend notes, e.g. why a bank has no calendar source. */
  notes: string[];
};

function meetingTime(v: Record<string, unknown>): { at: number; text: string | null } {
  const when = text(v.scheduled_at) ?? text(v.date);
  const at = Date.parse(when ?? "");
  if (!when || !Number.isFinite(at)) return { at: Infinity, text: when };
  if (v.time_known === false || isDateOnly(when)) return { at, text: `${shortDate(when)} (time TBA)` };
  return { at, text: zonedTime(when) };
}

/**
 * Next rate decision(s) of a pair: text, {bank, name, scheduled_at, expectation,
 * expected_change_bp} or a list of those (one per central bank).
 */
export function meetingView(value: unknown): MeetingView {
  const view: MeetingView = { lines: [], priced: false, notes: [] };
  const given = text(value);
  if (given) return { ...view, lines: [given], priced: true };
  const items = (Array.isArray(value) ? value : [value]).filter(
    (v): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v),
  );
  const timed = items.map((v) => ({ v, time: meetingTime(v) })).sort((a, b) => a.time.at - b.time.at);
  for (const { v, time } of timed) {
    const bank = bankName(v.bank, v.bank_label ?? v.bank_name);
    const name = text(v.name);
    const pricing =
      text(v.expectation) ??
      (isNum(v.expected_change_bp) ? `${fmt(v.expected_change_bp, { signed: true, digits: 0 })} bp priced` : null);
    // A bank without a scheduled decision only carries its reason: never a line that reads like a date.
    if (name || time.text || pricing) {
      const title =
        bank && name && !name.toLowerCase().includes(bank.toLowerCase())
          ? `${bank}: ${name}`
          : (name ?? (bank ? `${bank} rate decision` : null));
      if (pricing) view.priced = true;
      view.lines.push([title, time.text ?? "time unavailable", pricing].filter(Boolean).join(" · "));
    }
    for (const key of ["note", "reason"] as const) {
      const note = text(v[key]);
      if (note && !view.notes.includes(note)) view.notes.push(note);
    }
  }
  return view;
}

/**
 * The quote's as-of line with the source named once, by its display name:
 * "as of ECB 14:15 CET fix, Fri 25 Sep (daily, not a tradable quote) · ECB",
 * never the provider id beside the attribution ("· ecb · Source: ECB").
 */
export function quoteAsOfLine(q: Quote): string {
  return asOfLine({ ...q, provider: sourceName(q), feed: null });
}

/** The dashboard asset shape for the provenance helpers (dataChip, asOfLine). */
export function fxAsset(row: FxRow): Asset {
  const symbol = fxKey(row);
  return { symbol, name: pairLabel(symbol), asset_class: "fx", quote: row.quote ?? null, decision: null };
}

/** The text a pair row shows (search never sees ids or hidden quote fields). */
export function fxSearchText(row: FxRow): unknown[] {
  const symbol = fxKey(row);
  const meeting = meetingView(fxMeetings(row));
  return [
    pairLabel(symbol),
    symbol,
    row.bias,
    bankName(row.other_bank, row.other_bank_name),
    ...meeting.lines,
    ...meeting.notes,
    row.confirmation,
    row.invalidation,
    row.relative_basis,
  ];
}

// --- market calendar (/calendar) -----------------------------------------------------------------

export type ImpactFilter = "HIGH" | "ALL";

export function scheduledOf(item: CalendarItem): string | undefined {
  return item.scheduled_utc ?? item.scheduled_at;
}

export function present(value: unknown): boolean {
  return value !== null && value !== undefined && value !== "";
}

/** Numeric impact is n/10 (HIGH >= 7, MEDIUM >= 4); string impact is a label. */
export function passesImpact(item: { impact?: unknown }, filter: ImpactFilter): boolean {
  const impact = item.impact;
  if (filter === "HIGH")
    return typeof impact === "number" ? impact >= 7 : String(impact).toUpperCase() === "HIGH";
  return typeof impact === "number"
    ? impact >= 4
    : ["HIGH", "MEDIUM"].includes(String(impact).toUpperCase());
}

export function isReleased(item: CalendarItem): boolean {
  return present(item.actual);
}

/** Value with the release unit ("2.8%", "250 K"); missing stays explicit. */
export function withUnit(value: unknown, unit: unknown, missing = "Unavailable"): string {
  if (!present(value)) return missing;
  const u = typeof unit === "string" ? unit.trim() : "";
  const body = isNum(value) ? fmt(value, { digits: 3 }) : String(value);
  if (!u || !isNum(value)) return body;
  return u === "%" ? `${body}%` : `${body} ${u}`;
}

/**
 * Fallback only, for rows sent without date_window: a date-only row is due for
 * the 24 h after its anchor (config ui.calendar_date_only_hours, event_risk.timeline).
 * The backend's date_window always wins, so the configured value governs once it is sent.
 */
const DATE_ONLY_FALLBACK_MS = 24 * 60 * 60 * 1000;

/** End of a date-only row's release window: the backend's date_window, else 24 h after its anchor. */
export function releaseWindowEnd(item: CalendarItem): number | null {
  const end = Date.parse(item.date_window?.end_utc ?? "");
  if (Number.isFinite(end)) return end;
  const at = Date.parse(scheduledOf(item) ?? "");
  return Number.isFinite(at) ? at + DATE_ONLY_FALLBACK_MS : null;
}

/**
 * RELEASED (an actual exists), UPCOMING (before its time or window), DUE (a
 * date-only row inside its release window: the exact time is not published) or
 * AWAITING (the time has passed and no actual has arrived yet).
 */
export type ReleaseTiming = "RELEASED" | "UPCOMING" | "DUE" | "AWAITING";

export function releaseTiming(item: CalendarItem, now: number): ReleaseTiming {
  if (isReleased(item)) return "RELEASED";
  const at = Date.parse(scheduledOf(item) ?? "");
  if (item.time_known === false) {
    const end = releaseWindowEnd(item);
    if (end === null || now >= end) return "AWAITING";
    const start = Date.parse(item.date_window?.start_utc ?? "");
    const begins = Number.isFinite(start) ? start : at;
    return Number.isFinite(begins) && now < begins ? "UPCOMING" : "DUE";
  }
  return Number.isFinite(at) && at > now ? "UPCOMING" : "AWAITING";
}

export const TIMING_LABEL: Record<ReleaseTiming, string> = {
  RELEASED: "Released",
  UPCOMING: "Upcoming",
  DUE: "Due today",
  AWAITING: "Awaiting actual",
};

export type ConsensusView = {
  /** "Consensus", or the vendor's own label ("Finnhub estimate"), never relabelled. */
  label: string;
  value: string;
  /** The vendor's note, e.g. "not Street consensus". */
  note: string | null;
  missing: boolean;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

/**
 * Consensus of a calendar row (docs/EVENT_AWARENESS.md §8): a scalar consensus
 * with its unit, the §8 shape, else the row's vendor `estimates` under their own
 * label, else "NO CONSENSUS". `previous` is never an expectation.
 */
export function consensusView(item: CalendarItem): ConsensusView {
  const missing: ConsensusView = { label: "Consensus", value: NO_CONSENSUS, note: null, missing: true };
  const c = item.consensus;
  if (present(c) && !isRecord(c)) {
    if (typeof c === "string" && c.trim().toUpperCase() === NO_CONSENSUS) return missing;
    return { label: "Consensus", value: withUnit(c, item.unit), note: null, missing: false };
  }
  const source = isRecord(c) && Object.keys(c).length ? c : isRecord(item.estimates) ? item.estimates : null;
  const view = expectation(source, NO_CONSENSUS);
  if (view.missing) return missing;
  const value = view.lines.length
    ? view.lines.map((l) => `${l.label} ${l.value}`).join(" · ")
    : (view.text ?? NO_CONSENSUS);
  return {
    label: view.label ?? "Consensus",
    value,
    note: view.lines.length ? view.text : null,
    missing: false,
  };
}

/** "Relevance 70/100 · HIGH" (the event layer's score), "Relevance HIGH" without a score; null without either. */
export function relevanceText(item: CalendarItem): string | null {
  const level = item.relevance_level || isNum(item.relevance_score) ? levelOf(item.relevance_level, item.relevance_score) : null;
  if (isNum(item.relevance_score))
    return `Relevance ${Math.round(item.relevance_score)}/100${level ? ` · ${level}` : ""}`;
  return level ? `Relevance ${level}` : null;
}

/** Source credit of a row and its link (http/https only). */
export function sourceView(item: CalendarItem): { text: string; href: string | undefined } | null {
  const href = safeUrl(item.source_url);
  const credit = text(item.attribution);
  if (credit) return { text: credit, href };
  return href ? { text: `Source: ${new URL(href).hostname}`, href } : null;
}

/** "America/New_York" -> "New York": the city an IANA zone is named after. */
function zoneCity(zone: string): string {
  return (zone.split("/").pop() ?? zone).replaceAll("_", " ");
}

/** Calendar date (YYYY-MM-DD) and wall clock of an instant in an IANA zone; null for a zone the browser does not know. */
function zonedParts(t: number, zone: string): { day: string; clock: string } | null {
  try {
    const p = Object.fromEntries(
      new Intl.DateTimeFormat("en-GB", {
        timeZone: zone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      })
        .formatToParts(new Date(t))
        .map((part) => [part.type, part.value]),
    );
    return { day: `${p.year}-${p.month}-${p.day}`, clock: `${p.hour}:${p.minute}` };
  } catch {
    return null;
  }
}

const HOUR_MS = 60 * 60 * 1000;

export type ReleaseWindowView = {
  /** "23:00–03:00 next day Bucharest (after the US close)" or "all day Tue 6 Oct, New York time". */
  text: string;
  /** The backend's window label when the text shows it (a session such as "after the US close"), else null. */
  label: string | null;
};

/**
 * Release window of a date-only row, from the backend's date_window (event_risk.date_window). A session window is a
 * time range in Bucharest with its label; a whole-day window (the publisher's local day, BoJ's statement day) is that
 * day in its own zone, without the backend's internal label. Null for timed rows and rows without a window.
 */
export function releaseWindow(item: CalendarItem): ReleaseWindowView | null {
  const w = item.time_known === false ? item.date_window : null;
  if (!w) return null;
  const start = Date.parse(w.start_utc ?? "");
  const end = Date.parse(w.end_utc ?? "");
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  const zone = text(w.zone);
  const opens = zone ? zonedParts(start, zone) : null;
  const span = end - start;
  // 23-25 h from midnight: one whole day of that zone (a DST change makes it 23 or 25 h).
  if (zone && opens && opens.clock === "00:00" && span >= 23 * HOUR_MS && span <= 25 * HOUR_MS) {
    const day = calendarDay(opens.day);
    if (day) return { text: `all day ${day}, ${zoneCity(zone)} time`, label: null };
  }
  const label = text(w.label);
  const from = new Date(start).toISOString();
  // The window's last instant decides the day: a window ending at midnight ends that day, not the next.
  const nextDay = localDay(from) !== localDay(new Date(end - 1).toISOString());
  const range = `${localHourMinute(from)}–${localHourMinute(new Date(end).toISOString())}${nextDay ? " next day" : ""}`;
  return { text: `${range} ${APP_ZONE}${label ? ` (${label})` : ""}`, label };
}

/**
 * The row's time basis without what the release window already says: "after the US close (exact time not
 * published)" beside the window "(after the US close)" reads "exact time not published"; null when nothing is left.
 */
export function timeBasisText(item: CalendarItem): string | null {
  const basis = text(item.time_basis);
  if (!basis) return null;
  const label = releaseWindow(item)?.label;
  if (!label || !basis.toLowerCase().startsWith(label.toLowerCase())) return basis;
  const rest = basis
    .slice(label.length)
    .replace(/^[\s,;:–-]+/, "")
    .replace(/^\((.*)\)$/, "$1")
    .trim();
  return rest || null;
}

/** The text a release card shows (search never sees ids or hidden fields). */
export function calendarSearchText(item: CalendarItem): unknown[] {
  return [
    item.name,
    item.country,
    item.category ? sentence(item.category) : null,
    item.kind ? sentence(item.kind) : null,
    releaseWindow(item)?.text,
    timeBasisText(item),
    item.attribution,
    consensusView(item).value,
  ];
}

// --- historical performance (/performance) -------------------------------------------------------

const HORIZON_WORDS: Record<string, string> = {
  close: "Session close",
  next_session: "Next session close",
};

/** "4h" stays; session horizons read as words ("next_session" -> "Next session close"). */
export function horizonLabel(horizon: string): string {
  return HORIZON_WORDS[horizon] ?? (/^\d+[smhd]$/.test(horizon) ? horizon : sentence(horizon));
}

const DIMENSION_WORDS: Record<string, string> = {
  asset: "asset",
  event_type: "event type",
  regime: "regime",
  score_bucket: "score band",
  confidence_bucket: "evidence confidence",
  source_combination: "source combination",
  setup: "technical setup",
};

export function dimensionLabel(dimension: string): string {
  return DIMENSION_WORDS[dimension] ?? humanize(dimension).toLowerCase();
}

/** A breakdown group key (backend evaluation.group_keys) in plain words. */
export function groupLabel(dimension: string, key: string): string {
  if (dimension === "asset") return key;
  if (key === "NONE")
    return dimension === "event_type" ? "No event" : dimension === "source_combination" ? "No sources" : "None";
  if (key === "UNRECORDED") return "Not recorded";
  if (dimension === "source_combination") return key.split("+").join(" + ");
  if (dimension === "confidence_bucket" && /^\d+-\d+$/.test(key)) return `${key.replace("-", "–")}%`;
  const band = /^(STRONG|SETUP|LEAN)([+-])$/.exec(key);
  if (dimension === "score_bucket" && band) return `${sentence(band[1])} ${band[2] === "+" ? "long" : "short"}`;
  return sentence(key);
}

/** Rows with measured directional outcomes, and the names of those without. */
export function splitMeasured<T extends { summary: MetricSummary }>(
  rows: T[],
  name: (row: T) => string,
): { measured: T[]; unmeasured: string[] } {
  const has = (r: T) => (r.summary.observations ?? 0) > 0;
  return { measured: rows.filter(has), unmeasured: rows.filter((r) => !has(r)).map(name) };
}

/** "a, b, c and 4 more". */
export function shortList(names: string[], limit = 8): string {
  if (names.length <= limit) return names.join(", ");
  return `${names.slice(0, limit).join(", ")} and ${names.length - limit} more`;
}

/**
 * What a row of outcome metrics was measured on (docs/AUTOLEARN.md): published
 * (actionable) signals, counterfactual outcomes (WAIT and lean decisions
 * measured on the sign of their score: what the lean would have returned, not
 * a signal), both, or unknown when the service sent no counts.
 */
export type OutcomeBasis = "published" | "counterfactual" | "mixed" | "unknown";

export type OutcomeMix = {
  /** Measured outcomes of published signals; null when the service sent no count. */
  published: number | null;
  /** Measured counterfactual outcomes; null when the service sent no count. */
  counterfactual: number | null;
  basis: OutcomeBasis;
};

const outcomeCount = (value: unknown): number | null => (isNum(value) && value >= 0 ? value : null);

/**
 * The basis of one metrics row, from its `actionable` / `counterfactual`
 * counts (or an explicit `counterfactual_only` flag). Without counts the
 * requested population decides ("actionable" / "counterfactual"); "all" stays
 * unknown rather than being presented as signals.
 */
export function outcomeMix(summary: MetricSummary | null | undefined, population?: string | null): OutcomeMix {
  const published = outcomeCount(summary?.actionable);
  const counterfactual = outcomeCount(summary?.counterfactual);
  const requested: OutcomeBasis =
    population === "actionable" ? "published" : population === "counterfactual" ? "counterfactual" : "unknown";
  if (summary?.counterfactual_only === true) return { published: published ?? 0, counterfactual, basis: "counterfactual" };
  if (published === null && counterfactual === null) return { published, counterfactual, basis: requested };
  const p = published ?? 0;
  const c = counterfactual ?? 0;
  const basis: OutcomeBasis = p > 0 && c > 0 ? "mixed" : p > 0 ? "published" : c > 0 ? "counterfactual" : requested;
  return { published, counterfactual, basis };
}

/** "0 / 148" (published / counterfactual outcomes of a row); null when the service sent no counts. */
export function outcomeMixText(mix: OutcomeMix): string | null {
  if (mix.published === null && mix.counterfactual === null) return null;
  const n = (v: number | null) => (v ?? 0).toLocaleString("en-US");
  return `${n(mix.published)} / ${n(mix.counterfactual)}`;
}

export type HitRates = {
  /** Hit rate of published signals; null without one. */
  signal: number | null;
  /** What following the score sign of WAIT and lean decisions would have returned; null without such an outcome. */
  counterfactual: number | null;
  /** True when `signal` also counts counterfactual outcomes (an older service's single rate over a mixed row). */
  signalIncludesCounterfactual: boolean;
};

/**
 * The two hit rates of a metrics row, never mixed. The service reports them
 * apart (`hit_rate` over published signals only, `counterfactual_hit_rate`).
 * An older service sent one rate over every measured outcome: it is the
 * counterfactual rate of a counterfactual row, the signal rate of a published
 * one, and on a mixed or unknown row it is shown as the signal rate with the
 * flag set, so the page can say it includes counterfactual outcomes.
 */
export function hitRates(summary: MetricSummary | null | undefined, population?: string | null): HitRates {
  const rate = (v: unknown) => (isNum(v) ? v : null);
  const split =
    !!summary && (Object.hasOwn(summary, "counterfactual_hit_rate") || Object.hasOwn(summary, "hit_rate_basis"));
  if (split)
    return { signal: rate(summary.hit_rate), counterfactual: rate(summary.counterfactual_hit_rate), signalIncludesCounterfactual: false };
  const single = rate(summary?.hit_rate);
  const basis = outcomeMix(summary, population).basis;
  if (basis === "counterfactual") return { signal: null, counterfactual: single, signalIncludesCounterfactual: false };
  return { signal: single, counterfactual: null, signalIncludesCounterfactual: basis === "mixed" && single !== null };
}

/**
 * The one line that says what the performance figures are when they are not
 * (only) published signals; null when every measured outcome is a published
 * signal, when nothing is measured, or when the service sent no counts.
 */
export function counterfactualNotice(mix: OutcomeMix, population?: string | null): string | null {
  const c = mix.counterfactual ?? 0;
  if (mix.basis === "counterfactual" && (c > 0 || mix.counterfactual === null))
    return population === "counterfactual"
      ? "Counterfactual outcomes: what following the score sign of WAIT and lean decisions would have returned. Not a signal hit rate."
      : "No published signal has a measured outcome yet. Every figure below is counterfactual: what following the score sign of WAIT and lean decisions would have returned. It is not a signal hit rate.";
  if (mix.basis === "mixed")
    return `Returns, MFE, MAE and drawdown below cover ${(mix.published ?? 0).toLocaleString("en-US")} published signal outcomes and ${c.toLocaleString("en-US")} counterfactual ones (WAIT and lean decisions measured on their score sign). Choose "Actionable only" for published signals alone.`;
  return null;
}

export type PerformanceSummary = {
  /** Decisions with any recorded outcome row. */
  decisions: number | null;
  /** Decisions with a measured directional outcome. */
  directional: number | null;
  /** True when `directional` is the largest count at any one horizon (an older payload without the backend's count). */
  directionalAtBestHorizon: boolean;
  status: string;
  /**
   * Published vs counterfactual: the service's counts of decisions measured as
   * each (`published_signals`, `counterfactual_decisions`); without them, the
   * outcome counts at the horizon with the most measured outcomes.
   */
  mix: OutcomeMix;
  /** That horizon ("5m") when `mix` is a horizon's count; null for the service's decision counts or nothing measured. */
  mixHorizon: string | null;
};

/**
 * Overview counts. A decision can have outcome rows without any directional
 * outcome (a neutral decision), so "recorded" and "measured" are shown apart
 * and the status never claims calibration work while nothing is measured.
 * Current payload: observations = decisions with a directional outcome, beside
 * decisions_recorded. An older payload (no decisions_recorded) sent the recorded
 * count as observations; its directional count is then the most at any one horizon.
 * `mix` says how many of the measured outcomes are published signals and how
 * many are counterfactual.
 */
export function performanceSummary(data: PerformanceReport): PerformanceSummary {
  const metrics = data.metrics ?? {};
  const horizons = Array.isArray(data.horizons) ? data.horizons : Object.keys(metrics).filter((k) => k !== "paper");
  const counts = horizons.map((h) => (metrics[h] as MetricSummary | undefined)?.observations).filter(isNum);
  const recorded = isNum(data.decisions_recorded) ? data.decisions_recorded : null;
  const total = isNum(data.directional_observations)
    ? data.directional_observations
    : recorded !== null && isNum(data.observations)
      ? data.observations
      : null;
  const directional = total ?? (counts.length ? Math.max(...counts) : null);
  const decisions = recorded ?? (isNum(data.observations) ? data.observations : null);
  const status =
    !data.status || data.status === "INSUFFICIENT_DATA" || directional === 0 ? "NOT ENOUGH DATA" : data.status;
  const measured = (h: string) => {
    const n = (metrics[h] as MetricSummary | undefined)?.observations;
    return isNum(n) ? n : 0;
  };
  const bestHorizon = horizons.reduce<string | null>(
    (best, h) => (measured(h) > (best === null ? 0 : measured(best)) ? h : best),
    null,
  );
  // The service's own counts of decisions (distinct across horizons) win over one horizon's outcome counts.
  const reported =
    outcomeCount(data.published_signals) !== null || outcomeCount(data.counterfactual_decisions) !== null
      ? outcomeMix({ actionable: data.published_signals ?? 0, counterfactual: data.counterfactual_decisions ?? 0 }, data.population)
      : null;
  const none: OutcomeMix = { published: null, counterfactual: null, basis: "unknown" };
  return {
    decisions,
    directional,
    directionalAtBestHorizon: total === null && directional !== null,
    status,
    // Nothing measured: no basis to state.
    mix: !bestHorizon ? none : (reported ?? outcomeMix(metrics[bestHorizon] as MetricSummary, data.population)),
    mixHorizon: reported ? null : bestHorizon,
  };
}

/** "equal_notional sizing on 100000 …" -> "equal-notional sizing on 100,000 …". */
export function readableBasis(basis: string): string {
  return basis
    .replace(/\b[a-z]+(?:_[a-z]+)+\b/g, (w) => w.replaceAll("_", "-"))
    .replace(/\b\d{5,}\b/g, (n) => Number(n).toLocaleString("en-US"));
}

// --- alerts (/alerts) ----------------------------------------------------------------------------

/** GET /api/alerts?limit=… (the API accepts 1-500); the page pages through them client-side. */
export const ALERTS_LIMIT = 500;
export const ALERTS_PAGE = 50;

export function kindLabel(kind: unknown): string {
  return typeof kind === "string" && kind ? sentence(kind) : "Unavailable";
}

const NO_LABELS: ReadonlyMap<string, string> = new Map();

/** Provider display names by id, from the provider health list ({name, label}) the dashboard carries. */
export function providerLabels(providers: unknown): ReadonlyMap<string, string> {
  if (!Array.isArray(providers)) return NO_LABELS;
  const labels = new Map<string, string>();
  for (const p of providers) {
    const name = isRecord(p) ? text(p.name) : null;
    const label = isRecord(p) ? text(p.label) : null;
    if (name && label) labels.set(name, label);
  }
  return labels;
}

/**
 * Display name of the provider an alert is about: the alert's label, else its metrics label, else the name the
 * provider list gives its id. Never the raw id (alerts stored before labels were recorded carry only the id).
 */
export function alertProvider(alert: AlertItem, labels: ReadonlyMap<string, string> = NO_LABELS): string | null {
  const metrics = alert.metrics ?? {};
  const id = text(metrics.provider);
  return text(alert.label) ?? text(metrics.label) ?? text(metrics.provider_label) ?? (id ? (labels.get(id) ?? null) : null);
}

/** Where an alert leads: its asset, its event, or Provider health (under the provider's name) for provider alerts. */
export function alertLink(
  alert: AlertItem,
  labels: ReadonlyMap<string, string> = NO_LABELS,
): { href: string; text: string } | null {
  if (alert.symbol) return { href: `/assets/${encodeURIComponent(alert.symbol)}`, text: alert.symbol };
  if (alert.event_id) return { href: `/events/${encodeURIComponent(alert.event_id)}`, text: "Event" };
  const provider = alertProvider(alert, labels);
  if (alert.kind === "PROVIDER" || provider || text(alert.metrics?.provider))
    return { href: "/providers", text: provider ?? "Provider health" };
  return null;
}

/** "Showing 50 of 151 alerts" / "151 alerts", saying when only the newest `limit` were loaded. */
export function alertCountText(shown: number, total: number, loaded: number, limit = ALERTS_LIMIT): string {
  const base = shown < total ? `Showing ${shown} of ${total} alerts` : `${total} ${total === 1 ? "alert" : "alerts"}`;
  return loaded >= limit ? `${base} · only the newest ${limit} are loaded` : base;
}
