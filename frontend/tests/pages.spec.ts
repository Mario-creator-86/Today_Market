import { test, expect, type Page } from "@playwright/test";

/** Test-only API fixtures, keyed by pathname. Unknown endpoints return []. */
type Fixtures = Record<string, unknown | ((url: URL) => unknown)>;

const emptyDashboard = {
  providers: [],
  assets: [],
  events: [],
  calendar: [],
  regimes: [],
};

async function mockApi(page: Page, fixtures: Fixtures) {
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api/, "");
    if (path === "/dashboard") return route.fulfill({ json: fixtures["/dashboard"] ?? emptyDashboard });
    if (!(path in fixtures)) return route.fulfill({ json: [] });
    const value = fixtures[path];
    if (value && typeof value === "object" && "status" in (value as object) && "body" in (value as object))
      return route.fulfill(value as { status: number; body: string });
    return route.fulfill({ json: typeof value === "function" ? value(url) : value });
  });
}

/** An ECB daily fix as the backend sends it for an FX pair (data_class DAILY_CLOSE, close_status CURRENT). */
const ecbFix = (price: number, extra: Record<string, unknown> = {}) => ({
  price,
  timestamp: "2026-09-25T12:15:00Z",
  provider: "ecb",
  market_status: "LAST CLOSE",
  freshness_status: "STALE",
  change_pct: null,
  data_class: "DAILY_CLOSE",
  close_status: "CURRENT",
  as_of_label: "ECB 14:15 CET fix, 2026-09-25 (daily, not a tradable quote)",
  attribution: "Source: ECB",
  ...extra,
});

const fxRows = [
  {
    symbol: "DXY",
    price: 101.2,
    bias: "WAIT",
    fed_rate: null,
    relative_basis: "DXY is a basket; compare component pairs",
    quote: ecbFix(101.2, { data_class: "DERIVED_DAILY", derived: true, as_of_label: "USD index replica from ECB fixes, 2026-09-25" }),
  },
  {
    symbol: "NZDUSD",
    price: 0.5812,
    bias: "WAIT",
    fed_rate: 3.875,
    other_bank: "rbnz",
    other_bank_name: "RBNZ",
    other_rate: null,
    relative_basis: "Both central banks, expectations and yields are required",
    // The API's per-bank list: a bank without a decision source carries only its reason.
    next_meeting: { bank: "fed", bank_name: "Fed", name: "FOMC rate decision", scheduled_at: "2026-10-28T18:00:00+00:00", time_known: true },
    next_meetings: [
      {
        bank: "rbnz",
        bank_name: "RBNZ",
        name: null,
        scheduled_at: null,
        time_known: null,
        reason: "No rate-decision calendar source for the Reserve Bank of New Zealand",
      },
      { bank: "fed", bank_name: "Fed", name: "FOMC rate decision", scheduled_at: "2026-10-28T18:00:00+00:00", time_known: true, reason: null },
    ],
    quote: ecbFix(0.5812),
  },
  {
    symbol: "EURUSD",
    price: 1.1712,
    bias: "BEARISH LEAN / WAIT",
    fed_rate: 4.25,
    other_rate: 2.0,
    other_bank: "ECB",
    policy_differential: 2.25,
    yield_differential: -0.4,
    next_meeting: "Fed hold priced for 29 Oct",
    confirmation: "EUR/USD below 1.1650 with US 2Y rising",
    invalidation: "Close above 1.1800",
    as_of: "2026-09-27T00:00:00Z",
    quote: ecbFix(1.1712),
  },
  {
    symbol: "GBPUSD",
    price: 1.3345,
    bias: "WAIT",
    fed_rate: 3.875,
    fed_rate_range: [3.75, 4.0],
    other_bank: "boe",
    other_bank_name: "BoE",
    other_rate: null,
    relative_basis: "Both central banks, expectations and yields are required",
    next_meeting: { name: "FOMC rate decision", scheduled_at: "2026-10-28T18:00:00+00:00" },
    // Hidden provenance: search must never match it.
    quote: ecbFix(1.3345, { label: "ECB reference rate (cross via EUR)" }),
  },
];

