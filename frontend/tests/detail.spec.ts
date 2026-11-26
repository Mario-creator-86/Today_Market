import { test, expect, type Page } from "@playwright/test";

/*
 * Asset (§28) and event (§29) detail pages against intercepted, test-only API
 * responses. Nothing here reaches a real backend.
 */

const NOW = new Date("2026-09-26T12:00:00Z");
const emptyDashboard = {
  providers: [],
  assets: [],
  events: [],
  calendar: [],
  regimes: [],
};

function bars(start: number, from: number, step: number, count = 30) {
  return Array.from({ length: count }, (_, i) => ({
    time: new Date(
      NOW.getTime() - (count - i) * 60000 + start * 60000,
    ).toISOString(),
    close: from + step * i,
  }));
}

const event = {
  id: "ev-1",
  summary_fact: "Iran announces naval exercise near the Strait of Hormuz",
  event_type: "GEOPOLITICAL_ENERGY",
  verification_status: "CONFIRMED",
  fact_confidence: 92,
  first_seen_utc: "2026-09-26T09:00:00Z",
  last_updated_utc: "2026-09-26T09:30:00Z",
  event_time: "2026-09-26T08:55:00Z",
  assets: ["BTC", "BRENT"],
  sources: ["Reuters", "Official statement"],
  interpretation: null,
};

/** Panel sections are titled by an h2; locate the card by its heading. */
function panel(page: Page, title: string) {
  return page.locator(".panel", {
    has: page.getByRole("heading", { name: title, exact: true }),
  });
}

async function mockApi(page: Page, routes: Record<string, unknown>) {
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path in routes) {
      const body = routes[path];
      return typeof body === "number"
        ? route.fulfill({ status: body, body: "not found" })
        : route.fulfill({ json: body });
    }
    return route.fulfill({
      json: path === "/api/dashboard" ? emptyDashboard : [],
    });
  });
}

const fullAsset = {
  symbol: "BTC",
  name: "Bitcoin",
  asset_class: "crypto",
  quote: {
    price: 64250.5,
    timestamp: NOW.toISOString(),
    provider: "Test fixture",
    market_status: "LIVE",
    freshness_status: "FRESH",
    change_pct: 1.24,
    change_basis: "rolling_24h",
  },
  decision: {
    id: "d1",
    direction: "LONG",
    category: "LONG SETUP",
    score: 42,
    evidence_confidence: 88,
    market_confirmation: 71,
    data_quality: 95,
    reasons: [],
    why: ["Test verified catalyst"],
    confirmation: "BTC holds above 64,500 (resistance)",
    invalidation: "BTC breaks below 63,000 (support)",
    confirmation_level: 64500,
    invalidation_level: 63000,
    audit_status: "PASS",
    components: {},
    historical_probability: { p: 0.64, n: 418, horizon: "4h" },
    // The published decision's key (backend decision.py); not `driver_contributions`.
    drivers: {
      rows: [
        { driver: "Rates", value: -18 },
        { driver: "USD", value: -12 },
        { driver: "Technical", value: 8 },
        { driver: "Positioning", value: null, status: "missing" },
      ],
      net: -22,
      unmeasured: ["positioning"],
    },
  },
  bars: bars(0, 63000, 40),
  technical_levels: {
    support: 63000,
    support_basis: "previous_day_low",
    resistance: 64500,
    resistance_basis: "swing_high",
    vwap: 63800,
    previous_day: { high: 64900, low: 62800 },
    observations: 30,
    scope: "30 one-minute bars",
  },
  cross_asset: {
    DXY: bars(0, 100, 0.01),
    US10Y: { name: "US Treasury 10Y", bars: bars(5, 4.2, 0.001) },
    NASDAQ: null,
  },
  events: [event],
  // Descriptive Deribit positioning (backend positioning.summarize), BTC and ETH only.
  positioning: {
    currency: "BTC",
    as_of: "2026-09-26T11:55:00Z",
    age_seconds: 300,
    freshness_status: "FRESH",
    options: {
      totals: { call_oi: 229695.7, put_oi: 119850.7, put_call_ratio: 0.522, oi_weighted_iv: 41.84 },
      expiries: [],
    },
    futures: {
      perpetual: { basis_pct: 0.00999, funding_8h: 3e-8, open_interest: 845686380, open_interest_unit: "USD" },
    },
    crowding: [],
    reason: "Open interest, skew and funding describe positioning; they are not a direction or a price target",
  },
};

test("asset detail shows signal, signed drivers, levels, cross-asset overlay and comparables", async ({
  page,
}) => {
  await mockApi(page, {
    "/api/assets/BTC": fullAsset,
    "/api/assets/BTC/comparables": {
      symbol: "BTC",
      minimum_observations: 200,
      by_horizon: {
        "1h": { observations: 12, hit_rate: 0.58, mean_return_pct: 0.31 },
      },
      items: [
        {
          decision_id: "old-1",
          created_at: "2026-09-20T07:00:00Z",
          direction: "LONG",
          category: "LONG SETUP",
          score: 38,
          outcomes: { "1h": { return_pct: 0.8 } },
        },
      ],
    },
  });
  await page.goto("/assets/BTC");

  const top = page.getByRole("region", { name: "BTC price and signal" });
  await expect(top).toContainText("+1.24%");
  await expect(top).toContainText("24h");
  await expect(top).toContainText("LONG SETUP");
  await expect(top).toContainText("+42");
  await expect(top).toContainText("88%");
  // The card shows the same signal label as the summary.
  await expect(page.locator(".setup .setup-head .badge")).toHaveText("LONG SETUP");
  await expect(page.locator(".setup")).toContainText(
    "64% · n=418 · horizon 4 hours",
  );
  // A LIVE quote in a live session: one LIVE badge, not two.
  await expect(top.locator(".badge").filter({ hasText: /^LIVE$/ })).toHaveCount(1);
  // Score, evidence and confirmation are shown once, in the summary.
  await expect(page.locator(".setup")).not.toContainText("Market confirmation");
  await expect(page.locator(".setup")).not.toContainText("BTC holds above 64,500");
  await expect(page.locator(".setup a")).toHaveCount(0);

  const why = page.getByRole("table", { name: "Why is BTC moving" });
  await expect(why.getByRole("row", { name: /Rates/ })).toContainText("-18");
  await expect(why.getByRole("row", { name: /Technical/ })).toContainText("+8");
  await expect(why.getByRole("row", { name: /Positioning/ })).toContainText(
    "Missing",
  );
  await expect(why.getByRole("row", { name: /Positioning/ })).not.toContainText(
    "0",
  );
  await expect(why.getByRole("row", { name: /Net/ })).toContainText("-22");

  const levels = page.locator(".key-levels");
  await expect(levels).toContainText("Support");
  await expect(levels).toContainText("63,000");
  await expect(levels).toContainText("previous day low");
  await expect(levels).toContainText("Previous day");

  const conditions = panel(page, "Confirmation & invalidation");
  await expect(conditions).toContainText("BTC holds above 64,500");
  await expect(conditions).toContainText("Level 64,500");

  const timeline = panel(page, "Event timeline");
  await expect(timeline).toContainText("Strait of Hormuz");
  await expect(timeline).toContainText("Reuters");

  const cross = panel(page, "Cross-asset chart");
  await expect(
    cross.getByRole("img", { name: /BTC compared with DXY, US10Y/ }),
  ).toBeVisible();
  await expect(cross.locator("canvas").first()).toBeVisible();
  const legend = cross.getByRole("list", { name: "Chart legend" });
  await expect(
    legend.getByRole("listitem").filter({ hasText: "NASDAQ" }),
  ).toContainText("No data");
  await expect(
    legend.getByRole("listitem").filter({ hasText: "DXY" }),
  ).toContainText("%");
  // US10Y history starts 5 minutes later: common start 11:35 UTC = 14:35 Bucharest.
  await expect(cross).toContainText("14:35");

  const comparables = page.getByRole("table", {
    name: "Historical comparable BTC signals",
  });
  await expect(comparables).toContainText("+0.80%");
  await expect(comparables).toContainText("10:00"); // 07:00 UTC in Bucharest
  await expect(page.getByText("Insufficient (minimum n=200)")).toBeVisible();
  await page.screenshot({
    path: "test-results/asset-detail.png",
    fullPage: true,
  });
});

