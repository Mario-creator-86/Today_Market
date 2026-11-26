import { test, expect, type Page, type WebSocketRoute } from "@playwright/test";

/*
 * Dashboard (§27, §40, §46-47) with intercepted, test-only API responses.
 * Saturday 26 Sep 2026 12:00 UTC = 15:00 Europe/Bucharest (EEST, UTC+3).
 */
const NOW = new Date("2026-09-26T12:00:00Z");
const iso = (offsetMinutes: number) =>
  new Date(NOW.getTime() + offsetMinutes * 60_000).toISOString();

const quote = (extra: Record<string, unknown> = {}) => ({
  price: 100,
  timestamp: NOW.toISOString(),
  provider: "Test fixture",
  market_status: "LIVE",
  freshness_status: "FRESH",
  change_pct: 0.5,
  ...extra,
});

const decision = (extra: Record<string, unknown> = {}) => ({
  id: `d-${Math.random()}`,
  direction: "LONG",
  score: 50,
  evidence_confidence: 80,
  market_confirmation: 70,
  data_quality: 100,
  reasons: [],
  confirmation: "Test confirmation",
  invalidation: "Test invalidation",
  audit_status: "PASS",
  components: {},
  ...extra,
});

const asset = (
  symbol: string,
  asset_class: string,
  q: Record<string, unknown> | null,
  d: Record<string, unknown> | null = null,
) => ({
  symbol,
  name: `${symbol} name`,
  asset_class,
  quote: q ? quote(q) : null,
  decision: d ? decision(d) : null,
});