test("FX page reads current backend keys, labels provenance and orders pairs per spec", async ({ page }) => {
  await mockApi(page, { "/fx": fxRows });
  await page.goto("/fx");
  await expect(page.getByRole("heading", { name: "Foreign exchange", exact: true })).toBeVisible();
  // Confirmation / invalidation sit in a full-width detail line under each pair.
  const rows = page.locator("tbody tr:not([data-kind=detail])");
  await expect(rows).toHaveCount(4);
  await expect(rows.nth(0)).toContainText("EUR/USD");
  await expect(rows.nth(1)).toContainText("GBP/USD");
  await expect(rows.nth(2)).toContainText("NZD/USD");
  await expect(rows.nth(3)).toContainText("DXY");
  const eur = rows.first();
  await expect(eur.getByRole("link", { name: "EUR/USD" })).toHaveAttribute("href", "/assets/EURUSD");
  await expect(eur).toContainText("Fed 4.25%");
  await expect(eur).toContainText("ECB 2%");
  await expect(eur).toContainText("+2.25 pp");
  await expect(eur).toContainText("-0.4 pp");
  await expect(eur).toContainText("Fed hold priced for 29 Oct");
  // The price is an ECB daily fix and says so.
  await expect(eur).toContainText("DAILY CLOSE");
  await expect(eur).toContainText("ECB 14:15 CET fix");
  // The source is named once, by its display name: never the provider id beside the attribution ("· ecb · Source: ECB").
  await expect(eur).toContainText("(daily, not a tradable quote) · ECB");
  await expect(eur).not.toContainText("· ecb");
  await expect(eur).not.toContainText("Source: ECB");
  const details = page.locator("tbody tr[data-kind=detail]");
  await expect(details.first()).toContainText("Close above 1.1800");
  await expect(details.first()).toContainText("EUR/USD below 1.1650 with US 2Y rising");

  const gbp = rows.nth(1);
  await expect(gbp).toContainText("Fed 3.75–4.00% (midpoint 3.875%)");
  await expect(gbp).toContainText("BoE rate unavailable");
  // The next meeting the API sends ({name, scheduled_at}) is shown, never "Unavailable".
  await expect(gbp).toContainText("FOMC rate decision · Wed 28 Oct 20:00 Bucharest");
  await expect(gbp).toContainText("Market pricing unavailable");
  await expect(details.nth(1)).toContainText("Both central banks, expectations and yields are required");
  const nzd = rows.nth(2);
  await expect(nzd).toContainText("RBNZ rate unavailable");
  await expect(nzd).toContainText("Fed: FOMC rate decision · Wed 28 Oct 20:00 Bucharest");
  await expect(nzd).toContainText("No rate-decision calendar source for the Reserve Bank of New Zealand");
  await expect(nzd).not.toContainText("RBNZ rate decision");

  const dxy = rows.last();
  await expect(dxy).toContainText("DERIVED");
  await expect(dxy).toContainText("No rate decision in the calendar");
  // Missing inputs stay explicit, never zero; a missing Fed rate is worded like "BoE rate unavailable".
  await expect(dxy).toContainText("Fed rate unavailable");
  await expect(dxy).not.toContainText("Fed Unavailable");
  await expect(dxy).not.toContainText("0 pp");
  // A basket has no counterpart bank: the counterpart rate and differentials do not apply, never "Unavailable".
  await expect(dxy).toContainText("Basket, no single counterpart bank");
  await expect(dxy).not.toContainText("Unavailable");
  for (const column of ["Policy differential", "2Y yield differential"]) {
    const cell = dxy.locator(`td[data-label="${column}"] span`);
    await expect(cell).toHaveText("Not applicable");
    await expect(cell).toHaveAttribute("title", "DXY is a basket; compare component pairs");
  }
  // A pair's missing differential is still missing data.
  await expect(gbp.locator('td[data-label="Policy differential"]')).toHaveText("Unavailable");
  await expect(details.last()).toContainText("DXY is a basket");

  // Pairs with incomplete inputs are counted (DXY is a basket, not a pair with one counterpart bank).
  await expect(page.getByText("Relative policy is incomplete for 2 of 3 pairs", { exact: false })).toBeVisible();
  // A date-only observation is shown as a date, not an invented 03:00.
  await expect(page.getByText("Policy inputs as of 27 Sep", { exact: true })).toBeVisible();

  // Search reads the shown text only: the hidden quote label of GBP/USD does not match "ecb".
  await page.getByLabel("Search records").fill("ecb");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("EUR/USD");
  await page.getByLabel("Search records").fill("new zealand");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("NZD/USD");
});

