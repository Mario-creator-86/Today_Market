import { describe, expect, it } from "vitest";
import type { Alert, RegimeDriver, RegimeDriverInput } from "./api";
import {
  alertRows,
  closeDateText,
  driverInputParts,
  featureLabel,
  marketGroups,
  symbolLabel,
} from "../components/dashboard/view-model";

/** A driver input with fields newer than the shared type (close_status, close_date, via). */
const input = (fields: Record<string, unknown>) => fields as RegimeDriverInput;

describe("regime driver inputs", () => {
  const risk: RegimeDriver = {
    driver: "Risk appetite",
    direction: "up",
    input: "SP500 +0.51%, NASDAQ +0.48%, VIX -4.44%",
    inputs: [
      { feature: "SP500", field: "change_pct", value: 0.51, vote: 1 },
      { feature: "NASDAQ", field: "change_pct", value: 0.48, vote: 1 },
      { feature: "VIX", field: "change_pct", value: -4.44, vote: 1 },
    ],
    basis: "last_close",
  };

  it("labels features like the rest of the dashboard", () => {
    expect(symbolLabel("SP500")).toBe("S&P 500");
    expect(symbolLabel("VIX")).toBe("VIX");
    expect(featureLabel("CURVE:US10Y-US2Y")).toBe("US10Y-US2Y curve");
    expect(featureLabel("CREDIT:HY")).toBe("Credit (HY)");
    expect(closeDateText("2026-09-22")).toBe("22 Sep");
    expect(closeDateText("22 Sep")).toBeNull();
    expect(closeDateText(null)).toBeNull();
  });

  it("flags a STALE close with its date, from the input itself (regime.drivers inputs)", () => {
    const inputs = risk.inputs!.map((i) =>
      i.feature === "VIX" ? { ...i, close_status: "STALE", close_date: "2026-09-22" } : { ...i, close_status: "CURRENT" },
    );
    const parts = driverInputParts({ ...risk, inputs });
    expect(parts?.map((p) => p.text)).toEqual(["S&P 500 +0.51%", "NASDAQ +0.48%", "VIX -4.44% (stale close 22 Sep)"]);
    expect(parts?.map((p) => p.stale)).toEqual([false, false, true]);
    expect(parts?.[2].note).toBe("stale close 22 Sep");
    // The tooltip says what STALE means (not the latest expected close), never that it is older than the other inputs.
    expect(parts?.[2].title).toBe(
      "Not the latest expected close: last close 22 Sep. It still counts while its market is closed, up to the configured maximum age.",
    );
    expect(parts?.[2].title).not.toMatch(/other inputs/);
    expect(parts?.[0].title).toBeNull();
  });

  it("rebuilds the text from the inputs: the backend's own input text carries no stale marker", () => {
    // The backend's `input` string is unchanged ("... VIX -4.44%"); the chip still flags the stale VIX close.
    const inputs = risk.inputs!.map((i) => (i.feature === "VIX" ? { ...i, close_status: "STALE", close_date: "2026-09-22" } : i));
    expect(driverInputParts({ ...risk, inputs })?.map((p) => p.text).join(", ")).toBe(
      "S&P 500 +0.51%, NASDAQ +0.48%, VIX -4.44% (stale close 22 Sep)",
    );
    // Only the backend's text is usable: the stale inputs are named after it.
    const unmeasured = [
      input({ feature: "VIX", field: "change_pct", value: null, close_status: "STALE", close_date: "2026-09-22" }),
      input({ feature: "SP500", field: "change_pct", value: null, close_status: "CURRENT" }),
    ];
    expect(driverInputParts({ ...risk, inputs: unmeasured })).toEqual([
      {
        text: "SP500 +0.51%, NASDAQ +0.48%, VIX -4.44% (stale close: VIX 22 Sep)",
        stale: true,
        note: "stale close: VIX 22 Sep",
        title:
          "Not the latest expected close: last close 22 Sep. It still counts while its market is closed, up to the configured maximum age.",
      },
    ]);
    const two = [
      input({ feature: "VIX", close_status: "STALE", close_date: "2026-09-22" }),
      input({ feature: "SP500", close_status: "STALE" }),
    ];
    const both = driverInputParts({ ...risk, inputs: two });
    expect(both?.[0].note).toBe("stale close: VIX 22 Sep, S&P 500");
    expect(both?.[0].title).toMatch(/^Not the latest expected closes\./);
  });

  it("falls back to regime.feature_sources when the input carries no close status", () => {
    const sources = {
      VIX: { basis: "last_close", close_status: "STALE", close_date: "2026-09-22" },
      SP500: { basis: "last_close", close_status: "CURRENT", close_date: "2026-09-25" },
    };
    expect(driverInputParts(risk, sources)?.map((p) => p.text)).toEqual([
      "S&P 500 +0.51%",
      "NASDAQ +0.48%",
      "VIX -4.44% (stale close 22 Sep)",
    ]);
    // A stale close without a date still says so; the input's own CURRENT wins over the source.
    const inputs = [
      { feature: "VIX", field: "change_pct", value: -4.44, close_status: "STALE" },
      { feature: "SP500", field: "change_pct", value: 0.51, close_status: "CURRENT" },
    ];
    expect(driverInputParts({ ...risk, inputs }, { SP500: { close_status: "STALE" } })?.map((p) => p.text)).toEqual([
      "VIX -4.44% (stale close)",
      "S&P 500 +0.51%",
    ]);
  });

  it("keeps basis points, proxies and the backend text", () => {
    const rates: RegimeDriver = {
      driver: "Rates",
      direction: "down",
      inputs: [
        { feature: "US2Y", field: "change_bp", value: -6, vote: -1 },
        input({ feature: "GOLD", field: "change_pct", value: -1.77, vote: 0, via: "PAXG" }),
      ],
    };
    expect(driverInputParts(rates)?.map((p) => p.text)).toEqual(["US2Y -6 bp", "GOLD -1.77% via PAXG"]);
    expect(driverInputParts({ driver: "USD", direction: "down", input: "DXY -0.3%" })).toEqual([
      { text: "DXY -0.3%", stale: false, note: null, title: null },
    ]);
    expect(driverInputParts({ driver: "Credit", direction: "unavailable", inputs: [] })).toBeNull();
    // Unmeasured inputs never become a number.
    expect(
      driverInputParts({ driver: "Oil", direction: "unavailable", inputs: [{ feature: "WTI", field: "change_pct", value: null }] }),
    ).toBeNull();
  });
});

