import { describe, expect, it } from "vitest";
import type { Asset } from "./api";
import { currentAsset } from "./freshness";
import { ANALYSIS_STALE_FALLBACK_MS } from "./api";
import {
  analysisClock,
  decisionAge,
  effectiveStatus,
  normalizeStatus,
  statusKind,
  statusTone,
  waitForEvent,
} from "./trade-watch";

const NOW = Date.parse("2026-09-26T12:00:00Z");
const btc = (direction = "LONG", extra: Partial<Asset> = {}): Asset => ({
  symbol: "BTC",
  name: "Bitcoin",
  asset_class: "crypto",
  quote: {
    price: 65000,
    timestamp: new Date(NOW).toISOString(),
    provider: "test",
    market_status: "LIVE",
    freshness_status: "FRESH",
    change_pct: 1,
  },
  decision: {
    id: "d",
    direction,
    score: 54,
    evidence_confidence: 91,
    market_confirmation: 63,
    data_quality: 100,
    reasons: ["Verified evidence"],
    confirmation: "c",
    invalidation: "i",
    audit_status: "PASS",
    components: {},
  },
  ...extra,
});

describe("status labels and tones", () => {
  it("normalizes and classifies", () => {
    expect(normalizeStatus("conditional_long")).toBe("CONDITIONAL LONG");
    expect(normalizeStatus(null)).toBe("WAIT");
    expect(statusKind("WAIT FOR FED")).toBe("wait-for");
    expect(statusKind("CONDITIONAL SHORT")).toBe("conditional-short");
    expect(statusKind("LONG")).toBe("long");
    expect(statusKind("SHORT")).toBe("short");
    expect(statusKind("WAIT")).toBe("wait");
    expect(waitForEvent("WAIT FOR FED")).toBe("FED");
    expect(waitForEvent("WAIT")).toBeNull();
  });
  it("never colours a conditional status like a confirmed one", () => {
    expect(statusTone("LONG")).toBe("positive");
    expect(statusTone("SHORT")).toBe("negative");
    expect(statusTone("CONDITIONAL LONG")).toBe("info");
    expect(statusTone("CONDITIONAL SHORT")).toBe("info");
    expect(statusTone("WAIT FOR FED")).toBe("neutral");
    expect(statusTone("WAIT")).toBe("outline");
  });
});

describe("local downgrade only", () => {
  it("keeps an actionable LONG with a fresh live quote", () => {
    expect(effectiveStatus({ trade_status: "LONG" }, currentAsset(btc(), NOW))).toEqual({
      status: "LONG",
      heldBack: null,
      reason: null,
    });
  });
  it("a stale quote turns LONG and CONDITIONAL into WAIT with the reason", () => {
    const stale = currentAsset(btc(), NOW + 60_000);
    const long = effectiveStatus({ trade_status: "LONG" }, stale);
    expect(long.status).toBe("WAIT");
    expect(long.heldBack).toBe("LONG");
    expect(long.reason).toMatch(/freshness expired/);
    expect(effectiveStatus({ trade_status: "CONDITIONAL LONG" }, stale).status).toBe("WAIT");
  });
  it("CONDITIONAL never becomes LONG locally, and LONG needs a matching actionable decision", () => {
    const fresh = currentAsset(btc(), NOW);
    expect(effectiveStatus({ trade_status: "CONDITIONAL LONG" }, fresh).status).toBe("CONDITIONAL LONG");
    expect(effectiveStatus({ trade_status: "SHORT" }, fresh).status).toBe("WAIT");
    const lean = currentAsset(btc("BULLISH LEAN / WAIT"), NOW);
    expect(effectiveStatus({ trade_status: "LONG" }, lean).status).toBe("WAIT");
  });
  it("WAIT FOR <EVENT> and WAIT are shown as sent", () => {
    const stale = currentAsset(btc(), NOW + 60_000);
    expect(effectiveStatus({ trade_status: "WAIT FOR FED" }, stale).status).toBe("WAIT FOR FED");
    expect(effectiveStatus({ trade_status: "WAIT" }, undefined).status).toBe("WAIT");
  });
  it("closed markets and missing quotes hold a side back", () => {
    const fresh = currentAsset(btc(), NOW);
    const closed = effectiveStatus({ trade_status: "CONDITIONAL SHORT", awaiting_reopen: true }, fresh);
    expect(closed).toMatchObject({ status: "WAIT", heldBack: "CONDITIONAL SHORT" });
    expect(closed.reason).toMatch(/awaiting reopen/);
    expect(effectiveStatus({ trade_status: "LONG" }, undefined).status).toBe("WAIT");
  });
  it.each(["WAIT", "SHORT", "BEARISH LEAN / WAIT"])("rejects a cached CONDITIONAL LONG when the decision is %s", (direction) => {
    expect(effectiveStatus({ trade_status: "CONDITIONAL LONG" }, currentAsset(btc(direction), NOW)).status).toBe("WAIT");
  });
  it("requires an unblocked decision and preserves valid conditional leans", () => {
    const lean = btc("BULLISH LEAN / WAIT");
    lean.decision!.audit_status = "NOT_REQUIRED";
    expect(effectiveStatus({ trade_status: "CONDITIONAL LONG" }, lean).status).toBe("CONDITIONAL LONG");
    lean.decision!.audit_status = "BLOCK";
    expect(effectiveStatus({ trade_status: "CONDITIONAL LONG" }, lean).status).toBe("WAIT");
    expect(effectiveStatus({ trade_status: "CONDITIONAL LONG" }, btc("WAIT", { decision: null })).status).toBe("WAIT");
  });
  it("keeps the category, audit and block reasons consistent after local expiry", () => {
    const asset = btc();
    asset.decision!.category = "LONG SETUP";
    const stale = currentAsset(asset, NOW + 60_000);
    expect(stale.decision).toMatchObject({ direction: "WAIT", category: "WAIT / DATA INSUFFICIENT", audit_status: "BLOCK" });
    expect(stale.decision!.blocks).toContain(stale.decision!.reasons[0]);
    expect(asset.decision!.category).toBe("LONG SETUP");
  });
});

