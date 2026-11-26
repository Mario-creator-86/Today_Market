import { describe, expect, it } from "vitest";
import type { Asset } from "./api";
import {
  ageText,
  bestSetups,
  currentAsset,
  decisionAge,
  isActionable,
  isLean,
  leanSetups,
  priceSinceDecision,
  withLiveQuotes,
} from "./freshness";

const timestamp = Date.parse("2026-09-26T10:00:00Z");
const asset: Asset = {
  symbol: "BTC",
  name: "Bitcoin",
  asset_class: "crypto",
  quote: {
    price: 100,
    timestamp: new Date(timestamp).toISOString(),
    provider: "test",
    market_status: "LIVE",
    freshness_status: "FRESH",
    change_pct: null,
  },
  decision: {
    id: "decision",
    direction: "LONG",
    score: 70,
    evidence_confidence: 90,
    market_confirmation: 90,
    data_quality: 100,
    reasons: ["Verified test evidence"],
    confirmation: "test",
    invalidation: "test",
    audit_status: "PASS",
    components: {},
  },
};
describe("locally enforced decision gates", () => {
  it.each(["LONG", "SHORT", "BULLISH LEAN / WAIT", "BEARISH LEAN / WAIT"])(
    "expires %s even while streamed quotes stay fresh",
    (direction) => {
      const cached = { ...asset, decision: { ...asset.decision!, direction, expires_at: new Date(timestamp).toISOString() } };
      expect(currentAsset(cached, timestamp).decision?.direction).toBe(direction);
      const expired = currentAsset(cached, timestamp + 1);
      expect(expired.decision?.direction).toBe("WAIT");
      expect(expired.decision?.blocks).toContain("Decision expired; awaiting recalculation");
      expect(expired.quote?.freshness_status).toBe("FRESH");
      expect(cached.decision.direction).toBe(direction);
    },
  );
  it.each<[string, number, number]>([
    ["LONG", 90, 90], ["LONG", 89, 90], ["SHORT", 110, 110], ["SHORT", 111, 110],
    ["BULLISH LEAN / WAIT", 89, 90], ["BEARISH LEAN / WAIT", 111, 110],
  ])("invalidates %s immediately when the current price %s crosses %s", (direction, price, level) => {
    const cached = { ...asset, quote: { ...asset.quote!, price },
      decision: { ...asset.decision!, direction, invalidation_level: level } };
    const invalidated = currentAsset(cached, timestamp);
    expect(invalidated.decision?.direction).toBe("WAIT");
    expect(invalidated.decision?.blocks).toContain("Price invalidation crossed");
    expect(invalidated.quote).toBe(cached.quote);
  });
  it.each([
    { stub: true }, { provider: "feed-stub" }, { data_class: "PROXY" },
    { data_class: "UNRECOGNIZED" }, { data_class: "LIVE", daily_close: true },
  ])("never keeps directional guidance from ineligible live metadata %o", (metadata) => {
    const shown = currentAsset({ ...asset, quote: { ...asset.quote!, ...metadata } }, timestamp);
    expect(shown.decision?.direction).toBe("WAIT");
    expect(shown.quote?.market_status).toBe("LIVE"); // Venue state stays separate from ineligible data.
  });
  it("preserves a valid setup and holds malformed expiry metadata at WAIT", () => {
    const valid = { ...asset, decision: { ...asset.decision!, invalidation_level: 90,
      expires_at: new Date(timestamp + 1000).toISOString() } };
    expect(currentAsset(valid, timestamp)).toBe(valid);
    expect(currentAsset({ ...valid, decision: { ...valid.decision, expires_at: "invalid" } }, timestamp).decision?.direction).toBe("WAIT");
  });
  it("expires cached prices and decisions even when no new snapshot arrives", () => {
    expect(isActionable(currentAsset(asset, timestamp + 14000))).toBe(true);
    const expired = currentAsset(asset, timestamp + 16000);
    expect(expired.quote?.freshness_status).toBe("STALE");
    // The venue is still open: only the freshness verdict changes (MD-2).
    expect(expired.quote?.market_status).toBe("LIVE");
    expect(expired.decision?.direction).toBe("WAIT");
    expect(isActionable(expired)).toBe(false);
    expect(asset.decision?.direction).toBe("LONG");
  });
  it("keeps a quote stamped just after the page clock's last tick fresh, and refuses a far-future stamp", () => {
    // The page clock ticks once a second, so a streamed quote can be newer than `now`.
    const justAhead = currentAsset(asset, timestamp - 900);
    expect(justAhead).toBe(asset);
    expect(isActionable(currentAsset(asset, timestamp - 5000))).toBe(true);
    const farAhead = currentAsset(asset, timestamp - 5001);
    expect(farAhead.quote?.freshness_status).toBe("STALE");
    expect(farAhead.decision?.direction).toBe("WAIT");
  });
  it("honors provider freshness thresholds and refuses invalid timestamps", () => {
    expect(currentAsset(asset, timestamp + 4000, 3).decision?.direction).toBe(
      "WAIT",
    );
    expect(
      currentAsset(
        { ...asset, freshness_threshold_seconds: 30 },
        timestamp + 16000,
      ).decision?.direction,
    ).toBe("LONG");
    expect(
      currentAsset(
        { ...asset, quote: { ...asset.quote!, timestamp: "invalid" } },
        timestamp,
      ).decision?.direction,
    ).toBe("WAIT");
  });
  it("keeps the session badge for a current official close but still blocks setups", () => {
    const close: Asset = {
      ...asset,
      symbol: "US2Y",
      quote: {
        ...asset.quote!,
        timestamp: "2026-09-25T20:00:00Z",
        freshness_status: "STALE",
        data_class: "DAILY_CLOSE",
        close_status: "CURRENT",
      },
    };
    const shown = currentAsset(close, timestamp);
    expect(shown.quote?.market_status).toBe("LIVE");
    expect(shown.decision?.direction).toBe("WAIT");
    expect(shown.decision?.reasons[0]).toMatch(/daily close/);
    expect(isActionable(shown)).toBe(false);
    // A late close is still flagged.
    const late = currentAsset(
      { ...close, quote: { ...close.quote!, close_status: "STALE" } },
      timestamp,
    );
    expect(late.quote?.market_status).toBe("LIVE");
    expect(late.quote?.freshness_status).toBe("STALE");
  });
  it("says why there is no setup when there is no quote at all", () => {
    const proxyOnly = currentAsset(
      {
        ...asset,
        symbol: "GOLD",
        quote: null,
        proxy: {
          symbol: "PAXG",
          price: 3772.5,
          timestamp: new Date(timestamp).toISOString(),
          provider: "binance",
        },
      },
      timestamp,
    );
    expect(proxyOnly.decision?.direction).toBe("WAIT");
    expect(proxyOnly.decision?.reasons[0]).toBe(
      "No level source; only a direction proxy. Setups need a live level.",
    );
    expect(proxyOnly.decision?.reasons[0]).not.toMatch(/freshness expired/);
    const none = currentAsset({ ...asset, quote: null, proxy: null }, timestamp);
    expect(none.decision?.reasons[0]).toBe(
      "No quote from any source. Setups need a live level.",
    );
  });
  it("never treats a daily close as fresh, even when mislabelled", () => {
    const mislabelled: Asset = {
      ...asset,
      quote: { ...asset.quote!, data_class: "DAILY_CLOSE" },
    };
    expect(isActionable(currentAsset(mislabelled, timestamp))).toBe(false);
  });
  it("requires an exact actionable direction and a passing audit", () => {
    for (const direction of [
      "BULLISH LEAN / WAIT",
      "BEARISH LEAN / WAIT",
      "WAIT",
      "LONG_PENDING",
    ]) {
      expect(
        isActionable({ ...asset, decision: { ...asset.decision!, direction } }),
      ).toBe(false);
    }
    expect(
      isActionable({
        ...asset,
        decision: { ...asset.decision!, audit_status: "PENDING" },
      }),
    ).toBe(false);
    for (const direction of ["LONG", "SHORT", "STRONG LONG", "STRONG SHORT"]) {
      expect(
        isActionable({ ...asset, decision: { ...asset.decision!, direction } }),
      ).toBe(true);
    }
  });
});

