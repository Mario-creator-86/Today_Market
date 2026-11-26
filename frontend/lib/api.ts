import { bucharestDateTime } from "./event-time";
import { labelCase } from "./format";

/** Settings admin key when the backend runs in local mode (ADMIN_AUTH=local): no password on this computer. */
export const LOCAL_ADMIN = "local";

/** Headers for /admin calls. X-Local-Admin is a custom header (it forces a CORS preflight, so other websites cannot
 * send it); the backend accepts it only from this computer in local mode. A typed token is sent as well when present. */
export function adminHeaders(token: string): Record<string, string> {
  return token && token !== LOCAL_ADMIN
    ? { "X-Local-Admin": "1", "X-Admin-Token": token }
    : { "X-Local-Admin": "1" };
}

export function normalizeApiUrl(value: string): string {
  const base = value.replace(/\/+$/, "");
  return base.endsWith("/api") ? base : `${base}/api`;
}
export const API = normalizeApiUrl(
  process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000/api",
);
/**
 * FastAPI error detail as display text: a string detail, or the message of the
 * first validation item (prefixed with its field when the message omits it).
 */
export function errorDetail(body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail.trim();
  // Structured detail, e.g. {message, fields}: the message, plus the fields it names.
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as { message?: unknown; msg?: unknown; fields?: unknown };
    const message = [d.message, d.msg].find((m): m is string => typeof m === "string" && !!m.trim())?.trim() ?? "";
    const fields = Array.isArray(d.fields) ? d.fields.filter((f): f is string => typeof f === "string" && !!f) : [];
    const named = fields.filter((f) => !message.includes(f));
    return [message, named.length ? `Fields: ${named.join(", ")}` : ""].filter(Boolean).join(" ");
  }
  const first = (Array.isArray(detail) ? detail[0] : null) as { loc?: unknown; msg?: unknown } | null;
  if (typeof first?.msg !== "string") return "";
  const field = Array.isArray(first.loc) ? first.loc.at(-1) : undefined;
  return typeof field === "string" && field !== "body" && !first.msg.includes(field)
    ? `${field}: ${first.msg}`
    : first.msg;
}
function parse(body: string): unknown {
  try {
    return JSON.parse(body);
  } catch {
    return null;
  }
}
/** A request the service answered with an error status (`status`), as opposed to a network failure or timeout. */
export class ApiError extends Error {
  readonly status: number;
  /** The backend's own explanation (FastAPI `detail`), empty when it sent none. */
  readonly detail: string;
  constructor(message: string, status: number, detail = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}
/** The service could not be reached (connection failed) or did not answer in time. */
export class NetworkError extends Error {
  readonly timeout: boolean;
  constructor(message: string, timeout: boolean, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "NetworkError";
    this.timeout = timeout;
  }
}
/** The service answered 404: the record does not exist (do not keep polling it). */
export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}
/** The service answered at all (4xx/5xx), so it is reachable even though the request failed. */
export function isServiceError(error: unknown): boolean {
  return error instanceof ApiError;
}
/**
 * Worth asking again later: the service was unreachable or timed out, or answered 408, 429 or 5xx. A 404, an
 * authentication refusal or a validation error gives the same answer on every retry.
 */
export function isTransientError(error: unknown): boolean {
  if (error instanceof NetworkError) return true;
  return error instanceof ApiError && (error.status >= 500 || error.status === 408 || error.status === 429);
}
/** A query's retry option (TanStack `retry`): off, a number of retries, or a rule of (failures, error). */
export type RetryRule = boolean | number | ((failureCount: number, error: Error) => boolean) | undefined;
/**
 * Would a query's own retry rule ask again after this error? `retry: false` (or 0) opts out, a rule decides, and
 * otherwise only transient errors are asked again. The workspace uses it for its background retry of failed pages.
 */
export function retriesAfter(retry: RetryRule, error: Error): boolean {
  if (typeof retry === "function") return retry(0, error);
  if (retry === false || retry === 0) return false;
  return isTransientError(error);
}
export type ErrorView = {
  /** not_found: the service answered 404; service: any other error status or an unreadable reply; network: no answer. */
  kind: "not_found" | "service" | "network";
  /** One plain sentence, e.g. "The service returned an error (HTTP 500)." */
  title: string;
  /** The backend's explanation or a hint; may be empty. */
  detail: string;
};
/** How a failed request reads on screen: never "unreachable" for a service that answered. */
export function describeError(error: unknown): ErrorView {
  if (error instanceof ApiError) {
    if (error.status === 404) {
      // FastAPI's bare "Not Found" (an unknown route) says nothing the title does not.
      const detail = /^not found\.?$/i.test(error.detail) ? "" : error.detail;
      return { kind: "not_found", title: "Not found.", detail: detail || "The service has no record at this address." };
    }
    return { kind: "service", title: `The service returned an error (HTTP ${error.status}).`, detail: error.detail };
  }
  if (error instanceof SyntaxError)
    return { kind: "service", title: "The service sent a reply this page could not read.", detail: "" };
  if (error instanceof NetworkError && error.timeout)
    return { kind: "network", title: "Unable to reach the data service.", detail: "It did not answer in time." };
  return {
    kind: "network",
    title: "Unable to reach the data service.",
    detail: error instanceof NetworkError ? "Check that the backend is running." : ((error as Error)?.message ?? ""),
  };
}
/** JSON request to the backend. Callers may pass their own `signal` (default: 15 s timeout). */
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    signal: init?.signal ?? AbortSignal.timeout(15000),
    headers: { "Content-Type": "application/json", ...init?.headers },
  }).catch((e: unknown) => {
    // A caller's own abort (e.g. a cancelled query) stays an AbortError.
    if ((e as Error)?.name === "AbortError") throw e;
    if ((e as Error)?.name === "TimeoutError")
      throw new NetworkError("Request timed out. The service did not answer in time.", true, { cause: e });
    // fetch rejects only when no answer arrived (browser text such as "Failed to fetch" is not shown).
    throw new NetworkError("Unable to reach the data service. Check that the backend is running.", false, { cause: e });
  });
  const body = await response.text();
  if (!response.ok) {
    const detail = errorDetail(parse(body)).slice(0, 500);
    throw new ApiError(
      detail
        ? `Request failed (${response.status}): ${detail}`
        : `Request failed (${response.status}). Check the service and your access settings.`,
      response.status,
      detail,
    );
  }
  // 204 / empty bodies carry no payload.
  return (body ? JSON.parse(body) : undefined) as T;
}
export function number(value: unknown, digits = 2): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("en-US", { maximumFractionDigits: digits })
    : "Unavailable";
}
/**
 * "Mon 28 Sep 02:10" in Europe/Bucharest (the same format as event times and as-of lines); "Not recorded" when
 * missing or invalid. Fixed month names (ICU may say "Sept") and no leading zero on the day.
 */