describe("decision age", () => {
  // 07:22 Bucharest on Mon 28 Sep; the last successful cycle started at 02:10 Bucharest (23:10 UTC the day before).
  const now = Date.parse("2026-09-28T04:22:00Z");
  const decided = "2026-09-27T23:10:49Z";
  const record = (extra: Record<string, unknown> = {}) => ({
    last_analysis: decided,
    analysis: { last_success_at: decided, last_attempt_at: decided, last_error_type: null, cadence_seconds: 300, stale: false, ...extra },
  });
  const at = (msAgo: number) => new Date(now - msAgo).toISOString();

  it("reads the header's analysis verdict and the last successful cycle", () => {
    const stale = analysisClock(record({ stale: true }), now);
    expect(stale).toMatchObject({ recorded: true, stale: true, lastSuccess: Date.parse(decided) });
    expect(stale.reason).toMatch(/No full analysis has completed since then/);
    expect(analysisClock(record(), now)).toEqual({ recorded: true, stale: false, reason: null, lastSuccess: Date.parse(decided) });
    // An older backend: no record, only last_analysis.
    expect(analysisClock({ last_analysis: decided, analysis: null }, now)).toMatchObject({ recorded: false, lastSuccess: null });
    expect(analysisClock(undefined, now)).toMatchObject({ recorded: false, stale: false, lastSuccess: null });
  });

  it("a decision from the last successful cycle stays quiet while the next cycle runs", () => {
    // The cycle started 8 min ago (more than one 5-minute cadence) and is still publishing: not out of date.
    const running = record({ last_success_at: at(8 * 60_000), last_attempt_at: at(8 * 60_000) });
    expect(decisionAge(at(8 * 60_000), now, analysisClock(running, now))).toBeNull();
    // A decision the running cycle already replaced is newer than the last success: quiet too.
    expect(decisionAge(at(60_000), now, analysisClock(running, now))).toBeNull();
  });

  it("flags a decision the last successful cycle did not replace, in Bucharest time", () => {
    const clock = analysisClock(record({ last_success_at: at(2 * 60_000) }), now);
    const age = decisionAge(decided, now, clock);
    expect(age).toMatchObject({ at: decided, text: "Decided 02:10 (5 h 11 min ago)" });
    expect(age?.reason).toBe("The last full analysis (07:20 Bucharest) published no new decision for this asset; this one is older.");
    expect(decisionAge("2026-09-27T21:10:00Z", now, clock)?.text).toBe("Decided 00:10 (7 h ago)");
    // An earlier Bucharest day carries the date.
    expect(decisionAge("2026-09-26T20:10:00Z", now, clock)?.text).toBe("Decided Sat 26 Sep 23:10 (1 day ago)");
    expect(decisionAge("2026-09-25T20:10:00Z", now, clock)?.text).toBe("Decided Fri 25 Sep 23:10 (2 days ago)");
  });

  it("while the header calls the analysis stale, every decision says when it was made, with the header's reason", () => {
    const failed = analysisClock(record({ last_attempt_at: at(3 * 60_000), last_error_type: "RuntimeError", stale: true }), now);
    const age = decisionAge(decided, now, failed);
    expect(age?.text).toBe("Decided 02:10 (5 h 11 min ago)");
    expect(age?.reason).toBe(failed.reason);
    expect(age?.reason).toMatch(/failed; signals are from the last successful run/);
  });

  it("stays quiet for missing, invalid or future times", () => {
    const clock = analysisClock(record({ stale: true }), now);
    expect(decisionAge(undefined, now, clock)).toBeNull();
    expect(decisionAge("not a time", now, clock)).toBeNull();
    expect(decisionAge(at(-60_000), now, clock)).toBeNull();
  });

  it("without the backend's record, uses the header's fallback age", () => {
    expect(ANALYSIS_STALE_FALLBACK_MS).toBe(15 * 60_000);
    expect(decisionAge(at(10 * 60_000), now, null)).toBeNull();
    expect(decisionAge(at(15 * 60_000), now, undefined)).toBeNull();
    const old = decisionAge(at(20 * 60_000), now, analysisClock({ last_analysis: decided }, now));
    expect(old?.text).toBe("Decided 07:02 (20 min ago)");
    expect(old?.reason).toBe("No newer decision for more than 15 min; it may be out of date.");
  });
});
