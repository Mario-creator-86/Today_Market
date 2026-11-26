import type { Asset } from "./api";
import { isDailyClass, isLiveData } from "./market-data";

const actionableDirections = new Set([
  "LONG",
  "SHORT",
  "STRONG LONG",
  "STRONG SHORT",
]);

export function isActionable(asset: Asset): boolean {
  return (
    !!asset.decision &&
    actionableDirections.has(asset.decision.direction) &&
    asset.decision.audit_status === "PASS"
  );
}

function holdDecision(asset: Asset, reason: string): Asset {
  if (!asset.decision) return asset;
  return {
    ...asset,
    decision: {
      ...asset.decision,
      direction: "WAIT",
      category: "WAIT / DATA INSUFFICIENT",
      audit_status: "BLOCK",
      blocks: [...new Set([reason, ...(Array.isArray(asset.decision.blocks) ? asset.decision.blocks : [])])],
      reasons: [...new Set([reason, ...(Array.isArray(asset.decision.reasons) ? asset.decision.reasons : [])])],
    },
  };
}

/**
 * A quote may be stamped this far after the page clock and still count as
 * current: the clock ticks once a second, so a streamed quote is often newer
 * than its last tick. The service's own limit (max_future_seconds) is 5 s.
 */
const FUTURE_TOLERANCE_MS = 5000;

/**
 * Re-evaluate cached observations without modifying the query cache. Only a
 * fresh live quote in a live session keeps a decision; daily closes and
 * proxies never do. A valid quote cannot extend a decision's own deadline or override its invalidation level.
 * A quote that aged past the threshold gets freshness_status STALE; its
 * market_status (the venue's session state) is left as the service sent it.
 */
export function currentAsset(
  asset: Asset,
  now: number,
  thresholdSeconds = 15,
): Asset {
  const threshold =
    asset.quote?.freshness_threshold_seconds ??
    asset.freshness_threshold_seconds ??
    thresholdSeconds;
  const ttl = Number.isFinite(threshold) && threshold > 0 ? threshold : 15;
  const timestamp = asset.quote ? Date.parse(asset.quote.timestamp) : NaN;
  const age = now - timestamp;
  const liveData = isLiveData(asset.quote);
  // A daily close is never a fresh observation (§24), whatever it is labelled.
  const fresh =
    Number.isFinite(timestamp) &&
    age >= -FUTURE_TOLERANCE_MS &&
    age <= ttl * 1000 &&
    asset.quote?.freshness_status === "FRESH" &&
    liveData;
  const live = asset.quote?.market_status === "LIVE";
  if (fresh && live) {
    const d = asset.decision;
    const direction = d?.direction ?? "";
    const side = /\b(LONG|BULLISH)\b/.test(direction) ? 1 : /\b(SHORT|BEARISH)\b/.test(direction) ? -1 : 0;
    if (side && d?.expires_at != null) {
      const deadline = Date.parse(d.expires_at);
      if (!Number.isFinite(deadline)) return holdDecision(asset, "Decision expiry unavailable; awaiting recalculation");
      if (now > deadline) return holdDecision(asset, "Decision expired; awaiting recalculation");
    }
    const invalidation = d?.invalidation_level;
    if (side && typeof invalidation === "number" && Number.isFinite(invalidation)
      && (asset.quote!.price - invalidation) * side <= 0) {
      return holdDecision(asset, "Price invalidation crossed");
    }
    return asset;
  }
  const reason = !asset.quote
    ? asset.proxy
      ? "No level source; only a direction proxy. Setups need a live level."
      : "No quote from any source. Setups need a live level."
    : fresh
      ? "Market is not live. Wait for an open market and fresh observations."
      : isDailyClass(asset.quote)
        ? "Only a daily close is available. Setups require a fresh live observation."
        : !liveData
          ? "Non-live quote cannot confirm a decision"
          : "Quote freshness expired. Wait for a fresh market observation.";
  // Only the freshness verdict is downgraded: `market_status` stays the venue's
  // session state (LIVE, LAST CLOSE, …), which never depends on the quote's age.
  return holdDecision({
    ...asset,
    quote: asset.quote
      ? { ...asset.quote, freshness_status: fresh ? asset.quote.freshness_status : "STALE" }
      : null,
  }, reason);
}

type Timed = { timestamp?: string | null };

/** The later of two observations by their own timestamp; the first one on a tie or without a valid time. */
function later<T extends Timed>(first: T | null | undefined, second: T | null | undefined): T | null {
  if (!first) return second ?? null;
  if (!second) return first;
  const a = Date.parse(first.timestamp ?? "");
  const b = Date.parse(second.timestamp ?? "");
  return Number.isFinite(b) && (!Number.isFinite(a) || b > a) ? second : first;
}

/**
 * The asset page's record with the newest quote and proxy the app holds: the
 * page reloads its full record (bars, levels, events) every few tens of
 * seconds, while the dashboard cache the workspace keeps current (stream and
 * polling) carries the same quote and proxy objects, often newer. Each is
 * taken from the dashboard only when it is at least as new; a quote the
 * dashboard does not carry is never dropped. A newer published decision, or
 * a read-time downgrade of the same decision, takes effect immediately with
 * its event overlay. The detail record retains its bars and other history.
 */