function fullDashboard() {
  return {
    as_of: NOW.toISOString(),
    timezone: "Europe/Bucharest",
    last_analysis: iso(-5),
    regimes: ["INFLATION_SHOCK"],
    regime: {
      regimes: ["INFLATION_SHOCK"],
      posture: "DEFENSIVE",
      confidence: 78,
      coverage_pct: 60,
      basis: "Rule coverage",
      drivers: [
        { driver: "Rates", direction: "up", arrow: "↑" },
        { driver: "USD", direction: "up", arrow: "↑" },
        { driver: "Oil", direction: "up", arrow: "↑" },
        { driver: "Risk appetite", direction: "down", arrow: "↓" },
        { driver: "Credit", direction: "unavailable", arrow: null },
      ],
    },
    assets: [
      asset("RUSSELL", "index", { price: 2200 }),
      asset(
        "BTC",
        "crypto",
        { price: 65000, change_pct: 1.24, change_basis: "rolling_24h" },
        {
          direction: "LONG",
          category: "LONG SETUP",
          score: 55,
          historical_probability: { status: "NOT ENOUGH DATA", n: 12 },
        },
      ),
      asset(
        "ETH",
        "crypto",
        { price: 3000, change_pct: -2.1 },
        {
          direction: "BEARISH LEAN / WAIT",
          category: "BEARISH LEAN / WAIT",
          score: -30,
          audit_status: "BLOCK",
          reasons: ["Technical confirmation absent or opposed"],
          why: ["ETF outflows confirmed by two sources"],
        },
      ),
      asset("SP500", "index", {
        price: 5400,
        market_status: "FRIDAY CLOSE",
        freshness_status: "STALE",
        timestamp: iso(-24 * 60),
      }),
      asset("US10Y", "bond", {
        price: 4.25,
        change_pct: null,
        change_bp: 7,
        market_status: "LAST CLOSE",
      }),
      asset("EURUSD", "fx", {
        price: 1.08123,
        provider: "twelvedata-stub",
        stub: true,
      }),
      asset(
        "GOLD",
        "commodity",
        { price: 2350 },
        {
          direction: "SHORT",
          category: "SHORT SETUP",
          score: -80,
          evidence_confidence: 94,
          market_confirmation: 81,
          why: ["Fed repricing more hawkish than ECB", "US real yields rising"],
          confirmation: "Break below 2,330 with DXY above 104",
          invalidation: "US yields fall + DXY loses 103.5",
          confirmation_level: 2330,
          historical_probability: { p: 0.64, n: 418, horizon: "4h" },
        },
      ),
      asset(
        "NVDA",
        "equity",
        { price: 120 },
        { direction: "STRONG LONG", score: 92 },
      ),
      asset("MSFT", "equity", { price: 420 }, { score: 41 }),
      asset("AAPL", "equity", { price: 220 }, { score: 45 }),
      asset(
        "TSLA",
        "equity",
        { price: 250 },
        { direction: "SHORT", score: -50 },
      ),
    ],
    events: [
      {
        id: "hormuz",
        topic: "IRAN_HORMUZ",
        topic_basis: "rule",
        importance: 0.92,
        importance_basis: "source tier x extractor importance",
        summary_fact:
          "Iran announced inspections of tankers in the Strait of Hormuz. Further details pending.",
        event_type: "GEOPOLITICAL_ENERGY",
        verification_status: "CONFIRMED",
        fact_confidence: 95,
        first_seen_utc: iso(-30),
        last_updated_utc: iso(-10),
        assets: ["BRENT"],
        sources: ["Reuters", "AP"],
        interpretation: null,
        causal_paths: [
          {
            rule_id: "hormuz-oil",
            effects: { BRENT: 1, US10Y: 1, BTC: -1 },
            active: true,
          },
        ],
      },
      {
        id: "hormuz-older",
        topic: "IRAN_HORMUZ",
        topic_basis: "rule",
        importance: 0.5,
        summary_fact: "Older Hormuz duplicate.",
        event_type: "GEOPOLITICAL_ENERGY",
        verification_status: "CONFIRMED",
        fact_confidence: 90,
        first_seen_utc: iso(-300),
        last_updated_utc: iso(-300),
        assets: [],
        sources: ["Reuters"],
        interpretation: null,
      },
      {
        id: "fed",
        topic: "FED_POLICY",
        topic_basis: "rule",
        importance: 0.88,
        summary_fact: "A Fed governor said further hikes remain possible.",
        event_type: "CENTRAL_BANK",
        verification_status: "PARTIALLY_CONFIRMED",
        fact_confidence: 60,
        first_seen_utc: iso(-20),
        last_updated_utc: iso(-20),
        assets: ["US2Y", "DXY"],
        sources: ["Bloomberg"],
        interpretation: null,
      },
      {
        id: "rumour",
        summary_fact: "Unconfirmed report of an exchange outage.",
        event_type: "SOCIAL_DISCOVERY",
        verification_status: "UNCONFIRMED",
        fact_confidence: 10,
        first_seen_utc: iso(-2),
        last_updated_utc: iso(-2),
        assets: [],
        sources: ["x-unknown"],
        interpretation: null,
      },
      {
        id: "denied",
        // Topic guessed from entities: the headline titles the item.
        topic: "US_CHINA",
        topic_basis: "entities",
        importance: 0.3,
        summary_fact: "Tariff report was denied by the ministry.",
        event_type: "TRADE",
        verification_status: "CONTRADICTED",
        fact_confidence: 20,
        first_seen_utc: iso(-5),
        last_updated_utc: iso(-5),
        assets: [],
        sources: [],
        interpretation: null,
      },
      {
        id: "old",
        topic: "OLD_TOPIC",
        importance: 0.99,
        summary_fact: "Stale item must not appear.",
        event_type: "OTHER",
        verification_status: "STALE",
        fact_confidence: 90,
        first_seen_utc: iso(-3000),
        last_updated_utc: iso(-3000),
        assets: [],
        sources: ["Reuters"],
        interpretation: null,
      },
    ],
    calendar: [
      {
        id: "past",
        name: "Past payrolls",
        scheduled_utc: iso(-60),
        impact: 10,
      },
      {
        id: "cpi",
        name: "US CPI",
        scheduled_utc: iso(30),
        impact: 10,
        country: "US",
      },
      {
        id: "low",
        name: "Low impact survey",
        scheduled_utc: iso(45),
        impact: 3,
      },
      {
        id: "auction",
        name: "Treasury auction",
        scheduled_utc: iso(300),
        impact: 7,
      },
      {
        id: "mich",
        name: "Michigan sentiment",
        scheduled_utc: iso(120),
        impact: 8,
      },
    ],
    providers: [
      {
        name: "binance",
        status: "ONLINE",
        latency_ms: 40,
        last_success: iso(0),
        error: null,
      },
      {
        name: "fred",
        status: "ONLINE",
        latency_ms: 120,
        last_success: iso(-1),
        error: null,
      },
      {
        name: "fmp",
        status: "OFFLINE",
        latency_ms: null,
        last_success: null,
        error: "timeout",
      },
      {
        name: "twelvedata-stub",
        status: "ONLINE",
        latency_ms: 1,
        last_success: iso(0),
        error: null,
        stub: true,
      },
    ],
    alerts: [
      {
        id: "a-medium",
        kind: "DIVERGENCE",
        priority: "MEDIUM",
        title: "BTC showing unusual relative strength",
        message: "Oil +4%, US10Y +6bp, DXY +0.4%, BTC still +1.2%",
        symbol: "BTC",
        created_at: iso(-3),
        decision_id: null,
        event_id: null,
        metrics: {},
      },
      {
        id: "a-high",
        kind: "LEVEL_BREAK",
        priority: "HIGH",
        title: "BTC broke support",
        message: "US10Y +8bp, DXY +0.5%. Macro + technical signals aligned.",
        symbol: "BTC",
        created_at: iso(-1),
        decision_id: null,
        event_id: "hormuz",
        metrics: {},
      },
    ],
  };
}

async function serve(page: Page, dashboard: () => unknown) {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    return route.fulfill({
      json: path === "/api/dashboard" ? dashboard() : [],
    });
  });
}

