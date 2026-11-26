import { describe, expect, it } from "vitest";
import type { Asset, CausalPath, MarketEvent } from "../../lib/api";
import { breakingStream, latestConfirmed, tileView, whatMattersNow } from "./selectors";

const event = (extra: Partial<MarketEvent> = {}): MarketEvent => ({
  id: "mapped-news",
  summary_fact: "The central bank announced a policy change.",
  event_type: "CENTRAL_BANK",
  verification_status: "CONFIRMED",
  fact_confidence: 90,
  first_seen_utc: "2026-10-01T10:00:00Z",
  last_updated_utc: "2026-10-01T10:00:00Z",
  importance: 0.72,
  assets: [],
  sources: ["Official release"],
  interpretation: null,
  ...extra,
});

describe("what matters now", () => {
  it("retains confirmed news mapped to supplied affected assets or asset lists", () => {
    const affected = event({ id: "affected", affected_assets: ["US2Y", "DXY"] });
    const listed = event({ id: "listed", assets: ["BTC"] });
    expect(whatMattersNow([affected, listed])).toEqual([affected, listed]);
  });

  it("excludes high-importance headlines without established asset impact", () => {
    const noise = [
      "SEC approves private-fund reporting rule",
      "Steel plant construction announced",
      "Tax cut proposal announced",
    ].map((summary_fact, i) => event({ id: `noise-${i}`, summary_fact }));
    const mapped = event({ id: "mapped", assets: ["BRENT"], importance: 0.1 });
    expect(whatMattersNow([...noise, mapped])).toEqual([mapped]);
  });

  it.each([
    "PARTIALLY_CONFIRMED", "CORROBORATED", "UNCONFIRMED", "UNVERIFIED",
    "CONTRADICTED", "STALE", "UNKNOWN", "",
  ])("excludes %s facts even when their asset mapping is present", (verification_status) => {
    expect(whatMattersNow([event({ verification_status, assets: ["BTC"] })])).toEqual([]);
  });

  it("retains confirmed news with an active, nonzero causal effect", () => {
    const mapped = event({ causal_paths: [{ active: true, effects: { BRENT: 1, BTC: -1 } }] });
    expect(whatMattersNow([mapped])).toEqual([mapped]);
  });

  it("excludes explicit zero and cancelling net effects despite a broad asset list", () => {
    const zero = event({ assets: ["BTC"], causal_paths: [{ active: true, effects: { BTC: 0 } }] });
    const cancelling = event({
      id: "cancelling",
      affected_assets: ["BRENT"],
      causal_paths: [
        { active: true, effects: { BRENT: 1 } },
        { active: true, effects: { BRENT: -1 } },
      ],
    });
    expect(whatMattersNow([zero, cancelling])).toEqual([]);
  });

  it.each([{}, { BTC: NaN }, { BTC: Infinity }, { BTC: null }, { BTC: "unknown" }])(
    "excludes unmeasured directional effects %j without falling back to broad names",
    (effects) => {
      const path = { active: true, effects } as CausalPath;
      expect(whatMattersNow([event({ affected_assets: ["BTC"], causal_paths: [path] })])).toEqual([]);
    },
  );

  it("does not establish impact from an inactive path or empty asset names", () => {
    const inactive = event({ causal_paths: [{ active: false, effects: { BTC: 1 } }] });
    const empty = event({ id: "empty", assets: ["", " "] });
    expect(whatMattersNow([inactive, empty])).toEqual([]);
  });

  it("preserves importance ranking, recency ties, one topic and the five-item limit", () => {
    const inputs = [
      event({ id: "older", topic: "POLICY", assets: ["US2Y"], importance: 0.8 }),
      event({
        id: "newer", topic: "POLICY", assets: ["US2Y"], importance: 0.8,
        event_time: "2026-10-01T10:01:00Z",
      }),
      event({ id: "impactless", topic: "ENERGY", importance: 1 }),
      event({ id: "energy", topic: "ENERGY", assets: ["BRENT"], importance: 0.7 }),
      event({ id: "crypto", assets: ["BTC"], importance: 0.6 }),
      event({ id: "fx", assets: ["EURUSD"], importance: 0.5 }),
      event({ id: "zero-importance", assets: ["GOLD"], importance: 0 }),
      event({ id: "unmeasured", assets: ["SP500"], importance: null, event_time: "2026-10-01T10:02:00Z" }),
    ];
    const original = [...inputs];
    expect(whatMattersNow(inputs).map((e) => e.id)).toEqual([
      "newer", "energy", "crypto", "fx", "zero-importance",
    ]);
    expect(whatMattersNow(inputs, 10).map((e) => e.id)).toEqual([
      "newer", "energy", "crypto", "fx", "zero-importance", "unmeasured",
    ]);
    expect(inputs).toEqual(original);
  });
});

