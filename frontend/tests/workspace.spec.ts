import { test, expect, type Page, type Route, type WebSocketRoute } from "@playwright/test";

// Isolate every HTTP-fixture test from a running backend's stream. Tests can override this at page level.
test.beforeEach(async ({ context }) => {
  await context.routeWebSocket("**/ws", (socket) => socket.close());
});

const emptyDashboard = {
  providers: [],
  assets: [],
  events: [],
  calendar: [],
  regimes: [],
};

test("cached actionable signals expire locally after feed failure and lean signals stay excluded", async ({
  page,
}) => {
  const time = new Date("2026-09-26T12:00:00Z");
  await page.clock.install({ time });
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  let offline = false;
  const asset = {
    symbol: "BTC",
    name: "Bitcoin",
    asset_class: "crypto",
    quote: {
      price: 100,
      timestamp: time.toISOString(),
      provider: "Test fixture",
      market_status: "LIVE",
      freshness_status: "FRESH",
      change_pct: null,
    },
    decision: {
      id: "test",
      direction: "LONG",
      score: 70,
      evidence_confidence: 90,
      market_confirmation: 90,
      data_quality: 100,
      reasons: ["Test evidence"],
      confirmation: "Test confirmation",
      invalidation: "Test invalidation",
      audit_status: "PASS",
      components: {},
    },
  };
  await page.route("**/api/**", (route) =>
    offline
      ? route.fulfill({ status: 503, body: "offline" })
      : route.fulfill({
          json: {
            ...emptyDashboard,
            assets: [
              asset,
              {
                ...asset,
                symbol: "ETH",
                decision: {
                  ...asset.decision,
                  direction: "BULLISH LEAN / WAIT",
                },
              },
            ],
          },
        }),
  );
  await page.goto("/");
  await expect(page.locator(".setup")).toHaveCount(1);
  await expect(page.locator(".setup .badge")).toHaveText("LONG");
  offline = true;
  await page.clock.fastForward(16000);
  await expect(page.locator(".setup")).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "No actionable setups" }),
  ).toBeVisible();
  await expect(page.locator(".market-tile").first()).toContainText("STALE");
  await expect(page.locator(".market-tile").first()).not.toContainText("FRESH");
});

test("asset detail downgrades a cached decision after freshness expires", async ({
  page,
}) => {
  const time = new Date("2026-09-26T12:00:00Z");
  await page.clock.install({ time });
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  let offline = false;
  const asset = {
    symbol: "BTC",
    name: "Bitcoin",
    asset_class: "crypto",
    quote: {
      price: 100,
      timestamp: time.toISOString(),
      provider: "Test fixture",
      market_status: "LIVE",
      freshness_status: "FRESH",
    },
    decision: {
      direction: "LONG",
      score: 70,
      evidence_confidence: 90,
      market_confirmation: 90,
      reasons: [],
      audit_status: "PASS",
      components: {},
    },
  };
  await page.route("**/api/**", (route) =>
    route.request().url().endsWith("/dashboard")
      ? route.fulfill({ json: emptyDashboard })
      : offline
        ? route.fulfill({ status: 503, body: "offline" })
        : route.fulfill({ json: asset }),
  );
  await page.goto("/assets/BTC");
  await expect(page.locator(".setup .badge")).toHaveText("LONG");
  offline = true;
  await page.clock.fastForward(16000);
  await expect(page.locator(".setup .badge")).toHaveText("WAIT / DATA INSUFFICIENT");
  await expect(page.locator(".asset-price")).toContainText("STALE");
});

for (const trigger of ["expiry", "invalidation"] as const) {
  test(`fresh streamed quotes cannot keep a decision after ${trigger}`, async ({ page }) => {
    const time = new Date("2026-09-26T12:00:00Z");
    await page.clock.install({ time });
    let socket: WebSocketRoute | undefined;
    await page.routeWebSocket("**/ws", (ws) => { socket = ws; });
    const quote = { price: 100, timestamp: time.toISOString(), provider: "Test fixture",
      market_status: "LIVE", freshness_status: "FRESH" };
    const asset = { symbol: "BTC", name: "Bitcoin", asset_class: "crypto", quote,
      decision: { id: "cached", direction: "LONG", category: "LONG SETUP", score: 60,
        audit_status: "PASS", components: {}, reasons: [], invalidation_level: 90,
        created_at: time.toISOString(), expires_at: new Date(time.getTime() + 5000).toISOString() } };
    await page.route("**/api/**", (route) => route.fulfill({ json:
      route.request().url().endsWith("/dashboard") ? { ...emptyDashboard, assets: [asset] } : asset,
    }));
    await page.goto("/assets/BTC");
    await expect(page.locator(".setup .badge")).toHaveText("LONG SETUP");
    await expect.poll(() => !!socket).toBe(true);
    if (trigger === "expiry") await page.clock.fastForward(6000);
    socket!.send(JSON.stringify({ type: "asset_quotes", data: {
      as_of: time.toISOString(), proxies: {}, quotes: { BTC: {
        ...quote, price: trigger === "invalidation" ? 90 : 101,
        timestamp: new Date(time.getTime() + (trigger === "expiry" ? 6000 : 0)).toISOString(),
      } },
    } }));
    await expect(page.locator(".setup .badge")).toHaveText("WAIT / DATA INSUFFICIENT");
    await expect(page.locator(".setup")).toContainText(trigger === "expiry"
      ? "Decision expired; awaiting recalculation" : "Price invalidation crossed");
    await expect(page.locator(".asset-price")).not.toContainText("STALE");
  });
}

