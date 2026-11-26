import { test, expect, type Page } from "@playwright/test";

/*
 * Market coverage labelling (docs/MARKET_COVERAGE.md, §27, §46-47, §49) with
 * intercepted, test-only API responses. Saturday 26 Sep 2026 12:00 UTC.
 * Every value keeps its source, as-of time and basis; proxies are labelled
 * as proxies and never shown as the asset's level.
 */
const NOW = new Date("2026-09-26T12:00:00Z");

const session = (state = "FRIDAY CLOSE") => ({
  state,
  label: state,
  venue: "Test venue",
  basis: "Test session calendar",
});

const treasury2y = {
  symbol: "US2Y",
  name: "US 2Y yield",
  asset_class: "bond",
  session: session(),
  coverage: { status: "DAILY_ONLY", providers: ["treasury"], missing_keys: [] },
  proxy: null,
  decision: null,
  quote: {
    price: 4.81,
    timestamp: "2026-09-25T20:00:00Z",
    provider: "treasury",
    market_status: "FRIDAY CLOSE",
    freshness_status: "STALE",
    change_pct: null,
    change_bp: -6,
    change_basis: "previous_close",
    previous_close: 4.87,
    previous_close_at: "2026-09-24T20:00:00Z",
    data_class: "DAILY_CLOSE",
    close_status: "CURRENT",
    as_of_label: "Fri 25 Sep 16:00 ET",
    label: "US Treasury 2Y par yield",
    attribution: "Source: U.S. Department of the Treasury",
    delay_label: "daily close",
  },
};

const eurusd = {
  symbol: "EURUSD",
  name: "EUR/USD",
  asset_class: "fx",
  // A weekday-style live session: a current fix must not turn the badge STALE.
  session: session("LIVE"),
  coverage: {
    status: "DAILY_ONLY",
    providers: ["ecb", "massive"],
    missing_keys: ["MASSIVE_API_KEY"],
  },
  proxy: null,
  decision: {
    id: "d-eur",
    direction: "LONG",
    category: "LONG SETUP",
    score: 60,
    evidence_confidence: 80,
    market_confirmation: 70,
    data_quality: 100,
    reasons: [],
    confirmation: "Test",
    invalidation: "Test",
    audit_status: "PASS",
    components: {},
  },
  quote: {
    price: 1.1403,
    timestamp: "2026-09-25T12:15:00Z",
    provider: "ecb",
    market_status: "LIVE",
    freshness_status: "STALE",
    change_pct: 0.317,
    change_basis: "previous_fix",
    previous_close: 1.1367,
    previous_close_at: "2026-09-24T12:15:00Z",
    data_class: "DAILY_CLOSE",
    close_status: "CURRENT",
    as_of_label: "Fri 25 Sep 14:15 CET",
    attribution: "Source: ECB",
  },
};

const dxy = {
  symbol: "DXY",
  name: "US dollar index",
  asset_class: "fx",
  session: session(),
  coverage: { status: "DAILY_ONLY", providers: ["ecb-derived"], missing_keys: [] },
  proxy: null,
  decision: null,
  quote: {
    price: 97.412,
    timestamp: "2026-09-25T12:15:00Z",
    provider: "ecb-derived",
    market_status: "FRIDAY CLOSE",
    freshness_status: "STALE",
    change_pct: -0.21,
    change_basis: "previous_fix",
    previous_close_at: "2026-09-24T12:15:00Z",
    data_class: "DERIVED_DAILY",
    close_status: "CURRENT",
    derived: true,
    label: "USD index replica (ICE DXY formula) · ECB fix 25 Sep · derived by this app",
  },
};

const sp500 = {
  symbol: "SP500",
  name: "S&P 500",
  asset_class: "index",
  session: session(),
  coverage: {
    status: "PROXY_ONLY",
    providers: ["fred", "massive"],
    missing_keys: ["FRED_API_KEY", "MASSIVE_API_KEY"],
  },
  quote: null,
  decision: null,
  proxy: {
    symbol: "SPY",
    proxy_for: "SP500",
    label: "SPY ETF — proxy for S&P 500 direction",
    caveat:
      "ETF price, not the index level (about 1/10 of the index); direction only",
    price: 655.21,
    change_pct: 0.42,
    change_basis: "previous_close",
    previous_close: 652.47,
    timestamp: "2026-09-25T20:00:00Z",
    provider: "finnhub",
    relation: "direction_only",
    market_status: "FRIDAY CLOSE",
    freshness_status: "STALE",
  },
};