const panel = (page: Page, title: string) =>
  page.locator("section").filter({
    has: page.getByRole("heading", { name: title, exact: true }),
  });

async function expandBackground(page: Page) {
  await expect(page.locator(".regime")).toBeVisible();
  // Without the event layer and an interpretation the first collapsed section holds only the calendar panel.
  for (const label of ["Next important events", "Verified news and alerts", "Provider health and market brief", "All tracked markets"]) {
    const group = page.locator("details.dashboard-details").filter({ has: page.locator(":scope > summary", { hasText: label }) });
    if (await group.getAttribute("open") === null) await group.locator(":scope > summary").click();
  }
}

test("header shows LIVE, provider health, last full analysis and Bucharest time; stream updates live", async ({
  page,
}) => {
  await page.clock.install({ time: NOW });
  // Mocked socket stays open: the shell reports LIVE and accepts snapshots.
  let socket: WebSocketRoute | undefined;
  await page.routeWebSocket("**/ws", (ws) => {
    socket = ws;
  });
  await serve(page, fullDashboard);
  await page.goto("/");
  const header = page.locator("header.topbar");
  await expect(header.getByText("LIVE", { exact: true })).toBeVisible();
  await expect(header).toContainText("3/4 providers online");
  await expect(header).toContainText(/Last full analysis Sat 26 Sep 14:55/);
  await expect(header.locator(".clock")).toContainText("15:00");
  await expect(header.locator(".clock")).toContainText("Bucharest");
  await expect(
    header.getByRole("link", { name: "Alerts, 2 unread" }),
  ).toBeVisible();

  const base = fullDashboard();
  socket!.send(
    JSON.stringify({
      type: "snapshot",
      data: {
        ...base,
        events: [
          {
            ...base.events[0],
            id: "breaking",
            topic: "OPEC",
            summary_fact: "OPEC announced an emergency meeting.",
            verification_status: "CONFIRMED",
            sources: ["Reuters"],
            first_seen_utc: NOW.toISOString(),
          },
          ...base.events,
        ],
      },
    }),
  );
  await expandBackground(page);
  const stream = panel(page, "Breaking / verified").getByRole("listitem");
  await expect(stream.first()).toContainText(
    "OPEC announced an emergency meeting.",
  );
  await expect(stream.first()).toContainText("CONFIRMED");
  await expect(stream.first()).toContainText("Source: Reuters");
});

test("polling fallback is labelled when the socket is unavailable", async ({
  page,
}) => {
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  await serve(page, fullDashboard);
  await page.goto("/");
  await expect(
    page.locator("header.topbar").getByText("POLLING", { exact: true }),
  ).toBeVisible();
});