export function localTime(value: unknown): string {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value)))
    return "Not recorded";
  return bucharestDateTime(Date.parse(value));
}
/** A span of seconds: "0 s", "45 s", "5 min", "6 h", "1 day"; missing values stay missing. */
export function durationText(seconds: unknown): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "Unavailable";
  const s = Math.abs(seconds);
  if (s >= 86400 && s % 86400 === 0) return `${seconds / 86400} ${s === 86400 ? "day" : "days"}`;
  if (s >= 3600 && s % 3600 === 0) return `${seconds / 3600} h`;
  if (s >= 60 && s % 60 === 0) return `${seconds / 60} min`;
  return `${seconds.toLocaleString("en-US", { maximumFractionDigits: 1 })} s`;
}
/**
 * A configuration key or enum as a label: "second_opinion" → "Second opinion", "LEVEL_BREAK" → "Level break",
 * "sec_filing" → "SEC filing" (known acronyms stay upper case; lib/format labelCase).
 */
export function labelText(key: string): string {
  return labelCase(key);
}
/** Without the backend's analysis record, a last full analysis older than this is flagged as out of date. */
export const ANALYSIS_STALE_FALLBACK_MS = 15 * 60_000;
export type AnalysisView = {
  /** The last successful full analysis (ISO), or null when none is recorded. */
  at: string | null;
  /** Its age at `now`, when known. */
  ageMs: number | null;
  stale: boolean;
  /** Plain-English explanation when stale. */
  reason: string | null;
};
function isoOrNull(value: unknown): string | null {
  return typeof value === "string" && Number.isFinite(Date.parse(value)) ? value : null;
}
/**
 * Freshness of the last full analysis cycle. The backend's `analysis` record decides (stale = no success within
 * twice the cadence, or the last attempt failed); an older backend without it is judged by the age of
 * `last_analysis` alone (ANALYSIS_STALE_FALLBACK_MS).
 */
export function analysisView(d: Pick<Dashboard, "last_analysis" | "analysis"> | null | undefined, now: number): AnalysisView {
  const record = d?.analysis && typeof d.analysis === "object" ? d.analysis : null;
  const at = isoOrNull(record && "last_success_at" in record ? record.last_success_at : d?.last_analysis);
  const ageMs = at ? Math.max(0, now - Date.parse(at)) : null;
  if (!record) {
    const stale = ageMs !== null && ageMs > ANALYSIS_STALE_FALLBACK_MS;
    return { at, ageMs, stale, reason: stale ? `Older than ${durationText(ANALYSIS_STALE_FALLBACK_MS / 1000)}; signals may be out of date.` : null };
  }
  const attempt = isoOrNull(record.last_attempt_at);
  const failed = !!record.last_error_type && (!at || (attempt !== null && Date.parse(attempt) >= Date.parse(at)));
  const cadence = typeof record.cadence_seconds === "number" && record.cadence_seconds > 0 ? record.cadence_seconds : null;
  const overdue = cadence !== null && (ageMs === null || ageMs > 2 * cadence * 1000);
  const stale = typeof record.stale === "boolean" ? record.stale : failed || overdue;
  if (!stale) return { at, ageMs, stale, reason: null };
  const expected = cadence !== null ? ` It normally runs every ${durationText(cadence)}.` : "";
  const reason = failed
    ? `${attempt ? `The analysis attempt at ${localTime(attempt)} failed` : "The last analysis attempt failed"}; ${at ? "signals are from the last successful run" : "no full analysis has completed yet"}.${expected}`
    : `${at ? "No full analysis has completed since then" : "No full analysis has completed yet"}; signals may be out of date.${expected}`;
  return { at, ageMs, stale, reason };
}
/** Long day label in Europe/Bucharest, e.g. "Saturday 26 September". */
export function localDay(value: unknown): string {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value)))
    return "Date unavailable";
  return new Date(value).toLocaleDateString("en-GB", {
    timeZone: "Europe/Bucharest",
    weekday: "long",
    day: "numeric",
    month: "long",
  });
}
/** Wall-clock time (HH:MM:SS) in Europe/Bucharest. */
export function localClock(date: Date = new Date()): string {
  return date.toLocaleTimeString("en-GB", { timeZone: "Europe/Bucharest" });
}
/** Hour and minute (HH:MM) of an ISO timestamp in Europe/Bucharest. */
export function localHourMinute(value: unknown): string {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value)))
    return "Time unavailable";
  return new Date(value).toLocaleTimeString("en-GB", {
    timeZone: "Europe/Bucharest",
    hour: "2-digit",
    minute: "2-digit",
  });
}
export function safeUrl(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  try {
    const u = new URL(value);
    return ["https:", "http:"].includes(u.protocol) ? u.href : undefined;
  } catch {
    return undefined;
  }
}
export type RecordData = Record<string, unknown>;
export type Decision = {
  id: string;
  direction: string;
  score: number;
  evidence_confidence: number;
  market_confirmation: number;
  data_quality: number;
  reasons: string[];
  /** Publication and read-time blocks explaining why guidance is held at WAIT. */
  blocks?: string[];
  confirmation: string;
  invalidation: string;
  audit_status: string;
  components: Record<string, number>;
  /** Contract category, e.g. "LONG SETUP" or "BEARISH LEAN / WAIT". */
  category?: string;
  /** Verified-fact WHY bullets. */
  why?: string[];
  /** Chief analyst explanation (reasons, confirmation, invalidation). */
  explanation?: {
    reasons?: string[];
    confirmation?: string;
    invalidation?: string;
  } | null;
  confirmation_level?: number | null;
  invalidation_level?: number | null;
  components_missing?: string[];
  historical_probability?: HistoricalProbability | null;
  created_at?: string;
  /** Server-configured validity deadline; fresh quote frames cannot extend it. */
  expires_at?: string | null;
  /** name -> {value, status, basis}; a missing component is never a measured zero. */
  component_detail?: Record<string, ComponentDetail>;
  /** Signed weighted contributions by driver as published (backend scoring.driver_contributions). */
  drivers?: DriverContributions | null;
  /** Older payloads' key for `drivers`. */
  driver_contributions?: DriverContributions | null;
  /** When the cycle decided (older payloads: created_at only). */
  decided_at?: string;
  /** The price the decision was made at. */
  price?: number | null;
  /** Support and resistance at decision time. */
  levels?: { support?: number | null; resistance?: number | null } | null;
};
/** `{p, n, horizon}` when calibrated, else `{status: "NOT ENOUGH DATA", n}`. */
export type HistoricalProbability = {
  p?: number | null;
  n?: number | null;
  horizon?: string | null;
  status?: string;
  calibration?: RecordData;
  /** When not calibrated: the observations needed, and why there is no probability. */
  minimum?: number | null;
  reason?: string | null;
};
/** What a change is measured against (docs/MARKET_COVERAGE.md). */
export type ChangeBasis =
  | "previous_close"
  | "previous_fix"
  | "rolling_24h"
  | "provider";