const nasdaq = {
  symbol: "NASDAQ",
  name: "Nasdaq",
  asset_class: "index",
  session: session(),
  coverage: {
    status: "NOT_CONFIGURED",
    providers: ["fred", "massive"],
    missing_keys: ["FRED_API_KEY", "MASSIVE_API_KEY"],
  },
  quote: null,
  proxy: null,
  decision: null,
};

const wti = {
  symbol: "WTI",
  name: "WTI crude",
  asset_class: "commodity",
  session: session(),
  coverage: { status: "DAILY_ONLY", providers: ["eia"], missing_keys: [] },
  proxy: null,
  decision: null,
  quote: {
    price: 91.4,
    timestamp: "2026-09-14T19:30:00Z",
    provider: "eia",
    market_status: "FRIDAY CLOSE",
    freshness_status: "STALE",
    change_pct: -1.1,
    change_basis: "previous_close",
    previous_close_at: "2026-09-11T19:30:00Z",
    data_class: "DAILY_CLOSE",
    close_status: "STALE",
    // The EIA template (config official_data) already says "as of".
    as_of_label: "EIA spot, as of Mon 14 Sep",
    attribution: "Source: U.S. Energy Information Administration",
  },
};

// Weekend gold: no level, a PAXG proxy whose change is a rolling 24h window (Binance has no close).
const gold = {
  symbol: "GOLD",
  name: "Gold",
  asset_class: "commodity",
  session: session(),
  coverage: { status: "PROXY_ONLY", providers: ["massive"], missing_keys: ["MASSIVE_API_KEY"] },
  quote: null,
  decision: null,
  proxy: {
    symbol: "PAXG",
    proxy_for: "GOLD",
    label: "PAX Gold (PAXG) — proxy for gold direction",
    caveat: "Gold-backed token traded 24/7; direction only",
    price: 3772.5,
    change_pct: 0.42,
    change_basis: "rolling_24h",
    timestamp: "2026-09-26T11:59:00Z",
    provider: "binance",
    relation: "direction_only",
    market_status: "LIVE",
    freshness_status: "FRESH",
  },
};

// FRED daily close: its API terms require the FRED notice wherever it is shown.
const brent = {
  symbol: "BRENT",
  name: "Brent crude",
  asset_class: "commodity",
  session: session(),
  coverage: { status: "DAILY_ONLY", providers: ["eia", "fred"], missing_keys: [] },
  proxy: null,
  decision: null,
  quote: {
    price: 94.12,
    timestamp: "2026-09-25T20:00:00Z",
    provider: "fred",
    market_status: "FRIDAY CLOSE",
    freshness_status: "STALE",
    change_pct: 0.8,
    change_basis: "previous_close",
    previous_close_at: "2026-09-24T20:00:00Z",
    data_class: "DAILY_CLOSE",
    close_status: "CURRENT",
    as_of_label: "Fri 25 Sep (FRED DCOILBRENTEU)",
  },
};

// Friday's last live print of the asset's own feed, seen on Saturday.
const gbpusd = {
  symbol: "GBPUSD",
  name: "GBP/USD",
  asset_class: "fx",
  session: session(),
  coverage: { status: "OK", providers: ["massive"], missing_keys: [] },
  proxy: null,
  decision: null,
  quote: {
    price: 1.33412,
    timestamp: "2026-09-25T20:59:00Z",
    provider: "massive",
    market_status: "FRIDAY CLOSE",
    freshness_status: "STALE",
    change_pct: -0.12,
    change_basis: "previous_close",
    data_class: "LIVE",
    close_status: null,
  },
};

// Every source switched off by the administrator, no data.
const usdjpy = {
  symbol: "USDJPY",
  name: "USD/JPY",
  asset_class: "fx",
  session: session(),
  coverage: { status: "DISABLED", providers: [], missing_keys: [], disabled: ["ecb"] },
  quote: null,
  proxy: null,
  decision: null,
};