test("regime, strip, ranked setups, leans, the events timeline, next events and stream", async ({
  page,
}) => {
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  await serve(page, fullDashboard);
  await page.goto("/");
  await expect(page).toHaveTitle("Today's market · Market Intelligence");
  await expandBackground(page);

  // Regime: posture, rule-coverage confidence, driver arrows.
  await expect(
    page.getByRole("heading", { name: "DEFENSIVE · INFLATION SHOCK" }),
  ).toBeVisible();
  // The hero shows the arrows on one line; a driver without a measurement says so instead of an arrow.
  await expect(page.getByRole("list", { name: "Driver directions" }).getByRole("listitem")).toHaveText([
    "Rates↑up",
    "USD↑up",
    "Oil↑up",
    "Risk appetite↓down",
    "CreditUnavailable",
  ]);
  await page.getByText("Data coverage and basis", { exact: true }).click();
  await expect(page.getByText(/Confidence 78% — rule coverage/)).toBeVisible();
  const drivers = page.getByRole("list", { name: "Regime drivers" });
  await expect(drivers.getByRole("listitem")).toHaveText([
    "Rates↑up",
    "USD↑up",
    "Oil↑up",
    "Risk appetite↓down",
    "CreditUnavailable",
  ]);

  // Market strip: exactly the configured 13 symbols, in order (RUSSELL excluded).
  const tiles = page.locator(".market-tile");
  await expect(tiles.locator("strong")).toHaveText([
    "BTC",
    "ETH",
    "S&P 500",
    "NASDAQ",
    "DXY",
    "US2Y",
    "US10Y",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "GOLD",
    "WTI",
    "BRENT",
  ]);
  // A compact tile: symbol, ONE tag, the level and the change coloured by its sign. Nothing else is text.
  await expect(tiles.nth(0)).toHaveText("BTCLIVE65,000+1.24%");
  await expect(tiles.nth(0).locator(".market-tile-value span")).toHaveClass(/text-positive/);
  await expect(tiles.nth(1).locator(".market-tile-value span")).toHaveClass(/down/);
  // The change basis, the session and the as-of line are the tile's tooltip (and on the asset page).
  await expect(tiles.nth(0)).toHaveAttribute("title", /\+1\.24% 24h\nLIVE\nSession state: LIVE\nas of Sat 26 Sep 15:00 Bucharest · Test fixture/);
  await expect(tiles.nth(0).locator(".badge")).toHaveCount(1);
  // The tag stays legible (never 8 px).
  await expect(tiles.nth(0).locator(".badge")).toHaveCSS("font-size", "10px");
  await expect(tiles.nth(0).locator(".market-chip")).toHaveText("LIVE");
  await expect(tiles.nth(1)).toContainText("-2.10%");
  // Friday's last live print on a Saturday: quiet LAST PRINT, not a prominent STALE.
  await expect(tiles.nth(2)).toHaveAttribute("title", /Session state: FRIDAY CLOSE/);
  await expect(tiles.nth(2).locator(".market-chip")).toHaveText("LAST PRINT");
  await expect(tiles.nth(2).locator(".market-chip")).toHaveAttribute(
    "data-variant",
    "outline",
  );
  await expect(tiles.nth(3).locator(".market-chip")).toHaveText(
    "NOT DELIVERED",
  );
  // No asset delivered: no level, no change, and the tooltip says why.
  await expect(tiles.nth(3)).toHaveText("NASDAQNOT DELIVERED—");
  await expect(tiles.nth(3)).toHaveAttribute("title", /No quote/);
  await expect(tiles.nth(6)).toContainText("+7 bp");
  await expect(tiles.nth(6)).toHaveAttribute("title", /Session state: LAST CLOSE/);
  // A yield reads as a yield, like on its asset page.
  await expect(tiles.nth(6).locator(".market-tile-value b")).toHaveText("4.25%");
  await expect(tiles.nth(0).locator(".market-tile-value b")).toHaveText("65,000");
  await expect(tiles.nth(7)).toContainText("DEVELOPMENT STUB");
  await expect(tiles.nth(7)).toContainText("1.08123");
  // A stub is never live data: its one tag says DEVELOPMENT STUB, and no LIVE anywhere on the tile.
  await expect(tiles.nth(7).locator(".market-chip")).toHaveText("DEVELOPMENT STUB");
  await expect(tiles.nth(7)).toHaveAttribute("data-chip", "STUB");
  await expect(tiles.nth(7)).toHaveAttribute("title", /Session state: MARKET OPEN/);
  await expect(tiles.nth(7)).not.toContainText("LIVE");
  // No tile wraps to more than two lines of text.
  for (const tile of await tiles.all()) expect((await tile.boundingBox())!.height).toBeLessThan(80);

  // Best setups: max five, ranked by |score|; MSFT (41) drops out.
  const setups = page.locator(".setup");
  await expect(setups.locator(".setup-head strong")).toHaveText([
    "NVDA",
    "GOLD",
    "BTC",
    "TSLA",
    "AAPL",
  ]);
  const gold = setups.nth(1);
  await expect(gold.locator(".badge")).toHaveText("SHORT SETUP");
  await expect(gold.locator(".score strong")).toHaveText("-80");
  await expect(gold).toContainText("Evidence confidence94%");
  await expect(gold).toContainText("Market confirmation81%");
  await expect(gold).toContainText("64% · n=418 · horizon 4 hours");
  await expect(gold).toContainText("Fed repricing more hawkish than ECB");
  await expect(gold).toContainText("Break below 2,330 with DXY above 104");
  await expect(gold).toContainText("US yields fall + DXY loses 103.5");
  await expect(setups.nth(2)).toContainText("NOT ENOUGH DATA · n=12");
  await expect(setups.nth(0)).toContainText("No verified WHY bullets");

  // WAIT-with-lean shown separately, never as a setup.
  const leans = page.locator(".lean-setup");
  await expect(leans).toHaveCount(1);
  await expect(leans.first()).toContainText("ETH");
  await expect(leans.first()).toContainText("BEARISH LEAN / WAIT");
  await expect(leans.first()).toContainText("Technical confirmation absent");

  // Events timeline, "Latest confirmed": confirmed facts with mapped market exposure only, newest first, one line
  // each. Partial, contradicted and unverified news remains in the full stream.
  const timeline = panel(page, "Events");
  await expect(page.getByRole("heading", { name: "What matters now", exact: true })).toHaveCount(0);
  const latest = timeline.getByRole("list", { name: "Latest confirmed" }).getByRole("listitem");
  await expect(latest).toHaveCount(1);
  await expect(latest.nth(0)).toHaveText(
    "Sat 26 Sep 14:30Iran announced inspections of tankers in the Strait of Hormuz.BRENT↑ upUS10Y↑ upBTC↓ down",
  );
  await expect(latest.nth(0)).not.toContainText("Further details pending");
  await expect(latest.nth(0).getByRole("link")).toHaveAttribute("href", "/events/hormuz");
  await expect(timeline).not.toContainText("Unconfirmed report");
  await expect(timeline).not.toContainText("Tariff report");
  await expect(timeline).not.toContainText("A Fed governor");
  await expect(timeline.getByRole("link", { name: "All events" })).toHaveAttribute("href", "/events");

  // "Coming up": the next scheduled high-impact releases (the calendar, without the event layer), time, name
  // and impact on one line each.
  await expect(timeline.getByRole("list", { name: "Coming up" }).getByRole("listitem")).toHaveText([
    "15:30US CPI10/10",
    "17:00Michigan sentiment8/10",
    "20:00Treasury auction7/10",
  ]);
  await expect(timeline.getByRole("link", { name: "Calendar" })).toHaveAttribute("href", "/calendar");

  // Next important events: upcoming, high impact, Bucharest time, Impact N/10.
  const next = panel(page, "Next important events").getByRole("listitem");
  await expect(next).toHaveCount(3);
  await expect(next.nth(0)).toContainText("Sat 26 Sep 15:30");
  await expect(next.nth(0)).toContainText("US CPI");
  await expect(next.nth(0).locator("strong")).toHaveAttribute("title", "US CPI");
  await expect(next.nth(0)).toContainText("Impact 10/10");
  await expect(next.nth(1)).toContainText("17:00");
  await expect(next.nth(1)).toContainText("Michigan sentiment");
  await expect(next.nth(1)).toContainText("Impact 8/10");
  await expect(next.nth(2)).toContainText("20:00");
  await expect(next.nth(2)).toContainText("Impact 7/10");

  // Breaking / verified: newest first, labelled, with source.
  const stream = panel(page, "Breaking / verified").getByRole("listitem");
  await expect(stream.first()).toContainText("UNVERIFIED");
  await expect(stream.first()).toContainText("Source: x-unknown");
  await expect(stream.nth(1)).toContainText("CONTRADICTED");
  await expect(stream.nth(1)).toContainText("Source: Not recorded");
  await expect(stream.nth(2)).toContainText("PARTIAL");
  await expect(stream.nth(3)).toContainText("CONFIRMED");
  await expect(stream.nth(3)).toContainText("Source: Reuters, AP");
  await expect(stream.filter({ hasText: "Stale item" })).toHaveCount(0);

  // Alerts: HIGH first with kind, links to asset and event.
  const alerts = page.locator("#alerts").getByRole("listitem");
  await expect(alerts.first()).toContainText("HIGH PRIORITY");
  await expect(alerts.first()).toContainText("LEVEL BREAK");
  await expect(alerts.first()).toContainText("BTC broke support");
  await expect(
    alerts.first().getByRole("link", { name: "BTC detail" }),
  ).toHaveAttribute("href", "/assets/BTC");
  await expect(alerts.nth(1)).toContainText("MEDIUM");
  await expect(alerts.nth(1)).toContainText("DIVERGENCE");

  // Provider health labels stubs; "View all" opens the Providers page.
  await expect(
    panel(page, "Provider health").getByText("DEVELOPMENT STUB"),
  ).toBeVisible();
  await expect(
    panel(page, "Provider health").getByRole("link", { name: "View all" }),
  ).toHaveAttribute("href", "/providers");

  // All markets: every delivered asset links to its page, including those
  // outside the strip and the setups (RUSSELL).
  const markets = panel(page, "All markets");
  await expect(
    markets.getByRole("list", { name: "Indices markets" }).getByRole("link"),
  ).toHaveText(["RUSSELL", "S&P 500"]);
  await expect(
    markets.getByRole("list", { name: "Equities markets" }).getByRole("link"),
  ).toHaveText(["NVDA", "MSFT", "AAPL", "TSLA"]);
  await expect(markets.getByRole("link", { name: "RUSSELL" })).toHaveAttribute(
    "href",
    "/assets/RUSSELL",
  );
  await expect(markets.getByRole("link")).toHaveCount(11);
});