/** LIVE feed, official end-of-day value, or a daily value computed by this app. */
export type DataClass = "LIVE" | "DAILY_CLOSE" | "DERIVED_DAILY";
/** Latest expected publication (CURRENT) or genuinely late (STALE); null for live data. */
export type CloseStatus = "CURRENT" | "STALE";
/** Venue session (backend sessions.session_info), sent on every asset. */
export type AssetSession = {
  /** LIVE | LAST CLOSE | FRIDAY CLOSE | AFTER HOURS | PREMARKET | … */
  state: string;
  label?: string | null;
  venue?: string | null;
  /** Plain venue name from config market_sessions (e.g. "NYSE (cash index hours)"). */
  venue_label?: string | null;
  basis?: string | null;
  session_close?: string | null;
  next_session_close?: string | null;
};
export type CoverageStatus =
  | "OK"
  | "DAILY_ONLY"
  | "PROXY_ONLY"
  | "NOT_CONFIGURED"
  | "NO_DATA_YET"
  /** Every level source is switched off by an administrator and there is no data. */
  | "DISABLED";
/** Which sources can fill the asset and which env keys would add one. */
export type AssetCoverage = {
  status: CoverageStatus | string;
  providers?: string[];
  /** Every unset env key, including each key of a source that needs several (Alpaca: key ID and secret). */
  missing_keys?: string[];
  /** Sources switched off by an administrator in Settings → Data providers. */
  disabled?: string[];
};
/** A different instrument that tracks the asset, shown under its own name for direction only. */
export type AssetProxy = {
  symbol: string;
  proxy_for?: string;
  label?: string | null;
  caveat?: string | null;
  price: number;
  change_pct?: number | null;
  change_basis?: ChangeBasis | string | null;
  previous_close?: number | null;
  timestamp: string;
  provider: string;
  feed?: string | null;
  delay_label?: string | null;
  attribution?: string | null;
  relation?: string | null;
  market_status?: string | null;
  freshness_status?: string | null;
};
export type Quote = {
  price: number;
  timestamp: string;
  provider: string;
  market_status: string;
  freshness_status: string;
  change_pct: number | null;
  freshness_threshold_seconds?: number;
  /** Change in basis points (yields), when the provider reports it. */
  change_bp?: number | null;
  /** Session state: LIVE | LAST CLOSE | FRIDAY CLOSE | AFTER HOURS | PREMARKET | UNKNOWN. */
  market_state?: string;
  age_seconds?: number;
  /** Development stub record (PROVIDER_MODE=stub); never a real quote. */
  stub?: boolean;
  /** Legacy flag for official end-of-day values (see data_class). */
  daily_close?: boolean;
  previous_close?: number | null;
  previous_close_at?: string | null;
  change_basis?: ChangeBasis | string | null;
  data_class?: DataClass | string | null;
  close_status?: CloseStatus | string | null;
  /** Human-readable as-of text, e.g. "Fri 25 Sep 16:00 ET". */
  as_of_label?: string | null;
  /** Display name of the instrument, e.g. "US Treasury 2Y par yield". */
  label?: string | null;
  /** e.g. "Source: U.S. Department of the Treasury". */
  attribution?: string | null;
  /** True for values computed by this app (the USD index replica). */
  derived?: boolean;
  /** Provider feed, e.g. "iex". */
  feed?: string | null;
  delay_label?: string | null;
};
export type Asset = {
  symbol: string;
  name: string;
  asset_class: string;
  freshness_threshold_seconds?: number;
  quote: Quote | null;
  session?: AssetSession | null;
  coverage?: AssetCoverage | null;
  proxy?: AssetProxy | null;
  decision: Decision | null;
  bars?: { time: string | number; close: number }[];
  /** Stored daily bars by New York session date (up to about a year); [] when none. */
  daily_bars?: DailyBar[] | null;
  events?: MarketEvent[];
  technical_levels?: Record<string, number | null>;
  /** DXY / US10Y / Nasdaq (or configured) overlay bars for the cross-asset chart. */
  cross_asset?: CrossAsset | null;
  /** Bar interval in seconds per cross-asset series: 60 (minute bars) or 86400 (daily closes). */
  cross_asset_interval?: Record<string, number> | null;
  /** Event overlay (docs/EVENT_AWARENESS.md §4); the published decision above stays unchanged. */
  event_risk?: AssetEventRisk | null;
  /** Crypto derivatives positioning (GET /api/assets/{BTC|ETH}); descriptive only, never a direction. */
  positioning?: PositioningSnapshot | null;
};
/** Option totals of a positioning snapshot. */
export type PositioningOptionTotals = {
  call_oi?: number | null;
  put_oi?: number | null;
  put_call_ratio?: number | null;
  oi_weighted_iv?: number | null;
};
/** One option expiry of a positioning snapshot. */
export type PositioningExpiry = {
  expiry?: string;
  days_to_expiry?: number | null;
  skew?: { put_minus_call_iv?: number | null } | null;
};
/** Perpetual future of a positioning snapshot. */
export type PositioningPerpetual = {
  basis_pct?: number | null;
  funding_8h?: number | null;
  open_interest?: number | null;
  open_interest_unit?: string | null;
};
/** Backend positioning.summarize; every field may be absent. */
export type PositioningSnapshot = {
  currency?: string;
  as_of?: string | null;
  age_seconds?: number | null;
  /** FRESH | STALE | MISSING */
  freshness_status?: string | null;
  options?: { totals?: PositioningOptionTotals | null; expiries?: PositioningExpiry[] | null } | null;
  futures?: { perpetual?: PositioningPerpetual | null } | null;
  crowding?: { flag?: string }[] | null;
  reason?: string | null;
};
export type MarketEvent = {
  id: string;
  summary_fact: string;
  event_type: string;
  verification_status: string;
  fact_confidence: number;
  first_seen_utc: string;
  event_time?: string;
  /**
   * False when the publication stamp is a date without a clock time (a notice
   * dated "2 Oct", stored at the start of the publisher's day): the UI then
   * shows the date only. Absent on older backends (the time is shown as sent).
   */
  event_time_known?: boolean | null;
  /** The same fact as the event layer names it (event_risk `published_date_only`). */
  published_date_only?: boolean | null;
  last_updated_utc: string;
  assets: string[];
  sources: unknown[];
  interpretation: RecordData | null;
  evidence?: RecordData[];
  topic?: string | null;
  /** Where the topic came from: "rule" (a causal rule), "entities" (entity heuristics); null without a topic. */
  topic_basis?: "rule" | "entities" | string | null;
  entities?: string[];
  novelty?: number | null;
  /** Deterministic importance, 0..1. */
  importance?: number | null;
  /** What the importance is derived from, e.g. "source tier". */
  importance_basis?: string | null;
  affected_assets?: string[];
  causal_paths?: CausalPath[];
  extraction?: RecordData | null;
  trigger?: string | null;
  trigger_basis?: string | null;
  injection_suspected?: boolean;
  /** Measured per-asset reactions per window (backend analytics). */
  reactions?: Reaction[];
  /** Event-level basket confirmation per horizon. */
  baskets?: ReactionBasket[];
  fact_check?: FactCheckResult | null;
};
export type CausalPath = {
  rule_id?: string;
  trigger?: string;
  effects?: Record<string, number>;
  strength?: number;
  active?: boolean;
  suppressed_by?: string | null;
  steps?: CausalStep[];
  decay?: number;
};
export type CausalStep = {
  variable: string;
  direction: number;
  label?: string;
  from?: string | null;
};
export type Provider = {
  name: string;
  /** Human name from configuration, e.g. "Finnhub (equities)"; show `label ?? name`. */
  label?: string | null;
  /** Settings category id (ai, market, macro, calendars, filings, news, …); null when not configured. */
  category?: string | null;
  status: string;
  latency_ms: number | null;
  last_success: string | null;
  /** Short plain-English failure, e.g. "Rate limited (HTTP 429)"; shown as is. */
  error: string | null;
  /** Machine detail of the failure (diagnostics only, never shown). */
  error_type?: string | null;
  stub?: boolean;
  /** False when switched off by an administrator (status DISABLED). */
  enabled?: boolean;
};
export type Regime = {
  regimes: string[];
  posture: string;
  confidence: number | null;
  coverage_pct: number;
  /** "live" (fresh intraday features) or "last_close" (close-to-close changes). */
  basis: "live" | "last_close" | string;
  /** e.g. "last close Fri 25 Sep, markets closed". */
  basis_label?: string | null;
  /** Required feature names no rule could evaluate (posture INSUFFICIENT_DATA). */
  missing_required?: string[];
  /** Rates / USD / Oil / Risk appetite arrows. */
  drivers?: RegimeDriver[];
  /** Fresh direct quotes and approved direction proxies, separate from historical inputs. */
  current_drivers?: RegimeDriver[];
  divergences?: { feature: string; text: string }[];
  /** Per-feature provenance: basis (live, last close, proxy), close status and date, and the proxy it came via. */
  feature_sources?: Record<string, FeatureSource> | null;
};
/** regime.feature_sources entry. */
export type FeatureSource = {
  basis?: string;
  close_status?: CloseStatus | string | null;
  close_date?: string | null;
  as_of?: string | null;
  /** The instrument a proxied feature was read from. */
  via?: string | null;
};
export type RegimeDriver = {
  driver: string;
  direction: "up" | "down" | "flat" | "unavailable" | string;
  arrow?: string | null;
  missing?: string[];
  /** The input that set the arrow, e.g. "US2Y -6 bp". */
  input?: string | null;
  /** Measured inputs and their votes (backend regime.drivers); used when `input` is absent. */
  inputs?: RegimeDriverInput[];
  /** "live" | "last_close" | … */
  basis?: string | null;
};
export type RegimeDriverInput = {
  feature: string;
  /** "change_bp" | "change_pct" | … */
  field?: string;
  value?: number | null;
  vote?: number;
  /** For a daily close: CURRENT (latest expected publication) or STALE (late); null otherwise. */
  close_status?: CloseStatus | string | null;
  /** The close's date, "YYYY-MM-DD"; null for live inputs. */
  close_date?: string | null;
  /** The instrument a proxied input was read from. */
  via?: string | null;
};
/** In-app alert (docs/CONTRACTS.md "Alert"). */
export type Alert = {
  id: string;
  kind: string;
  priority: string;
  title: string;
  message: string;
  symbol: string | null;
  created_at: string;
  decision_id?: string | null;
  event_id?: string | null;
  metrics?: RecordData;
  /** Provider alerts: the provider's human name. */
  label?: string | null;
};
/** Alerts created after `lastRead` (every alert when the list was never marked read: lastRead NaN). */
function unreadIn(alerts: Alert[], lastRead: number): number {
  return alerts.filter((a) => {
    const created = Date.parse(a.created_at);
    return !Number.isFinite(lastRead) || (Number.isFinite(created) && created > lastRead);
  }).length;
}
export type UnreadCount = {
  unread: number;
  /** The count is a lower bound ("at least"). */
  more: boolean;
  /** Only a longer list of alerts can tell: the caller should load it. */
  needsLonger: boolean;
};
/**
 * The unread alert count from what is known. `alerts` is a short newest-first list (the dashboard's); when every one
 * of them is unread, older ones may be too, and `longer` (the newest `limit` alerts, undefined until loaded) is
 * counted instead. `total` (the number of stored alerts, when known) makes the count exact without the longer list
 * when `alerts` holds every stored alert or the list was never marked read (then every stored alert is unread).
 * The count is a lower bound while the longer list is missing (loading or failed), and when even that list is
 * entirely unread while more alerts are stored (or it is full, when the total is not known).
 */