// Keyless source configured, nothing fetched yet.
const us10y = {
  symbol: "US10Y",
  name: "US 10Y yield",
  asset_class: "bond",
  session: session(),
  coverage: { status: "NO_DATA_YET", providers: ["treasury"], missing_keys: [] },
  quote: null,
  proxy: null,
  decision: null,
};

const FRED_NOTICE =
  "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.";

function coverageDashboard(regime: Record<string, unknown>) {
  return {
    as_of: NOW.toISOString(),
    timezone: "Europe/Bucharest",
    last_analysis: NOW.toISOString(),
    regimes: (regime.regimes as string[]) ?? [],
    regime,
    assets: [treasury2y, eurusd, dxy, sp500, nasdaq, wti, gold, brent, gbpusd, usdjpy, us10y],
    events: [],
    calendar: [],
    providers: [],
    alerts: [],
    notices: [{ id: "fred_api", text: FRED_NOTICE, providers: ["fred"] }],
  };
}

const lastCloseRegime = {
  regimes: ["RISK_OFF"],
  posture: "DEFENSIVE",
  confidence: 55,
  coverage_pct: 40,
  basis: "last_close",
  basis_label: "last close Fri 25 Sep, markets closed",
  missing_required: [],
  drivers: [
    {
      driver: "Rates",
      direction: "down",
      arrow: "↓",
      input: "US2Y -6 bp",
      basis: "last_close",
    },
    {
      // No `input` text: the chip is built from the measured inputs.
      driver: "USD",
      direction: "down",
      arrow: "↓",
      inputs: [{ feature: "DXY", field: "change_pct", value: -0.21, vote: -1 }],
      missing: [],
      basis: "last_close",
    },
    {
      // WTI has a close, but a late one: not "no data yet".
      driver: "Oil",
      direction: "unavailable",
      arrow: null,
      missing: ["WTI"],
    },
    {
      driver: "Risk appetite",
      direction: "unavailable",
      arrow: null,
      missing: ["NASDAQ", "SP500"],
    },
    {
      driver: "Credit",
      direction: "unavailable",
      arrow: null,
      missing: ["CREDIT:BAA10Y"],
    },
  ],
};

const insufficientRegime = {
  regimes: [],
  posture: "INSUFFICIENT_DATA",
  confidence: null,
  coverage_pct: 20,
  basis: "last_close",
  basis_label: "last close Fri 25 Sep, markets closed",
  missing_required: ["NASDAQ", "SP500", "WTI", "USDJPY", "US10Y", "CURVE:US10Y-US2Y"],
  drivers: [],
};

async function serve(page: Page, routes: Record<string, unknown>) {
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    return route.fulfill({ json: path in routes ? routes[path] : [] });
  });
}

const tile = (page: Page, label: string) =>
  page.locator(".market-tile").filter({
    has: page.locator("strong", { hasText: new RegExp(`^${label}$`) }),
  });