test("calendar cards show Bucharest time, flag, scenarios and measured reaction", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-09-26T08:00:00Z") });
  await mockApi(page, {
    "/calendar": [
      {
        id: "cpi",
        name: "CPI YoY",
        country: "US",
        scheduled_utc: "2026-09-26T12:30:00Z",
        impact: 10,
        consensus: 2.8,
        previous: 2.7,
        actual: null,
        unit: "%",
        time_known: true,
        time_basis: "BLS release calendar (ICS)",
        attribution: "Source: U.S. Bureau of Labor Statistics",
        source_url: "https://www.bls.gov/schedule/news_release/cpi.htm",
        scenarios: [
          { upper: 2.6, interpretation: "Fed pricing dovish", implications: { USD: -1, Nasdaq: 1, BTC: 1 } },
          { lower: 2.7, upper: 2.9, interpretation: "Near consensus", implications: [] },
          { lower: 3.0, interpretation: "Fed repricing hawkish", implications: [{ asset: "US10Y", direction: 1 }] },
        ],
      },
      {
        id: "ifo",
        name: "Ifo business climate",
        country: "DE",
        scheduled_utc: "2026-09-25T08:00:00Z",
        impact: 8,
        consensus: 88.0,
        previous: 87.5,
        actual: 89.1,
        raw_surprise: 1.1,
        z_score: null,
        reactions: [{ symbol: "EURUSD", horizon: "15m", change: 0.21, unit: "pct", status: "CONFIRMED" }],
      },
      {
        // The id looks like a search hit for "ecb"; search must only read what the card shows.
        id: "ecb2ec95-auction",
        name: "3-Year Note auction",
        country: "US",
        scheduled_at: "2026-09-26T04:00:00+00:00",
        time_known: false,
        time_basis: "date only; closing time not published",
        // The publisher's local day (the backend's internal label is never shown).
        date_window: {
          start_utc: "2026-09-26T04:00:00+00:00",
          end_utc: "2026-09-27T04:00:00+00:00",
          label: "publisher local day",
          zone: "America/New_York",
        },
        impact: 8,
        consensus: null,
        attribution: "Source: U.S. Treasury",
      },
      {
        id: "tsla",
        name: "TSLA earnings",
        country: "US",
        scheduled_at: "2026-09-27T20:00:00Z",
        time_known: false,
        time_basis: "after the US close (Finnhub gives the session only; exact time not published)",
        date_window: {
          start_utc: "2026-09-27T20:00:00+00:00",
          end_utc: "2026-09-28T00:00:00+00:00",
          label: "after the US close",
          zone: "America/New_York",
        },
        impact: 7,
        consensus: null,
        estimates: {
          label: "Finnhub estimate",
          values: [
            { name: "EPS", value: 0.4464, unit: "USD/share" },
            { name: "Revenue", value: 28179264130, unit: "USD" },
          ],
          eps: 0.4464,
          note: "Finnhub's estimates include its own proprietary estimates; not Street consensus",
        },
      },
    ],
  });
  await page.goto("/calendar");
  const cpi = page.locator("article", { hasText: "CPI YoY" });
  await expect(cpi.locator("time")).toHaveText("15:30");
  // The country code only: flag emoji render as letters on Windows ("us US").
  await expect(cpi).toContainText("US");
  await expect(cpi).not.toContainText("🇺🇸");
  await expect(cpi).toContainText("Impact 10/10");
  // Time basis and source credit on a timed row too.
  await expect(cpi).toContainText("Time basis: BLS release calendar (ICS)");
  await expect(cpi.getByRole("link", { name: "Source: U.S. Bureau of Labor Statistics" })).toHaveAttribute(
    "href",
    "https://www.bls.gov/schedule/news_release/cpi.htm",
  );
  await expect(cpi).toContainText("2.8%");
  await expect(cpi).toContainText("Upcoming");
  await expect(cpi).toContainText("≤ 2.6%");
  await expect(cpi).toContainText("2.7–2.9%");
  await expect(cpi).toContainText("USD negative");
  await expect(cpi).toContainText("US10Y positive");
  await expect(cpi.getByText("Surprise z-score").locator("..")).toContainText("Pending");

  const ifo = page.locator("article", { hasText: "Ifo business climate" });
  await expect(ifo).toContainText("Released");
  await expect(ifo).toContainText("+1.1");
  await expect(ifo).toContainText("Not enough data");
  await expect(ifo).toContainText("EURUSD");
  await expect(ifo).toContainText("+0.21%");
  await expect(ifo).toContainText("CONFIRMED");
  await expect(ifo).not.toContainText("Scenario analysis");

  // A date-only row is due all day, not "Awaiting actual" from midnight; no consensus is said so.
  const auction = page.locator("article", { hasText: "3-Year Note auction" });
  await expect(auction.locator("time")).toHaveText("Time TBA");
  await expect(auction).toContainText("Due today");
  await expect(auction).not.toContainText("Awaiting actual");
  await expect(auction).toContainText("NO CONSENSUS");
  await expect(auction).toContainText("Release window: all day Sat 26 Sep, New York time");
  await expect(auction).toContainText("Time basis: date only; closing time not published");
  await expect(auction).not.toContainText("publisher local day");

  // Finnhub estimates are shown under their own label, never as consensus.
  const tsla = page.locator("article", { hasText: "TSLA earnings" });
  await expect(tsla).toContainText("Finnhub estimate");
  await expect(tsla).toContainText("EPS 0.4464 USD/share · Revenue 28.18B USD");
  await expect(tsla).toContainText("not Street consensus");
  await expect(tsla.getByText("Consensus", { exact: true })).toHaveCount(0);
  // The session window is a Bucharest time range, stated once: the time basis does not repeat it.
  await expect(tsla).toContainText("Release window: 23:00–03:00 next day Bucharest (after the US close)");
  await expect(tsla).toContainText("Time basis: Finnhub gives the session only; exact time not published");
  await expect(tsla).not.toContainText("Time basis: after the US close");

  await expect(page.getByRole("heading", { name: "Friday 25 September" })).toBeVisible();
  await page.screenshot({ path: "test-results/calendar-desktop.png", fullPage: true });
  await page.getByLabel("Country filter").selectOption("DE");
  await expect(page.getByText("CPI YoY", { exact: true })).toHaveCount(0);
  await page.getByLabel("Country filter").selectOption("ALL");
  await page.getByLabel("Release filter").selectOption("UPCOMING");
  await expect(page.getByText("Ifo business climate", { exact: true })).toHaveCount(0);
  await expect(page.getByText("CPI YoY", { exact: true })).toBeVisible();
  // A date-only release inside its day is still to come.
  await expect(page.getByText("3-Year Note auction", { exact: true })).toBeVisible();
  await page.getByLabel("Release filter").selectOption("ALL");

  // Search reads the card text, never ids or raw keys.
  await page.getByLabel("Search records").fill("ecb");
  await expect(page.locator("article")).toHaveCount(0);
  await expect(page.getByText("No matching releases", { exact: true })).toBeVisible();
  await page.getByLabel("Search records").fill("impact");
  await expect(page.locator("article")).toHaveCount(0);
  await page.getByLabel("Search records").fill("auction");
  await expect(page.locator("article")).toHaveCount(1);
});

test("sources page shows per-event verification and the registry with tiers", async ({ page }) => {
  await mockApi(page, {
    "/sources": [
      {
        id: "bls",
        name: "Bureau of Labor Statistics",
        domain: "bls.gov",
        homepage: "https://www.bls.gov/",
        kind: "official_statistics",
        tier: 1,
        official: true,
        independence_group: "bls",
        trust_weight: 1.0,
      },
      { id: "x-unknown", name: "Unknown X account", domain: "x.com", kind: "social_unknown", tier: 5, official: false, trust_weight: 0.1 },
    ],
    "/events": [
      {
        id: "e1",
        summary_fact: "US CPI rose 3.1% year over year",
        verification_status: "PARTIALLY_CONFIRMED",
        fact_confidence: 0.72,
        first_seen_utc: "2026-09-26T12:31:00Z",
        evidence: [
          {
            id: "ev1",
            source_id: "bls",
            source: "Bureau of Labor Statistics",
            url: "https://www.bls.gov/cpi",
            published_at: "2026-09-26T12:30:00Z",
            ingested_at: "2026-09-26T12:31:00Z",
            relation: "supports",
            group: "bls",
            weight: 1.0,
            official: true,
          },
          {
            id: "ev2",
            source_id: "x-unknown",
            source: "Unknown X account",
            published_at: "2026-09-26T12:35:00Z",
            ingested_at: "2026-09-26T12:36:00Z",
            relation: "contradicts",
            group: "x",
            weight: 0.1,
            discovery_only: true,
          },
        ],
      },
      {
        id: "e2",
        summary_fact: "Explosion reported near Hormuz",
        verification_status: "UNCONFIRMED",
        fact_confidence: 0.1,
        first_seen_utc: "2026-09-26T09:00:00Z",
        evidence: [],
      },
    ],
  });
  await page.goto("/sources");
  const cpi = page.locator("article", { hasText: "US CPI rose 3.1%" });
  await expect(cpi).toContainText("1 supporting · 1 contradicting · 2 independent source groups");
  await expect(cpi).toContainText("PARTIALLY CONFIRMED");
  await expect(cpi.locator("tbody tr").first()).toContainText("Tier 1 · weight 1 · official");
  await expect(cpi.locator("tbody tr").first()).toContainText("Sat 26 Sep 15:30");
  await expect(cpi.locator("tbody tr").last()).toContainText("CONTRADICTS");
  await expect(cpi.locator("tbody tr").last()).toContainText("DISCOVERY ONLY");
  await expect(page.locator("article", { hasText: "Hormuz" })).toContainText("No evidence recorded");

  const registry = page.locator("section", { has: page.getByRole("heading", { name: "Source registry" }) });
  await expect(registry.locator("tbody tr").first()).toContainText("Bureau of Labor Statistics");
  await expect(registry.locator("tbody tr").first()).toContainText("Tier 1");
  // Site links come only from the registry's homepage (never https://<domain>).
  await expect(registry.getByRole("link", { name: "bls.gov" })).toHaveAttribute("href", "https://www.bls.gov/");

  await page.screenshot({ path: "test-results/sources-desktop.png", fullPage: true });
  await page.getByLabel("Verification filter").selectOption("UNCONFIRMED");
  await expect(page.locator("article", { hasText: "US CPI rose" })).toHaveCount(0);
});