export function unreadAlertCount(
  alerts: Alert[],
  lastRead: number,
  total: number | null | undefined,
  longer: Alert[] | undefined,
  limit: number,
): UnreadCount {
  const everRead = Number.isFinite(lastRead);
  const shown = unreadIn(alerts, lastRead);
  const saturated = alerts.length > 0 && shown === alerts.length;
  // A total below the list's own length is inconsistent: treated as unknown.
  const stored = typeof total === "number" && Number.isInteger(total) && total >= alerts.length ? total : null;
  if (stored !== null && !everRead) return { unread: stored, more: false, needsLonger: false };
  if (!saturated || stored === alerts.length) return { unread: shown, more: false, needsLonger: false };
  if (longer === undefined) return { unread: shown, more: true, needsLonger: true };
  const counted = unreadIn(longer, lastRead);
  const full = counted === longer.length && (stored !== null ? longer.length < stored : longer.length >= limit);
  return { unread: Math.max(shown, counted), more: full, needsLonger: true };
}
export type Brief = {
  kind: "MORNING" | "INTRADAY" | null;
  generated_at: string | null;
  local_date?: string;
  triggers?: string[];
  changes: string[];
  sections: { title: string; items: string[] }[];
  /** Why there is no brief ("No morning brief for 2026-10-02 yet"); only on the empty answer. */
  reason?: string | null;
  /** Morning brief: published after its scheduled window (it then describes a later moment than its schedule). */
  late?: boolean | null;
  minutes_late?: number | null;
  /** The scheduled local time, "09:00". */
  scheduled_local?: string | null;
};