test("asset detail degrades gracefully: missing components, no overlays, no comparables", async ({
  page,
}) => {
  await mockApi(page, {
    "/api/assets/ETH": {
      symbol: "ETH",
      name: "Ethereum",
      asset_class: "crypto",
      quote: {
        price: 2500,
        timestamp: NOW.toISOString(),
        provider: "Test fixture",
        market_status: "LIVE",
        freshness_status: "FRESH",
        change_pct: null,
      },
      decision: {
        direction: "WAIT",
        category: "WAIT / DATA INSUFFICIENT",
        score: 0,
        evidence_confidence: 0,
        market_confirmation: 0,
        data_quality: 40,
        reasons: ["Critical quote stale"],
        audit_status: "BLOCK",
        components: { event: 0, technical: 4.5 },
        component_detail: {
          event: { value: null, status: "missing", basis: "No verified event" },
          technical: { value: 30, status: "measured", basis: "Above VWAP" },
        },
      },
      bars: [],
      events: [],
    },
    "/api/assets/ETH/comparables": 404,
  });
  await page.goto("/assets/ETH");
  const top = page.getByRole("region", { name: "ETH price and signal" });
  await expect(top).toContainText("No change reported");
  await expect(top).toContainText("WAIT / DATA INSUFFICIENT");
  const why = page.getByRole("table", { name: "Why is ETH moving" });
  await expect(why.getByRole("row", { name: /Events/ })).toContainText(
    "Missing",
  );
  await expect(why.getByRole("row", { name: /Technical/ })).toContainText(
    "+4.5",
  );
  await expect(page.getByText("Levels not established")).toBeVisible();
  await expect(page.getByText("Cross-asset history unavailable")).toBeVisible();
  await expect(panel(page, "Historical comparable signals")).toContainText(
    "Not enough data",
  );
});

test("stale quote downgrades the displayed setup category to WAIT", async ({
  page,
}) => {
  await mockApi(page, {
    "/api/assets/BTC": {
      ...fullAsset,
      quote: { ...fullAsset.quote, timestamp: "2026-09-26T11:00:00Z" },
    },
  });
  await page.goto("/assets/BTC");
  const top = page.getByRole("region", { name: "BTC price and signal" });
  await expect(top).toContainText("STALE");
  await expect(top).not.toContainText("LONG SETUP");
  await expect(page.locator(".setup .setup-head .badge")).toHaveText("WAIT / DATA INSUFFICIENT");
});

test("a late quote in an open session keeps the venue state: STALE is the data chip, never the session", async ({
  page,
}) => {
  // MD-2: the venue trades (session LIVE) while the quote is an hour old.
  const late = {
    ...fullAsset,
    session: { state: "LIVE", venue: "CRYPTO", basis: "Crypto trades around the clock" },
    quote: { ...fullAsset.quote, timestamp: "2026-09-26T11:00:00Z", market_state: "LIVE" },
  };
  const routes: Record<string, unknown> = { "/api/assets/BTC": late };
  await mockApi(page, routes);
  await page.goto("/assets/BTC");
  const top = page.getByRole("region", { name: "BTC price and signal" });
  // Freshness is the chip; the session badge beside it says the venue is open, never LIVE or STALE.
  const badges = top.locator(".asset-price .badge");
  await expect(badges).toHaveText(["STALE", "MARKET OPEN"]);
  await expect(badges.first()).toHaveAttribute("data-variant", "negative");
  const source = panel(page, "Data source");
  const row = (label: string) =>
    source.locator(".detail-grid > div").filter({ has: page.locator("dt", { hasText: new RegExp(`^${label}$`) }) }).locator("dd");
  await expect(row("Data")).toContainText("STALE");
  await expect(row("Session")).toHaveText("LIVE · Crypto (24/7) · Crypto trades around the clock");
  await expect(row("Session")).not.toContainText("STALE");
  await top.screenshot({ path: "test-results/asset-late-quote.png" });

  // A fresh quote: the chip says LIVE once, no second badge repeats it.
  routes["/api/assets/BTC"] = { ...late, quote: { ...late.quote, timestamp: NOW.toISOString() } };
  await page.goto("/assets/BTC");
  await expect(badges).toHaveText(["LIVE"]);
  await expect(row("Session")).toHaveText("LIVE · Crypto (24/7) · Crypto trades around the clock");
});