test("settings tabs: provider health, models with availability, read-only config and alerts", async ({ page }) => {
  await mockApi(page, {
    "/settings": {
      roles: [{ role: "chief_analyst", primary: "qwen3:8b", backup: null, second_opinion: null }],
      refresh: { quotes_seconds: 5, news_seconds: 300, analysis_seconds: 3600 },
      scoring: { version: "7", content_hash: "abcdef0123456789abcdef", weights: { macro: 0.3 }, thresholds: { minimum_calibration: 30 } },
      autolearn: { minimum_observations: 30, mode: "SHADOW", automatic_promotion: false },
      sources: [{ id: "fed", name: "Federal Reserve", domain: "federalreserve.gov", kind: "central_bank", tier: 1, official: true, trust_weight: 1 }],
    },
    "/providers": [
      { name: "binance", status: "ONLINE", latency_ms: 42, last_success: "2026-09-26T09:00:00Z", error: null },
      { name: "x", status: "DEGRADED", latency_ms: null, last_success: null, error: "HTTPStatusError: HTTP 429" },
    ],
    "/models": [
      {
        name: "qwen3:8b",
        capabilities: ["completion", "tools", "thinking"],
        tools_supported: true,
        thinking_supported: true,
        context_window: 40960,
        availability: true,
        status: "AVAILABLE",
        last_seen: "2026-09-26T09:00:00Z",
      },
      { name: "old-model", capabilities: [], availability: false, status: "UNAVAILABLE", context_window: null },
    ],
    "/alerts": [
      { id: "a1", kind: "SETUP", priority: "HIGH", title: "BTC long setup", message: "Macro and technicals aligned", symbol: "BTC", created_at: "2026-09-26T09:00:00Z" },
    ],
  });
  await page.goto("/settings");
  const providers = page.getByRole("table", { name: "Provider health" }).locator("tbody tr");
  await expect(providers.first()).toContainText("x");
  await expect(providers.first()).toContainText("DEGRADED");
  await expect(providers.first()).toContainText("HTTP 429");
  await expect(providers.last()).toContainText("42 ms");

  await page.getByLabel("Admin token").fill("temporary-test-token");
  await page.getByRole("tab", { name: "AI models" }).click();
  const qwen = page.locator("tr", { hasText: "qwen3:8b" });
  await expect(qwen).toContainText("40,960 tokens");
  await expect(qwen).toContainText("thinking");
  await expect(page.locator("tr", { hasText: "old-model" })).toContainText("UNAVAILABLE");
  await expect(page.getByRole("button", { name: "Discover models" })).toBeEnabled();
  await page.screenshot({ path: "test-results/settings-models.png", fullPage: true });
  await page.getByLabel("Backup").click();
  await expect(page.getByRole("option", { name: "old-model (unavailable)" })).toHaveAttribute("aria-disabled", "true");
  await page.keyboard.press("Escape");

  await page.getByRole("tab", { name: "Sources" }).click();
  await expect(page.locator("tr", { hasText: "Federal Reserve" })).toContainText("Tier 1");
  await page.getByRole("tab", { name: "Refresh" }).click();
  await expect(page.locator("tr", { hasText: "news" })).toContainText("every 5 min");
  await expect(page.locator("tr", { hasText: "analysis" })).toContainText("every 1 h");
  await page.getByRole("tab", { name: "Scoring" }).click();
  await expect(page.getByText(/^Rules version 7/)).toBeVisible();
  await page.getByRole("tab", { name: "Alerts" }).click();
  await expect(page.locator("tr", { hasText: "BTC long setup" })).toContainText("HIGH");
  await expect(page.getByText("The backend does not expose alert settings yet.")).toBeVisible();
  await page.getByRole("tab", { name: "Autolearn" }).click();
  await expect(page.locator("tr", { hasText: "mode" })).toContainText("SHADOW");
  await expect(page.getByRole("button", { name: "Create candidate" })).toBeDisabled();
  await page.getByLabel("Asset symbol").fill("btc");
  await expect(page.getByRole("button", { name: "Create candidate" })).toBeEnabled();
  // The token survived every tab switch and was never persisted.
  await expect(page.getByLabel("Admin token")).toHaveValue("temporary-test-token");
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain("temporary-test-token");
});