/**
 * "published 1 h 35 min after the 09:00 schedule" for a morning brief composed
 * after its scheduled window; null for a brief on time or without the fields.
 */
export function briefLateText(brief: Pick<Brief, "late" | "minutes_late" | "scheduled_local"> | null | undefined): string | null {
  const minutes = brief?.minutes_late;
  if (brief?.late !== true || typeof minutes !== "number" || !Number.isFinite(minutes) || minutes <= 0) return null;
  const whole = Math.round(minutes);
  const span = whole < 90 ? `${whole} min` : `${Math.floor(whole / 60)} h${whole % 60 ? ` ${whole % 60} min` : ""}`;
  const schedule = typeof brief.scheduled_local === "string" && brief.scheduled_local.trim();
  return `published ${span} after ${schedule ? `the ${schedule} schedule` : "its schedule"}`;
}
export type Dashboard = {
  freshness_threshold_seconds?: number;
  as_of: string;
  timezone: string;
  regimes: string[];
  regime?: Regime;
  assets: Asset[];
  events: MarketEvent[];
  calendar: RecordData[];
  providers: Provider[];
  alerts: unknown[];
  /** Number of stored alerts: `alerts` carries only the newest of them. Absent on older backends. */
  alerts_total?: number | null;
  /** Time of the last successful full analysis cycle (never advanced by the morning brief or other jobs). */
  last_analysis: string | null;
  /** The full analysis cycle's own record; absent on older backends. */
  analysis?: AnalysisStatus | null;
  /** Ordered market-strip symbols from configuration. */
  market_strip?: string[];
  /** Minimum calendar impact (n/10) for "Next important events". */
  calendar_min_impact?: number;
  /** Notices a data provider's terms require while its data is on screen (e.g. the FRED API notice). */
  notices?: DataNotice[];
  /** Deterministic event-risk layer (docs/EVENT_AWARENESS.md §4); absent on older backends. */
  event_risk?: EventRisk | null;
  /** Stored market interpretation (AI or deterministic fallback); absent on older backends. */
  interpretation?: Interpretation | null;
  /** Set when the event-risk layer failed to build: `event_risk` is then null and the rest still renders (§8). */
  event_risk_error?: string | null;
};

/** GET /api/dashboard → analysis: stale = no success within twice the cadence, or the last attempt failed. */
export type AnalysisStatus = {
  last_success_at?: string | null;
  last_attempt_at?: string | null;
  /** Machine detail of the last failure (never shown). */
  last_error_type?: string | null;
  cadence_seconds?: number | null;
  stale?: boolean | null;
};

/**
 * WebSocket {"type": "asset_quotes"} payload: the live parts of `assets[]` that changed since the last full snapshot,
 * keyed by symbol, in the same shapes as the snapshot's assets[].quote / assets[].proxy (null: the asset has none).
 */