describe("dashboard setup ranking", () => {
  const make = (
    symbol: string,
    direction: string,
    score: number,
    ev = 50,
  ): Asset => ({
    ...asset,
    symbol,
    decision: { ...asset.decision!, direction, score, evidence_confidence: ev },
  });
  it("ranks actionable setups by absolute score and caps the list", () => {
    const list = [
      make("A", "LONG", 40),
      make("B", "SHORT", -90),
      make("C", "STRONG LONG", 70),
      make("D", "LONG", 40, 95),
      make("E", "BULLISH LEAN / WAIT", 99),
    ];
    expect(bestSetups(list, 3).map((a) => a.symbol)).toEqual(["B", "C", "D"]);
  });
  it("keeps lean signals out of setups and lists them separately", () => {
    const list = [make("A", "LONG", 60), make("L", "BEARISH LEAN / WAIT", -30)];
    expect(bestSetups(list).map((a) => a.symbol)).toEqual(["A"]);
    expect(leanSetups(list).map((a) => a.symbol)).toEqual(["L"]);
    expect(isLean(make("W", "WAIT", 0))).toBe(false);
  });
  it("drops a lean once the quote expires", () => {
    const lean = make("L", "BULLISH LEAN / WAIT", 30);
    expect(isLean(currentAsset(lean, timestamp + 16000))).toBe(false);
  });
});