test("a lean or setup disappears once its quote goes stale", async ({
  page,
}) => {
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  let offline = false;
  await page.route("**/api/**", (route) =>
    offline
      ? route.fulfill({ status: 503, body: "offline" })
      : route.fulfill({ json: fullDashboard() }),
  );
  await page.goto("/");
  await expect(page.locator(".lean-setup")).toHaveCount(1);
  offline = true;
  await page.clock.fastForward(16000);
  await expect(page.locator(".lean-setup")).toHaveCount(0);
  await expect(page.locator(".setup")).toHaveCount(0);
  await expect(page.locator(".market-tile").first()).toContainText("STALE");
});

test("missing data degrades to explicit unavailable states", async ({
  page,
}) => {
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  await serve(page, () => ({
    as_of: new Date().toISOString(),
    regimes: [],
    regime: {
      regimes: [],
      posture: "INSUFFICIENT_DATA",
      confidence: null,
      coverage_pct: 10,
      basis: "",
    },
    assets: [],
    events: [],
    calendar: [
      {
        id: "no-impact",
        name: "Unknown impact",
        scheduled_utc: "2999-01-01T00:00:00Z",
      },
    ],
    providers: [],
    last_analysis: null,
  }));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Insufficient data" }),
  ).toBeVisible();
  await expect(page.getByRole("list", { name: "Regime drivers" })).toHaveCount(
    0,
  );
  await expect(
    page.getByText("A regime requires fresh cross-asset observations."),
  ).toBeVisible();
  await expect(page.locator(".market-tile")).toHaveCount(13);
  await expect(page.locator(".market-tile").first()).toHaveText("BTCNOT DELIVERED—");
  await expect(page.locator(".market-tile").first()).toHaveAttribute("title", /No quote/);
  await expect(page.locator(".market-tile").first()).not.toContainText("0%");
  // Nothing actionable: ONE line, with the standing reason (no event layer, so no event to wait for).
  await expect(
    page.getByRole("heading", { name: "No actionable setups" }),
  ).toBeVisible();
  await expect(page.locator(".setups-wait")).toHaveText(
    "Today's best setupsNo actionable setups— waiting for verified catalysts, fresh prices and market confirmation.WAIT",
  );
  expect((await page.locator(".setups-wait").boundingBox())!.height).toBeLessThan(70);
  // The timeline says in one line each that it has nothing to list.
  const timeline = panel(page, "Events");
  await expect(timeline).toContainText("No scheduled events in this window.");
  await expect(timeline).toContainText("No confirmed news with an identified asset impact yet.");
  await expandBackground(page);
  await expect(page.getByText("No upcoming high-impact events")).toBeVisible();
  // No threshold from the backend: none is stated.
  await expect(
    page.getByText("Scheduled high-impact releases will appear here."),
  ).toBeVisible();
  await expect(page.getByText("Stream quiet")).toBeVisible();
  await expect(page.getByText("No alerts", { exact: true })).toBeVisible();
  const header = page.locator("header.topbar");
  await expect(header).toContainText("0/0 providers online");
  await expect(header).toContainText("Last full analysis Not recorded");
});