export type AssetQuotesFrame = {
  as_of: string | null;
  quotes: Record<string, Quote | null>;
  proxies: Record<string, AssetProxy | null>;
};
function recordOf<T>(value: unknown): Record<string, T | null> {
  const out: Record<string, T | null> = {};
  if (!value || typeof value !== "object" || Array.isArray(value)) return out;
  for (const [symbol, item] of Object.entries(value))
    if (symbol !== "__proto__" && (item === null || (typeof item === "object" && !Array.isArray(item))))
      out[symbol] = item as T | null;
  return out;
}
/** A well-formed asset_quotes payload, or null for anything else (such a frame is ignored). */
export function parseAssetQuotes(data: unknown): AssetQuotesFrame | null {
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const d = data as { as_of?: unknown; quotes?: unknown; proxies?: unknown };
  return {
    as_of: isoOrNull(d.as_of),
    quotes: recordOf<Quote>(d.quotes),
    proxies: recordOf<AssetProxy>(d.proxies),
  };
}
/** Two frames held back by the update throttle as one: the later frame wins per symbol. */
export function combineAssetQuotes(older: AssetQuotesFrame, newer: AssetQuotesFrame): AssetQuotesFrame {
  return {
    as_of: newer.as_of ?? older.as_of,
    quotes: { ...older.quotes, ...newer.quotes },
    proxies: { ...older.proxies, ...newer.proxies },
  };
}
/**
 * The dashboard with a frame's quotes and proxies in place: each asset named in the frame gets the new quote / proxy,
 * every other asset (and everything else) stays as it was, and as_of moves to the frame's time. Undefined without a
 * dashboard: there is nothing to patch until the first snapshot or poll arrives.
 */
export function mergeAssetQuotes<T extends { as_of?: string; assets?: Asset[] }>(
  dashboard: T | undefined,
  frame: AssetQuotesFrame,
): T | undefined {
  if (!dashboard || typeof dashboard !== "object" || !Array.isArray(dashboard.assets)) return dashboard;
  let changed = false;
  const assets = dashboard.assets.map((asset) => {
    const symbol = asset && typeof asset === "object" ? asset.symbol : undefined;
    if (typeof symbol !== "string") return asset;
    const hasQuote = Object.hasOwn(frame.quotes, symbol);
    const hasProxy = Object.hasOwn(frame.proxies, symbol);
    if (!hasQuote && !hasProxy) return asset;
    changed = true;
    return {
      ...asset,
      ...(hasQuote ? { quote: frame.quotes[symbol] } : {}),
      ...(hasProxy ? { proxy: frame.proxies[symbol] } : {}),
    };
  });
  if (!changed && !frame.as_of) return dashboard;
  return { ...dashboard, assets, ...(frame.as_of ? { as_of: frame.as_of } : {}) } as T;
}

/* --- Event awareness (docs/EVENT_AWARENESS.md). Every field is backend-owned; the UI only formats it. --- */

export type EventPhase =
  | "SCHEDULED"
  | "APPROACHING"
  | "IMMINENT"
  | "RELEASED"
  | "IN_PROGRESS"
  | "POST_EVENT"
  | "RESOLVED";
/** CRITICAL ≥ 90, VERY HIGH 75-89, HIGH 60-74, MODERATE 40-59 (backend bands). */
export type RelevanceLevel = "CRITICAL" | "VERY HIGH" | "HIGH" | "MODERATE" | "LOW";
/** One exposed asset: level very_high | high | medium | low, weight 0..1. */
export type EventExposure = { symbol: string; level?: string | null; weight?: number | null };
/** e.g. {sub_event: "PRESS_CONFERENCE", time_utc, phase}. */
export type SubEvent = {
  sub_event: string;
  time_utc?: string | null;
  phase?: EventPhase | string | null;
  time_known?: boolean;
};
/** Deterministic scenario template ("possible", never certain). */
export type EventScenario = {
  id?: string;
  label?: string;
  condition?: string;
  /** Expected direction per exposed asset: +1 / -1 / 0 (or "up" / "down"). */
  expected?: Record<string, number | string | null>;
  /**
   * Exposed assets whose competing causal paths leave no net direction in this
   * scenario (e.g. equities after a strong jobs report: growth up, rates up);
   * they carry no expected arrow. Absent on older backends.
   */
  unresolved?: string[] | null;
  note?: string | null;
};
/** Consensus / pricing value: a number, text, or {value, unit}. */
export type ExpectationValue =
  | number
  | string
  | boolean
  | null
  | { value?: number | string | null; unit?: string | null; [key: string]: unknown };
/** One expected value of a consensus (§8): {name, value, unit}. */
export type ConsensusValue = { name?: string | null; value?: number | string | null; unit?: string | null };
/**
 * Consensus (§8): "NO CONSENSUS", or {label: "Consensus" | "Finnhub estimate", values, source}.
 * `previous` is never an expectation (it stays in known_information).
 */
export type Consensus = {
  label?: string | null;
  values?: ConsensusValue[] | null;
  source?: string | null;
  [key: string]: unknown;
};
/** What kind of occurrence an event is (§8); only DECISION events say "Decision published". */
export type EventKind =
  | "DECISION"
  | "PRESS_CONFERENCE"
  | "MINUTES"
  | "SPEECH"
  | "TESTIMONY"
  | "PROJECTIONS"
  | "RELEASE"
  | "AUCTION"
  | "EARNINGS"
  | "EXPIRY"
  | "STATEMENT"
  | "OTHER";