test("dashboard explains unavailable feeds and navigation works", async ({
  page,
}) => {
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 503, body: "offline" }),
  );
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Today's market" }),
  ).toBeVisible();
  // Nothing ever loaded: no claim that there are no setups, only that the sections wait for the service.
  await expect(
    page.getByRole("heading", { name: "No actionable setups" }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Market sections appear once the data service answers."),
  ).toBeVisible();
  await expect(page.getByRole("alert").first()).toBeVisible();
  await page.screenshot({
    path: "test-results/dashboard-desktop.png",
    fullPage: true,
  });
  await page.getByRole("link", { name: "Calendar", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Market calendar" }),
  ).toBeVisible();
  // No data: no filters, record count or "No matching releases" (M11), only the outage notice.
  await expect(page.getByRole("alert").first()).toBeVisible();
  await expect(page.getByLabel("Impact filter")).toHaveCount(0);
  await expect(page.getByText("No matching releases")).toHaveCount(0);
});

test("all intelligence pages and detail routes handle unavailable data", async ({
  page,
}) => {
  await page.route("**/api/**", (route) => {
    const endpoint = new URL(route.request().url()).pathname;
    const data =
      endpoint === "/api/dashboard"
        ? emptyDashboard
        : endpoint === "/api/assets/BTC"
          ? {
              symbol: "BTC",
              name: "Bitcoin",
              asset_class: "crypto",
              quote: null,
              decision: null,
              bars: [],
              events: [],
            }
          : endpoint === "/api/events/test"
            ? {
                id: "test",
                summary_fact: "Test evidence record",
                verification_status: "UNVERIFIED",
                fact_confidence: 0,
                assets: [],
                sources: [],
                evidence: [],
                interpretation: null,
              }
            : endpoint === "/api/performance"
              ? {
                  observations: 0,
                  probability: null,
                  status: "NOT ENOUGH DATA",
                  paper_trades: [],
                }
              : [];
    return route.fulfill({ json: data });
  });
  for (const [path, heading] of [
    ["/events", "Event intelligence"],
    ["/fx", "Foreign exchange"],
    ["/sources", "Sources & verification"],
    ["/performance", "Historical performance"],
    ["/assets/BTC", "Bitcoin"],
    ["/events/test", "Event evidence"],
  ]) {
    await page.goto(path);
    await expect(
      page.getByRole("heading", { name: heading, exact: true }),
    ).toBeVisible();
  }
  await expect(page.getByText("Interpretation not available")).toBeVisible();
});

test("numeric calendar impact filtering excludes low impact events", async ({
  page,
}) => {
  await page.route("**/api/**", (route) =>
    route.fulfill({
      json: route.request().url().endsWith("/dashboard")
        ? emptyDashboard
        : [
            {
              id: "high",
              scheduled_utc: "2026-09-26T12:00:00Z",
              name: "High impact test",
              impact: 9,
            },
            {
              id: "medium",
              scheduled_utc: "2026-09-26T13:00:00Z",
              name: "Medium impact test",
              impact: 5,
            },
            {
              id: "low",
              scheduled_utc: "2026-09-26T14:00:00Z",
              name: "Low impact test",
              impact: 2,
            },
          ],
    }),
  );
  await page.goto("/calendar");
  await expect(
    page.getByText("High impact test", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Medium impact test", { exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("Impact filter").selectOption("ALL");
  await expect(
    page.getByText("Medium impact test", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Low impact test", { exact: true })).toHaveCount(
    0,
  );
});

test("mobile workspace remains navigable without horizontal page overflow", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/**", (route) =>
    route.fulfill({ json: emptyDashboard }),
  );
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Today's market" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/dashboard-mobile.png",
    fullPage: true,
  });
});
test("settings never invents models or persists credentials", async ({
  page,
}) => {
  await page.route("**/api/**", (route) =>
    route.fulfill({
      json: route.request().url().endsWith("/settings")
        ? { roles: [], models: [] }
        : route.request().url().endsWith("/dashboard")
          ? { providers: [], assets: [], events: [], calendar: [], regimes: [] }
          : [],
    }),
  );
  await page.goto("/settings");
  await page.getByRole("tab", { name: "AI models" }).click();
  await expect(
    page.getByRole("button", { name: "Discover models" }),
  ).toBeDisabled();
  await page.getByLabel("Admin token").fill("temporary-test-token");
  await expect(
    page.getByRole("button", { name: "Discover models" }),
  ).toBeEnabled();
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(
    "temporary-test-token",
  );
});

test("dashboard shows regime posture as rule coverage and reads the morning brief", async ({
  page,
}) => {
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  await page.route("**/api/dashboard", (route) =>
    route.fulfill({
      json: {
        ...emptyDashboard,
        as_of: new Date().toISOString(),
        regimes: ["INFLATION_SHOCK", "USD_STRENGTH"],
        regime: {
          regimes: ["INFLATION_SHOCK", "USD_STRENGTH"],
          posture: "DEFENSIVE",
          confidence: 78,
          coverage_pct: 40,
          basis: "Rule coverage",
        },
      },
    }),
  );
  await page.route("**/api/brief?kind=MORNING", (route) =>
    route.fulfill({
      json: {
        kind: "MORNING",
        generated_at: "2026-07-01T04:30:00Z",
        changes: [],
        sections: [{ title: "Top catalysts", items: ["Test catalyst"] }],
      },
    }),
  );
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "DEFENSIVE · INFLATION SHOCK / USD STRENGTH" }),
  ).toBeVisible();
  await page.getByText("Data coverage and basis", { exact: true }).click();
  await expect(page.getByText("Not a probability.")).toBeVisible();
  await page.getByText("Provider health and market brief", { exact: true }).click();
  await page.getByLabel("Brief type").selectOption("MORNING");
  await page.getByRole("button", { name: "Read brief" }).click();
  await expect(page.getByRole("heading", { name: "Top catalysts" })).toBeVisible();
  await expect(page.getByText(/Morning brief · /)).toBeVisible();
});

/* --- Shell, error states, settings and provider health (test-only API responses) --- */