describe("breaking stream", () => {
  it("keeps non-stale unverified and impactless news in the full stream", () => {
    const inputs = [
      event({ id: "confirmed-impactless" }),
      event({ id: "unverified", verification_status: "UNCONFIRMED", event_time: "2026-10-01T10:02:00Z" }),
      event({ id: "unknown", verification_status: "UNKNOWN", event_time: "2026-10-01T10:01:00Z" }),
      event({ id: "stale", verification_status: "STALE", event_time: "2026-10-01T10:03:00Z" }),
    ];
    expect(breakingStream(inputs).map((e) => e.id)).toEqual(["unverified", "unknown", "confirmed-impactless"]);
  });
});

describe("latest confirmed", () => {
  it("lists confirmed facts with asset impact newest first, one per topic, within the limit", () => {
    const inputs = [
      event({ id: "old", assets: ["BTC"], event_time: "2026-10-01T08:00:00Z", importance: 1 }),
      event({ id: "new", assets: ["BRENT"], event_time: "2026-10-01T12:00:00Z", importance: 0.1 }),
      event({ id: "same-topic-older", topic: "POLICY", assets: ["US2Y"], event_time: "2026-10-01T09:00:00Z" }),
      event({ id: "same-topic-newer", topic: "POLICY", assets: ["US2Y"], event_time: "2026-10-01T11:00:00Z" }),
      event({ id: "impactless", event_time: "2026-10-01T13:00:00Z" }),
      event({ id: "partial", verification_status: "PARTIALLY_CONFIRMED", assets: ["ETH"], event_time: "2026-10-01T14:00:00Z" }),
    ];
    const original = [...inputs];
    expect(latestConfirmed(inputs).map((e) => e.id)).toEqual(["new", "same-topic-newer", "old"]);
    expect(latestConfirmed(inputs, 1).map((e) => e.id)).toEqual(["new"]);
    expect(latestConfirmed([])).toEqual([]);
    expect(inputs).toEqual(original);
  });
});

describe("compact strip tile", () => {
  const session = { state: "LIVE", label: "Market open" };
  const base = { symbol: "US2Y", name: "US 2Y", asset_class: "bond", session } as unknown as Asset;
  const close = {
    price: 4.78, timestamp: "2026-10-01T20:00:00+00:00", provider: "treasury", market_status: "LAST CLOSE",
    freshness_status: "STALE", change_pct: -2.05, change_bp: -10, data_class: "DAILY_CLOSE", close_status: "CURRENT",
    change_basis: "previous_close", previous_close_at: "2026-09-30T20:00:00+00:00",
  };
  const tile = (asset: Asset | undefined) => tileView({ symbol: asset?.symbol ?? "X", label: asset?.symbol ?? "X", asset });

  it("tags a current daily close with its day and keeps the change basis for the tooltip", () => {
    const view = tile({ ...base, quote: close } as Asset);
    expect(view.tag).toMatchObject({ label: "CLOSE Thu", tone: "info" });
    expect(view.move).toEqual({ text: "-10 bp", sign: -1 });
    expect(view.title).toContain("-10 bp vs prev close 30 Sep");
    expect(view.title).toContain("DAILY CLOSE");
  });

  it("tags a derived value, a late close and live data as what they are", () => {
    expect(tile({ ...base, quote: { ...close, data_class: "DERIVED_DAILY" } } as Asset).tag.label).toBe("DERIVED Thu");
    expect(tile({ ...base, quote: { ...close, close_status: "STALE" } } as Asset).tag).toMatchObject({
      label: "STALE", tone: "negative", prominent: true,
    });
    const live = { ...close, data_class: "LIVE", close_status: null, market_status: "LIVE", freshness_status: "FRESH", change_bp: null, change_pct: 2.76 };
    const view = tile({ ...base, quote: live } as Asset);
    expect(view.tag).toMatchObject({ label: "LIVE", tone: "positive" });
    expect(view.move).toEqual({ text: "+2.76%", sign: 1 });
  });

  it("names the proxy when only a proxy exists: its direction, never a level", () => {
    const proxy = {
      symbol: "PAXG", price: 4190.15, change_pct: 0.43, change_basis: "rolling_24h", provider: "binance",
      timestamp: "2026-10-02T07:34:37+00:00", market_status: "LIVE", freshness_status: "FRESH",
    };
    const view = tile({ ...base, symbol: "GOLD", quote: null, proxy } as unknown as Asset);
    expect(view.tag).toMatchObject({ label: "PROXY via PAXG", tone: "neutral" });
    expect(view.headline).toBe("—");
    expect(view.move).toEqual({ text: "+0.43%", sign: 1 });
    const late = tile({ ...base, symbol: "GOLD", quote: null, proxy: { ...proxy, freshness_status: "STALE" } } as unknown as Asset);
    expect(late.tag).toMatchObject({ label: "STALE · via PAXG", tone: "negative" });
  });

  it("keeps the missing-source states and never invents a change", () => {
    const missing = tile({ ...base, quote: null, coverage: { status: "NOT_CONFIGURED", missing_keys: ["FRED_API_KEY"] } } as unknown as Asset);
    expect(missing.tag.label).toBe("NOT CONFIGURED");
    expect(missing.move).toBeNull();
    expect(missing.title).toContain("FRED_API_KEY");
    expect(tile(undefined).tag.label).toBe("NOT DELIVERED");
  });
});