test("the price chart is kept across polls: new bars update it, pan and zoom stay", async ({
  page,
}) => {
  let polls = 0;
  await mockApi(page, {});
  // Every poll moves the forming bar and the price, as the live service does.
  await page.route("**/api/assets/BTC", (route) => {
    polls += 1;
    const last = fullAsset.bars[fullAsset.bars.length - 1];
    return route.fulfill({
      json: {
        ...fullAsset,
        quote: { ...fullAsset.quote, price: 64250 + polls },
        bars: [...fullAsset.bars.slice(0, -1), { ...last, close: last.close + polls }],
      },
    });
  });
  await page.goto("/assets/BTC");
  const history = panel(page, "Price history");
  await expect(
    history.getByRole("img", { name: /BTC price history: one-minute bars/ }),
  ).toBeVisible();
  await expect(history.locator("canvas").first()).toBeVisible();
  await expect(history).toContainText("1-minute bars");
  await history
    .locator("canvas")
    .evaluateAll((els) => els.forEach((el) => el.setAttribute("data-original", "1")));
  const tagged = await history.locator("canvas[data-original]").count();
  expect(tagged).toBeGreaterThan(0);
  const top = page.getByRole("region", { name: "BTC price and signal" });
  const seen = polls;
  // The full record is reloaded every 30 s (the price follows the dashboard in between).
  await page.clock.fastForward(31_000);
  await expect.poll(() => polls).toBeGreaterThan(seen);
  await expect(top).toContainText(`64,${250 + polls}`);
  // The same canvases: the chart was updated in place, not destroyed and rebuilt.
  await expect(history.locator("canvas[data-original]")).toHaveCount(tagged);
});

