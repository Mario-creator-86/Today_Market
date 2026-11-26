import type { Asset, Dashboard, TradeWatchItem } from "./api";
import { ANALYSIS_STALE_FALLBACK_MS, analysisView, durationText } from "./api";
import type { Tone } from "./event-risk";
import { bucharestDay, bucharestHourMinute, parseTime, timeAgo } from "./event-time";
import { isActionable, isLean } from "./freshness";
import { APP_ZONE } from "./market-data";

/*
 * Trade-watch statuses (docs/EVENT_AWARENESS.md §3): the backend decides the
 * status; the browser can only downgrade it when its own freshness gates fail
 * (a cached status never outlives an expired quote). It never upgrades one.
 * Pure, with relative imports only, so vitest can collect it.
 */

export type StatusKind = "long" | "short" | "conditional-long" | "conditional-short" | "wait-for" | "wait";

/** "conditional_long" → "CONDITIONAL LONG"; missing → "WAIT". */
export function normalizeStatus(value: unknown): string {
  const s = typeof value === "string" ? value.trim().toUpperCase().replaceAll("_", " ").replace(/\s+/g, " ") : "";
  return s || "WAIT";
}

export function statusKind(value: unknown): StatusKind {
  const s = normalizeStatus(value);
  if (/^WAIT FOR\b/.test(s)) return "wait-for";
  if (/^CONDITIONAL LONG\b/.test(s)) return "conditional-long";
  if (/^CONDITIONAL SHORT\b/.test(s)) return "conditional-short";
  if (/^(STRONG )?LONG\b/.test(s)) return "long";
  if (/^(STRONG )?SHORT\b/.test(s)) return "short";
  return "wait";
}

/** LONG green, SHORT red, CONDITIONAL blue, WAIT FOR <EVENT> amber, WAIT quiet. */
export function statusTone(value: unknown): Tone {
  switch (statusKind(value)) {
    case "long":
      return "positive";
    case "short":
      return "negative";
    case "conditional-long":
    case "conditional-short":
      return "info";
    case "wait-for":
      return "neutral";
    default:
      return "outline";
  }
}

/** "WAIT FOR FED" → "FED"; null for other statuses. */
export function waitForEvent(value: unknown): string | null {
  const m = normalizeStatus(value).match(/^WAIT FOR (.+)$/);
  return m ? m[1].trim() : null;
}

function side(label: string): number {
  return /\b(LONG|BULLISH)\b/.test(label) ? 1 : /\b(SHORT|BEARISH)\b/.test(label) ? -1 : 0;
}

export type EffectiveStatus = {
  status: string;
  /** The backend status, when the browser held it back. */
  heldBack: string | null;
  reason: string | null;
};

/** A fresh, live quote after `currentAsset` (daily closes and stale quotes never count). */
function liveQuote(asset: Asset | undefined): boolean {
  return asset?.quote?.freshness_status === "FRESH" && asset.quote.market_status === "LIVE";
}

/**
 * The status to show for a trade-watch item, given the asset already passed
 * through `currentAsset`. WAIT and WAIT FOR <EVENT> are shown as sent. LONG /
 * SHORT need an actionable published decision on the same side; CONDITIONAL
 * needs a fresh live quote and an unblocked setup or lean on the same side.
 * Otherwise the result is WAIT, with the reason.
 */
export function effectiveStatus(item: Pick<TradeWatchItem, "trade_status" | "awaiting_reopen">, asset: Asset | undefined): EffectiveStatus {
  const status = normalizeStatus(item.trade_status);
  const kind = statusKind(status);
  if (kind === "wait" || kind === "wait-for") return { status, heldBack: null, reason: null };
  const hold = (reason: string): EffectiveStatus => ({ status: "WAIT", heldBack: status, reason });
  if (item.awaiting_reopen) return hold("Market closed — awaiting reopen before this can be confirmed.");
  if (!asset?.quote) return hold("No quote delivered for this asset.");
  if (!liveQuote(asset))
    return hold(asset.decision?.reasons?.[0] || "Quote not live. Wait for a fresh market observation.");
  if (kind === "long" || kind === "short") {
    const direction = asset.decision?.direction ?? "";
    if (!isActionable(asset) || side(direction) !== side(status))
      return hold("The published decision is not actionable on this side.");
  } else if (
    asset.decision?.audit_status === "BLOCK" ||
    !(isActionable(asset) || isLean(asset)) ||
    side(asset.decision?.direction ?? "") !== side(status)
  ) {
    return hold("The published decision no longer supports this conditional direction.");
  }
  return { status, heldBack: null, reason: null };
}

// --- decision age ---------------------------------------------------------------------------

/**
 * The full analysis cycle as the decision-age rule reads it. Built from the
 * same `analysisView` the header uses, so the cards and the header never
 * disagree about whether the analysis is out of date.
 */
export type AnalysisClock = {
  /** The backend sent its `analysis` record; without it only the header's fallback age applies. */
  recorded: boolean;
  /** The header's verdict: no successful cycle within the backend's limit, or the last attempt failed. */
  stale: boolean;
  /** The header's plain-English reason when stale. */
  reason: string | null;
  /** Start of the last successful cycle (ms); every decision that cycle published carries exactly this time. */
  lastSuccess: number | null;
};

/** The analysis clock of a dashboard payload (its `analysis` record, else `last_analysis`). */
export function analysisClock(
  dashboard: Pick<Dashboard, "last_analysis" | "analysis"> | null | undefined,
  now: number,
): AnalysisClock {
  const record = dashboard?.analysis;
  const view = analysisView(dashboard, now);
  const recorded = !!record && typeof record === "object";
  return { recorded, stale: view.stale, reason: view.reason, lastSuccess: recorded ? parseTime(view.at) : null };
}

export type DecisionAge = {
  /** The decision time as sent (for <time dateTime>). */
  at: string;
  /** "Decided 02:10 (5 h 11 min ago)"; with the day when it is not today in Bucharest: "Decided Sat 26 Sep 23:10 (1 day ago)". */
  text: string;
  /** Why it is flagged, for a tooltip. */
  reason: string;
};

/**
 * The published decision's time and age when it may be out of date: the
 * header calls the analysis stale (its reason is reused), or the last
 * successful cycle did not replace this asset's decision (a cycle stamps every
 * decision with its own start time, the time it records as its success, so an
 * older decision missed it). A decision from the last successful cycle stays
 * quiet while the next cycle runs, however long that takes. Without the
 * backend's record, the header's fallback age applies. Null for a missing,
 * invalid or future time.
 */
export function decisionAge(createdAt: unknown, now: number, clock?: AnalysisClock | null): DecisionAge | null {
  const t = parseTime(createdAt);
  if (t === null || typeof createdAt !== "string" || t > now) return null;
  let reason: string;
  if (clock?.recorded) {
    if (clock.stale) reason = clock.reason ?? "The full analysis is out of date; signals may be out of date.";
    else if (clock.lastSuccess !== null && t < clock.lastSuccess)
      reason = `The last full analysis (${bucharestHourMinute(clock.lastSuccess)} ${APP_ZONE}) published no new decision for this asset; this one is older.`;
    else return null;
  } else {
    if (now - t <= ANALYSIS_STALE_FALLBACK_MS) return null;
    reason = `No newer decision for more than ${durationText(ANALYSIS_STALE_FALLBACK_MS / 1000)}; it may be out of date.`;
  }
  const day = bucharestDay(t) === bucharestDay(now) ? "" : `${bucharestDay(t)} `;
  return { at: createdAt, text: `Decided ${day}${bucharestHourMinute(t)} (${timeAgo(now - t)})`, reason };
}