test("browser notifications are opt-in and notify each new HIGH alert once", async ({
  page,
}) => {
  await page.clock.install({ time: NOW });
  await page.addInitScript(() => {
    const calls: { title: string; tag?: string }[] = [];
    (window as unknown as { __notes: typeof calls }).__notes = calls;
    // Permission survives reloads like a real browser grant.
    class FakeNotification {
      static get permission(): NotificationPermission {
        return sessionStorage.getItem("fake-permission") === "granted"
          ? "granted"
          : "default";
      }
      static async requestPermission() {
        sessionStorage.setItem("fake-permission", "granted");
        return "granted" as const;
      }
      constructor(title: string, options?: NotificationOptions) {
        calls.push({ title, tag: options?.tag });
      }
    }
    Object.defineProperty(window, "Notification", {
      value: FakeNotification,
      configurable: true,
    });
  });
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  const alerts: Record<string, unknown>[] = [
    {
      id: "old-high",
      kind: "SETUP",
      priority: "HIGH",
      title: "Old setup",
      message: "Two hours old",
      symbol: null,
      created_at: iso(-120),
    },
  ];
  await serve(page, () => ({ ...fullDashboard(), alerts: [...alerts] }));
  await page.goto("/");
  await expandBackground(page);
  const notes = () =>
    page.evaluate(
      () => (window as unknown as { __notes: { tag?: string }[] }).__notes,
    );

  // Nothing is sent before the viewer opts in.
  await page
    .getByRole("button", { name: "Enable browser notifications" })
    .click();
  await expect(
    page.getByRole("button", { name: "Disable browser notifications" }),
  ).toBeVisible();
  expect(
    await page.evaluate(() =>
      localStorage.getItem("mi.alerts.browser-notifications"),
    ),
  ).toBe("on");
  // Old alerts are never pushed.
  expect(await notes()).toEqual([]);

  alerts.push(
    {
      id: "new-high",
      kind: "LEVEL_BREAK",
      priority: "HIGH",
      title: "BTC broke support",
      message: "US10Y +8bp",
      symbol: "BTC",
      created_at: iso(0),
    },
    {
      id: "new-low",
      kind: "ANOMALY",
      priority: "LOW",
      title: "Low alert",
      message: "",
      symbol: null,
      created_at: iso(0),
    },
  );
  await page.clock.fastForward(6000);
  await expect
    .poll(notes)
    .toEqual([{ title: "HIGH PRIORITY · BTC broke support", tag: "new-high" }]);
  // Further polls and a reload do not repeat it.
  await page.clock.fastForward(6000);
  await page.reload();
  await expandBackground(page);
  await expect(page.locator("#alerts")).toContainText("BTC broke support");
  await page.clock.fastForward(6000);
  expect(await notes()).toEqual([]);
  // Disabling stops notifications.
  await page
    .getByRole("button", { name: "Disable browser notifications" })
    .click();
  await expect(
    page.getByRole("button", { name: "Enable browser notifications" }),
  ).toBeVisible();
});