test("performance shows metrics per horizon, breakdowns and paper trade details", async ({ page }) => {
  // Published signals, in the backend's shape: hit_rate is the signal hit rate, counterfactual outcomes apart.
  const summary = (n: number, hit: number | null) => ({
    observations: n,
    actionable: n,
    counterfactual: 0,
    counterfactual_only: false,
    hit_rate: hit,
    hit_rate_basis: n ? "published" : null,
    counterfactual_hit_rate: null,
    mean_return_pct: n ? 0.42 : null,
    median_return_pct: n ? 0.3 : null,
    mean_mfe_pct: n ? 1.1 : null,
    mean_mae_pct: n ? -0.6 : null,
    brier_score: null,
    calibration_error: null,
    false_positive_rate: n ? 0.4 : null,
    max_drawdown_return_points: n ? 1.5 : null,
  });
  await mockApi(page, {
    // The backend's shape: observations = decisions with a directional outcome, beside decisions_recorded;
    // breakdown_horizon echoes the requested horizon, default_horizon is the configured one.
    "/performance": (url: URL) => ({
      observations: 10,
      decisions_recorded: 12,
      probability: null,
      status: "UNCALIBRATED",
      population: "all",
      default_horizon: "4h",
      breakdown_horizon: url.searchParams.get("horizon") ?? "4h",
      horizons: ["1h", "4h", "next_session"],
      dimensions: ["asset", "regime", "event_type"],
      metrics: {
        "1h": summary(0, null),
        "4h": summary(10, 0.6),
        next_session: summary(0, null),
        paper: {
          closed_trades: 1,
          open_trades: 1,
          hit_rate: 1,
          profit_factor: null,
          mean_return_pct: 0.8,
          basis: "Net of configured fees and slippage; equal_notional sizing on 100000 initial virtual equity",
        },
      },
      breakdowns: {
        asset: { BTC: summary(6, 0.5), ETH: summary(0, null) },
        regime: { RISK_ON: summary(4, 0.75) },
        event_type: { NONE: summary(3, 0.33) },
      },
      paper_trades: [
        {
          id: "t1",
          symbol: "BTC",
          side: 1,
          status: "CLOSED",
          opened_at: "2026-09-25T10:00:00Z",
          closed_at: "2026-09-25T14:00:00Z",
          entry_price: 100000,
          exit_price: 100800,
          stop: 99000,
          target: 101000,
          exit_reason: "TIME",
          return_pct: 0.8,
          mfe_pct: 0.95,
          mae_pct: -0.2,
        },
        { id: "t2", symbol: "ETH", side: -1, status: "OPEN", opened_at: "2026-09-26T09:00:00Z", entry_price: 4000, stop: null, target: 3900, mfe_pct: 0.1, mae_pct: -0.3 },
      ],
    }),
  });
  await page.goto("/performance");
  // Decisions recorded and directional outcomes measured are separate numbers.
  const overview = page.locator(".performance-overview");
  await expect(overview.locator("div", { hasText: "Decisions recorded" }).locator("strong")).toHaveText("12");
  await expect(overview.locator("div", { hasText: "Directional outcomes measured" }).locator("strong")).toHaveText("10");
  // The backend's own count: no "most at any one horizon" estimate.
  await expect(overview).not.toContainText("most at any one horizon");
  await expect(overview).toContainText("UNCALIBRATED");
  const horizons = page.locator("section", { has: page.getByRole("heading", { name: "Metrics by horizon" }) });
  await expect(horizons.locator("tr", { hasText: "4h" })).toContainText("60.0%");
  await expect(horizons.locator("tr", { hasText: "4h" })).toContainText("+0.42%");
  // A horizon without measured outcomes is listed as not enough data, never as 0%.
  await expect(horizons.locator("tr", { hasText: "1h" })).toHaveCount(0);
  await expect(horizons).toContainText("Not enough data yet for: 1h, Next session close.");

  await expect(page.getByRole("heading", { name: "Breakdown by asset · 4h" })).toBeVisible();
  const breakdown = page.locator("section", { has: page.getByRole("heading", { name: /^Breakdown by/ }) });
  // Groups without a measured outcome are listed in a footnote, not as rows of zeros.
  await expect(breakdown.locator("tbody tr", { hasText: "BTC" })).toHaveCount(1);
  await expect(breakdown.locator("tbody tr", { hasText: "ETH" })).toHaveCount(0);
  await expect(breakdown).toContainText("Not enough data yet for: ETH.");
  await page.screenshot({ path: "test-results/performance-desktop.png", fullPage: true });
  await page.getByLabel("Breakdown dimension").selectOption("regime");
  await expect(page.locator("tr", { hasText: "RISK ON" })).toContainText("75.0%");
  await page.getByLabel("Breakdown dimension").selectOption("event_type");
  await expect(breakdown.locator("tbody tr").first()).toContainText("No event");
  await expect(breakdown).not.toContainText("NONE");

  // The default option keeps naming the configured horizon; session horizons read as words.
  const horizonSelect = page.getByLabel("Breakdown horizon");
  await expect(horizonSelect.locator("option").first()).toHaveText("Default horizon (4h)");
  await horizonSelect.selectOption("next_session");
  await expect(page.getByRole("heading", { name: "Breakdown by event type · Next session close" })).toBeVisible();
  await expect(horizonSelect.locator("option").first()).toHaveText("Default horizon (4h)");
  await expect(page.getByRole("main").getByRole("alert")).toHaveCount(0);
  await expect(page.getByText("equal-notional sizing on 100,000 initial virtual equity", { exact: false })).toBeVisible();

  const btc = page.locator("tr", { hasText: "BTC" }).filter({ hasText: "TIME" });
  await expect(btc).toContainText("LONG");
  await expect(btc).toContainText("100,000");
  await expect(btc).toContainText("100,800");
  await expect(btc).toContainText("99,000");
  await expect(btc).toContainText("101,000");
  await expect(btc).toContainText("+0.95%");
  await expect(btc).toContainText("-0.2%");
  await expect(btc).toContainText("Fri 25 Sep 13:00");
  const eth = page.locator("tr", { hasText: "ETH" });
  await expect(eth).toContainText("SHORT");
  await expect(eth).toContainText("None set");
  await expect(eth).toContainText("Open");
  await expect(page.getByText("Paper evaluation only, never real trading.", { exact: false })).toBeVisible();
});

test("social feed labels X statuses and degrades when the endpoint is missing", async ({ page }) => {
  await mockApi(page, { "/social": { status: 404, body: "Not Found" } });
  await page.goto("/social");
  await expect(page.getByText("Social feed not available", { exact: true })).toBeVisible();
  await expect(page.getByText("Unable to reach the data service")).toHaveCount(0);

  await page.unrouteAll({ behavior: "ignoreErrors" });
  await mockApi(page, {
    "/social": [
      { id: "1", text: "Explosion near Hormuz", author_username: "anon123", created_at: "2026-09-26T09:00:00Z", verification_status: "UNVERIFIED" },
      { id: "2", text: "FOMC statement released", author_username: "federalreserve", created_at: "2026-09-26T10:00:00Z", verification_status: "OFFICIAL_STATEMENT", event_id: "e1" },
      { id: "3", text: "Tanker seized", author_username: "wire", created_at: "2026-09-26T08:00:00Z", verification_status: "CONTRADICTED" },
    ],
  });
  await page.reload();
  await expect(page.locator("article")).toHaveCount(3);
  await expect(page.locator("article").first()).toContainText("OFFICIAL STATEMENT");
  await expect(page.locator("article").first().getByRole("link", { name: "Linked event" })).toHaveAttribute("href", "/events/e1");
  await page.getByLabel("Social status filter").selectOption("CONTRADICTED");
  await expect(page.locator("article")).toHaveCount(1);
  await expect(page.locator("article")).toContainText("Tanker seized");
});