test("compact strip tiles show symbol, one tag, level and change; basis, session, as-of and proxies are in the tooltip", async ({
  page,
}) => {
  await serve(page, { "/api/dashboard": coverageDashboard(lastCloseRegime) });
  await page.goto("/");

  // Daily close with a bp change: one CLOSE <day> tag in the info tone, never red.
  const us2y = tile(page, "US2Y");
  await expect(us2y).toHaveText(/^US2Y\s*CLOSE Fri\s*4\.81%\s*-6 bp$/);
  await expect(us2y.locator(".market-chip")).toHaveCount(1);
  await expect(us2y.locator(".market-chip")).toHaveText("CLOSE Fri");
  await expect(us2y.locator(".market-chip")).toHaveAttribute(
    "data-variant",
    "info",
  );
  await expect(us2y.locator(".market-tile-value span")).toHaveClass(/down/);
  await expect(us2y).toHaveAttribute("data-chip", "DAILY CLOSE");
  await expect(us2y.locator(".text-destructive")).toHaveCount(0);
  await expect(us2y.locator('[data-variant="negative"]')).toHaveCount(0);
  // The tooltip carries what the tile leaves out: change basis, data class, session, as-of and source.
  await expect(us2y).toHaveAttribute("title", /-6 bp vs prev close 24 Sep/);
  await expect(us2y).toHaveAttribute("title", /^US Treasury 2Y par yield\n/);
  await expect(us2y).toHaveAttribute("title", /\nDAILY CLOSE\n/);
  await expect(us2y).toHaveAttribute("title", /Session state: FRIDAY CLOSE/);
  // The source as its attribution names it, never the provider id.
  await expect(us2y).toHaveAttribute(
    "title",
    /as of Fri 25 Sep 16:00 ET · U\.S\. Department of the Treasury/,
  );
  await expect(us2y).not.toHaveAttribute("title", /· treasury/);
  await expect(us2y).not.toHaveAttribute("title", /STALE/);
  await expect(us2y).toHaveAttribute(
    "title",
    /Source: U\.S\. Department of the Treasury/,
  );

  // A current ECB fix while the market trades: a close tag, never LIVE; the tooltip says MARKET OPEN.
  const eur = tile(page, "EURUSD");
  await expect(eur).toHaveText(/^EURUSD\s*CLOSE Fri\s*1\.1403\s*\+0\.32%$/);
  await expect(eur.locator(".market-chip")).toHaveAttribute("data-variant", "info");
  await expect(eur.locator(".market-tile-value span")).toHaveClass(/text-positive/);
  await expect(eur).toHaveAttribute("title", /\+0\.32% vs prev ECB fix 24 Sep/);
  await expect(eur).toHaveAttribute("title", /Session state: MARKET OPEN/);
  await expect(eur).toHaveAttribute("title", /as of Fri 25 Sep 14:15 CET · ECB/);
  await expect(eur).not.toHaveAttribute("title", /LIVE|STALE/);

  // Derived by this app.
  const dollar = tile(page, "DXY");
  await expect(dollar).toHaveText(/^DXY\s*DERIVED Fri\s*97\.412\s*-0\.21%$/);
  await expect(dollar).toHaveAttribute("data-chip", "DERIVED");
  await expect(dollar).toHaveAttribute("title", /USD index replica/);
  await expect(dollar).toHaveAttribute("title", /Derived by this app; not an official index value/);

  // Proxy only: no level, the proxy's direction under its own name.
  const spx = tile(page, "S&P 500");
  await expect(spx).toHaveText(/^S&P 500\s*PROXY via SPY\s*—\s*\+0\.42%$/);
  await expect(spx.locator("b")).toHaveText("—");
  await expect(spx.locator(".market-chip")).toHaveAttribute("data-variant", "neutral");
  await expect(spx).toHaveAttribute("data-chip", "PROXY");
  // Friday's last SPY print on a Saturday is not a late quote.
  await expect(spx.locator('[data-variant="negative"]')).toHaveCount(0);
  await expect(spx).toHaveAttribute("title", /No level · proxy only/);
  await expect(spx).toHaveAttribute("title", /Session state: FRIDAY CLOSE/);
  await expect(spx).not.toHaveAttribute("title", /UNKNOWN|655\.21|stale/i);
  // A time formatted by the app names its zone; service labels keep theirs (ET above).
  await expect(spx).toHaveAttribute(
    "title",
    /SPY ETF — proxy for S&P 500 direction[\s\S]*Proxy change: \+0\.42% vs prev close \(SPY\)[\s\S]*not the index level[\s\S]*finnhub, as of Fri 25 Sep 23:00 Bucharest[\s\S]*needs FRED_API_KEY or MASSIVE_API_KEY/,
  );

  // PAXG's change is a rolling 24h window, never read as gold's Friday change.
  const gld = tile(page, "GOLD");
  await expect(gld).toHaveText(/^GOLD\s*PROXY via PAXG\s*—\s*\+0\.42%$/);
  await expect(gld).toHaveAttribute("title", /Session state: FRIDAY CLOSE/);
  await expect(gld).not.toHaveAttribute("title", /3,772/);
  await expect(gld).toHaveAttribute(
    "title",
    /Proxy change: \+0\.42% over a rolling 24 hours \(PAXG; not since a close\)/,
  );

  // Friday's last print of the asset's own feed: quiet LAST PRINT, not a red STALE.
  const cable = tile(page, "GBPUSD");
  await expect(cable).toHaveText(/^GBPUSD\s*LAST PRINT\s*1\.33412\s*-0\.12%$/);
  await expect(cable.locator(".market-chip")).toHaveAttribute("data-variant", "outline");
  await expect(cable.locator('[data-variant="negative"]')).toHaveCount(0);
  await expect(cable).toHaveAttribute("title", /as of Fri 25 Sep 23:59 Bucharest · massive/);

  // Switched off by the administrator: a quiet DISABLED, not "not configured".
  const yen = tile(page, "USDJPY");
  await expect(yen).toHaveText(/^USDJPY\s*DISABLED\s*—$/);
  await expect(yen.locator(".market-chip")).toHaveAttribute("data-variant", "outline");
  await expect(yen).toHaveAttribute("title", /Source switched off/);
  await expect(yen).toHaveAttribute("title", /DISABLED · switched off in Settings/);
  await expect(yen).toHaveAttribute("title", /Switched off in Settings → Data providers: ecb/);
  await expect(yen).not.toHaveAttribute("title", /NOT CONFIGURED/);

  // Not configured: the tooltip names the key and keeps the session state.
  const ndx = tile(page, "NASDAQ");
  await expect(ndx).toHaveText(/^NASDAQ\s*NOT CONFIGURED\s*—$/);
  await expect(ndx).toHaveAttribute("title", /No source configured/);
  await expect(ndx).toHaveAttribute(
    "title",
    /NOT CONFIGURED · needs FRED_API_KEY or MASSIVE_API_KEY/,
  );
  await expect(ndx).toHaveAttribute("title", /Session state: FRIDAY CLOSE/);
  await expect(ndx).not.toHaveAttribute("title", /UNKNOWN/);

  // No data yet from a keyless source: said as such, without a change.
  await expect(tile(page, "US10Y")).toHaveText(/^US10Y\s*NO DATA YET\s*—$/);

  // A genuinely late close stays prominent.
  const oil = tile(page, "WTI");
  await expect(oil).toHaveText(/^WTI\s*STALE\s*91\.4\s*-1\.10%$/);
  await expect(oil.locator(".market-chip")).toHaveAttribute(
    "data-variant",
    "negative",
  );
  await expect(oil).toHaveAttribute("title", /STALE · late close/);
  await expect(oil).toHaveAttribute(
    "title",
    /EIA spot, as of Mon 14 Sep · U\.S\. Energy Information Administration/,
  );
  await expect(oil).not.toHaveAttribute("title", /as of EIA spot/);

  // Daily closes never become setups.
  await expect(page.locator(".setup")).toHaveCount(0);

  // FRED data is on screen: the FRED API notice sits in the dashboard footer.
  await expect(
    page.getByRole("list", { name: "Data provider notices" }).getByRole("listitem"),
  ).toHaveText([FRED_NOTICE]);
  await page.locator(".market-strip").screenshot({
    path: "test-results/coverage-strip.png",
  });
});