test("unread alert count clears after marking read", async ({ page }) => {
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  await serve(page, fullDashboard);
  await page.goto("/");
  await expect(
    page.getByRole("link", { name: "Alerts, 2 unread" }),
  ).toBeVisible();
  await expandBackground(page);
  await page.getByRole("button", { name: "Mark 2 read" }).click();
  await expect(
    page.getByRole("link", { name: "Alerts, none unread" }),
  ).toBeVisible();
});

test("a provider outage that has recovered is marked RESOLVED and listed after the open alerts", async ({
  page,
}) => {
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  const providerAlert = (
    id: string,
    provider: string,
    status: string,
    title: string,
    minutes: number,
  ) => ({
    id,
    kind: "PROVIDER",
    priority: status === "ONLINE" ? "LOW" : "HIGH",
    title,
    message: status === "ONLINE" ? "Back online (was offline)" : "Timed out",
    symbol: null,
    created_at: iso(minutes),
    metrics: { provider, status },
  });
  const base = fullDashboard();
  await serve(page, () => ({
    ...base,
    alerts: [
      // Recovered, with a recovery alert: resolved at its time (14:10 Bucharest).
      providerAlert("fred-down", "fred", "OFFLINE", "FRED: offline", -60),
      providerAlert("fred-back", "fred", "ONLINE", "FRED: recovered", -50),
      // No recovery alert, but Provider health shows it ONLINE now.
      providerAlert("binance-down", "binance", "OFFLINE", "Binance: offline", -30),
      // Still OFFLINE in Provider health: stays open.
      providerAlert("fmp-down", "fmp", "OFFLINE", "FMP: offline", -90),
      ...base.alerts,
    ],
  }));
  await page.goto("/");
  await expandBackground(page);
  const rows = page.locator("#alerts").getByRole("listitem");
  await expect(rows.locator("h3")).toHaveText([
    "BTC broke support",
    "FMP: offline",
    "BTC showing unusual relative strength",
    "FRED: recovered",
    "Binance: offline",
    "FRED: offline",
  ]);
  await expect(rows.nth(1)).toContainText("HIGH PRIORITY");
  await expect(rows.nth(1)).not.toContainText("RESOLVED");
  await expect(rows.nth(4)).toContainText("RESOLVED");
  await expect(rows.nth(4)).not.toContainText("RESOLVED 1");
  await expect(rows.nth(5)).toContainText("RESOLVED 14:10");
  // A resolved outage keeps its priority as text, no longer in the alarm colour.
  await expect(rows.nth(5).locator(".badge").first()).toHaveAttribute("data-variant", "outline");
});

test("dashboard stays within a phone viewport with full data", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  await serve(page, fullDashboard);
  await page.goto("/");
  await expect(page.locator(".setup")).toHaveCount(5);
  // One column on a phone (globals.css): the decorative icon is dropped, so no side column narrows the text.
  await expect(page.locator(".regime-icon")).toBeHidden();
  const banner = await page.locator("section.regime").boundingBox();
  const title = await page.locator("#regime-heading").boundingBox();
  expect(banner && title && title.x - banner.x < 30).toBe(true);
  // The timeline keeps one line per event: a long name is cut with an ellipsis and kept whole as a tooltip.
  const row = panel(page, "Events").getByRole("list", { name: "Coming up" }).locator(".events-name").first();
  await expect(row).toHaveCSS("text-overflow", "ellipsis");
  await expect(row).toHaveAttribute("title", "US CPI");
  // The strip is one scrolling row on a phone; the page itself never scrolls sideways.
  const strip = await page.locator(".market-strip").boundingBox();
  expect(strip!.height).toBeLessThan(90);
  // In the calendar panel long event names wrap to two lines, with the full name as a tooltip.
  await page.locator("details.dashboard-details > summary", { hasText: "Next important events" }).click();
  const name = panel(page, "Next important events").locator("li strong").first();
  await expect(name).toHaveCSS("-webkit-line-clamp", "2");
  await expect(name).toHaveAttribute("title", "US CPI");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/dashboard-full-mobile.png",
    fullPage: true,
  });
});

/** Texts that assert an empty result; none may show before there is data to support it. */
const EMPTY_VERDICTS = [
  "No actionable setups",
  "Patience is a position.",
  "No alerts",
  "No verified events yet",
  "No upcoming high-impact events",
  "No scheduled events in this window.",
  "No other scheduled events in this window.",
  "No confirmed news with an identified asset impact yet.",
  "Stream quiet",
  "Provider status unavailable",
  "Awaiting evidence",
];