test("the price shows from the dashboard while the asset record is still loading", async ({ page }) => {
  await mockApi(page, {
    "/api/dashboard": { ...emptyDashboard, assets: [{ ...fullAsset, quote: { ...fullAsset.quote, price: 64321 } }] },
  });
  // Registered last, so it runs first: the full record never arrives during this test.
  await page.route("**/api/assets/BTC", () => {});
  await page.goto("/assets/BTC");
  await expect(page.getByRole("region", { name: "BTC price and signal" })).toContainText("64,321");
  await expect(page.getByRole("heading", { level: 1, name: "Bitcoin" })).toBeVisible();
  // Bars, levels and events wait for the record: no empty "no history" states meanwhile.
  await expect(page.getByText("Loading market intelligence")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Price history", exact: true })).toHaveCount(0);
});

test("the price follows the dashboard's live quote between the asset record's reloads", async ({ page }) => {
  let assetCalls = 0;
  let dashboardCalls = 0;
  await mockApi(page, {});
  await page.route("**/api/assets/BTC", (route) => {
    assetCalls += 1;
    return route.fulfill({ json: fullAsset });
  });
  // The workspace keeps the dashboard current (here by polling: the stream is closed); each read is newer.
  await page.route("**/api/dashboard", (route) => {
    dashboardCalls += 1;
    const quote = {
      ...fullAsset.quote,
      price: 64300 + dashboardCalls,
      timestamp: new Date(NOW.getTime() + dashboardCalls * 1000).toISOString(),
    };
    return route.fulfill({ json: { ...emptyDashboard, assets: [{ ...fullAsset, quote }] } });
  });
  await page.goto("/assets/BTC");
  const top = page.getByRole("region", { name: "BTC price and signal" });
  await expect(top).toContainText(/64,3\d\d/);
  await expect(top).not.toContainText("64,250.5");
  const loads = assetCalls;
  const shown = /64,3\d\d/.exec(await top.innerText())?.[0] ?? "";
  expect(shown).not.toBe("");
  await page.clock.fastForward(6000);
  await expect(top).toContainText(/64,3\d\d/);
  await expect(top).not.toContainText(shown);
  // No full reload of bars, levels and events for a price change.
  expect(assetCalls).toBe(loads);
  await page.clock.fastForward(30_000);
  await expect.poll(() => assetCalls).toBeGreaterThan(loads);
  // A reload with an older quote never takes the price back.
  await expect(top).not.toContainText("64,250.5");
});

for (const revision of ["newer", "same-id"] as const) {
  test(`a ${revision} dashboard downgrade replaces cached detail guidance before a full reload`, async ({ page }) => {
    const loaded = {
      ...fullAsset,
      decision: { ...fullAsset.decision, created_at: new Date(NOW.getTime() - 60_000).toISOString(),
        expires_at: new Date(NOW.getTime() + 60_000).toISOString() },
      event_risk: { trade_status: "CONDITIONAL LONG", adjusted_confidence: 50 },
    };
    let dashboardAsset = { ...loaded, decision: { ...loaded.decision, reasons: [] as string[] } };
    let assetCalls = 0;
    await mockApi(page, {});
    await page.route("**/api/assets/BTC", (route) => {
      assetCalls += 1;
      return assetCalls === 1
        ? route.fulfill({ json: loaded })
        : route.fulfill({ status: 503, json: { detail: "Test detail reload unavailable" } });
    });
    await page.route("**/api/dashboard", (route) =>
      route.fulfill({ json: { ...emptyDashboard, assets: [dashboardAsset] } }),
    );
    await page.goto("/assets/BTC");
    const card = page.locator(".setup");
    const top = page.getByRole("region", { name: "BTC price and signal" });
    await expect(card.locator(".setup-head .badge")).toHaveText("LONG SETUP");
    await expect(panel(page, "Event risk")).toContainText("CONDITIONAL LONG");
    const loads = assetCalls;
    dashboardAsset = {
      ...loaded,
      decision: {
        ...loaded.decision,
        id: revision === "newer" ? "d2" : loaded.decision.id,
        created_at: revision === "newer" ? new Date(NOW.getTime() - 30_000).toISOString() : loaded.decision.created_at,
        direction: "WAIT", category: "WAIT / DATA INSUFFICIENT", audit_status: "BLOCK",
        reasons: ["Evidence changed or expired"],
      },
      event_risk: { trade_status: "WAIT", adjusted_confidence: 0 },
    };
    await page.clock.fastForward(6000);
    await expect(card.locator(".setup-head .badge")).toHaveText("WAIT / DATA INSUFFICIENT");
    await expect(top).toContainText("WAIT / DATA INSUFFICIENT");
    await expect(card).toContainText("Evidence changed or expired");
    await expect(panel(page, "Confirmation & invalidation")).toContainText("BLOCK");
    await expect(panel(page, "Event risk").locator(".asset-trade-status")).toHaveText("WAIT");
    await expect(panel(page, "Price history").getByRole("img", { name: /BTC price history: one-minute bars/ })).toBeVisible();
    if (revision === "newer") {
      await expect.poll(() => assetCalls).toBeGreaterThan(loads);
      // The configured retry completes; a failed reload must not start an immediate request loop.
      await page.clock.fastForward(2000);
      await expect.poll(() => assetCalls).toBe(loads + 2);
      await expect(card.locator(".setup-head .badge")).toHaveText("WAIT / DATA INSUFFICIENT");
    } else {
      expect(assetCalls).toBe(loads);
      await expect(panel(page, "Why is BTC moving?")).toContainText("Technical");
      await page.screenshot({
        path: "../artifacts/continuation-2026-10-01/detail-blocked-regression-fixture-desktop.png", fullPage: true,
      });
      await page.setViewportSize({ width: 390, height: 844 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await page.screenshot({
        path: "../artifacts/continuation-2026-10-01/detail-blocked-regression-fixture-phone.png", fullPage: true,
      });
    }
  });
}

const nvda = {
  symbol: "NVDA",
  name: "NVIDIA",
  asset_class: "equity",
  session: {
    state: "FRIDAY CLOSE",
    venue: "XNYS",
    basis: "NYSE official calendar; pre-market and after-hours from the extended-hours provider feed",
  },
  coverage: { status: "DAILY_ONLY", providers: ["alpaca"], missing_keys: [] },
  proxy: null,
  quote: {
    price: 225.05,
    timestamp: "2026-09-25T20:59:34Z",
    provider: "alpaca",
    feed: "iex",
    market_status: "FRIDAY CLOSE",
    freshness_status: "STALE",
    change_pct: 0.4,
    change_basis: "previous_close",
    data_class: "DAILY_CLOSE",
    close_status: "CURRENT",
    as_of_label: "FRIDAY CLOSE (IEX last trade 2026-09-25T20:59:34.720445+00:00)",
    attribution: "Source: Alpaca Market Data",
  },
  decision: {
    id: "d-nvda",
    direction: "WAIT",
    category: "WAIT / DATA INSUFFICIENT",
    score: 0,
    evidence_confidence: 0,
    market_confirmation: 0,
    data_quality: 60,
    reasons: ["Critical quote unavailable, stale or market closed"],
    confirmation: "",
    invalidation: "",
    audit_status: "NOT_REQUIRED",
    components: {},
    historical_probability: {
      status: "NOT ENOUGH DATA",
      n: 151,
      horizon: "4h",
      minimum: 30,
      reason: "No promoted calibration for this asset and horizon",
    },
    drivers: {
      rows: [
        { driver: "Rates", value: 0 },
        { driver: "Technical", value: 0 },
      ],
      net: 0,
      unmeasured: ["event", "macro", "technical", "positioning", "reaction", "cross_asset", "regime"],
    },
  },
  bars: [],
  daily_bars: ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"].map(
    (time, i) => ({ time, open: 220 + i, high: 226 + i, low: 218 + i, close: 221 + i, volume: null }),
  ),
  daily_bars_basis: "Daily bars: Alpaca IEX",
  technical_levels: {
    support: 208.94,
    support_basis: "sma_20",
    resistance: 234.75,
    resistance_basis: "range_20d_high",
    range_20d: { high: 234.75, low: 208.94 },
    sma_20: 208.94,
    sma_50: 190.035,
    daily_sessions: 274,
    reference_session: "2026-09-28",
    daily_basis: "daily bars: Alpaca IEX",
    observations: 0,
    scope: "0 one-minute bars; daily from daily bars: Alpaca IEX",
  },
  cross_asset: {
    DXY: ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"].map((time, i) => ({ time, close: 97 + i / 10 })),
    US10Y: ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"].map((time, i) => ({ time, close: 4.2 + i / 100 })),
    NASDAQ: [],
  },
  cross_asset_interval: { DXY: 86400, US10Y: 86400, NASDAQ: 60 },
  // Pre-event overlay: |decision score| x (1 - penalty), a conviction, not a percent.
  event_risk: { score: 64, level: "HIGH", trade_status: "WAIT", adjusted_confidence: 40, base_confidence: 55 },
  events: [],
};

test("daily-close assets chart their stored daily bars and word their sources and levels", async ({
  page,
}) => {
  await mockApi(page, {
    "/api/assets/NVDA": nvda,
    "/api/assets/NVDA/comparables": {
      symbol: "NVDA",
      minimum_observations: 200,
      by_horizon: { close: { observations: 0 }, next_session: { observations: 0 } },
      items: [
        {
          decision_id: "old-wait",
          created_at: "2026-09-24T19:00:00Z",
          direction: "WAIT",
          category: "WAIT",
          score: 2,
          outcomes: { close: { return_pct: null, raw_return_pct: 0.42 } },
        },
        // No outcome measured yet: not listed.
        { decision_id: "open", created_at: "2026-09-25T19:00:00Z", direction: "WAIT", score: 1, outcomes: {} },
      ],
    },
  });
  await page.goto("/assets/NVDA");

  const history = panel(page, "Price history");
  // The levels below were computed from 274 sessions of the same bars: the chart says it shows the latest of them.
  await expect(history).toContainText(
    "Daily closes (session dates) · latest 5 of 274 sessions, 21 Sep 2026 – 25 Sep 2026 · Daily bars: Alpaca IEX",
  );
  await expect(history.getByRole("img", { name: /NVDA price history: daily closes/ })).toBeVisible();
  await expect(history.locator("canvas").first()).toBeVisible();
  await expect(page.getByText("No price history")).toHaveCount(0);

  // An IEX last trade is not the official close; raw ISO times and venue codes are made readable.
  const source = panel(page, "Data source");
  await expect(source).toContainText("Last IEX trade of the session, not the official exchange close");
  await expect(source).not.toContainText("Official daily close");
  await expect(source).toContainText("Alpaca Market Data (IEX)");
  // One instant, told once: the service's time, without the session state the Session row gives.
  const asOf = source.locator(".detail-grid > div").filter({ has: page.locator("dt", { hasText: /^As of$/ }) });
  await expect(asOf.locator("dd")).toHaveText("IEX last trade Fri 25 Sep 23:59 Bucharest");
  await expect(source).not.toContainText("2026-09-25T");
  await expect(source).toContainText("FRIDAY CLOSE · NYSE");
  await expect(source).not.toContainText("XNYS");

  // The summary names the source, not the provider id, and the session state once (its badge).
  const top = page.getByRole("region", { name: "NVDA price and signal" });
  await expect(top).toContainText("as of IEX last trade Fri 25 Sep 23:59 Bucharest · Alpaca Market Data (IEX)");
  await expect(top).not.toContainText("· alpaca");
  expect((await top.locator(".asset-price").innerText()).match(/FRIDAY CLOSE/g)).toHaveLength(1);

  const levels = page.locator(".key-levels");
  await expect(levels).toContainText("SMA 20");
  await expect(levels).toContainText("20-day range high");
  await expect(levels).not.toContainText("daily sessions");
  await expect(levels).not.toContainText("sma 20");
  await expect(panel(page, "Support / resistance")).toContainText(
    "Computed from 274 daily sessions (Alpaca IEX).",
  );

  // `drivers` rows whose inputs were all unmeasured are missing, not measured zeros.
  const why = page.getByRole("table", { name: "Why is NVDA moving" });
  await expect(why.getByRole("row", { name: /Rates/ })).toContainText("Missing");
  await expect(why.getByRole("row", { name: /Technical/ })).toContainText("Missing");

  await expect(page.locator(".setup")).toContainText(
    "Not enough data · no promoted calibration for this asset and horizon · horizon 4 hours · 151 comparable past decisions (minimum 30)",
  );

  const risk = panel(page, "Event risk");
  await expect(risk).toContainText("Event-adjusted conviction");
  await expect(risk).toContainText("40/100 · published decision unchanged");
  await expect(risk).toContainText(
    "Decision score strength, 55/100 before the event, reduced by the pre-event penalty.",
  );
  await expect(risk).not.toContainText("40%");

  // Overlays with only daily closes: the comparison runs on daily closes.
  const cross = panel(page, "Cross-asset chart");
  await expect(cross.getByRole("img", { name: /NVDA compared with DXY, US10Y/ })).toBeVisible();
  await expect(cross).toContainText("% change of daily closes (session dates) since Tue 22 Sep");
  const legend = cross.getByRole("list", { name: "Chart legend" });
  await expect(legend.getByRole("listitem").filter({ hasText: "NASDAQ" })).toContainText("No data");
  await expect(legend.getByRole("listitem").filter({ hasText: "NVDA" })).not.toContainText("No data");

  const comparables = panel(page, "Historical comparable signals");
  await expect(comparables).toContainText("At close");
  await expect(comparables).toContainText("Next session");
  await expect(comparables).not.toContainText("next_session");
  const table = page.getByRole("table", { name: "Historical comparable NVDA signals" });
  await expect(table.getByRole("row")).toHaveCount(2);
  await expect(table).toContainText("+0.42%");
  await expect(table).toContainText("no direction");
  await expect(comparables).toContainText("shows the asset's own move instead, marked");
  await expect(comparables).not.toContainText("unsigned");
});

test("a single stored daily close is stated with its date, never drawn as a flat history line", async ({ page }) => {
  await mockApi(page, {
    "/api/assets/SP500": {
      symbol: "SP500",
      name: "S&P 500",
      asset_class: "index",
      session: { state: "FRIDAY CLOSE", venue: "XNYS_INDEX" },
      coverage: { status: "DAILY_ONLY", providers: ["fred"], missing_keys: [] },
      proxy: null,
      quote: {
        price: 7743.41,
        timestamp: "2026-09-25T20:00:00Z",
        provider: "fred",
        market_status: "FRIDAY CLOSE",
        freshness_status: "STALE",
        change_pct: null,
        data_class: "DAILY_CLOSE",
        close_status: "CURRENT",
        as_of_label: "FRED SP500, close 2026-09-25 (daily; FRED lags its source)",
        attribution: "Source: FRED, Federal Reserve Bank of St. Louis",
      },
      decision: null,
      bars: [],
      daily_bars: [{ time: "2026-09-25", open: null, high: null, low: null, close: 7743.41, volume: null }],
      daily_bars_basis: "Daily closes: FRED",
      events: [],
    },
  });
  await page.goto("/assets/SP500");
  const history = panel(page, "Price history");
  await expect(history).toContainText("Only one daily close stored");
  await expect(history).toContainText(
    "SP500 closed at 7,743.41 on Fri 25 Sep. A price history chart appears once more daily closes are stored.",
  );
  await expect(history).toContainText("Daily closes: FRED");
  await expect(history.getByRole("img", { name: /price history/ })).toHaveCount(0);
  await expect(history.locator("canvas")).toHaveCount(0);
  // The as-of text names no clock time, so the stored time is added once, in the app zone.
  const source = panel(page, "Data source");
  const asOf = source.locator(".detail-grid > div").filter({ has: page.locator("dt", { hasText: /^As of$/ }) });
  await expect(asOf.locator("dd")).toHaveText(
    "FRED SP500, close Fri 25 Sep (daily; FRED lags its source) · Fri 25 Sep 23:00 Bucharest",
  );
  const top = page.getByRole("region", { name: "SP500 price and signal" });
  await expect(top).toContainText(
    "as of FRED SP500, close Fri 25 Sep (daily; FRED lags its source) · FRED, Federal Reserve Bank of St. Louis",
  );
});

const FRED_NOTICE =
  "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.";

test("daily closes from FRED in the charts carry the FRED notice, and an unpenalised conviction says so", async ({
  page,
}) => {
  const sessions = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"];
  // NVDA is quoted by Alpaca; only the NASDAQ overlay (FRED NASDAQCOM) is FRED data.
  await mockApi(page, {
    "/api/assets/NVDA": {
      ...nvda,
      cross_asset: {
        DXY: nvda.cross_asset.DXY,
        US10Y: nvda.cross_asset.US10Y,
        NASDAQ: sessions.map((time, i) => ({ time, close: 18000 + i * 50 })),
      },
      cross_asset_interval: { DXY: 86400, US10Y: 86400, NASDAQ: 86400 },
      notices: [{ id: "fred_api", text: FRED_NOTICE, providers: ["fred"] }],
      event_risk: { ...nvda.event_risk, adjusted_confidence: 0, base_confidence: 0 },
      decision: {
        ...nvda.decision,
        historical_probability: {
          ...nvda.decision.historical_probability,
          n_basis: "past decisions for this asset and horizon in the same score bucket and regime",
        },
      },
    },
  });
  await page.goto("/assets/NVDA");
  const cross = panel(page, "Cross-asset chart");
  await expect(cross.getByRole("img", { name: /NVDA compared with DXY, US10Y, NASDAQ/ })).toBeVisible();
  // The dashboard lists no notice; the asset's own notices name the FRED overlay.
  const source = panel(page, "Data source");
  await expect(
    source.getByRole("list", { name: "Data provider notices" }).getByRole("listitem"),
  ).toHaveText([FRED_NOTICE]);
  const risk = panel(page, "Event risk");
  await expect(risk).toContainText("0/100 · published decision unchanged");
  await expect(risk).toContainText("Decision score strength; no pre-event penalty applied.");
  await expect(risk).not.toContainText("reduced by the pre-event penalty");
  await expect(page.locator(".setup")).toContainText(
    "151 past decisions for this asset and horizon in the same score bucket and regime (minimum 30)",
  );
});

test("on daily closes, an overlay with only recent one-minute bars is listed, not plotted", async ({
  page,
}) => {
  // NASDAQ from a live feed: one-minute bars of the last session only.
  const minutes = [0, 1, 2].map((i) => ({
    time: new Date(Date.UTC(2026, 8, 25, 14, i)).toISOString(),
    close: 18000 + i,
  }));
  await mockApi(page, {
    "/api/assets/NVDA": {
      ...nvda,
      cross_asset: { DXY: nvda.cross_asset.DXY, NASDAQ: minutes },
      cross_asset_interval: { DXY: 86400, NASDAQ: 60 },
    },
  });
  await page.goto("/assets/NVDA");
  const cross = panel(page, "Cross-asset chart");
  // The comparison keeps its first common session (DXY starts on 22 Sep), not NASDAQ's one day.
  await expect(
    cross.getByRole("img", { name: /NVDA compared with DXY, as % change since Tue 22 Sep/ }),
  ).toBeVisible();
  const legend = cross.getByRole("list", { name: "Chart legend" });
  await expect(legend.getByRole("listitem").filter({ hasText: "NASDAQ" })).toContainText(
    "One-minute bars only",
  );
  await expect(legend.getByRole("listitem").filter({ hasText: "NASDAQ" })).not.toContainText("No data");
});

test("a yield's move since the decision is in basis points", async ({ page }) => {
  await mockApi(page, {
    "/api/assets/US10Y": {
      ...fullAsset,
      symbol: "US10Y",
      name: "US 10Y yield",
      asset_class: "bond",
      quote: { ...fullAsset.quote, price: 4.25, change_pct: null, change_bp: 4 },
      bars: [],
      cross_asset: null,
      positioning: null,
      decision: {
        ...fullAsset.decision,
        created_at: "2026-09-26T11:50:00Z",
        price: 4.21,
        levels: { support: 4.1, resistance: 4.3 },
      },
    },
  });
  await page.goto("/assets/US10Y");
  const why = panel(page, "Why is US10Y moving?");
  await expect(why).toContainText("Yield at decision 4.21% · now 4.25% (+4 bp)");
  await expect(why).not.toContainText("+0.95%");
});

test("an overlay without history never marks the plotted asset as missing", async ({ page }) => {
  await mockApi(page, {
    "/api/assets/BTC": { ...fullAsset, cross_asset: { DXY: [], US10Y: [], NASDAQ: [] } },
  });
  await page.goto("/assets/BTC");
  const cross = panel(page, "Cross-asset chart");
  await expect(cross).toContainText("No stored history for DXY, US10Y and NASDAQ yet");
  const legend = cross.getByRole("list", { name: "Chart legend" });
  await expect(legend.getByRole("listitem").filter({ hasText: "BTC" })).toContainText(
    "30 one-minute bars",
  );
  await expect(legend.getByRole("listitem").filter({ hasText: "BTC" })).not.toContainText("No data");
  await expect(legend.getByRole("listitem").filter({ hasText: "DXY" })).toContainText("No data");
});

test("an old decision shows its time, STALE DECISION and how far the price moved", async ({
  page,
}) => {
  await mockApi(page, {
    "/api/dashboard": { ...emptyDashboard, analysis: { cadence_seconds: 300 } },
    "/api/assets/BTC": {
      ...fullAsset,
      decision: {
        ...fullAsset.decision,
        // 5 h before NOW; the price was above the support the decision saw.
        created_at: "2026-09-26T06:59:00Z",
        decided_at: "2026-09-26T07:00:00Z",
        price: 65000,
        levels: { support: 64800, resistance: 65500 },
      },
    },
  });
  await page.goto("/assets/BTC");
  const card = page.locator(".setup");
  await expect(card).toContainText("Decision as of Sat 26 Sep 10:00 Bucharest · 5 h ago");
  await expect(card.getByText("STALE DECISION")).toBeVisible();
  const why = panel(page, "Why is BTC moving?");
  await expect(why).toContainText("Decision as of Sat 26 Sep 10:00 Bucharest");
  await expect(why.getByText("STALE DECISION")).toBeVisible();
  await expect(why).toContainText("Price at decision 65,000 · now 64,250.5 (-1.15%)");
  await expect(why).toContainText("The price is below the 64,800 support this decision saw");
});

test("a decision is flagged stale by the service's analysis status, the same rule as the dashboard cards", async ({
  page,
}) => {
  const old = {
    ...fullAsset,
    decision: { ...fullAsset.decision, created_at: "2026-09-26T06:59:00Z", decided_at: "2026-09-26T07:00:00Z" },
  };
  // 5 h old, but it comes from the last successful cycle and the service calls its analysis current: not stale.
  await mockApi(page, {
    "/api/dashboard": {
      ...emptyDashboard,
      analysis: { cadence_seconds: 300, stale: false, last_success_at: "2026-09-26T06:59:00Z" },
    },
    "/api/assets/BTC": old,
  });
  await page.goto("/assets/BTC");
  const card = page.locator(".setup");
  await expect(card).toContainText("Decision as of Sat 26 Sep 10:00 Bucharest · 5 h ago");
  await expect(page.getByText("STALE DECISION")).toHaveCount(0);
});

test("no STALE DECISION is shown before the dashboard says how old a decision may be", async ({
  page,
}) => {
  await mockApi(page, {
    "/api/assets/BTC": {
      ...fullAsset,
      decision: { ...fullAsset.decision, created_at: "2026-09-26T06:59:00Z", decided_at: "2026-09-26T07:00:00Z" },
    },
  });
  // Registered last, so it runs first: the dashboard never answers during this test.
  await page.route("**/api/dashboard", () => {});
  await page.goto("/assets/BTC");
  const card = page.locator(".setup");
  await expect(card).toContainText("Decision as of Sat 26 Sep 10:00 Bucharest · 5 h ago");
  await expect(page.getByText("STALE DECISION")).toHaveCount(0);
});

test("BTC shows its descriptive derivatives positioning", async ({ page }) => {
  await mockApi(page, { "/api/assets/BTC": fullAsset });
  await page.goto("/assets/BTC");
  const positioning = panel(page, "Positioning (descriptive)");
  await expect(positioning).toContainText("0.52");
  await expect(positioning).toContainText("calls 229,695.7 · puts 119,850.7 (contracts)");
  await expect(positioning).toContainText("41.8%");
  await expect(positioning).toContainText("+0.0100%");
  await expect(positioning).toContainText("845,686,380 USD");
  await expect(positioning).toContainText("FRESH");
  await expect(positioning).toContainText("not a direction");
});

test("an unknown asset shows a not-found state and is not polled", async ({ page }) => {
  let calls = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/assets/NOPE") calls += 1;
  });
  await mockApi(page, { "/api/assets/NOPE": 404 });
  await page.goto("/assets/NOPE");
  await expect(
    page.getByRole("heading", { level: 1, name: "Asset not found" }),
  ).toBeVisible();
  await expect(page.getByText("Unknown asset NOPE")).toBeVisible();
  await expect(page.getByRole("link", { name: "Go to the dashboard" })).toHaveAttribute("href", "/");
  await expect(page.getByText("Unable to reach the data service")).toHaveCount(0);
  await expect(page).toHaveTitle("Asset not found · Market Intelligence");
  const seen = calls;
  await page.clock.fastForward(6000);
  await page.clock.fastForward(6000);
  await expect(page.getByText("Loading market intelligence")).toHaveCount(0);
  await expect(page.getByText("Unknown asset NOPE")).toBeVisible();
  expect(calls).toBe(seen);
  // Nor on reconnect: the page never flips back to a loading or error state.
  await page.context().setOffline(true);
  await page.context().setOffline(false);
  await page.clock.fastForward(1000);
  await expect(
    page.getByRole("heading", { level: 1, name: "Asset not found" }),
  ).toBeVisible();
  await expect(page.getByText("Loading market intelligence")).toHaveCount(0);
  await expect(page.getByText("Unable to reach the data service")).toHaveCount(0);
  expect(calls).toBe(seen);
});