test("regime banner shows the last-close basis and driver inputs", async ({
  page,
}) => {
  await serve(page, { "/api/dashboard": coverageDashboard(lastCloseRegime) });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "DEFENSIVE · RISK OFF" }),
  ).toBeVisible();
  await page.getByText("Data coverage and basis", { exact: true }).click();
  await expect(
    page.getByText("Basis: last close Fri 25 Sep, markets closed"),
  ).toBeVisible();
  await expect(page.getByText(/across last-close observations/)).toBeVisible();
  // The as-of time names its zone.
  await expect(page.locator(".regime-asof")).toHaveText(/^As of Sat 26 Sep 15:00 Bucharest$/);
  const drivers = page.getByRole("list", { name: "Regime drivers" });
  await expect(drivers.getByRole("listitem")).toHaveText([
    "Rates↓downUS2Y -6 bp· last close",
    "USD↓downDXY -0.21%· last close",
    "Oilstale or not usable",
    "Risk appetitenot configured / stale or not usable",
    "CreditUnavailable",
  ]);
  await expect(drivers).not.toContainText("no data yet");
  await expect(drivers.getByRole("listitem").nth(3)).toHaveAttribute(
    "title",
    "Not configured: NASDAQ; Stale or not usable: S&P 500",
  );
});

test("insufficient regime lists what is missing and why", async ({ page }) => {
  await serve(page, {
    "/api/dashboard": coverageDashboard(insufficientRegime),
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Insufficient data" }),
  ).toBeVisible();
  await expect(page.getByText(/MIXED/)).toHaveCount(0);
  const missing = page.getByRole("list", { name: "Missing regime inputs" });
  // "No data yet" only for an input that really has none: S&P 500 has an SPY proxy
  // and WTI a late close, so they were stale or not usable.
  await expect(missing.getByRole("listitem")).toHaveText([
    "Not configured: NASDAQ (needs FRED_API_KEY or MASSIVE_API_KEY) · Configure in Settings → Data providers",
    "Switched off: USDJPY · Switch on in Settings → Data providers",
    "No data yet: US10Y",
    "Stale or not usable: S&P 500, WTI",
    "Missing: US10Y-US2Y curve",
  ]);
  await expect(
    missing.getByRole("link", { name: "Configure in Settings → Data providers" }),
  ).toHaveAttribute("href", "/settings");
  await expect(
    missing.getByRole("link", { name: "Switch on in Settings → Data providers" }),
  ).toHaveAttribute("href", "/settings");
});

test("asset detail shows provenance, the labelled proxy and missing keys", async ({
  page,
}) => {
  await serve(page, {
    "/api/dashboard": coverageDashboard(lastCloseRegime),
    "/api/assets/US2Y": treasury2y,
    "/api/assets/SP500": sp500,
    "/api/assets/NASDAQ": nasdaq,
    "/api/assets/EURUSD": eurusd,
    "/api/assets/BRENT": brent,
    "/api/assets/GOLD": gold,
    "/api/assets/USDJPY": usdjpy,
    "/api/assets/US10Y": us10y,
  });
  const source = page.locator(".panel", {
    has: page.getByRole("heading", { name: "Data source", exact: true }),
  });

  await page.goto("/assets/US2Y");
  const top = page.getByRole("region", { name: "US2Y price and signal" });
  // Yields are quoted in percent.
  await expect(top).toContainText("4.81%");
  await expect(top).toContainText("-6 bp");
  await expect(top).toContainText("vs prev close 24 Sep");
  await expect(top).toContainText("DAILY CLOSE");
  await expect(top).not.toContainText("STALE");
  await expect(source).toContainText("US Treasury 2Y par yield");
  await expect(source).toContainText(
    "Official daily close · latest expected publication",
  );
  // The source as its attribution names it, once.
  await expect(source).toContainText("U.S. Department of the Treasury");
  await expect(source).not.toContainText("treasury · Source:");
  // One instant, told once: the service's own time, not the same instant again in the app zone.
  const asOf = source.locator(".detail-grid > div").filter({ has: page.locator("dt", { hasText: /^As of$/ }) });
  await expect(asOf.locator("dd")).toHaveText("Fri 25 Sep 16:00 ET");
  await expect(top).toContainText("as of Fri 25 Sep 16:00 ET · U.S. Department of the Treasury");
  await expect(source).toContainText("Previous close 4.87% (24 Sep)");
  // A keyless daily source has nothing to configure.
  await expect(
    source.getByRole("link", { name: "Configure in Settings → Data providers" }),
  ).toHaveCount(0);
  // Treasury data needs no provider notice.
  await expect(source.getByRole("list", { name: "Data provider notices" })).toHaveCount(0);

  await page.goto("/assets/SP500");
  const spxTop = page.getByRole("region", { name: "SP500 price and signal" });
  await expect(spxTop).toContainText("No level · proxy only");
  await expect(spxTop).toContainText("PROXY");
  await expect(spxTop).toContainText("FRIDAY CLOSE");
  await expect(spxTop).toContainText(
    "+0.42% vs prev close via SPY · as of Fri 25 Sep 23:00 Bucharest · finnhub",
  );
  const proxy = page.getByRole("group", { name: "Proxy for SP500" });
  await expect(proxy).toContainText("SPY ETF — proxy for S&P 500 direction");
  await expect(proxy).toContainText("not the index level");
  await expect(proxy).toContainText("SPY 655.21 (not the SP500 level)");
  await expect(proxy).toContainText("+0.42% vs prev close");
  await expect(proxy).toContainText("FRIDAY CLOSE");
  await expect(proxy).toContainText("Fri 25 Sep 23:00 Bucharest");
  await expect(proxy).not.toContainText("STALE");
  await expect(source).toContainText(
    "A source for SP500 needs FRED_API_KEY or MASSIVE_API_KEY.",
  );

  // Gold on a weekend: the PAXG change carries its rolling-24h basis at the top too.
  await page.goto("/assets/GOLD");
  const goldTop = page.getByRole("region", { name: "GOLD price and signal" });
  await expect(goldTop).toContainText("+0.42% 24h via PAXG");
  await expect(goldTop).not.toContainText("LIVE");

  // A current ECB fix with a missing key: neutral wording, never "a live source".
  await page.goto("/assets/EURUSD");
  const eurTop = page.getByRole("region", { name: "EURUSD price and signal" });
  await expect(eurTop).toContainText("DAILY CLOSE");
  await expect(eurTop).toContainText("MARKET OPEN");
  await expect(eurTop).not.toContainText("LIVE");
  await expect(source).toContainText("Another source needs MASSIVE_API_KEY.");
  await expect(source).not.toContainText("live source");
  await expect(source).toContainText("MARKET OPEN · Test venue");

  // A FRED close carries the FRED API notice in its provenance.
  await page.goto("/assets/BRENT");
  await expect(
    source.getByRole("list", { name: "Data provider notices" }).getByRole("listitem"),
  ).toHaveText([FRED_NOTICE]);

  // Switched off: says so, and links to switch it back on, not to configure a key.
  await page.goto("/assets/USDJPY");
  const yenTop = page.getByRole("region", { name: "USDJPY price and signal" });
  await expect(yenTop).toContainText("DISABLED");
  await expect(source).toContainText("Sources switched off in Settings");
  await expect(source).toContainText("Switched off in Settings: ecb.");
  await expect(
    source.getByRole("link", { name: "Configure in Settings → Data providers" }),
  ).toHaveCount(0);
  await expect(
    source.getByRole("link", { name: "Switch on in Settings → Data providers" }),
  ).toHaveAttribute("href", "/settings");

  // Keyless source, no data yet: nothing to configure, so no Settings link.
  await page.goto("/assets/US10Y");
  await expect(source).toContainText("Source configured, no data yet");
  await expect(source.getByRole("link")).toHaveCount(0);

  await page.goto("/assets/NASDAQ");
  await expect(source).toContainText("No source configured");
  await expect(source).toContainText(
    "A source for NASDAQ needs FRED_API_KEY or MASSIVE_API_KEY.",
  );
  await expect(source).toContainText("Configured sources (level or proxy): fred, massive.");
  const configure = source.getByRole("link", {
    name: "Configure in Settings → Data providers",
  });
  await expect(configure).toHaveAttribute("href", "/settings");
  await configure.click();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(
    page.getByRole("tab", { name: "Data providers", selected: true }),
  ).toBeVisible();
});

test("asset detail says which configured source is failing and separates level sources from proxies", async ({
  page,
}) => {
  const health = [
    { name: "massive", label: "Massive", status: "AUTH_ERROR", error: "Authentication rejected (HTTP 403)", latency_ms: null, last_success: null },
    { name: "binance", status: "ONLINE", error: null, latency_ms: 40, last_success: NOW.toISOString() },
  ];
  const goldSplit = {
    ...gold,
    decision: {
      id: "d-gold",
      direction: "WAIT",
      category: "WAIT",
      score: 0,
      evidence_confidence: 0,
      market_confirmation: 0,
      data_quality: 50,
      reasons: [],
      confirmation: "",
      invalidation: "",
      audit_status: "NOT_REQUIRED",
      components: {},
    },
    coverage: {
      status: "PROXY_ONLY",
      providers: ["massive", "binance"],
      level_providers: ["massive"],
      proxy_providers: ["binance"],
      missing_keys: [],
    },
  };
  // Treasury data comes from the treasury_yields adapter: its health row is not named "treasury".
  const us10yFailing = {
    ...us10y,
    coverage: {
      ...us10y.coverage,
      level_providers: ["treasury"],
      proxy_providers: [],
      sources: [
        {
          name: "treasury",
          label: "U.S. Treasury",
          role: "level",
          state: "READY",
          health: "AUTH_ERROR",
          error: "Authentication rejected (HTTP 403)",
        },
        // Needs a key: named by the key text, never listed as a usable source.
        { name: "fred", label: "FRED", role: "level", state: "MISSING_KEY", health: null, error: null },
      ],
    },
  };
  // A proxy label from a stored row still carrying a raw ISO time.
  const sp500Iso = {
    ...sp500,
    proxy: { ...sp500.proxy, delay_label: "IEX last trade 2026-09-25T20:00:07.045350+00:00" },
  };
  await serve(page, {
    "/api/dashboard": {
      ...coverageDashboard(lastCloseRegime),
      // The page shows the dashboard's live copy of the quote and proxy: the stored ISO label comes from there too.
      assets: coverageDashboard(lastCloseRegime).assets.map((a) => (a.symbol === "SP500" ? sp500Iso : a)),
      providers: [
        ...health,
        { name: "treasury_yields", status: "AUTH_ERROR", error: "Authentication rejected (HTTP 403)", latency_ms: null, last_success: null },
      ],
    },
    "/api/assets/GOLD": goldSplit,
    "/api/assets/SP500": sp500Iso,
    "/api/assets/US10Y": us10yFailing,
  });
  const source = page.locator(".panel", {
    has: page.getByRole("heading", { name: "Data source", exact: true }),
  });
  await page.goto("/assets/GOLD");
  await expect(source).toContainText("No level source; proxy only");
  await expect(source).toContainText("Level source: Massive — Authentication rejected (HTTP 403).");
  await expect(source).toContainText("Direction proxy: binance.");
  await expect(source).not.toContainText("Sources that can serve it");
  await expect(
    source.getByRole("link", { name: "Check in Settings → Data providers" }),
  ).toHaveAttribute("href", "/settings");
  // No quote at all: the setup card says there is no level, not that a quote expired.
  await expect(page.locator(".setup")).toContainText(
    "No level source; only a direction proxy. Setups need a live level.",
  );
  await expect(page.locator(".setup")).not.toContainText("freshness expired");

  // An older service without the split: one list, still with the failing source named.
  await page.goto("/assets/SP500");
  await expect(source).toContainText(
    "Configured sources (level or proxy): fred, Massive — Authentication rejected (HTTP 403).",
  );
  // Raw ISO times in a proxy's delay label are made readable.
  const proxy = page.getByRole("group", { name: "Proxy for SP500" });
  await expect(proxy).toContainText("finnhub · IEX last trade Fri 25 Sep 23:00 Bucharest");
  await expect(proxy).not.toContainText("2026-09-25T");

  // Coverage `sources` carry the health of the adapter behind each source, whatever its name.
  await page.goto("/assets/US10Y");
  await expect(source).toContainText("Source configured, no data yet");
  await expect(source).toContainText("Level source: U.S. Treasury — Authentication rejected (HTTP 403).");
  await expect(source).not.toContainText("FRED");
  await expect(source).not.toContainText("Direction prox");
  await expect(
    source.getByRole("link", { name: "Check in Settings → Data providers" }),
  ).toHaveAttribute("href", "/settings");
});

test("coverage labels fit a 390px phone without horizontal page scroll", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await serve(page, {
    "/api/dashboard": coverageDashboard(insufficientRegime),
    "/api/assets/SP500": sp500,
    "/api/assets/US2Y": treasury2y,
  });
  for (const path of ["/", "/assets/SP500", "/assets/US2Y"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
    if (path === "/")
      await expect(page.locator(".market-tile")).toHaveCount(13);
    else await expect(page.locator(".panel").first()).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  }
  await page.goto("/");
  await expect(page.getByRole("list", { name: "Missing regime inputs" })).toBeVisible();
  await page.screenshot({
    path: "test-results/coverage-mobile.png",
    fullPage: true,
  });
});