test("enabling notifications on the alerts page turns on the shared opt-in", async ({ page }) => {
  await page.addInitScript(() => {
    class FakeNotification {
      static get permission(): NotificationPermission {
        return sessionStorage.getItem("fake-permission") === "granted" ? "granted" : "default";
      }
      static async requestPermission() {
        sessionStorage.setItem("fake-permission", "granted");
        return "granted" as const;
      }
    }
    Object.defineProperty(window, "Notification", { value: FakeNotification, configurable: true });
  });
  await mockApi(page, { "/alerts": [] });
  await page.goto("/alerts");
  await page.getByRole("button", { name: "Enable browser notifications" }).click();
  await expect(page.getByRole("button", { name: "Disable browser notifications" })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("mi.alerts.browser-notifications"))).toBe("on");
  // The dashboard panel reads the same preference.
  await page.goto("/");
  await page.getByText("Verified news and alerts", { exact: true }).click();
  await expect(page.getByRole("button", { name: "Disable browser notifications" })).toBeVisible();
});

test("provider health and alerts routes render, and pages fit a phone", async ({ page }) => {
  await mockApi(page, {
    "/providers": [{ name: "ollama", status: "AUTH_ERROR", latency_ms: null, last_success: null, error: "HTTP 401" }],
    "/alerts": [],
  });
  await page.goto("/providers");
  await expect(page.getByRole("heading", { name: "Provider health", exact: true })).toBeVisible();
  await expect(page.locator("tr", { hasText: "ollama" })).toContainText("AUTH ERROR");
  await expect(page).toHaveTitle(/Provider health/);
  await page.goto("/alerts");
  await expect(page.getByText("No alerts yet", { exact: true })).toBeVisible();

  // Every section is reachable from the main navigation.
  const nav = page.getByRole("navigation", { name: "Main navigation" });
  for (const [name, href] of [
    ["X / Social", "/social"],
    ["Alerts", "/alerts"],
    ["Providers", "/providers"],
  ]) {
    await expect(nav.getByRole("link", { name, exact: true })).toHaveAttribute("href", href);
  }
  await expect(page.getByRole("banner").getByRole("link", { name: /providers online/ })).toHaveAttribute(
    "href",
    "/providers",
  );

  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of ["/fx", "/calendar", "/sources", "/settings", "/performance", "/social"]) {
    await page.goto(path);
    await expect(page.locator("h1")).toBeVisible();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
      `${path} overflows horizontally`,
    ).toBe(true);
  }
});

test("performance never claims calibration work while no directional outcome is measured", async ({ page }) => {
  const none = { observations: 0, hit_rate: null };
  await mockApi(page, {
    "/performance": {
      observations: 0,
      decisions_recorded: 676,
      probability: null,
      status: "INSUFFICIENT_DATA",
      default_horizon: "4h",
      breakdown_horizon: "4h",
      horizons: ["1h", "4h"],
      metrics: { "1h": none, "4h": none },
      breakdowns: { asset: { BTC: none, ETH: none } },
      paper_trades: [],
    },
  });
  await page.goto("/performance");
  const overview = page.locator(".performance-overview");
  await expect(overview.locator("div", { hasText: "Decisions recorded" }).locator("strong")).toHaveText("676");
  await expect(overview.locator("div", { hasText: "Directional outcomes measured" }).locator("strong")).toHaveText("0");
  await expect(overview).not.toContainText("most at any one horizon");
  await expect(overview).toContainText("NOT ENOUGH DATA");
  await expect(overview).not.toContainText("UNCALIBRATED");
  const breakdown = page.locator("section", { has: page.getByRole("heading", { name: /^Breakdown by/ }) });
  await expect(breakdown.locator("tbody tr")).toHaveCount(0);
  await expect(breakdown).toContainText("No group has a measured directional outcome yet (2 recorded without one).");
});