export function withLiveQuotes<T extends Asset>(
  asset: T,
  live: (Pick<Asset, "quote" | "proxy"> & Partial<Pick<Asset, "decision" | "event_risk">>) | null | undefined,
): T {
  if (!live) return asset;
  const quote = later(live.quote, asset.quote);
  const proxy = later(live.proxy, asset.proxy);
  const own = asset.decision;
  const incoming = live.decision;
  const at = (decision: Asset["decision"] | undefined) => {
    const created = Date.parse(decision?.created_at ?? "");
    return Number.isFinite(created) ? created : Date.parse(decision?.decided_at ?? "");
  };
  const ownAt = at(own);
  const incomingAt = at(incoming);
  const newer = !!incoming && Number.isFinite(incomingAt) && (!Number.isFinite(ownAt) || incomingAt > ownAt);
  const same = !!own && !!incoming && !!own.id && own.id === incoming.id;
  // Quote-only frames keep the dashboard's original decision. They must not
  // revive a same-id decision the newer detail response already blocked.
  const downgrade = same && (incoming.audit_status === "BLOCK" || (incoming.direction === "WAIT" && own.direction !== "WAIT"));
  const decision = newer ? incoming : downgrade ? { ...own, ...incoming } : own;
  const overlayCurrent = newer || (same && (own.audit_status !== "BLOCK" || incoming.audit_status === "BLOCK"));
  const eventRisk = overlayCurrent && (newer || Object.hasOwn(live, "event_risk"))
    ? live.event_risk ?? null
    : asset.event_risk;
  return quote === asset.quote && proxy === (asset.proxy ?? null) && decision === own && eventRisk === asset.event_risk
    ? asset
    : { ...asset, quote, proxy, decision, event_risk: eventRisk };
}

/** Decision fields the timing checks read (backend decision payload). */
export type DecisionTimingFields = {
  created_at?: string | null;
  decided_at?: string | null;
  /** Quote price when the decision was made. */
  price?: number | null;
  /** Levels at decision time. */
  levels?: { support?: number | null; resistance?: number | null } | null;
};

export type DecisionAge = {
  /** When the decision was made (decided_at, else created_at). */
  at: string;
  ageMs: number;
  /** Flagged by the dashboard's analysis rule (lib/trade-watch decisionAge): a newer cycle should have replaced it. */
  stale: boolean;
};

/**
 * Age of a published decision; null when it carries no valid time. `stale`
 * is the caller's verdict from the same rule as the dashboard cards (the
 * service's analysis status, lib/trade-watch decisionAge), so the two pages
 * never disagree about one decision; unknown (the dashboard has not answered)
 * means not stale.
 */
export function decisionAge(
  decision: DecisionTimingFields | null | undefined,
  now: number,
  stale: boolean | null | undefined,
): DecisionAge | null {
  const at = [decision?.decided_at, decision?.created_at].find(
    (v): v is string => typeof v === "string" && Number.isFinite(Date.parse(v)),
  );
  if (!at) return null;
  return { at, ageMs: Math.max(0, now - Date.parse(at)), stale: stale === true };
}

/** "just now", "12 min ago", "1 h 20 min ago", "5 h ago", "2 days ago". */
export function ageText(ms: number): string {
  if (!Number.isFinite(ms) || ms < 60_000) return "just now";
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 3 && minutes % 60) return `${hours} h ${minutes % 60} min ago`;
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

export type PriceSinceDecision = {
  /** Quote price when the decision was made. */
  decisionPrice: number;
  /** Change of the current level since then, in percent. */
  changePct: number;
  /** The same change in basis points of the level: for a yield quoted in percent (4.21 -> 4.25 is +4 bp). */
  changeBp: number;
  /** The current level is outside the support-resistance range the decision saw. */
  leftRange: boolean;
  support: number | null;
  resistance: number | null;
};

/** How far the current level moved since the decision; null without both prices. */
export function priceSinceDecision(
  decision: DecisionTimingFields | null | undefined,
  price: number | null | undefined,
): PriceSinceDecision | null {
  const then = decision?.price;
  if (typeof then !== "number" || !Number.isFinite(then) || then <= 0) return null;
  if (typeof price !== "number" || !Number.isFinite(price)) return null;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const support = num(decision?.levels?.support);
  const resistance = num(decision?.levels?.resistance);
  return {
    decisionPrice: then,
    changePct: (price / then - 1) * 100,
    changeBp: (price - then) * 100,
    leftRange: (support !== null && price < support) || (resistance !== null && price > resistance),
    support,
    resistance,
  };
}

/** WAIT with a directional lean, e.g. "BULLISH LEAN / WAIT" (after local freshness checks). */
export function isLean(asset: Asset): boolean {
  return !!asset.decision && /\bLEAN\b/.test(asset.decision.direction);
}

function magnitude(asset: Asset): number {
  const score = asset.decision?.score;
  return typeof score === "number" && Number.isFinite(score)
    ? Math.abs(score)
    : -1;
}

function evidence(asset: Asset): number {
  const value = asset.decision?.evidence_confidence;
  return typeof value === "number" && Number.isFinite(value) ? value : -1;
}

/** Strongest first: |decision score| descending, evidence confidence as tie-break. */
export function rankByStrength(assets: Asset[]): Asset[] {
  return [...assets].sort(
    (a, b) => magnitude(b) - magnitude(a) || evidence(b) - evidence(a),
  );
}

/** Today's best setups: actionable, ranked by strength, at most `limit`. */
export function bestSetups(assets: Asset[], limit = 5): Asset[] {
  return rankByStrength(assets.filter(isActionable)).slice(0, limit);
}

/** WAIT-with-lean candidates shown separately from actionable setups. */
export function leanSetups(assets: Asset[], limit = 4): Asset[] {
  return rankByStrength(assets.filter(isLean)).slice(0, limit);
}