type Handler = (url: URL, route: Route) => unknown;

/**
 * Intercept /api/**: `handlers[path]` answers that path (a value, or a function of the URL that may fulfil the
 * route itself and return undefined); the dashboard defaults to an empty one and everything else to [].
 */
async function serveApi(page: Page, handlers: Record<string, unknown | Handler> = {}) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api/, "");
    const handler = handlers[path];
    if (typeof handler === "function") {
      const value = await (handler as Handler)(url, route);
      if (value !== undefined) return route.fulfill({ json: value });
      return;
    }
    if (handler !== undefined) return route.fulfill({ json: handler });
    return route.fulfill({ json: path === "/dashboard" ? emptyDashboard : [] });
  });
}

const PROVIDERS = [
  { name: "binance", label: "Binance (crypto)", status: "ONLINE", latency_ms: 40, last_success: null, error: null },
  { name: "nyfed_rates", label: "New York Fed reference rates", status: "PENDING", latency_ms: null, last_success: null, error: null },
  { name: "massive", label: "Massive (US equities)", status: "AUTH_ERROR", latency_ms: null, last_success: null, error: "Access denied (HTTP 403)" },
  { name: "sec_edgar", status: "NOT CONFIGURED", latency_ms: null, last_success: null, error: null },
];

test("section pages recover by themselves after a short outage, without a focus change", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-09-26T12:00:00Z") });
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  let down = true;
  await serveApi(page, {
    "/settings": (_: URL, route: Route) =>
      down ? void route.abort("connectionrefused") : { roles: [], admin_mode: "local" },
    "/providers": (_: URL, route: Route) => (down ? void route.abort("connectionrefused") : PROVIDERS),
    "/dashboard": (_: URL, route: Route) => (down ? void route.abort("connectionrefused") : emptyDashboard),
  });
  await page.goto("/settings");
  await expect(page.getByRole("alert").filter({ hasText: "Unable to reach the data service" }).first()).toBeVisible();
  // Plain words only: no raw browser text, and never a password prompt in local mode.
  await expect(page.getByText("Failed to fetch")).toHaveCount(0);
  await expect(page.getByText("Locked.")).toHaveCount(0);

  down = false;
  await page.clock.fastForward(11_000);
  await expect(page.getByText("Unable to reach the data service")).toHaveCount(0);
  await expect(page.getByRole("table", { name: "Provider health" }).locator("tr", { hasText: "Binance (crypto)" })).toBeVisible();
});

test("error notices say what happened: an HTTP error, a missing record, or no answer", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-09-26T12:00:00Z") });
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  let settingsFails = true;
  let providerCalls = 0;
  await serveApi(page, {
    "/settings": (_: URL, route: Route) =>
      settingsFails
        ? void route.fulfill({ status: 500, json: { detail: "Injected failure" } })
        : { roles: [], admin_mode: "local" },
    "/providers": (_: URL, route: Route) => {
      providerCalls++;
      return void route.fulfill({ status: 404, json: { detail: "Not Found" } });
    },
  });
  await page.goto("/settings");
  const failure = page.getByRole("alert").filter({ hasText: "The service returned an error (HTTP 500)." });
  await expect(failure).toContainText("Injected failure");
  await expect(page.getByText("Unable to reach the data service")).toHaveCount(0);
  settingsFails = false;
  await failure.getByRole("button", { name: "Retry now" }).click();
  await expect(failure).toHaveCount(0);

  // A 404 is "Not found", is asked once (no retry) and is not polled again.
  await page.goto("/providers");
  await expect(page.getByRole("alert").filter({ hasText: "Not found." })).toContainText(
    "The service has no record at this address.",
  );
  const calls = providerCalls;
  await page.clock.fastForward(40_000);
  // Let any (wrongly) triggered request reach the route before counting.
  await page.waitForTimeout(1000);
  expect(providerCalls).toBe(calls);
  await expect(page.getByText("Unable to reach the data service")).toHaveCount(0);
});

test("unknown addresses are real 404 pages inside the themed shell", async ({ page }) => {
  await serveApi(page);
  for (const path of ["/does-not-exist", "/xyz/abc"]) {
    const response = await page.goto(path);
    expect(response?.status(), path).toBe(404);
    await expect(page.getByRole("heading", { level: 1, name: "Page not found" })).toBeVisible();
    await expect(page.getByRole("main").getByRole("link", { name: "Back to the dashboard" })).toHaveAttribute("href", "/");
    await expect(page).toHaveTitle(/Page not found/);
    // The app's dark background, not a default white error page.
    expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe("rgb(11, 20, 34)");
  }
});