describe("decision age", () => {
  const decided = "2026-09-26T09:00:00Z";
  const at = Date.parse(decided);
  it("dates the decision and carries the dashboard's stale verdict", () => {
    const fresh = decisionAge({ created_at: decided }, at + 9 * 60_000, false);
    expect(fresh).toEqual({ at: decided, ageMs: 9 * 60_000, stale: false });
    expect(decisionAge({ created_at: decided }, at + 11 * 60_000, true)?.stale).toBe(true);
    // decided_at wins over created_at.
    expect(
      decisionAge({ created_at: decided, decided_at: "2026-09-26T09:00:30Z" }, at + 60_000, false)?.at,
    ).toBe("2026-09-26T09:00:30Z");
  });
  it("never flags a decision while the verdict is unknown, and never invents a time", () => {
    // Five hours old, but the dashboard has not answered yet.
    const old = decisionAge({ created_at: decided }, at + 5 * 3_600_000, null);
    expect(old).toEqual({ at: decided, ageMs: 5 * 3_600_000, stale: false });
    expect(decisionAge({ created_at: decided }, at + 5 * 3_600_000, undefined)?.stale).toBe(false);
    expect(decisionAge({ created_at: "bad" }, at, true)).toBeNull();
    expect(decisionAge(null, at, true)).toBeNull();
  });
  it("words the age", () => {
    expect(ageText(30_000)).toBe("just now");
    expect(ageText(12 * 60_000)).toBe("12 min ago");
    expect(ageText(80 * 60_000)).toBe("1 h 20 min ago");
    expect(ageText(5 * 3_600_000 + 7 * 60_000)).toBe("5 h ago");
    expect(ageText(50 * 3_600_000)).toBe("2 days ago");
  });
  it("measures the move since the decision and flags a price outside its range", () => {
    const d = { price: 84166, levels: { support: 84135.78, resistance: 84342 } };
    const moved = priceSinceDecision(d, 83442);
    expect(moved?.decisionPrice).toBe(84166);
    expect(moved?.changePct).toBeCloseTo(-0.8602, 3);
    expect(moved?.leftRange).toBe(true);
    // A yield quoted in percent: 4.21 -> 4.25 is +4 bp (a +0.95% relative change).
    const yieldMove = priceSinceDecision({ price: 4.21 }, 4.25);
    expect(yieldMove?.changeBp).toBeCloseTo(4, 6);
    expect(yieldMove?.changePct).toBeCloseTo(0.95, 2);
    expect(priceSinceDecision(d, 84200)?.leftRange).toBe(false);
    expect(priceSinceDecision({ price: null }, 84200)).toBeNull();
    expect(priceSinceDecision(d, undefined)).toBeNull();
  });
});

describe("live quotes on the asset page", () => {
  const later = new Date(timestamp + 5000).toISOString();
  const proxy = {
    symbol: "PAXG",
    price: 3772.5,
    timestamp: new Date(timestamp).toISOString(),
    provider: "binance",
  };
  it("takes the dashboard's quote and proxy when they are at least as new", () => {
    const live = {
      quote: { ...asset.quote!, price: 101, timestamp: later },
      proxy: { ...proxy, price: 3780, timestamp: later },
    };
    const merged = withLiveQuotes({ ...asset, proxy }, live);
    expect(merged.quote?.price).toBe(101);
    expect(merged.proxy?.price).toBe(3780);
    // Nothing else is taken over: the page's own decision stays.
    expect(merged.decision).toBe(asset.decision);
    // Same time: the dashboard's copy (it may carry a newer status).
    expect(withLiveQuotes(asset, { quote: { ...asset.quote!, freshness_status: "STALE" } }).quote?.freshness_status).toBe(
      "STALE",
    );
  });
  it("keeps the page's own quote when the dashboard's is older or missing", () => {
    const older = { quote: { ...asset.quote!, price: 99, timestamp: new Date(timestamp - 5000).toISOString() } };
    expect(withLiveQuotes(asset, older).quote?.price).toBe(100);
    expect(withLiveQuotes(asset, { quote: null, proxy: null }).quote?.price).toBe(100);
    expect(withLiveQuotes(asset, undefined)).toBe(asset);
    // An invalid page time never wins over a valid dashboard time.
    const invalid = { ...asset, quote: { ...asset.quote!, timestamp: "invalid" } };
    expect(withLiveQuotes(invalid, { quote: { ...asset.quote!, price: 102 } }).quote?.price).toBe(102);
  });
});