test("asset routes are canonical and titled with the asset", async ({ page }) => {
  await mockApi(page, { "/api/assets/BTC": fullAsset });
  await page.goto("/assets/btc");
  await expect(page).toHaveURL(/\/assets\/BTC$/);
  await expect(page.getByRole("heading", { level: 1, name: "Bitcoin" })).toBeVisible();
  await expect(page).toHaveTitle("Bitcoin (BTC) · Market Intelligence");
});

test("a doubly encoded symbol is not found, and the tab title says so too", async ({ page }) => {
  await mockApi(page, { "/api/assets/BTC": fullAsset });
  await page.goto("/assets/BTC%2520");
  await expect(page.getByRole("heading", { level: 1, name: "Asset not found" })).toBeVisible();
  await expect(page).toHaveTitle("Asset not found · Market Intelligence");
  // The streamed metadata never titles the page with an asset it does not show.
  await page.clock.fastForward(6000);
  await expect(page).toHaveTitle("Asset not found · Market Intelligence");
});

const fullEvent = {
  ...event,
  topic: "IRAN_HORMUZ",
  importance: 0.82,
  trigger: "GEOPOLITICAL_ENERGY",
  trigger_basis: "rule",
  injection_suspected: true,
  extraction: {
    who: ["Iranian navy"],
    what: "Announced a live-fire naval exercise",
    when: "Friday morning",
    where: "Strait of Hormuz",
    direct_claims: ["Exercise lasts three days"],
    new_information: [],
  },
  evidence: [
    {
      id: "e1",
      source: "Reuters",
      url: "https://example.com/reuters",
      title: "Iran navy drill",
      excerpt: "Iran's navy began a drill near Hormuz.",
      published_at: "2026-09-26T08:55:00Z",
      ingested_at: "2026-09-26T09:00:00Z",
      relation: "supports",
      weight: 0.9,
      official: false,
      direct: false,
      group: "reuters",
    },
    {
      id: "e2",
      source: "Ministry of Defence",
      url: "https://example.com/mod",
      title: "Official statement",
      excerpt: "Exercise confirmed by the ministry.",
      published_at: "2026-09-26T09:05:00Z",
      ingested_at: "2026-09-26T09:06:00Z",
      relation: "supports",
      weight: 1,
      official: true,
      direct: true,
      trust_tier: "1",
      group: "gov",
    },
    {
      id: "e3",
      source: "Test wire",
      title: "Denial",
      excerpt: "A spokesperson denied any exercise.",
      published_at: "2026-09-26T09:10:00Z",
      ingested_at: "2026-09-26T09:12:00Z",
      relation: "contradicts",
      weight: 0.4,
      group: "wire",
    },
  ],
  causal_paths: [
    {
      rule_id: "hormuz-oil",
      trigger: "GEOPOLITICAL_ENERGY",
      steps: [
        { variable: "oil_supply_risk", direction: 1, label: "Oil supply risk" },
        {
          variable: "inflation_expectations",
          direction: 1,
          label: "Inflation expectations",
        },
      ],
      effects: { BRENT: 1, US10Y: 1, BTC: -1 },
      strength: 70,
      decay: 0.83,
      active: true,
      suppressed_by: null,
    },
    {
      rule_id: "hormuz-risk-off",
      trigger: "GEOPOLITICAL_ENERGY",
      steps: [{ variable: "risk_appetite", direction: -1 }],
      effects: { NASDAQ: -1 },
      strength: 20,
      active: false,
      suppressed_by: "posture-risk-on",
    },
  ],
  reactions: [
    {
      symbol: "BRENT",
      horizon: "5m",
      change: 1.1,
      unit: "pct",
      expected_direction: 1,
      status: "PARTIAL",
    },
    {
      symbol: "BRENT",
      horizon: "1h",
      change: 4.2,
      unit: "pct",
      expected_direction: 1,
      status: "CONFIRMED",
    },
    {
      symbol: "US10Y",
      horizon: "1h",
      change: 7,
      unit: "bp",
      expected_direction: 1,
      status: "CONFIRMED",
    },
    {
      symbol: "BTC",
      horizon: "1h",
      change: 0.6,
      unit: "pct",
      expected_direction: -1,
      status: "DIVERGENT",
    },
  ],
  baskets: [
    {
      horizon: "5m",
      confirmation_pct: 50,
      confirming: [],
      partial: ["BRENT"],
      diverging: [],
      missing: ["US10Y", "BTC"],
    },
    {
      horizon: "1h",
      confirmation_pct: 66.7,
      confirmation_level: "MEDIUM",
      confirming: ["BRENT", "US10Y"],
      diverging: ["BTC"],
      missing: [],
      divergence: true,
    },
  ],
};