/** Backend surprise record (§8); only a validated AI assessment with evidence ever grades. */
export type EventSurprise = {
  assessment?: string | null;
  source?: "ai" | "none" | string | null;
  evidence_ids?: string[] | null;
  validated?: boolean | null;
};
export type EventObject = {
  /** Display id, e.g. "FED-20261028-FOMC". */
  event_id: string;
  /** Stored calendar id. */
  row_id?: string | null;
  name: string;
  event_class?: string | null;
  category?: string | null;
  /** DECISION | PRESS_CONFERENCE | MINUTES | SPEECH | TESTIMONY | … (§8). */
  kind?: EventKind | string | null;
  /** True only for policy decisions; the UI says "Decision published" for these alone. */
  is_decision?: boolean | null;
  speaker_role?: "CHAIR" | "VICE_CHAIR" | "GOVERNOR" | string | null;
  status?: string | null;
  phase?: EventPhase | string | null;
  /** Backend wording of the phase, e.g. "US CPI released; reaction in progress". */
  status_note?: string | null;
  /** "ANYTIME" for unscheduled events. */
  timing?: string | null;
  time_utc?: string | null;
  /** "21:00" in Europe/Bucharest (the UI recomputes it from time_utc when that is present). */
  time_bucharest?: string | null;
  date_bucharest?: string | null;
  /** False for date-only rows: no countdown, "time TBA". */
  time_known?: boolean | null;
  time_basis?: string | null;
  /** Release window of a date-only row: it is pending until `end_utc`, whatever its anchor `time_utc`. */
  date_window?: { start_utc?: string | null; end_utc?: string | null; label?: string | null; zone?: string | null } | null;
  /** Backend value at build time; the UI counts down from time_utc itself. */
  minutes_until_event?: number | null;
  importance_score?: number | null;
  expected_volatility_score?: number | null;
  relevance_score?: number | null;
  relevance_level?: RelevanceLevel | string | null;
  dominance_score?: number | null;
  /** The calendar row's own impact on its 0-10 scale (the calendar's number); null when the row has none. */
  impact?: number | null;
  /** Relevance / 10 (older payloads: impact out of 10 when the backend sent one). */
  impact_10?: number | null;
  affected_assets?: EventExposure[] | null;
  /** {label, values: [{name, value, unit}], source}, or the literal "NO CONSENSUS" (older payloads: a flat dict). */
  consensus?: Consensus | Record<string, ExpectationValue> | "NO CONSENSUS" | string | null;
  /** A dict, or the literal "UNKNOWN". */
  market_pricing?: Record<string, ExpectationValue> | "UNKNOWN" | string | null;
  known_information?: string[] | null;
  possible_surprises?: string[] | null;
  source_ids?: string[] | null;
  fact_confidence?: number | null;
  sub_events?: SubEvent[] | null;
  parent_event_id?: string | null;
  /** Reasons the data is incomplete. EVENT DATA INCOMPLETE itself is the block's flag (CRITICAL / VERY HIGH only). */
  data_incomplete?: string[] | null;
  verification_status?: string | null;
  surprise?: EventSurprise | null;
  scenarios?: EventScenario[] | null;
  /** Causal consequences of an active event: {SYM: +1 | -1}. */
  consequences?: Record<string, number | string | null> | null;
  first_seen_utc?: string | null;
  sources?: unknown[] | null;
};
export type EventRiskMode = {
  active: boolean;
  event_id?: string | null;
  name?: string | null;
  /** The countdown target (§8); the UI recomputes the countdown from it. */
  time_utc?: string | null;
  phase?: EventPhase | string | null;
  time_bucharest?: string | null;
  minutes_until?: number | null;
  /** The calendar row's own 0-10 impact; null when the row has none. */
  impact?: number | null;
  impact_10?: number | null;
  affected_groups?: string[] | null;
};
export type MarketsOpen = {
  crypto?: boolean;
  us_equities?: boolean;
  us_rates?: boolean;
  fx?: boolean;
  commodities?: boolean;
};
/** Per-asset event risk: max over events of relevance × exposure. */
export type AssetEventRisk = {
  score?: number | null;
  level?: RelevanceLevel | string | null;
  event_id?: string | null;
  minutes_until?: number | null;
  /** On /api/assets entries. */
  trade_status?: string | null;
  adjusted_confidence?: number | null;
  awaiting_reopen?: boolean | null;
  /** |decision score| before the pre-event penalty. */
  base_confidence?: number | null;
  /** How adjusted_confidence is derived from base_confidence (plain text). */
  confidence_basis?: string | null;
};
/** Score, or {score, level, …}. */
export type EventRiskValue = number | AssetEventRisk | null;
export type MarketViewItem = {
  symbol: string;
  stance?: string | null;
  event_risk?: EventRiskValue;
  event_id?: string | null;
  reason_facts?: string[] | null;
  awaiting_reopen?: boolean | null;
};
export type BreakItem = string | { text?: string; kind?: string | null; source?: string | null };
export type TradeWatchItem = {
  asset: string;
  /** LONG | SHORT | WAIT | CONDITIONAL LONG | CONDITIONAL SHORT | WAIT FOR <EVENT>. */
  trade_status: string;
  decision_score?: number | null;
  regime_favorability?: number | null;
  evidence_confidence?: number | null;
  market_confirmation?: number | null;
  event_risk?: EventRiskValue;
  adjusted_confidence?: number | null;
  /** Only when calibrated; null otherwise (with a reason). */
  historical_probability?: HistoricalProbability | null;
  historical_probability_reason?: string | null;
  horizon?: string | null;
  why?: string[] | null;
  upcoming_event?: string | Partial<EventObject> | null;
  confirmation?: string | string[] | null;
  invalidation?: string | string[] | null;
  awaiting_reopen?: boolean | null;
  status_reason?: string | null;
};
/**
 * One reaction cell (§8): {change, unit: "bp" | "pct", status}. Older payloads send a bare number
 * with its unit in the entry's `units` map; yields are always in bp.
 */
export type ReactionValue =
  | number
  | null
  | { change?: number | null; change_pct?: number | null; unit?: string | null; status?: string | null };