describe("live guidance on the asset page", () => {
  const page = {
    ...asset,
    decision: { ...asset.decision!, created_at: new Date(timestamp - 60_000).toISOString() },
    event_risk: { trade_status: "CONDITIONAL LONG", adjusted_confidence: 50 },
    bars: [{ time: new Date(timestamp).toISOString(), close: 100 }],
  };
  const blocked = {
    ...page,
    decision: { ...page.decision, direction: "WAIT", category: "WAIT / DATA INSUFFICIENT", audit_status: "BLOCK",
      blocks: ["Evidence changed or expired"], reasons: ["Evidence changed or expired"] },
    event_risk: { trade_status: "WAIT", adjusted_confidence: 0 },
  };

  it("takes a newer published decision and its overlay while retaining the detail history", () => {
    const live = { ...blocked, decision: { ...blocked.decision, id: "new", created_at: new Date(timestamp).toISOString() } };
    const shown = withLiveQuotes(page, live);
    expect(shown.decision).toBe(live.decision);
    expect(shown.event_risk).toBe(live.event_risk);
    expect(shown.bars).toBe(page.bars);
    expect(page.decision.direction).toBe("LONG");
  });

  it("takes a read-time downgrade of the same decision immediately", () => {
    const shown = withLiveQuotes(page, blocked);
    expect(shown.decision).toEqual(blocked.decision);
    expect(shown.decision.reasons).toEqual(["Evidence changed or expired"]);
    expect(shown.event_risk).toBe(blocked.event_risk);
    expect(isActionable(currentAsset(shown, timestamp))).toBe(false);
  });

  it("refreshes the same decision's event overlay when the published direction is unchanged", () => {
    const live = { ...page, event_risk: { trade_status: "WAIT FOR FED", adjusted_confidence: 30 } };
    const shown = withLiveQuotes(page, live);
    expect(shown.decision).toBe(page.decision);
    expect(shown.event_risk).toBe(live.event_risk);
  });

  it("does not revive a blocked detail decision from the dashboard's older passing copy", () => {
    const shown = withLiveQuotes(blocked, page);
    expect(shown.decision).toBe(blocked.decision);
    expect(shown.event_risk).toBe(blocked.event_risk);
    expect(isActionable(currentAsset(shown, timestamp))).toBe(false);
  });

  it("retains detail-only timing and drivers when the same decision is downgraded", () => {
    const detail = { ...page, decision: { ...page.decision,
      decided_at: new Date(timestamp - 60_000).toISOString(), price: 99,
      levels: { support: 90, resistance: 110 },
      drivers: { rows: [{ driver: "Technical", value: 20 }], net: 20 },
    } };
    const shown = withLiveQuotes(detail, blocked);
    expect(shown.decision.direction).toBe("WAIT");
    expect(shown.decision.drivers).toBe(detail.decision.drivers);
    expect(shown.decision.decided_at).toBe(detail.decision.decided_at);
    expect(shown.decision.price).toBe(99);
    expect(shown.decision.levels).toBe(detail.decision.levels);
  });

  it("allows a genuinely newer published decision to replace an old blocked one", () => {
    const live = { ...page, decision: { ...page.decision, id: "new", created_at: new Date(timestamp).toISOString() } };
    const shown = withLiveQuotes(blocked, live);
    expect(shown.decision).toBe(live.decision);
    expect(isActionable(currentAsset(shown, timestamp))).toBe(true);
  });

  it("keeps newer detail guidance when the dashboard has an older decision", () => {
    const newer = { ...page, decision: { ...page.decision, id: "new", created_at: new Date(timestamp).toISOString() } };
    const shown = withLiveQuotes(newer, blocked);
    expect(shown.decision).toBe(newer.decision);
    expect(shown.event_risk).toBe(newer.event_risk);
  });

  it("clears the old overlay when a newer decision has no event overlay", () => {
    const live = { ...blocked, decision: { ...blocked.decision, id: "new", created_at: new Date(timestamp).toISOString() },
      event_risk: null };
    expect(withLiveQuotes(page, live).event_risk).toBeNull();
  });
});