describe("all markets index", () => {
  it("groups every asset by class in a fixed order, keeping the API order and dropping duplicates", () => {
    const groups = marketGroups([
      { symbol: "NVDA", name: "Nvidia", asset_class: "equity" },
      { symbol: "BTC", name: "Bitcoin", asset_class: "crypto" },
      { symbol: "VIX", name: "Volatility index", asset_class: "index" },
      { symbol: "SP500", name: "S&P 500", asset_class: "index" },
      { symbol: "US30Y", name: "US Treasury 30Y", asset_class: "bond" },
      { symbol: "USDCAD", name: "USD/CAD", asset_class: "fx" },
      { symbol: "BTC", name: "Bitcoin", asset_class: "crypto" },
      { symbol: "XYZ", name: "Something", asset_class: "volatility_product" },
      { symbol: "", name: "blank", asset_class: "fx" },
    ]);
    expect(groups.map((g) => [g.label, g.assets.map((a) => a.symbol)])).toEqual([
      ["Crypto", ["BTC"]],
      ["Indices", ["VIX", "SP500"]],
      ["Rates", ["US30Y"]],
      ["FX", ["USDCAD"]],
      ["Equities", ["NVDA"]],
      ["Volatility product", ["XYZ"]],
    ]);
    expect(marketGroups([])).toEqual([]);
    expect(marketGroups(undefined)).toEqual([]);
  });
});