export type PostEvent = {
  event_id: string;
  name?: string | null;
  event_class?: string | null;
  phase?: EventPhase | string | null;
  /** {SYM: {"1m": …, "5m": …, "15m": …, "30m": …, "1h": …}}. */
  reactions?: Record<string, Record<string, ReactionValue>> | null;
  /** {SYM: "bp" | "pct"} when the cells are bare numbers. */
  units?: Record<string, string> | null;
  /** Market confirmation 0-100; null when no surprise direction is known yet. */
  confirmation?: number | null;
  divergence?: boolean | null;
  summary_facts?: string[] | null;
};
/** GET /api/dashboard → event_risk (docs/EVENT_AWARENESS.md §4). */
/** One released scheduled event of `released_today`. */
export type ReleasedEvent = {
  event_id?: string | null;
  name?: string | null;
  /** The scheduled time; `released_utc` (or the document's published_at) is the actual release when sent. */
  time_utc?: string | null;
  released_utc?: string | null;
  /** What was published: the official document's summary, else the calendar actual, else the official series values. */
  summary?: string | null;
  /** "Nonfarm payrolls +29k; Unemployment rate 4.2%": the release's values from the stored official series. */
  values_text?: string | null;
  document?: { title?: string | null; summary?: string | null; published_at?: string | null; event_id?: string | null } | null;
  /** The stored news event carrying the official document. */
  news_event_id?: string | null;
  affected_assets?: (string | EventExposure)[] | null;
  [key: string]: unknown;
};
export type EventRisk = {
  generated_at?: string | null;
  markets_open?: MarketsOpen | null;
  mode?: EventRiskMode | null;
  next_event?: EventObject | null;
  dominant_event?: EventObject | null;
  events?: EventObject[] | null;
  active_events?: EventObject[] | null;
  potential_events?: EventObject[] | null;
  asset_event_risk?: Record<string, AssetEventRisk> | null;
  favored?: MarketViewItem[] | null;
  pressured?: MarketViewItem[] | null;
  non_confirming?: MarketViewItem[] | null;
  what_can_break?: BreakItem[] | null;
  trade_watch?: TradeWatchItem[] | null;
  post_event?: PostEvent[] | null;
  /**
   * Scheduled events released today with the official document that published
   * them (kept to the end of the Bucharest day, after the event leaves
   * `events`). Read defensively (components/dashboard/opening releasedToday);
   * absent on older backends.
   */
  released_today?: ReleasedEvent[] | null;
  /** EVENT DATA INCOMPLETE flags, only for events at the configured levels (CRITICAL / VERY HIGH). */
  data_incomplete?: (string | { event_id?: string; name?: string; level?: string; flag?: string; reasons?: string[] })[] | null;
};
export type SurpriseAssessment =
  | "HAWKISH_SURPRISE"
  | "DOVISH_SURPRISE"
  | "NEUTRAL_PRICED"
  | "UNDETERMINED"
  | "NOT_APPLICABLE";
export type AssetComment = { symbol: string; comment?: string | null };
export type EventExplanation = {
  event_id: string;
  why_it_matters?: string | null;
  bull_case?: string | null;
  bear_case?: string | null;
  neutral_case?: string | null;
  what_is_priced?: string | null;
  what_is_not_priced?: string | null;
  confirmation_after_event?: string | null;
  surprise_assessment?: SurpriseAssessment | string | null;
};
/** MarketInterpretationOutput (backend/schemas.py): text only, never numbers the backend did not supply. */
export type MarketInterpretationOutput = {
  ai_view?: string | null;
  market_summary?: string | null;
  dominant_event?: { event_id: string; reason?: string | null; risk_comment?: string | null } | null;
  favored_assets?: AssetComment[] | null;
  pressured_assets?: AssetComment[] | null;
  non_confirming_assets?: AssetComment[] | null;
  next_events?: EventExplanation[] | null;
  key_risks?: string[] | null;
  trade_watch?: AssetComment[] | null;
};
export type Interpretation = {
  source: "ai" | "deterministic" | string;
  label?: string | null;
  model?: string | null;
  generated_at?: string | null;
  /** Age-based: older than the configured multiple of the refresh cadence (inputs are not compared). */
  stale?: boolean | null;
  /** Age at response time. */
  age_seconds?: number | null;
  /** The age beyond which the backend marks it stale, when it sends one. */
  stale_after_seconds?: number | null;
  basis_hash?: string | null;
  output?: MarketInterpretationOutput | null;
};
/** A notice required by the terms of the providers it names. */
export type DataNotice = {
  id: string;
  text: string;
  providers?: string[];
};
/* --- Asset / event detail contract types (docs/CONTRACTS.md) --- */
export type ComponentDetail = {
  value: number | null;
  status: "measured" | "missing" | string;
  basis?: string;
};
export type DriverContributions = {
  rows: { driver: string; value: number | null; status?: string }[];
  net: number | null;
  unmeasured?: string[];
};
/** One stored daily bar; `time` is the New York session date "YYYY-MM-DD". */
export type DailyBar = {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
};
export type Bar = {
  time: string | number;
  close: number;
  open?: number;
  high?: number;
  low?: number;
  volume?: number;
};
/** Overlay series for the cross-asset chart. */
export type CrossAssetSeries = {
  symbol?: string;
  name?: string;
  bars?: Bar[] | null;
  status?: string;
};
/** `{DXY: Bar[] | {bars, name}}` map, or a list of described series. */
export type CrossAsset =
  Record<string, Bar[] | CrossAssetSeries | null> | CrossAssetSeries[];
export type ComparableOutcome = {
  return_pct?: number | null;
  raw_return_pct?: number | null;
  mfe_pct?: number | null;
  mae_pct?: number | null;
  actionable?: boolean | null;
};
export type ComparableSummary = {
  observations?: number;
  hit_rate?: number | null;
  mean_return_pct?: number | null;
  median_return_pct?: number | null;
};
/** GET /assets/{symbol}/comparables (backend evaluation.comparables). */
export type Comparables = {
  symbol?: string;
  items: {
    decision_id?: string;
    created_at?: string;
    direction?: string;
    category?: string | null;
    score?: number | null;
    outcomes?: Record<string, ComparableOutcome>;
  }[];
  minimum_observations?: number;
  by_horizon?: Record<string, ComparableSummary>;
};
/** AI extractor output stored on the event (backend schemas.Extraction). */
export type Extraction = {
  who?: string[];
  what?: string;
  when?: string;
  where?: string;
  direct_claims?: string[];
  new_information?: string[];
  background_information?: string[];
};
export type Reaction = {
  event_id?: string;
  symbol: string;
  horizon: string;
  baseline_price?: number | null;
  observed_price?: number | null;
  change?: number | null;
  change_pct?: number | null;
  unit?: string;
  expected_direction?: number | null;
  status: string;
  baseline_timestamp?: string;
  observed_timestamp?: string;
};
export type ReactionBasket = {
  event_id?: string;
  horizon: string;
  confirmation_pct: number | null;
  confirmation_level?: string | null;
  confirming?: string[];
  partial?: string[];
  diverging?: string[];
  missing?: string[];
  no_reaction?: string[];
  divergence?: boolean;
  measured?: number;
  expected?: number;
};
export type FactCheckResult = {
  status?: string;
  verified_statements?: string[];
  unverified_statements?: string[];
  contradictions?: string[];
  explanation?: string;
};