test("performance labels counterfactual WAIT and lean outcomes and never shows them as a signal hit rate", async ({ page }) => {
  // The live shape on 2 Oct 2026 (backend evaluation.summarize): every measured outcome is counterfactual, so the
  // signal hit rate is null and the counterfactual rate is reported apart.
  const counterfactual = (n: number, hit: number) => ({
    observations: n,
    actionable: 0,
    counterfactual: n,
    counterfactual_only: true,
    hit_rate: null,
    hit_rate_basis: null,
    counterfactual_hit_rate: hit,
    mean_return_pct: 0.02,
    false_positive_rate: null,
    counterfactual_failure_rate: 1 - hit,
  });
  const published = {
    observations: 4,
    actionable: 4,
    counterfactual: 0,
    counterfactual_only: false,
    hit_rate: 0.75,
    hit_rate_basis: "published",
    counterfactual_hit_rate: null,
    mean_return_pct: 0.4,
    false_positive_rate: 0.25,
  };
  await mockApi(page, {
    "/performance": (url: URL) => {
      const population = url.searchParams.get("population") ?? "all";
      const signals = population === "actionable";
      return {
        observations: signals ? 4 : 148,
        decisions_recorded: 1514,
        published_signals: signals ? 4 : 0,
        counterfactual_decisions: signals ? 0 : 148,
        probability: null,
        status: "UNCALIBRATED",
        population,
        default_horizon: "4h",
        breakdown_horizon: "4h",
        horizons: ["5m", "4h"],
        metrics: { "5m": signals ? published : counterfactual(148, 0.554), "4h": signals ? published : counterfactual(60, 0.433) },
        breakdowns: { asset: { BTC: signals ? published : counterfactual(20, 0.5) } },
        paper_trades: [],
      };
    },
  });
  await page.goto("/performance");
  const note = page.getByRole("note", { name: "What these figures measure" });
  await expect(note).toHaveText(
    "No published signal has a measured outcome yet. Every figure below is counterfactual: what following the score sign of WAIT and lean decisions would have returned. It is not a signal hit rate.",
  );
  const mix = page.getByTestId("outcome-mix");
  await expect(mix.locator("strong")).toHaveText("0 / 148");
  await expect(mix).toContainText("Published signals / counterfactual");
  await expect(mix).toContainText("decisions with a measured outcome");
  const horizons = page.locator("section", { has: page.getByRole("heading", { name: "Metrics by horizon" }) });
  await expect(horizons.locator("caption")).toHaveText("Outcome metrics by horizon");
  await expect(page.getByText("Signal outcome metrics", { exact: false })).toHaveCount(0);
  const fiveMin = horizons.locator("tbody tr", { hasText: "5m" });
  const cell = (label: string) => fiveMin.locator(`[data-label="${label}"]`);
  await expect(cell("Outcomes")).toHaveText(/^148\s*0 published · 148 counterfactual$/);
  // No signal hit rate without a published signal; the counterfactual rate has its own column.
  await expect(cell("Signal hit rate")).toHaveText("No signal");
  await expect(cell("Signal hit rate").locator("span")).toHaveAttribute("title", "No published signal has a measured outcome in this row");
  await expect(cell("Counterfactual hit rate")).toHaveText("55.4%");
  await expect(cell("False-positive rate")).toHaveText("No signal");
  const breakdown = page.locator("section", { has: page.getByRole("heading", { name: /^Breakdown by/ }) });
  const btc = breakdown.locator("tbody tr", { hasText: "BTC" });
  await expect(btc.locator('[data-label="Signal hit rate"]')).toHaveText("No signal");
  await expect(btc.locator('[data-label="Counterfactual hit rate"]')).toHaveText("50.0%");
  await page.screenshot({ path: "test-results/performance-counterfactual.png", fullPage: true });

  // Published signals only: their hit rate and false-positive rate, no notice.
  await page.getByLabel("Population").selectOption("actionable");
  await expect(note).toHaveCount(0);
  await expect(mix.locator("strong")).toHaveText("4 / 0");
  await expect(cell("Outcomes")).toHaveText(/^4\s*4 published · 0 counterfactual$/);
  await expect(cell("Signal hit rate")).toHaveText("75.0%");
  await expect(cell("Counterfactual hit rate")).toHaveText("Not enough data");
  await expect(cell("False-positive rate")).toHaveText("25.0%");

  // The counterfactual population says what it is, without claiming that no signal exists.
  await page.getByLabel("Population").selectOption("counterfactual");
  await expect(note).toHaveText(/^Counterfactual outcomes: what following the score sign/);
  await expect(cell("Signal hit rate")).toHaveText("No signal");
  await expect(cell("Counterfactual hit rate")).toHaveText("55.4%");
});

test("alerts page loads the newest 500, pages them, links providers and marks them read", async ({ page }) => {
  const base = Date.parse("2026-09-26T09:00:00Z");
  const alerts: Record<string, unknown>[] = Array.from({ length: 120 }, (_, i) => ({
    id: `a${i}`,
    kind: i % 2 ? "LEVEL_BREAK" : "SETUP",
    priority: i % 3 === 0 ? "HIGH" : "LOW",
    title: `Alert number ${i}`,
    message: "Level crossed",
    symbol: "BTC",
    created_at: new Date(base - i * 60_000).toISOString(),
  }));
  alerts.push({
    id: "p1",
    kind: "PROVIDER",
    priority: "MEDIUM",
    title: "Finnhub (equities): rate limited",
    message: "Rate limited (HTTP 429)",
    symbol: null,
    label: "Finnhub (equities)",
    metrics: { provider: "finnhub", status: "RATE_LIMITED" },
    created_at: "2026-09-26T09:30:00Z",
  });
  await mockApi(page, { "/alerts": alerts });
  const request = page.waitForRequest((r) => r.url().includes("/api/alerts?limit=500"));
  await page.goto("/alerts");
  await request;
  const rows = page.locator("tbody tr");
  await expect(page.getByText("Showing 50 of 121 alerts", { exact: true })).toBeVisible();
  await expect(rows).toHaveCount(50);

  // Provider alerts lead to Provider health under the provider's display name.
  const provider = page.locator("tr", { hasText: "Finnhub (equities): rate limited" });
  await expect(provider).toContainText("Provider");
  await expect(provider.getByRole("link", { name: "Finnhub (equities)" })).toHaveAttribute("href", "/providers");
  await expect(page.locator("tr", { hasText: "Alert number 1" }).first()).toContainText("Level break");

  await page.getByRole("button", { name: "Show 50 more" }).click();
  await expect(rows).toHaveCount(100);
  await page.getByRole("button", { name: "Show 21 more" }).click();
  await expect(rows).toHaveCount(121);
  await expect(page.getByText("121 alerts", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Show \d+ more$/ })).toHaveCount(0);

  await page.getByLabel("Alert priority filter").selectOption("HIGH");
  await expect(page.getByText("40 alerts", { exact: true })).toBeVisible();

  // Unread is the shared per-viewer marker (the header bell reads it too).
  await page.getByRole("button", { name: "Mark 121 read" }).click();
  await expect(page.getByRole("button", { name: /^Mark \d+ read$/ })).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem("mi.alerts.last-read"))).not.toBeNull();
});