describe("dashboard alerts", () => {
  const alert = (id: string, priority: string, created_at: string, extra: Partial<Alert> = {}): Alert => ({
    id,
    kind: "LEVEL_BREAK",
    priority,
    title: id,
    message: "",
    symbol: null,
    created_at,
    ...extra,
  });
  // Priorities as config/rules.d/alerts.json assigns them to provider statuses.
  const priority: Record<string, string> = { ONLINE: "LOW", DEGRADED: "LOW", RATE_LIMITED: "MEDIUM" };
  const provider = (id: string, name: string, status: string, created_at: string): Alert =>
    alert(id, priority[status] ?? "HIGH", created_at, {
      kind: "PROVIDER",
      metrics: { provider: name, status },
    });

  it("resolved outages follow the open alerts with their recovery time; their recoveries stay visible", () => {
    // 28 Sep 2026, Bucharest = UTC+3.
    const alerts = [
      provider("whitehouse-403", "whitehouse", "AUTH_ERROR", "2026-09-28T09:53:00Z"),
      provider("treasury-down", "hm-treasury", "OFFLINE", "2026-09-28T09:50:29Z"),
      provider("massive-403", "massive", "AUTH_ERROR", "2026-09-28T09:49:00Z"),
      provider("whitehouse-legacy", "whitehouse", "AUTH_ERROR", "2026-09-28T09:02:00Z"),
      provider("deribit-down", "deribit", "OFFLINE", "2026-09-28T09:00:00Z"),
      alert("break-1", "MEDIUM", "2026-09-28T09:40:00Z"),
      alert("break-2", "MEDIUM", "2026-09-28T09:45:00Z"),
      provider("treasury-back", "hm-treasury", "ONLINE", "2026-09-28T09:50:30Z"),
      provider("whitehouse-back", "whitehouse", "ONLINE", "2026-09-28T09:56:00Z"),
    ];
    const providers = [
      { name: "whitehouse", status: "ONLINE" },
      { name: "hm-treasury", status: "ONLINE" },
      { name: "massive", status: "AUTH_ERROR" },
      { name: "deribit", status: "ONLINE" },
    ];
    expect(alertRows(alerts, providers).map((r) => [r.alert.id, r.resolved])).toEqual([
      ["massive-403", null],
      ["break-2", null],
      ["break-1", null],
      ["whitehouse-back", null],
      ["treasury-back", null],
      ["whitehouse-403", "RESOLVED 12:56"],
      ["treasury-down", "RESOLVED 12:50"],
      ["whitehouse-legacy", "RESOLVED 12:56"],
      // No recovery alert in the list: the current provider status says it is back, with no time to claim.
      ["deribit-down", "RESOLVED"],
    ]);
    expect(alertRows(alerts, providers, 3).map((r) => r.alert.id)).toEqual(["massive-403", "break-2", "break-1"]);
  });

  it("a failure after the last recovery stays open; a recovery on a later day gives its date", () => {
    const alerts = [
      provider("down-1", "finnhub", "RATE_LIMITED", "2026-09-27T20:00:00Z"),
      provider("back", "finnhub", "ONLINE", "2026-09-27T22:30:00Z"),
      provider("down-2", "finnhub", "OFFLINE", "2026-09-28T08:00:00Z"),
    ];
    expect(alertRows(alerts, [{ name: "finnhub", status: "OFFLINE" }]).map((r) => [r.alert.id, r.resolved])).toEqual([
      ["down-2", null],
      ["back", null],
      ["down-1", "RESOLVED Mon 28 Sep 01:30"],
    ]);
  });

  it("alerts without provider metrics, and providers not online, are never marked resolved", () => {
    const alerts = [
      alert("legacy", "HIGH", "2026-09-28T09:00:00Z", { kind: "PROVIDER" }),
      alert("setup", "HIGH", "2026-09-28T09:10:00Z", { kind: "SETUP", metrics: { provider: "gdelt", status: "OFFLINE" } }),
      provider("gdelt-down", "gdelt", "DEGRADED", "2026-09-28T09:20:00Z"),
    ];
    const rows = alertRows(alerts, [{ name: "gdelt", status: "DEGRADED" }, { name: "legacy", status: "ONLINE" }, null]);
    expect(rows.map((r) => [r.alert.id, r.resolved])).toEqual([
      ["setup", null],
      ["legacy", null],
      ["gdelt-down", null],
    ]);
    expect(alertRows([], undefined)).toEqual([]);
  });
});