test("navigation marks the current page, and on a phone keeps it in view with a fade on the hidden side", async ({
  page,
}) => {
  await serveApi(page);
  await page.goto("/providers");
  const nav = page.getByRole("navigation", { name: "Main navigation" });
  await expect(nav.getByRole("link", { name: "Providers", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(nav.locator('[aria-current="page"]')).toHaveCount(1);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/settings");
  const current = nav.getByRole("link", { name: "Settings", exact: true });
  await expect(current).toHaveAttribute("aria-current", "page");
  // Settings is the last link: it starts scrolled into view, with the fade on the links behind it.
  await expect
    .poll(() =>
      current.evaluate((link) => {
        const box = link.closest("nav")!.getBoundingClientRect();
        const item = link.getBoundingClientRect();
        return item.left >= box.left - 1 && item.right <= box.right + 1;
      }),
    )
    .toBe(true);
  await expect(nav).toHaveAttribute("data-fade", /start|both/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("the header flags an out-of-date full analysis and counts only providers expected online", async ({ page }) => {
  const NOW = new Date("2026-09-28T04:26:30Z");
  const ago = (minutes: number) => new Date(NOW.getTime() - minutes * 60_000).toISOString();
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  let dashboard: Record<string, unknown> = {
    ...emptyDashboard,
    as_of: NOW.toISOString(),
    last_analysis: ago(316),
    analysis: {
      last_success_at: ago(316),
      last_attempt_at: ago(2),
      last_error_type: "TypeError",
      cadence_seconds: 300,
      stale: true,
    },
    // Not configured and not checked yet are not expected online, so nothing needs attention.
    providers: PROVIDERS.filter((p) => p.status !== "AUTH_ERROR"),
  };
  await serveApi(page, { "/dashboard": () => dashboard });
  await page.goto("/fx");
  const header = page.locator("header.topbar");
  // One date format across the app: weekday, day without a leading zero, fixed month name.
  await expect(header).toContainText("Last full analysis Mon 28 Sep 02:10");
  const stale = header.locator(".text-warning", { hasText: "5 h 16 min ago" });
  await expect(stale).toBeVisible();
  await expect(stale).toHaveAttribute("title", /failed/);
  await expect(stale).toContainText("Out of date.");
  await expect(stale).not.toContainText("TypeError");
  const count = header.getByRole("link", { name: /providers online/ });
  await expect(count).toHaveText("1/1 providers online");
  await expect(count).not.toHaveClass(/text-warning/);
  await expect(count).toHaveAttribute("title", "1 online · 0 need attention · 1 not checked yet · 1 not configured · 0 switched off");

  // An older backend without the analysis record: judged by age (older than 15 minutes); a provider in error warns.
  dashboard = { ...emptyDashboard, as_of: NOW.toISOString(), last_analysis: ago(20), providers: PROVIDERS };
  await page.clock.fastForward(6000);
  await expect(header.locator(".text-warning", { hasText: "20 min ago" })).toBeVisible();
  await expect(count).toHaveText("1/2 providers online");
  await expect(count).toHaveClass(/text-warning/);

  dashboard = { ...emptyDashboard, as_of: NOW.toISOString(), last_analysis: ago(5), providers: [] };
  await page.clock.fastForward(6000);
  await expect(header).toContainText(/Last full analysis Mon 28 Sep \d\d:\d\d/);
  await expect(header).not.toContainText("ago");
});

test("the alert bell opens /alerts and counts unread alerts beyond the dashboard's short list", async ({ page }) => {
  const NOW = new Date("2026-09-26T12:00:00Z");
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  const alerts = (n: number) =>
    Array.from({ length: n }, (_, i) => ({
      id: `a${i}`,
      kind: "SETUP",
      priority: "LOW",
      title: `Test alert ${i}`,
      message: "",
      symbol: null,
      created_at: new Date(NOW.getTime() - (i + 1) * 60_000).toISOString(),
    }));
  const limits: (string | null)[] = [];
  await serveApi(page, {
    // The dashboard carries only the newest 30, all unread in a fresh browser.
    "/dashboard": { ...emptyDashboard, alerts: alerts(30) },
    "/alerts": (url: URL) => {
      limits.push(url.searchParams.get("limit"));
      return alerts(45);
    },
  });
  await page.goto("/fx");
  const bell = page.getByRole("banner").getByRole("link", { name: /^Alerts, / });
  await expect(bell).toHaveAttribute("href", "/alerts");
  await expect(bell).toHaveAttribute("aria-label", "Alerts, 45 unread");
  expect(limits).toContain("500");
});

test("provider health shows names, keeps re-reading while open and has no empty state while loading", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-09-26T12:00:00Z") });
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  let calls = 0;
  await serveApi(page, {
    "/providers": async () => {
      calls++;
      await held;
      return PROVIDERS;
    },
  });
  await page.goto("/providers");
  await expect(page.getByText("Loading provider status…")).toBeVisible();
  await expect(page.getByText("No providers reported")).toHaveCount(0);
  await expect(page.getByText("No matching providers")).toHaveCount(0);
  await expect(page.getByText(/\d+ records/)).toHaveCount(0);
  release();

  const table = page.getByRole("table", { name: "Provider health" });
  const massive = table.locator("tr", { hasText: "Massive (US equities)" });
  await expect(massive).toContainText("massive");
  await expect(massive).toContainText("Access denied (HTTP 403)");
  await expect(table.locator("tr", { hasText: "sec_edgar" })).toBeVisible();
  // The tiles add up to the total, pending included.
  const stat = (label: string) =>
    page.locator("div", { has: page.getByText(label, { exact: true }) }).last().locator("strong");
  await expect(stat("Pending")).toHaveText("1");
  await expect(stat("Total")).toHaveText("4");

  // Search and status filter.
  await page.getByLabel("Search records").fill("fed");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table.locator("tbody tr")).toContainText("New York Fed reference rates");
  await page.getByLabel("Search records").fill("");
  await page.getByLabel("Provider status filter").selectOption("ATTENTION");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table.locator("tbody tr")).toContainText("Massive (US equities)");

  // Re-read in the background without a click.
  const before = calls;
  await page.clock.fastForward(16_000);
  await expect.poll(() => calls).toBeGreaterThan(before);
});

test("the events list polls for new events while open", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-09-26T12:00:00Z") });
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  let calls = 0;
  await serveApi(page, {
    "/events": () => {
      calls++;
      return [];
    },
  });
  await page.goto("/events");
  await expect.poll(() => calls).toBeGreaterThan(0);
  const before = calls;
  await page.clock.fastForward(31_000);
  await expect.poll(() => calls).toBeGreaterThan(before);
});

const SETTINGS = {
  admin_mode: "local",
  editable: ["refresh", "alerts", "scoring", "sources", "roles"],
  roles: [
    { role: "chief_analyst", primary: "qwen3:8b", backup: null, second_opinion: "qwen3:8b" },
    { role: "market_interpreter", primary: "qwen3:8b", backup: null, second_opinion: null, inherited_from: "chief_analyst" },
  ],
  refresh: [
    { kind: "quotes", mode: "websocket", seconds: 5, label: "Real-time quotes (BTC/ETH, US equities, FX, futures)" },
    { kind: "interpretation", seconds: 300, inactive_seconds: 900, label: "Full market interpretation" },
    { kind: "release_watch", seconds: 30, tick_seconds: 5, label: "Authoritative source during event watch mode" },
  ],
  alerts: {
    version: "1.2.0",
    channels: [{ name: "in_app", status: "ONLINE", min_priority: "LOW", max_per_hour: 60 }],
    cooldown_seconds: { SETUP: 3600, LEVEL_BREAK: 1800 },
    kinds: ["SETUP", "LEVEL_BREAK"],
    priorities: ["LOW", "MEDIUM", "HIGH"],
  },
  autolearn: {
    decision_settle_seconds: 0,
    session_horizons: ["close", "next_session"],
    versions: {
      active: {},
      candidates: [{ id: "c1", symbol: "BTC", horizon: "4h", state: "SHADOW", created_at: "2026-09-26T09:00:00Z" }],
      challengers: [],
    },
  },
};

test("settings: slug tab ids, refresh cadences from the list, readable configuration and a confirmed rollback", async ({
  page,
}) => {
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  const posts: string[] = [];
  await serveApi(page, {
    "/settings": SETTINGS,
    "/models": [{ name: "qwen3:8b", availability: true, status: "AVAILABLE", capabilities: [] }],
    "/admin/calibration/BTC/rollback": (url: URL) => {
      posts.push(`${url.pathname}${url.search}`);
      return { state: "ROLLED_BACK" };
    },
  });
  await page.goto("/settings");
  // Radix derives element ids from tab values: no spaces, so aria-controls and aria-labelledby resolve.
  for (const tab of await page.getByRole("tab").all())
    expect(await tab.getAttribute("aria-controls")).not.toContain(" ");
  await expect(page.getByRole("tabpanel", { name: "Data providers" })).toBeVisible();

  await page.getByRole("tab", { name: "Refresh" }).click();
  const quotes = page.locator("tr", { hasText: "Real-time quotes" });
  await expect(quotes).toContainText("every 5 s");
  await expect(quotes).toContainText("WebSocket push");
  await expect(quotes).toContainText("when the push is unavailable");
  const analysis = page.locator("tr", { hasText: "Full market interpretation" });
  await expect(analysis).toContainText("every 5 min");
  // Other intervals say when they apply, never a configuration key.
  await expect(analysis).toContainText("While markets are closed: every 15 min");
  const watch = page.locator("tr", { hasText: "Authoritative source during event watch mode" });
  await expect(watch).toContainText("Schedule check: every 5 s");
  await expect(watch).not.toContainText("Tick");
  // A row without a delivery mode is not described as polling (or anything else the configuration does not say).
  await expect(analysis).not.toContainText("Polling");
  await expect(analysis).not.toContainText("push");
  await expect(page.getByText("Configuration unavailable")).toHaveCount(0);
  await expect(page.getByText(/audited admin configuration endpoint/)).toBeVisible();

  await page.getByRole("tab", { name: "Alerts" }).click();
  const alertsPanel = page.getByRole("tabpanel", { name: "Alerts" });
  await expect(alertsPanel.locator("tr", { hasText: "Priorities" })).toContainText("MEDIUM");
  expect(await alertsPanel.textContent()).not.toContain('{"');
  await alertsPanel.getByText("2 settings").click();
  // Nested settings: a delay reads as a span ("30 min"), never as a cadence or JSON.
  await expect(alertsPanel.getByRole("row", { name: "Level break 30 min", exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "Autolearn" }).click();
  const settle = page.locator("tr", { hasText: "Decision settle seconds" });
  await expect(settle).toContainText("0 s");
  await expect(settle).not.toContainText("every");
  await expect(page.locator("tr", { hasText: "Session horizons" })).toContainText("next session");
  await expect(page.getByText("Calibration versions (champions, candidates and challengers)")).toBeVisible();

  // Rolling back takes a second click that names what is rolled back; Cancel leaves everything as it was.
  await page.getByLabel("Asset symbol").fill("btc");
  await page.getByRole("button", { name: "Roll back", exact: true }).click();
  await expect(page.getByRole("button", { name: "Confirm roll back BTC 4h" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name: "Roll back", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Roll back", exact: true })).toBeFocused();
  expect(posts).toEqual([]);
  // A double-click arms the confirmation but never confirms it.
  await page.getByRole("button", { name: "Roll back", exact: true }).dblclick();
  await expect(page.getByRole("button", { name: "Confirm roll back BTC 4h" })).toBeVisible();
  expect(posts).toEqual([]);
  // Escape cancels from anywhere in the row, e.g. while typing in a field.
  await page.getByLabel("Horizon").press("Escape");
  await expect(page.getByRole("button", { name: "Roll back", exact: true })).toBeVisible();
  expect(posts).toEqual([]);
  await page.getByRole("button", { name: "Roll back", exact: true }).click();
  await page.getByRole("button", { name: "Confirm roll back BTC 4h" }).click();
  await expect(page.getByText("Rollback complete.")).toBeVisible();
  expect(posts).toEqual(["/api/admin/calibration/BTC/rollback?horizon=4h"]);
});

test("AI role forms: Save waits for a change, optional slots can be cleared and inheritance is shown", async ({ page }) => {
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  const puts: unknown[] = [];
  await serveApi(page, {
    "/settings": SETTINGS,
    "/models": [{ name: "qwen3:8b", availability: true, status: "AVAILABLE", capabilities: [] }],
    "/admin/roles/chief_analyst": (_: URL, route: Route) => {
      const body = route.request().postDataJSON();
      puts.push(body);
      return { ...body, role: "chief_analyst" };
    },
  });
  await page.goto("/settings");
  await page.getByRole("tab", { name: "AI models" }).click();
  await expect(page.getByRole("heading", { name: "Market interpreter" })).toBeVisible();
  // The backend copied the parent's models once; the note does not promise a live link.
  await expect(page.getByText("Copied from Chief analyst's assignment when this role was first set up")).toBeVisible();
  await expect(page.getByText("later changes to Chief analyst do not carry over")).toBeVisible();
  const save = page.getByRole("button", { name: "Save assignment: Chief analyst" });
  await expect(save).toBeDisabled();
  await expect(page.getByRole("button", { name: "Save assignment: Market interpreter" })).toBeDisabled();

  const chief = page.locator("form", { has: page.getByRole("heading", { name: "Chief analyst" }) });
  await chief.getByLabel("Second opinion").click();
  await page.getByRole("option", { name: "None" }).click();
  await expect(save).toBeEnabled();
  await save.click();
  await expect(chief.getByText("Assignment saved.")).toBeVisible();
  expect(puts).toEqual([{ primary: "qwen3:8b", backup: null, second_opinion: null }]);
  await expect(save).toBeDisabled();
});

test("a failed page keeps its notice while it is asked again in the background: no Loading flicker", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-09-26T12:00:00Z") });
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  let calls = 0;
  await serveApi(page, {
    "/settings": (_: URL, route: Route) => {
      calls++;
      return void route.fulfill({ status: 503, json: { detail: "Warming up" } });
    },
  });
  await page.goto("/settings");
  const failure = page.getByRole("alert").filter({ hasText: "The service returned an error (HTTP 503)." });
  await expect(failure).toContainText("Warming up");
  const notice = await failure.elementHandle();
  // Record whether the loading text ever comes back while the failed load is asked again.
  await page.evaluate(() => {
    const flags = window as unknown as { sawLoading: boolean };
    flags.sawLoading = false;
    new MutationObserver(() => {
      if (document.body.textContent?.includes("Loading workspace settings…")) flags.sawLoading = true;
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  });
  const before = calls;
  // The dashboard answers, so the shell asks the failed page again (every 10 s), with its one quick retry.
  await page.clock.fastForward(11_000);
  await expect.poll(() => calls).toBeGreaterThanOrEqual(before + 2);
  await expect(failure).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { sawLoading: boolean }).sawLoading)).toBe(false);
  // The same notice element throughout: never removed and re-inserted, so the outage is not announced again.
  expect(await notice!.evaluate((el) => el.isConnected)).toBe(true);
});

test("provider health shows each provider's category, filters by it and counts development stubs apart", async ({
  page,
}) => {
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  const providers = [
    { ...PROVIDERS[0], category: "market" },
    { name: "fred", label: "FRED (St. Louis Fed)", category: "macro", status: "ONLINE", latency_ms: 80, last_success: null, error: null },
    {
      name: "finnhub-stub",
      label: "Finnhub (development stub)",
      category: "market",
      status: "DEVELOPMENT STUB",
      latency_ms: null,
      last_success: null,
      error: null,
      stub: true,
    },
  ];
  await serveApi(page, { "/providers": providers, "/dashboard": { ...emptyDashboard, providers } });
  await page.goto("/providers");
  const table = page.getByRole("table", { name: "Provider health" });
  await expect(table.locator("tr", { hasText: "Binance (crypto)" })).toContainText("binance · Market data");
  await expect(table.locator("tr", { hasText: "FRED (St. Louis Fed)" })).toContainText("fred · Macro & official data");
  const stat = (label: string) =>
    page.locator("div", { has: page.getByText(label, { exact: true }) }).last().locator("strong");
  // A development stub serves sample data: its own tile, never "impaired" or "needs attention".
  await expect(stat("Development stubs")).toHaveText("1");
  await expect(stat("Impaired")).toHaveText("0");
  await expect(stat("Total")).toHaveText("3");
  await page.getByLabel("Provider status filter").selectOption("ATTENTION");
  await expect(page.getByText("No matching providers")).toBeVisible();
  await page.getByLabel("Provider status filter").selectOption({ label: "Development stub" });
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table.locator("tbody tr")).toContainText("Finnhub (development stub)");
  await page.getByLabel("Provider status filter").selectOption("ALL");

  // Categories in the settings order, and only those present.
  const category = page.getByLabel("Provider category filter");
  await expect(category.locator("option")).toHaveText(["All categories", "Market data", "Macro & official data"]);
  await category.selectOption({ label: "Macro & official data" });
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table.locator("tbody tr")).toContainText("FRED (St. Louis Fed)");

  // The header neither warns about the stub nor expects it online; its title names it.
  const count = page.locator("header.topbar").getByRole("link", { name: /providers online/ });
  await expect(count).toHaveText("2/2 providers online");
  await expect(count).not.toHaveClass(/text-warning/);
  await expect(count).toHaveAttribute("title", /· 1 development stub$/);
});

test("the unread count shares the Alerts list and reads it again only when a newer alert arrives", async ({ page }) => {
  const NOW = new Date("2026-09-26T12:00:00Z");
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  const alert = (i: number, minutesAgo: number) => ({
    id: `a${i}`,
    kind: "SETUP",
    priority: "LOW",
    title: `Test alert ${i}`,
    message: "",
    symbol: null,
    created_at: new Date(NOW.getTime() - minutesAgo * 60_000).toISOString(),
  });
  let list = Array.from({ length: 45 }, (_, i) => alert(i, i + 1));
  let reads = 0;
  await serveApi(page, {
    // The dashboard carries only the newest 30, all unread in a fresh browser.
    "/dashboard": () => ({ ...emptyDashboard, alerts: list.slice(0, 30) }),
    "/alerts": () => {
      reads++;
      return list;
    },
  });
  await page.goto("/fx");
  const bell = page.getByRole("banner").getByRole("link", { name: /^Alerts, / });
  await expect(bell).toHaveAttribute("aria-label", "Alerts, 45 unread");
  const first = reads;
  // Nothing new: the dashboard keeps polling, the long list is not read again.
  await page.clock.fastForward(65_000);
  expect(reads).toBe(first);

  // A newer alert reaches the dashboard: the list is read once more and the count follows.
  list = [alert(99, -1), ...list];
  await page.clock.fastForward(6_000);
  await expect(bell).toHaveAttribute("aria-label", "Alerts, 46 unread");
  expect(reads).toBe(first + 1);
});

test("the regime banner is one clean column on a phone and keeps its as-of time on narrow screens", async ({ page }) => {
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  await serveApi(page, {
    "/dashboard": {
      ...emptyDashboard,
      as_of: "2026-09-26T12:00:00Z",
      regimes: ["USD_STRENGTH"],
      regime: { regimes: ["USD_STRENGTH"], posture: "DEFENSIVE", confidence: 60, coverage_pct: 40, basis: "live" },
    },
  });
  await page.setViewportSize({ width: 900, height: 900 });
  await page.goto("/");
  const banner = page.locator("section.regime");
  await expect(banner.locator(".regime-asof")).toBeVisible();
  await expect(banner.locator(".regime-asof")).toContainText("As of Sat 26 Sep 15:00");

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(banner.locator(".regime-icon")).toBeHidden();
  await expect(banner.locator(".regime-asof")).toBeVisible();
  // The text uses the full width: the heading starts at the banner's padding, not after an icon column.
  const box = await banner.boundingBox();
  const heading = await banner.getByRole("heading", { level: 2 }).first().boundingBox();
  expect(heading!.x - box!.x).toBeLessThan(30);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("while the dashboard first loads the header says so, never 'Not recorded' or 'none unread'", async ({ page }) => {
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  const alert = {
    id: "a1",
    kind: "SETUP",
    priority: "LOW",
    title: "Test alert",
    message: "",
    symbol: null,
    created_at: "2026-09-28T10:20:00Z",
  };
  await serveApi(page, {
    "/dashboard": async () => {
      await held;
      return { ...emptyDashboard, last_analysis: "2026-09-28T10:25:00Z", alerts: [alert], alerts_total: 1 };
    },
  });
  await page.goto("/providers");
  const header = page.locator("header.topbar");
  const bell = header.getByRole("link", { name: /^Alerts/ });
  await expect(header).toContainText("Checking providers…");
  await expect(header).toContainText("Last full analysis Loading…");
  await expect(header).not.toContainText("Not recorded");
  await expect(header).not.toContainText("Provider health unavailable");
  await expect(bell).toHaveAttribute("aria-label", "Alerts, unread count loading");
  await expect(bell.locator("span")).toHaveCount(0);

  release();
  await expect(header).toContainText("0/0 providers online");
  await expect(header).toContainText("Last full analysis Mon 28 Sep 13:25");
  await expect(bell).toHaveAttribute("aria-label", "Alerts, 1 unread");
  await expect(bell).toHaveText("1");
});

test("when the dashboard fails the header says the values are unavailable instead of inventing them", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-09-26T12:00:00Z") });
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  let calls = 0;
  await serveApi(page, {
    "/dashboard": (_: URL, route: Route) => {
      calls++;
      return void route.fulfill({ status: 500, json: { detail: "Injected failure" } });
    },
  });
  await page.goto("/settings");
  const header = page.locator("header.topbar");
  await expect(header).toContainText("Provider health unavailable");
  await expect(header).toContainText("Last full analysis Unavailable");
  await expect(header).not.toContainText("Not recorded");
  const bell = header.getByRole("link", { name: /^Alerts/ });
  await expect(bell).toHaveAttribute("aria-label", "Alerts, unread count unavailable");
  await expect(bell.locator("span")).toHaveCount(0);
  // Asked again every few seconds: the header stays "unavailable" and never flips back to loading.
  await page.evaluate(() => {
    const flags = window as unknown as { sawLoading: boolean };
    flags.sawLoading = false;
    const header = document.querySelector("header.topbar")!;
    new MutationObserver(() => {
      if (/Loading…|Checking providers/.test(header.textContent ?? "")) flags.sawLoading = true;
    }).observe(header, { childList: true, subtree: true, characterData: true });
  });
  const before = calls;
  await page.clock.fastForward(12_000);
  await expect.poll(() => calls).toBeGreaterThan(before);
  await expect(header).toContainText("Last full analysis Unavailable");
  expect(await page.evaluate(() => (window as unknown as { sawLoading: boolean }).sawLoading)).toBe(false);
});

test("the bell counts every stored alert in a fresh browser and is a lower bound when the long list fails", async ({
  page,
}) => {
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  const NOW = Date.parse("2026-09-26T12:00:00Z");
  const alerts = Array.from({ length: 30 }, (_, i) => ({
    id: `a${i}`,
    kind: "SETUP",
    priority: "LOW",
    title: `Test alert ${i}`,
    message: "",
    symbol: null,
    created_at: new Date(NOW - (i + 1) * 60_000).toISOString(),
  }));
  let reads = 0;
  await serveApi(page, {
    // The newest 30 of 201 stored alerts.
    "/dashboard": { ...emptyDashboard, alerts, alerts_total: 201 },
    "/alerts": (_: URL, route: Route) => {
      reads++;
      return void route.fulfill({ status: 500, json: { detail: "Injected failure" } });
    },
  });
  await page.goto("/fx");
  const bell = page.getByRole("banner").getByRole("link", { name: /^Alerts, / });
  // Nothing was ever marked read: all 201 are unread, known without reading the long list.
  await expect(bell).toHaveAttribute("aria-label", "Alerts, 201 unread");
  await expect(bell).toHaveText("99+");
  expect(reads).toBe(0);

  // Marked read before all 30 were created: older ones may be unread too, and the long list cannot be read.
  await page.evaluate(() => localStorage.setItem("mi.alerts.last-read", "2026-09-25T00:00:00Z"));
  await page.reload();
  await expect.poll(() => reads).toBeGreaterThan(0);
  await expect(bell).toHaveAttribute("aria-label", "Alerts, at least 30 unread");
  await expect(bell).toHaveText("30+");
});

test("asset_quotes frames patch the cached dashboard's prices between full snapshots", async ({ page }) => {
  let socket: WebSocketRoute | undefined;
  // Mocked socket stays open: the shell reports LIVE and applies stream frames.
  await page.routeWebSocket("**/ws", (ws) => {
    socket = ws;
  });
  const at = new Date().toISOString();
  const quote = (price: number) => ({
    price,
    timestamp: at,
    provider: "Test fixture",
    market_status: "LIVE",
    freshness_status: "FRESH",
    change_pct: null,
  });
  const btc = { symbol: "BTC", name: "Bitcoin", asset_class: "crypto", quote: quote(100), decision: null };
  const dashboard = { ...emptyDashboard, as_of: at, assets: [btc] };
  await serveApi(page, { "/dashboard": dashboard });
  await page.goto("/");
  await expect(page.locator("header.topbar").getByText("LIVE", { exact: true })).toBeVisible();
  const price = page.locator(".market-tile").first().locator("b");
  await expect(price).toHaveText("100");
  const send = (message: unknown) => socket!.send(JSON.stringify(message));
  const prices = (quotes: Record<string, unknown>) => ({ type: "asset_quotes", data: { as_of: at, quotes, proxies: {} } });

  send(prices({ BTC: quote(64000), UNKNOWN: quote(1) }));
  await expect(price).toHaveText("64,000");
  // Stream types the pages do not use are ignored.
  send({ type: "quotes", data: [{ symbol: "BTC", price: 1 }] });
  // A snapshot, then a price frame: the newer prices land on top of the snapshot.
  send({ type: "snapshot", data: dashboard });
  send(prices({ BTC: quote(65000) }));
  await expect(price).toHaveText("65,000");
  // A price frame, then a snapshot: the snapshot is newer and wins.
  send(prices({ BTC: quote(66000) }));
  send({ type: "snapshot", data: dashboard });
  await expect(price).toHaveText("100");
  // null: the asset has no quote any more.
  send(prices({ BTC: null }));
  await expect(price).toHaveText("—");
});

test("settings tabs on a phone fade the hidden side and scroll the selected tab into view", async ({ page }) => {
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  await serveApi(page, { "/settings": SETTINGS });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/settings");
  const strip = page.getByRole("tablist", { name: "Settings sections" });
  await expect(strip).toHaveAttribute("data-fade", "end");
  await expect(strip).not.toHaveCSS("mask-image", "none");
  const inView = (name: string) =>
    page.getByRole("tab", { name, exact: true }).evaluate((tab) => {
      const box = tab.parentElement!.getBoundingClientRect();
      const item = tab.getBoundingClientRect();
      return item.left >= box.left - 1 && item.right <= box.right + 1;
    });
  await page.getByRole("tab", { name: "Refresh", exact: true }).click();
  await expect.poll(() => inView("Refresh")).toBe(true);
  await expect(strip).toHaveAttribute("data-fade", /both|start/);
  // Keyboard selection scrolls too: End selects the last tab.
  await page.getByRole("tab", { name: "Refresh", exact: true }).press("End");
  await expect(page.getByRole("tab", { name: "Autolearn", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect.poll(() => inView("Autolearn")).toBe(true);
  await expect(strip).toHaveAttribute("data-fade", "start");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("on a phone a wide page action moves under the description instead of squeezing it", async ({ page }) => {
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  // Permission not asked yet (usual on Android): the wide "Enable browser notifications" button is shown.
  await page.addInitScript(() => {
    class FakeNotification {
      static get permission(): NotificationPermission {
        return "default";
      }
      static async requestPermission() {
        return "default" as const;
      }
    }
    Object.defineProperty(window, "Notification", { value: FakeNotification, configurable: true });
  });
  await serveApi(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/alerts");
  const heading = page.locator(".page-heading").first();
  const action = heading.getByRole("button", { name: "Enable browser notifications" });
  await expect(action).toBeVisible();
  const text = (await heading.locator(":scope > div").first().boundingBox())!;
  const button = (await action.boundingBox())!;
  expect(button.y).toBeGreaterThanOrEqual(text.y + text.height - 1);
  // The title and description keep (nearly) the whole width.
  expect(text.width).toBeGreaterThan(300);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("the performance overview wraps beside the sidebar instead of clipping its status", async ({ page }) => {
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  const none = { observations: 0, hit_rate: null };
  await serveApi(page, {
    "/performance": {
      observations: 0,
      decisions_recorded: 676,
      probability: null,
      status: "INSUFFICIENT_DATA",
      default_horizon: "4h",
      breakdown_horizon: "4h",
      horizons: ["1h", "4h"],
      metrics: { "1h": none, "4h": none },
      breakdowns: { asset: { BTC: none } },
      paper_trades: [],
    },
  });
  for (const width of [720, 768, 800]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/performance");
    const overview = page.locator(".performance-overview");
    const status = overview.getByText("NOT ENOUGH DATA");
    await expect(status).toBeVisible();
    const card = (await overview.boundingBox())!;
    const badge = (await status.boundingBox())!;
    expect(badge.x + badge.width, `${width} px`).toBeLessThanOrEqual(card.x + card.width + 1);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
      `${width} px scrolls sideways`,
    ).toBe(true);
  }
});