test("an alert stored with only the provider id is named from the provider list, never by the id", async ({ page }) => {
  await mockApi(page, {
    "/dashboard": {
      ...emptyDashboard,
      providers: [
        { name: "gdelt_events", label: "GDELT events", status: "DEGRADED", latency_ms: null, last_success: null, error: "Not found (HTTP 404)" },
      ],
    },
    "/alerts": [
      {
        id: "legacy",
        kind: "PROVIDER",
        priority: "MEDIUM",
        title: "GDELT events: degraded",
        message: "Not found (HTTP 404)",
        symbol: null,
        metrics: { provider: "gdelt_events", status: "DEGRADED" },
        created_at: "2026-09-26T09:30:00Z",
      },
      {
        id: "unknown",
        kind: "PROVIDER",
        priority: "LOW",
        title: "Retired feed: offline",
        message: "Connection failed",
        symbol: null,
        metrics: { provider: "retired_feed", status: "OFFLINE" },
        created_at: "2026-09-26T09:00:00Z",
      },
    ],
  });
  await page.goto("/alerts");
  const legacy = page.locator("tr", { hasText: "GDELT events: degraded" });
  await expect(legacy.getByRole("link", { name: "GDELT events" })).toHaveAttribute("href", "/providers");
  await expect(legacy).not.toContainText("gdelt_events");
  // A provider the list does not know still leads to Provider health, without its id.
  const unknown = page.locator("tr", { hasText: "Retired feed: offline" });
  await expect(unknown.getByRole("link", { name: "Provider health" })).toHaveAttribute("href", "/providers");
  await expect(unknown).not.toContainText("retired_feed");
});

test("alerts page says when even the newest 500 are all unread", async ({ page }) => {
  const base = Date.parse("2026-09-26T09:00:00Z");
  const alerts = Array.from({ length: 500 }, (_, i) => ({
    id: `a${i}`,
    kind: "SETUP",
    priority: "LOW",
    title: `Alert number ${i}`,
    symbol: "BTC",
    created_at: new Date(base - i * 60_000).toISOString(),
  }));
  await mockApi(page, { "/alerts": alerts });
  await page.goto("/alerts");
  await expect(page.getByText("Showing 50 of 500 alerts · only the newest 500 are loaded", { exact: true })).toBeVisible();
  // Older alerts may be unread too: the count is a lower bound.
  await expect(page.getByRole("button", { name: "Mark 500+ read", exact: true })).toBeVisible();
});

test("a failed request shows the error only, never zero counts or empty states", async ({ page }) => {
  const failure = { status: 500, body: JSON.stringify({ detail: "Internal Server Error" }) };
  await mockApi(page, { "/calendar": failure, "/fx": failure, "/performance": failure, "/alerts": failure });
  for (const path of ["/calendar", "/fx", "/performance", "/alerts"]) {
    await page.goto(path);
    await expect(page.getByRole("main").getByRole("alert").first(), `${path} shows the error`).toBeVisible();
    await expect(page.getByRole("main").getByText(/^\d+ (records?|alerts?)$/), `${path} states no count`).toHaveCount(0);
    for (const empty of [
      "No matching releases",
      "No releases in the calendar",
      "No FX data",
      "No alerts yet",
      "No paper trades yet",
      "Decisions recorded",
    ])
      await expect(page.getByText(empty, { exact: true }), `${path}: ${empty}`).toHaveCount(0);
  }
});

test("wide tables stack on a phone and the calendar toolbar wraps on a tablet", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-09-26T08:00:00Z") });
  await mockApi(page, {
    "/fx": fxRows,
    "/alerts": [
      { id: "a1", kind: "SETUP", priority: "HIGH", title: "BTC long setup", message: "Macro and technicals aligned", symbol: "BTC", created_at: "2026-09-26T07:00:00Z" },
    ],
    "/calendar": [
      { id: "pce", name: "US personal income & outlays (PCE)", country: "US", scheduled_utc: "2026-09-26T12:30:00Z", impact: 9, time_known: true },
    ],
  });
  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of ["/fx", "/alerts"]) {
    await page.goto(path);
    await expect(page.locator("tbody tr").first()).toBeVisible();
    // Every column is on screen: the table does not scroll sideways and its header row is replaced by labels.
    const box = page.locator("[data-slot=table-container]").first();
    expect(await box.evaluate((el) => el.scrollWidth <= el.clientWidth + 1), `${path} table scrolls sideways`).toBe(true);
    await expect(page.locator("thead").first()).toBeHidden();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `${path} overflows`).toBe(true);
  }
  await page.goto("/fx");
  const firstPrice = page.locator("tbody tr td[data-label=Price]").first();
  expect(await firstPrice.evaluate((el) => getComputedStyle(el, "::before").content)).toContain("Price");

  // The release name gets its own full-width line on a phone.
  await page.goto("/calendar");
  const name = page.getByRole("heading", { name: "US personal income & outlays (PCE)" });
  expect((await name.boundingBox())?.width ?? 0).toBeGreaterThan(250);

  // The search box and three filters wrap instead of widening the page (701-955 px used to scroll).
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto("/calendar");
  await expect(page.getByLabel("Release filter")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("an overflowing table is a labelled region the keyboard can scroll", async ({ page }) => {
  const trade = {
    id: "t1",
    symbol: "BTC",
    side: 1,
    status: "CLOSED",
    opened_at: "2026-09-25T10:00:00Z",
    closed_at: "2026-09-25T14:00:00Z",
    entry_price: 100000,
    exit_price: 100800,
    stop: 99000,
    target: 101000,
    exit_reason: "TIME",
    return_pct: 0.8,
    mfe_pct: 0.95,
    mae_pct: -0.2,
  };
  await mockApi(page, {
    "/performance": {
      observations: 1,
      decisions_recorded: 1,
      status: "UNCALIBRATED",
      default_horizon: "4h",
      breakdown_horizon: "4h",
      horizons: ["4h"],
      metrics: { "4h": { observations: 1, hit_rate: 1 }, paper: { closed_trades: 1, open_trades: 0 } },
      breakdowns: {},
      paper_trades: [trade],
    },
  });
  // Wider than the phone layout (tables stack below 768 px), too narrow for the 13 paper-trade columns.
  await page.setViewportSize({ width: 800, height: 1024 });
  await page.goto("/performance");
  const region = page.getByRole("region", { name: "Paper trades (scrolls sideways)", exact: true });
  await expect(region).toBeVisible();
  await expect(region).toHaveAttribute("tabindex", "0");
  await expect(page.getByText("Scroll sideways to see every column.", { exact: true }).first()).toBeVisible();
  await region.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => region.evaluate((el) => el.scrollLeft)).toBeGreaterThan(0);
});