test("event detail shows what happened, sources, contradictions, causal chain and reactions", async ({
  page,
}) => {
  await mockApi(page, { "/api/events/ev-1": fullEvent });
  await page.goto("/events/ev-1");
  await expect(
    page.getByRole("heading", { name: "Event evidence", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("alert").filter({ hasText: "Injection suspected" }),
  ).toBeVisible();

  const fact = page.getByRole("region", { name: "Fact", exact: true });
  await expect(fact).toContainText("CONFIRMED");
  await expect(fact).toContainText("12:00"); // first seen 09:00 UTC = 12:00 Bucharest
  await expect(fact).toContainText("Importance 82/100");

  const what = panel(page, "What happened");
  await expect(what).toContainText("Announced a live-fire naval exercise");
  await expect(what).toContainText("Iranian navy");
  await expect(what).toContainText("Exercise lasts three days");

  const verification = panel(page, "Verification status");
  await expect(verification).toContainText(
    "3 documents from 3 independent source groups",
  );
  await expect(verification).toContainText("1 official · 1 direct");

  const sources = panel(page, "Sources");
  await expect(sources).toContainText("Reuters");
  await expect(sources).toContainText("Second-hand");
  await expect(sources).toContainText("Ministry of Defence");
  await expect(sources).toContainText("Trust tier 1");
  await expect(sources).toContainText("OFFICIAL");
  await expect(sources).not.toContainText("Test wire");

  const contradictions = panel(page, "Contradictions");
  await expect(contradictions).toContainText("Test wire");
  await expect(contradictions).toContainText(
    "A spokesperson denied any exercise.",
  );

  const chain = panel(page, "Causal chain");
  const main = chain.getByRole("region", { name: "Causal path Hormuz oil" });
  await expect(main).toContainText("Oil supply risk");
  await expect(main).toContainText("Inflation expectations");
  await expect(main).toContainText("measured at 1h");
  await expect(
    main.getByRole("listitem").filter({ hasText: "BRENT" }).last(),
  ).toContainText("+4.20%");
  await expect(
    main.getByRole("listitem").filter({ hasText: "US10Y" }).last(),
  ).toContainText("+7.0 bp");
  await expect(
    main.getByRole("listitem").filter({ hasText: "BTC" }).last(),
  ).toContainText("DIVERGENT");
  await expect(
    chain.getByRole("region", { name: "Causal path Hormuz risk off" }),
  ).toContainText('Suppressed by the exception "Posture risk on".');
  await chain.getByLabel("Reaction window").selectOption("5m");
  await expect(
    main.getByRole("listitem").filter({ hasText: "BRENT" }).last(),
  ).toContainText("+1.10%");
  await expect(
    main.getByRole("listitem").filter({ hasText: "US10Y" }).last(),
  ).toContainText("Not measured");

  const table = page.getByRole("table", {
    name: "Expected and actual market reaction",
  });
  await expect(table.getByRole("row", { name: /BTC/ })).toContainText("down");
  await expect(table.getByRole("row", { name: /BTC/ })).toContainText("+0.60%");
  await expect(table.getByRole("row", { name: /NASDAQ/ })).toHaveCount(0);

  const headline = panel(page, "Market confirmation").locator(
    ".market-confirmation",
  );
  await expect(headline).toContainText("50%"); // follows the selected 5m window
  await expect(headline).toContainText("at 5m");
  await chain.getByLabel("Reaction window").selectOption("1h");
  await expect(headline).toContainText("67%");
  const confirmation = panel(page, "Market confirmation");
  await expect(confirmation).toContainText("Divergence: BTC");
  await page.screenshot({
    path: "test-results/event-detail.png",
    fullPage: true,
  });
});

test("event detail without analysis says so instead of inventing values", async ({
  page,
}) => {
  await mockApi(page, {
    "/api/events/ev-2": {
      ...event,
      id: "ev-2",
      evidence: [],
      interpretation: null,
    },
  });
  await page.goto("/events/ev-2");
  await expect(page.getByText("No contradicting evidence found")).toBeVisible();
  await expect(page.getByText("Causal chain unavailable")).toBeVisible();
  await expect(
    page.getByText("No expected or measured reaction"),
  ).toBeVisible();
  await expect(panel(page, "Market confirmation")).toContainText(
    "Not computed yet",
  );
  await expect(page.getByText("Interpretation not available")).toBeVisible();
  await expect(
    page.getByRole("alert").filter({ hasText: "Injection" }),
  ).toHaveCount(0);
});

test("detail pages fit a phone screen without horizontal page scroll", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page, {
    "/api/assets/BTC": fullAsset,
    "/api/events/ev-1": fullEvent,
  });
  for (const path of ["/assets/BTC", "/events/ev-1"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
    await expect(page.locator(".panel").first()).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  }
});

test("the pre-event penalty is described from the penalty the service applied, not from rounding", async ({ page }) => {
  // |score| 42.34 is sent unrounded; the adjusted conviction is rounded to 42.3 although no penalty applied.
  await mockApi(page, {
    "/api/assets/NVDA": {
      ...nvda,
      event_risk: { ...nvda.event_risk, adjusted_confidence: 42.3, base_confidence: 42.34, penalty: 0 },
    },
  });
  await page.goto("/assets/NVDA");
  const risk = panel(page, "Event risk");
  await expect(risk).toContainText("42/100 · published decision unchanged");
  await expect(risk).toContainText("Decision score strength; no pre-event penalty applied.");
  await expect(risk).not.toContainText("reduced by the pre-event penalty");
});