test("while the first snapshot loads, no section claims an empty result", async ({
  page,
}) => {
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/dashboard") await gate;
    return route.fulfill({
      json: path === "/api/dashboard" ? fullDashboard() : [],
    });
  });
  await page.goto("/");
  await expect(page.getByText(/Loading market intelligence/)).toBeVisible();
  await expect(page.getByTestId("dashboard-placeholder")).toBeVisible();
  for (const text of EMPTY_VERDICTS)
    await expect(page.getByText(text, { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Today's best setups" }),
  ).toHaveCount(0);
  await expect(page.locator(".regime")).toHaveCount(0);

  release();
  await expect(
    page.getByRole("heading", { name: "DEFENSIVE · INFLATION SHOCK" }),
  ).toBeVisible();
  await expect(page.getByTestId("dashboard-placeholder")).toHaveCount(0);
});

test("a failing service with nothing cached shows the error, never a WAIT verdict or empty lists", async ({
  page,
}) => {
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 500, json: { detail: "Injected failure" } }),
  );
  await page.goto("/");
  await expect(page.getByRole("alert").filter({ hasText: "500" })).toBeVisible();
  await expect(
    page.getByText("Market sections appear once the data service answers."),
  ).toBeVisible();
  for (const text of EMPTY_VERDICTS)
    await expect(page.getByText(text, { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Today's best setups" }),
  ).toHaveCount(0);
  await expect(page.getByText("WAIT", { exact: true })).toHaveCount(0);
});

test("background polls leave the Refresh button alone; a click disables it until the answer", async ({
  page,
}) => {
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  let hold = false;
  let pending = 0;
  const waiting: (() => void)[] = [];
  const releaseAll = () => waiting.splice(0).forEach((resume) => resume());
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/dashboard" && hold) {
      pending += 1;
      await new Promise<void>((resume) => waiting.push(resume));
      pending -= 1;
    }
    // A poll the click superseded may already be aborted: fulfilling it is a no-op.
    return route
      .fulfill({ json: path === "/api/dashboard" ? fullDashboard() : [] })
      .catch(() => {});
  });
  await page.goto("/");
  const button = page.getByRole("button", { name: "Refresh" });
  await expect(
    page.getByRole("heading", { name: "DEFENSIVE · INFLATION SHOCK" }),
  ).toBeVisible();

  // The shell's 5 s poll is in flight: the button neither disables nor spins.
  hold = true;
  await page.clock.fastForward(6000);
  await expect.poll(() => pending).toBeGreaterThan(0);
  await expect(button).toBeEnabled();
  await expect(button.locator("svg")).not.toHaveClass(/spin/);
  releaseAll();
  await expect.poll(() => pending).toBe(0);

  // A click does, until its own answer arrives.
  await button.click();
  await expect.poll(() => pending).toBeGreaterThan(0);
  await expect(button).toBeDisabled();
  await expect(button.locator("svg")).toHaveClass(/spin/);
  hold = false;
  releaseAll();
  await expect(button).toBeEnabled();
  await expect(button.locator("svg")).not.toHaveClass(/spin/);
});

test("setup and lean cards say when a decision the last successful analysis did not replace was made", async ({
  page,
}) => {
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  const base = fullDashboard();
  // The cycle that started 8 min ago (more than one 5-minute cadence) succeeded; the next one is still running.
  const cycle = iso(-8);
  const body = {
    ...base,
    last_analysis: cycle,
    analysis: {
      last_success_at: cycle,
      last_attempt_at: cycle,
      last_error_type: null,
      cadence_seconds: 300,
      stale: false,
    },
    assets: base.assets.map((a) =>
      a.decision
        ? {
            ...a,
            // GOLD and ETH kept an older decision: that cycle did not replace them.
            decision: { ...a.decision, created_at: ["GOLD", "ETH"].includes(a.symbol) ? iso(-303) : cycle },
          }
        : a,
    ),
  };
  await serve(page, () => body);
  await page.goto("/");
  const setups = page.locator(".setup");
  await expect(setups.locator(".setup-head strong")).toHaveText(["NVDA", "GOLD", "BTC", "TSLA", "AAPL"]);
  // 06:57 UTC = 09:57 Bucharest, 5 h 3 min before 15:00.
  const gold = setups.nth(1).locator(".setup-decided");
  await expect(gold).toHaveText("Decided 09:57 (5 h 3 min ago)");
  await expect(gold).toHaveClass(/text-warning/);
  await expect(gold).toHaveAttribute(
    "title",
    "The last full analysis (14:52 Bucharest) published no new decision for this asset; this one is older.",
  );
  await expect(gold.locator("time")).toHaveAttribute("datetime", iso(-303));
  await expect(page.locator(".lean-setup .setup-decided")).toHaveText("Decided 09:57 (5 h 3 min ago)");
  // Decisions from the last successful cycle stay quiet while the next cycle runs.
  for (const i of [0, 2, 3, 4]) await expect(setups.nth(i).locator(".setup-decided")).toHaveCount(0);
});
