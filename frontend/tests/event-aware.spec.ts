import { test, expect, type Page, type WebSocketRoute } from "@playwright/test";

/*
 * Event-aware dashboard (docs/EVENT_AWARENESS.md §6) against intercepted,
 * test-only API responses shaped like the backend's `event_risk` and
 * `interpretation` blocks, with the binding field shapes of §8: consensus
 * {label, values: [{name, value, unit}], source} or "NO CONSENSUS", event
 * kind / is_decision, mode.time_utc, reaction cells {change, unit, status},
 * FAVORED / PRESSURED / NOT CONFIRMING stances, event_risk objects, ACTIVE
 * unscheduled events. Wednesday 30 Sep 2026 17:13 UTC = 20:13 Bucharest
 * (EEST, UTC+3); the FOMC decision is at 18:00 UTC = 21:00 Bucharest, 47 min away.
 */
const NOW = new Date("2026-09-30T17:13:00Z");
const iso = (minutes: number) => new Date(NOW.getTime() + minutes * 60_000).toISOString();
const FED = "FED-20260930-FOMC";

const quote = (price: number) => ({
  price,
  timestamp: NOW.toISOString(),
  provider: "Test fixture",
  market_status: "LIVE",
  freshness_status: "FRESH",
  change_pct: 0.4,
});

const decision = (extra: Record<string, unknown>) => ({
  id: `d-${Math.random()}`,
  direction: "WAIT",
  score: 0,
  evidence_confidence: 70,
  market_confirmation: 60,
  data_quality: 100,
  reasons: [],
  confirmation: "Test confirmation",
  invalidation: "Test invalidation",
  audit_status: "PASS",
  components: {},
  ...extra,
});

const asset = (symbol: string, asset_class: string, price: number, d?: Record<string, unknown>) => ({
  symbol,
  name: `${symbol} name`,
  asset_class,
  quote: quote(price),
  decision: d ? decision(d) : null,
});

type Block = Record<string, unknown>;

function fedEvent(extra: Block = {}): Block {
  return {
    event_id: FED,
    row_id: "cal-fomc-20260930",
    name: "FOMC rate decision",
    event_class: "FOMC",
    category: "CENTRAL_BANK",
    kind: "DECISION",
    is_decision: true,
    speaker_role: null,
    symbol: null,
    status: "APPROACHING",
    phase: "APPROACHING",
    timing: "SCHEDULED",
    time_utc: iso(47),
    time_bucharest: "21:00",
    date_bucharest: "2026-09-30",
    time_known: true,
    time_basis: "Fed calendar",
    minutes_until_event: 47,
    importance_score: 100,
    expected_volatility_score: 95,
    relevance_score: 96,
    relevance_level: "CRITICAL",
    dominance_score: 95,
    impact_10: 10,
    affected_assets: [
      { symbol: "US10Y", level: "high", weight: 0.75 },
      { symbol: "US2Y", level: "very_high", weight: 1 },
      { symbol: "DXY", level: "very_high", weight: 1 },
      { symbol: "EURUSD", level: "very_high", weight: 1 },
      { symbol: "NASDAQ", level: "very_high", weight: 1 },
      { symbol: "BTC", level: "high", weight: 0.75 },
      { symbol: "GOLD", level: "high", weight: 0.75 },
    ],
    consensus: {
      label: "Consensus",
      values: [{ name: "Fed funds upper bound", value: 4.5, unit: "%" }],
      source: "fmp",
    },
    market_pricing: {
      hike_probability: 0.78,
      hold_probability: 0.22,
      summary: "Hawkish outcome partially priced",
    },
    actual: null,
    known_information: ["Source: fomc_calendar", "Previous: 4.25 %"],
    possible_surprises: ["A", "B", "C", "D"],
    source_ids: ["fomc_calendar"],
    fact_confidence: 99,
    sub_events: [
      { sub_event: "PRESS_CONFERENCE", time_utc: iso(77), time_bucharest: "21:30", time_known: true, phase: "SCHEDULED" },
    ],
    parent_event_id: null,
    status_note: null,
    data_incomplete: [],
    verification_status: null,
    surprise: { assessment: "UNDETERMINED", source: "none", evidence_ids: [], validated: false },
    scenarios: [
      { id: "A", label: "As expected", condition: "Hike, neutral guidance", expected: {}, note: "Headline mostly priced." },
      {
        id: "B",
        label: "Hawkish",
        condition: "Hike with a higher rate path",
        expected: { US2Y: 1, DXY: 1, EURUSD: -1, NASDAQ: -1, BTC: -1 },
      },
      {
        id: "C",
        label: "Dovish",
        condition: "Hike, dovish guidance",
        expected: { US2Y: -1, DXY: -1, NASDAQ: 1, BTC: 1 },
      },
      { id: "D", label: "Unexpected hold", expected: {}, note: "Reason matters: easing inflation or financial stress." },
    ],
    ...extra,
  };
}

/** The trade-watch `upcoming_event` brief the backend sends (event_risk._event_brief). */
const brief = (e: Block): Block => {
  const keys = [
    "event_id",
    "name",
    "event_class",
    "time_utc",
    "time_bucharest",
    "date_bucharest",
    "time_known",
    "minutes_until_event",
    "phase",
    "relevance_level",
    "relevance_score",
    "timing",
  ];
  return Object.fromEntries(keys.map((k) => [k, e[k] ?? null]));
};

const hormuz: Block = {
  event_id: "GEO-20260930-HORMUZ",
  row_id: "news-hormuz",
  name: "Iran / Hormuz escalation",
  event_class: "GEO_ENERGY",
  category: "GEOPOLITICAL",
  kind: "OTHER",
  is_decision: false,
  status: "UNRESOLVED",
  phase: "ACTIVE",
  timing: "ANYTIME",
  time_utc: null,
  time_known: false,
  time_basis: "unscheduled (timing ANYTIME)",
  first_seen_utc: iso(-90),
  relevance_score: 94,
  relevance_level: "CRITICAL",
  impact_10: 9.4,
  verification_status: "CONFIRMED",
  consensus: "NO CONSENSUS",
  market_pricing: "UNKNOWN",
  affected_assets: [
    { symbol: "BRENT", level: "very_high", weight: 1 },
    { symbol: "NASDAQ", level: "high", weight: 0.75 },
  ],
  consequences: { BRENT: 1, US10Y: 1, DXY: 1, NASDAQ: -1 },
  data_incomplete: [],
  scenarios: [],
};

const missile: Block = {
  event_id: "GEO-20260930-X1",
  name: "Reported missile strike on a tanker",
  event_class: "GEO_CONFLICT",
  category: "GEOPOLITICAL",
  status: "POTENTIAL",
  phase: "UNDER_VERIFICATION",
  verification_status: "UNCONFIRMED",
  timing: "ANYTIME",
  time_utc: null,
  first_seen_utc: iso(-4),
  time_known: false,
  consensus: "NO CONSENSUS",
  market_pricing: "UNKNOWN",
  status_note: "Possible market-moving event under verification; no effect on scores",
};

function eventRisk(fed: Block = fedEvent(), extra: Block = {}): Block {
  return {
    generated_at: iso(0),
    timezone: "Europe/Bucharest",
    markets_open: { crypto: true, us_equities: true, us_rates: true, fx: true, commodities: true },
    mode: {
      active: true,
      event_id: FED,
      name: "FOMC rate decision",
      time_bucharest: "21:00",
      time_utc: fed.time_utc,
      minutes_until: 47,
      impact_10: 10,
      affected_groups: ["USD", "Treasuries", "Nasdaq", "BTC", "Gold", "FX"],
      phase: fed.phase,
    },
    next_event: fed,
    dominant_event: fed,
    events: [fed],
    active_events: [hormuz],
    potential_events: [missile],
    asset_event_risk: {
      BTC: { score: 71.3, level: "HIGH", event_id: FED, minutes_until: 47 },
      NASDAQ: { score: 88, level: "VERY HIGH", event_id: FED, minutes_until: 47 },
    },
    favored: [
      {
        symbol: "NASDAQ",
        stance: "FAVORED",
        implied: "FAVORED",
        event_risk: { score: 88, level: "VERY HIGH", minutes_until: 47 },
        event_id: FED,
        reason_facts: ["Risk-on: NASDAQ ↑", "US10Y -6 bp"],
      },
      {
        symbol: "BTC",
        stance: "FAVORED",
        implied: "FAVORED",
        event_risk: { score: 71.3, level: "HIGH", minutes_until: 47 },
        event_id: FED,
        reason_facts: ["Risk-on: BTC ↑"],
      },
    ],
    pressured: [],
    non_confirming: [
      {
        symbol: "ETH",
        stance: "NOT CONFIRMING",
        implied: "FAVORED",
        event_risk: { score: 60, level: "HIGH", minutes_until: 47 },
        event_id: FED,
        reason_facts: ["Risk-on: ETH ↑", "current price change does not confirm (no move beyond noise in the implied direction)"],
      },
    ],
    what_can_break: [
      { text: "FED Hawkish (possible): US2Y↑ DXY↑ NASDAQ↓", kind: "event", source: FED },
      { text: "US10Y reversal higher", kind: "driver", source: "Rates" },
      { text: "BTC below 62,000", kind: "level", source: "BTC" },
    ],
    trade_watch: [
      {
        asset: "BTC",
        symbol: "BTC",
        trade_status: "WAIT FOR FED",
        decision_score: 54,
        regime_favorability: 78,
        evidence_confidence: 91,
        market_confirmation: 63,
        event_risk: { score: 71.3, level: "HIGH", event_id: FED },
        adjusted_confidence: 42,
        historical_probability: null,
        historical_probability_reason: "not calibrated",
        horizon: "4h",
        why: ["Treasury yields lower", "DXY weaker", "BTC holding support"],
        upcoming_event: brief(fed),
        confirmation: "BTC breaks calculated resistance AND Fed reaction leaves yields and DXY stable or lower",
        invalidation: "Hawkish Fed reaction OR BTC breaks calculated support",
      },
      {
        asset: "NASDAQ",
        symbol: "NASDAQ",
        trade_status: "CONDITIONAL LONG",
        decision_score: 44,
        regime_favorability: 81,
        evidence_confidence: 84,
        market_confirmation: 58,
        event_risk: { score: 88, level: "VERY HIGH", event_id: FED },
        historical_probability: { p: 0.61, n: 212, horizon: "1d" },
        horizon: "1d",
        why: ["Lower yields support growth equities"],
        upcoming_event: brief(fed),
        confirmation: "Holds above the opening range after the decision",
        invalidation: "Yields reverse higher",
      },
      {
        asset: "EURUSD",
        symbol: "EURUSD",
        trade_status: "WAIT FOR FED",
        decision_score: -30,
        event_risk: { score: 96, level: "CRITICAL", event_id: FED },
        upcoming_event: brief(fed),
      },
      { asset: "ETH", symbol: "ETH", trade_status: "WAIT", decision_score: 12, event_risk: { score: 60, level: "HIGH" } },
      {
        asset: "GOLD",
        symbol: "GOLD",
        trade_status: "SHORT",
        decision_score: -80,
        regime_favorability: 70,
        evidence_confidence: 94,
        market_confirmation: 81,
        event_risk: { score: 35, level: "LOW", event_id: null },
        why: ["US real yields rising"],
        upcoming_event: null,
      },
    ],
    post_event: [],
    data_incomplete: [],
    ...extra,
  };
}

function interpretation(extra: Block = {}): Block {
  return {
    source: "ai",
    model: "qwen3:8b",
    generated_at: iso(-2),
    stale: false,
    basis_hash: "fixture",
    age_seconds: 120,
    output: {
      ai_view:
        "Risk-on conditions favor Nasdaq and BTC, but the Fed decision is today's dominant risk. Long setups remain provisional.",
      market_summary:
        "Market conditions are constructive. Lower Treasury yields and a weaker dollar favor growth equities and crypto. The Fed decision is the dominant event; the headline is largely priced, so guidance may matter more.",
      dominant_event: { event_id: FED, reason: "Largest cross-asset exposure today.", risk_comment: "Can reverse the setup." },
      favored_assets: [{ symbol: "NASDAQ", comment: "Cleanest macro tailwind." }],
      pressured_assets: [],
      non_confirming_assets: [{ symbol: "ETH", comment: "Not strongly confirming." }],
      next_events: [
        {
          event_id: FED,
          why_it_matters: "The rate path drives the dollar and yields.",
          bull_case: "Neutral guidance keeps yields contained.",
          bear_case: "A higher dot plot lifts yields and the dollar.",
          neutral_case: "Headline priced; a limited first reaction.",
          what_is_priced: "Most of the hike.",
          what_is_not_priced: "A higher rate path.",
          confirmation_after_event: "Yields and DXY stable or lower.",
          surprise_assessment: "UNDETERMINED",
        },
      ],
      key_risks: ["Hawkish guidance", "Oil reversal higher"],
      trade_watch: [{ symbol: "BTC", comment: "Provisional until the Fed is resolved." }],
    },
    ...extra,
  };
}

function dashboard(
  risk: Block | null = eventRisk(),
  interp: Block | null = interpretation(),
  extra: Block = {},
): Block {
  return {
    as_of: NOW.toISOString(),
    timezone: "Europe/Bucharest",
    last_analysis: iso(-5),
    regimes: ["RISK_ON"],
    regime: {
      regimes: ["RISK_ON"],
      posture: "CONSTRUCTIVE",
      confidence: 82,
      coverage_pct: 90,
      basis: "live",
      drivers: [
        { driver: "Rates", direction: "down", arrow: "↓", input: "US2Y -6 bp" },
        { driver: "USD", direction: "down", arrow: "↓" },
      ],
    },
    assets: [
      asset("BTC", "crypto", 65000, { direction: "LONG", category: "LONG SETUP", score: 54 }),
      asset("ETH", "crypto", 3000, { direction: "BULLISH LEAN / WAIT", score: 12 }),
      asset("NASDAQ", "index", 18000, { direction: "BULLISH LEAN / WAIT", score: 44, audit_status: "NOT_REQUIRED" }),
      asset("EURUSD", "fx", 1.08, { direction: "SHORT", category: "SHORT SETUP", score: -30 }),
      asset("GOLD", "commodity", 2350, { direction: "SHORT", category: "SHORT SETUP", score: -80 }),
    ],
    events: [],
    calendar: [],
    providers: [],
    alerts: [],
    ...(risk ? { event_risk: risk } : {}),
    ...(interp ? { interpretation: interp } : {}),
    ...extra,
  };
}

async function serve(page: Page, body: () => unknown, routes: Record<string, unknown> = {}) {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path in routes) return route.fulfill({ json: routes[path] });
    return route.fulfill({ json: path === "/api/dashboard" ? body() : [] });
  });
}

async function open(page: Page, body: () => unknown, routes: Record<string, unknown> = {}) {
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  await serve(page, body, routes);
}

const region = (page: Page, name: string) => page.getByRole("region", { name, exact: true });

/** The inputs behind the driver arrows, the basis label and the confidence are under this disclosure of the hero. */
async function openBasis(page: Page) {
  await page.getByText("Data coverage and basis", { exact: true }).click();
}

/** Content verification opens the optional background sections explicitly. */
async function expandBackground(page: Page) {
  await expect(page.locator(".regime")).toBeVisible();
  for (const label of ["More context and scenarios", "Active events and measured reactions", "Verified news and alerts", "Provider health and market brief", "All tracked markets"]) {
    const group = page.locator("details.dashboard-details").filter({ has: page.getByText(label, { exact: true }) });
    if (await group.count() && await group.getAttribute("open") === null) await group.locator(":scope > summary").click();
  }
}

for (const withRisk of [true, false]) {
  test(`irrelevant or unverified headlines stay in the news stream with event risk ${withRisk ? "available" : "absent"}`, async ({ page }) => {
    const events = [
      { id: "sec-private-funds", summary_fact: "SEC delays private-fund reporting changes.", verification_status: "CONFIRMED", assets: [], importance: 0.95 },
      { id: "steel-plant", summary_fact: "A regional steel plant announces a capacity change.", verification_status: "CONFIRMED", assets: [], importance: 0.90 },
      { id: "tax-cut-rumour", summary_fact: "Unverified report of a proposed tax cut.", verification_status: "UNCONFIRMED", assets: ["DXY"], importance: 0.99 },
    ].map((event) => ({ ...event, first_seen_utc: iso(-5), last_updated_utc: iso(-5), sources: ["Test fixture"] }));
    await open(page, () => dashboard(withRisk ? eventRisk() : null, interpretation(), { events }));
    await page.goto("/");
    await expect(page.locator(".regime")).toBeVisible();
    await expect(page.getByRole("heading", { name: "What matters now", exact: true })).toHaveCount(0);
    // The timeline lists confirmed facts with asset impact only: none here, said in one line.
    const timeline = region(page, "Events");
    await expect(timeline.getByRole("heading", { name: "Coming up", exact: true })).toBeVisible();
    await expect(timeline).toContainText("No confirmed news with an identified asset impact yet.");
    await expect(timeline).not.toContainText("SEC delays");
    await expect(timeline).not.toContainText("tax cut");
    await expect(page.getByRole("heading", { name: "Breaking / verified", exact: true })).toBeHidden();
    await page.getByText("Verified news and alerts", { exact: true }).click();
    const stream = page.locator("section").filter({ has: page.getByRole("heading", { name: "Breaking / verified", exact: true }) });
    await expect(stream.getByRole("listitem")).toHaveCount(3);
    await expect(stream.getByText("SEC delays private-fund reporting changes.", { exact: true })).toBeVisible();
    await expect(stream.getByText("UNVERIFIED", { exact: true })).toBeVisible();
  });
}

for (const [layout, width, height] of [["desktop", 1440, 1000], ["phone", 390, 844]] as const) {
  test(`opening dashboard is concise and keeps essential guidance visible on ${layout}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    const risk = eventRisk();
    const original = risk.trade_watch as Block[];
    risk.trade_watch = [...original, ...["AAPL", "NVDA", "WTI"].map((symbol) => ({ ...original[0], asset: symbol, symbol }))];
    const interp = interpretation();
    const output = interp.output as Block;
    output.next_events = [null, "invalid cached entry", ...output.next_events as Block[]];
    await open(page, () => dashboard(risk, interp));
    await page.goto("/");
    await expect(page.locator(".regime")).toContainText("Market now");
    await expect(page.getByRole("note", { name: "Market view", exact: true })).toContainText("Fed decision is today's dominant risk");
    await expect(page.getByRole("list", { name: "Driver directions" }).getByRole("listitem")).toHaveText([
      "Rates↓down",
      "USD↓down",
    ]);
    const next = region(page, "Next key event");
    await expect(next).toBeVisible();
    await expect(next).toContainText("21:00 Bucharest");
    await expect(next).toContainText("10/10");
    await expect(next.getByText("The rate path drives the dollar and yields.", { exact: true })).toBeVisible();
    await expect(region(page, "Favored now")).toBeVisible();
    await expect(region(page, "Positioning").locator(".positioning-break")).toContainText("US10Y reversal higher");
    const watch = region(page, "Today's trade watch");
    await expect(watch.locator(".watch-card:visible")).toHaveCount(5);
    const first = watch.locator(".watch-card").first();
    await expect(first.locator(".watch-why")).toBeVisible();
    await expect(first.locator(".decision-details")).toBeVisible();
    await expect(first.locator(".watch-metrics")).toBeHidden();
    const context = page.locator("details.dashboard-details").filter({ has: page.locator("summary", { hasText: "More context and scenarios" }) });
    await expect(context).not.toHaveAttribute("open", "");
    await expect(page.locator(".ai-summary")).toBeHidden();
    await expect(page.getByRole("heading", { name: "What matters now", exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Provider health", exact: true })).toBeHidden();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/opening-${layout}-fixture.png`, fullPage: true });
    await context.locator(":scope > summary").click();
    await expect(page.locator(".ai-summary")).toBeVisible();
    await first.getByText("Scores and evidence", { exact: true }).click();
    await expect(first.locator(".watch-metrics")).toBeVisible();
    await watch.getByText("More trade-watch candidates (3)", { exact: true }).click();
    await expect(watch.locator(".watch-card:visible")).toHaveCount(8);
  });
}

for (const [layout, width, height] of [["desktop", 1440, 1000], ["phone", 390, 844]] as const) {
  for (const state of ["insufficient", "stale-historical"] as const) {
    test(`opening dashboard exposes ${state} uncertainty on ${layout}`, async ({ page }) => {
      await page.setViewportSize({ width, height });
      const risk = eventRisk();
      let data: Block;
      if (state === "insufficient") {
        data = dashboard(risk, null, { regimes: [], regime: {
          posture: "INSUFFICIENT_DATA", missing_required: ["US10Y", "DXY"], drivers: [],
        } });
      } else {
        risk.generated_at = iso(-5);
        risk.non_confirming = [{ symbol: "NASDAQ", stance: "NOT CONFIRMING", reason_facts: [
          "Risk-on: NASDAQ ↑", "NASDAQ +1.2% (last close)", "No aligned decision side",
          "Historical context only; current-session confirmation is unavailable",
        ] }];
        data = dashboard(risk, interpretation({ generated_at: iso(-23), stale: true }), {
          regime: { regimes: ["RISK_ON"], posture: "CONSTRUCTIVE", basis: "last_close", drivers: [], current_drivers: [] },
        });
      }
      await open(page, () => data);
      await page.goto("/");
      if (state === "insufficient") {
        await expect(page.getByRole("heading", { name: "Insufficient data", exact: true })).toBeVisible();
        await expect(page.getByRole("list", { name: "Missing regime inputs" })).toBeVisible();
        await expect(page.getByRole("list", { name: "Missing regime inputs" })).toContainText("US10Y");
      } else {
        // The hero's one warning line, the basis tag and the stale tag.
        await expect(page.locator(".now-warning")).toHaveText("Today's direction is not confirmed: no current observations yet.");
        await expect(page.locator(".now-eyebrow")).toContainText("from the last close");
        await expect(page.locator(".now-eyebrow")).toContainText("STALE");
        await expect(region(page, "Positioning").getByRole("status")).toContainText("may be outdated");
        // The asset's general reasons are written once, under its group.
        await expect(region(page, "Not confirming").locator(".positioning-note")).toHaveText(
          "No aligned decision side · Historical context only; current-session confirmation is unavailable",
        );
      }
      await expect(region(page, "Next key event")).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await page.screenshot({ path: `test-results/opening-${state}-${layout}-fixture.png`, fullPage: true });
    });
  }
}

test("historical regime distinguishes current evidence and exposes all confirmation reasons", async ({ page }) => {
  const risk = eventRisk();
  risk.favored = [];
  risk.non_confirming = [{ symbol: "NASDAQ", stance: "NOT CONFIRMING", reason_facts: [
    "Risk-on: NASDAQ ↑", "NASDAQ -1.20% (via QQQ)", "Current evidence opposes the historical view",
  ] }];
  const data = dashboard(risk, interpretation({source: "deterministic", model: null,
    label: "Deterministic summary — current snapshot"}));
  data.regime = { ...(data.regime as Block), basis: "last_close", basis_label: "last close Fri 25 Sep",
    divergences: [{feature: "NASDAQ", text: "NASDAQ -1.20% via QQQ opposes its historical move"}],
    current_drivers: [
      {driver: "Risk appetite", direction: "down", input: "NASDAQ -1.20% via QQQ", basis: "proxy"},
      // The backend marks the driver "live" even when every input was read from a proxy.
      {driver: "USD", direction: "down", basis: "live", inputs: [
        {feature: "DXY", field: "change_pct", value: -0.31, vote: -1, basis: "proxy", via: "UUP"},
      ]},
      {driver: "Rates", direction: "up", basis: "live", inputs: [
        {feature: "US2Y", field: "change_bp", value: 3, vote: 1, basis: "live", via: null},
      ]},
      {driver: "Oil", direction: "unavailable", inputs: [], input: null, missing: ["WTI"], basis: null},
    ],
  };
  await open(page, () => data);
  await page.goto("/");
  await expandBackground(page);
  const banner = page.locator(".regime");
  // ONE basis tag (the backend's full label is its tooltip) and ONE source tag.
  const tags = banner.locator(".now-eyebrow .badge");
  await expect(tags).toHaveText(["from the last close", "Deterministic"]);
  await expect(tags.nth(0)).toHaveAttribute("title", "last close Fri 25 Sep");
  await expect(tags.nth(1)).toHaveAttribute("title", "Deterministic summary — current snapshot");
  // Today's observations sit apart from the close-based drivers; a proxy is never called live.
  await expect(banner.getByRole("list", { name: "Current directions" }).getByRole("listitem")).toHaveText([
    "Risk appetite↓downvia proxies",
    "USD↓downvia proxies",
    "Rates↑uplive",
  ]);
  // ONE warning line: the conflict, as the backend words it.
  await expect(banner.getByRole("note", { name: "Current evidence conflicts" })).toHaveText(
    "Today's observations conflict with this view: NASDAQ -1.20% via QQQ opposes its historical move",
  );
  await expect(banner.locator(".now-warning")).toHaveCount(1);
  await openBasis(page);
  await expect(banner.getByText("Basis: last close Fri 25 Sep", { exact: true })).toBeVisible();
  await expect(banner).toContainText("Historical inputs");
  await expect(banner).toContainText("Current observations");
  await expect(banner.getByRole("list", { name: "Current market observations" }).getByRole("listitem")).toHaveText([
    "Risk appetite↓downNASDAQ -1.20% via QQQ· via proxies",
    "USD↓downDXY -0.31% via UUP· via proxies",
    "Rates↑upUS2Y +3 bp· live",
  ]);
  await expect(banner).not.toContainText("Why now");
  await expect(region(page, "Not confirming").locator(".positioning-note")).toHaveText(
    "Current evidence opposes the historical view",
  );
});

/** A live FOMC decision five minutes after the statement. */
const liveFed = (extra: Block = {}) =>
  fedEvent({
    phase: "IN_PROGRESS",
    status: "IN_PROGRESS",
    time_utc: iso(-5),
    time_bucharest: "20:08",
    minutes_until_event: -5,
    sub_events: [
      { sub_event: "PRESS_CONFERENCE", time_utc: iso(25), time_bucharest: "20:38", time_known: true, phase: "SCHEDULED" },
    ],
    ...extra,
  });

test("next key event card: Bucharest time, live countdown, expectation, exposure, phases", async ({
  page,
}) => {
  await open(page, () => dashboard());
  await page.goto("/");
  await expandBackground(page);
  const card = region(page, "Next key event");
  await expect(card.getByRole("heading", { name: "FOMC rate decision" })).toBeVisible();
  await expect(card).toContainText("21:00 Bucharest");
  await expect(card).toContainText("IMPACT 10/10");
  // The fixture's event has no calendar impact: relevance / 10, labelled so; relevance shown apart.
  await expect(card).toContainText("IMPACT 10/10 (relevance-based)");
  // The level is a chip; the scores are under the card's details.
  await expect(card.locator(".badge").filter({ hasText: /^CRITICAL$/ })).toBeVisible();
  await expect(card).toContainText("Relevance 96/100 · Dominance 95/100");
  await expect(card).toContainText("DOMINANT EVENT");
  await expect(card).toContainText("Approaching");
  await expect(card).toContainText("Press conference 21:30 Bucharest");
  // ONE sentence of why it matters; its source tag is in the hero, not repeated here.
  await expect(card.locator(".next-event-explanation")).toHaveText("The rate path drives the dollar and yields.");
  await expect(card).not.toContainText("qwen3");
  // ONE line of expectations with the reported values and the backend's own note.
  await expect(card.locator(".event-expect")).toHaveText(
    "Consensus: Fed funds upper bound 4.5% · Market pricing: Hike probability 78%, Hold probability 22% — Hawkish outcome partially priced",
  );
  // §8 consensus: the label is kept, the unit stays with its value, previous is never an expectation.
  await expect(card).toContainText("Market expectationConsensusFed funds upper bound4.5%Source: fmp");
  await expect(card.locator(".event-expect")).not.toContainText("Previous");
  await expect(card.locator(".event-expectation").filter({ hasText: "Previous" })).toHaveCount(0);
  // Provenance (the source of the schedule, the time basis) is under the details, not in the card's text.
  await expect(card.getByText("Source: fomc_calendar", { exact: true })).toBeHidden();
  await card.getByText("Event timing and relevance details", { exact: true }).click();
  await expect(card.getByText("Source: fomc_calendar", { exact: true })).toBeVisible();
  await expect(card.getByText("Time basis: Fed calendar", { exact: true })).toBeVisible();
  await expect(card).toContainText("Hike probability78%");
  await expect(card).toContainText("Hold probability22%");
  await expect(card).toContainText("Hawkish outcome partially priced");
  await expect(card.getByRole("list", { name: "Most exposed assets" }).getByRole("listitem").first()).toHaveText(
    "US2Yvery high",
  );
  await expect(card).not.toContainText("EVENT DATA INCOMPLETE");
  // The dominant event is the next event: no separate dominant line.
  await expect(card.getByRole("note", { name: "Dominant event" })).toHaveCount(0);

  // The countdown is client arithmetic from time_utc and ticks every second.
  const timer = card.getByRole("timer");
  await expect(timer).toHaveText("00:47:00");
  await expect(card).toContainText("in 47 min");
  await page.clock.runFor(1000);
  await expect(timer).toHaveText("00:46:59");

  // Past T0 while the backend still says APPROACHING: due, never "Released".
  await page.clock.fastForward("47:00");
  await expect(card).toContainText("Due now · awaiting release");
  await expect(card).not.toContainText("Released");
});

test("regime shows the AI VIEW with its source and a WHY NOW label", async ({ page }) => {
  await open(page, () => dashboard());
  await page.goto("/");
  await expandBackground(page);
  await expect(page.getByRole("heading", { name: "CONSTRUCTIVE · RISK ON" })).toBeVisible();
  const view = page.getByRole("note", { name: "Market view", exact: true });
  // The view is the stored text, at most two sentences.
  await expect(view).toHaveText(
    "Risk-on conditions favor Nasdaq and BTC, but the Fed decision is today's dominant risk. Long setups remain provisional.",
  );
  // ONE basis tag and ONE source tag; the model is the source tag's tooltip.
  const tags = page.locator(".now-eyebrow .badge");
  await expect(tags).toHaveText(["live", "AI"]);
  await expect(tags.nth(1)).toHaveAttribute("title", "AI · qwen3:8b");
  await expect(page.locator(".now-eyebrow")).not.toContainText("STALE");
  await expect(page.locator(".now-warning")).toHaveCount(0);
  await openBasis(page);
  await expect(page.locator(".regime")).toContainText("Why now");
  await expect(page.getByRole("list", { name: "Regime drivers" }).getByRole("listitem")).toHaveText([
    "Rates↓downUS2Y -6 bp",
    "USD↓down",
  ]);
});

test("a stale interpretation says how old it is, never that its inputs changed", async ({ page }) => {
  await open(page, () => dashboard(eventRisk(), interpretation({ generated_at: iso(-23), stale: true, age_seconds: 1380 })));
  await page.goto("/");
  await expandBackground(page);
  await expect(page.locator(".now-eyebrow")).toContainText("STALE · older than 23 min");
  const ai = region(page, "AI market interpretation");
  await expect(ai).toContainText("STALE · older than 23 min");
  await expect(page.locator("body")).not.toContainText("inputs changed");
});

test("EVENT RISK MODE banner shows event, time, countdown, impact and affected groups", async ({ page }) => {
  await open(page, () => dashboard());
  await page.goto("/");
  await expandBackground(page);
  const banner = region(page, "Event risk mode");
  await expect(banner).toContainText("EVENT RISK MODE");
  await expect(banner).toContainText("FOMC rate decision");
  await expect(banner).toContainText("21:00 Bucharest");
  await expect(banner.getByRole("timer")).toHaveText("00:47:00");
  await expect(banner).toContainText("Impact10/10 (relevance-based)");
  await expect(banner).toContainText("Relevance96/100 · CRITICAL");
  await expect(banner).toContainText("USD · Treasuries · Nasdaq · BTC · Gold · FX");
  await expect(banner).toContainText("exposed assets may need to wait for this event");
  await page.clock.runFor(1000);
  await expect(banner.getByRole("timer")).toHaveText("00:46:59");
});

test("the EVENT RISK MODE banner keeps impact and relevance from the mode when the block does not list its event", async ({
  page,
}) => {
  await open(page, () =>
    dashboard(
      eventRisk(fedEvent(), {
        mode: {
          active: true,
          event_id: "ECB-20260930-DECISION",
          name: "ECB rate decision",
          time_utc: iso(40),
          minutes_until: 40,
          impact: 8,
          impact_10: 8.5,
          relevance_score: 84.6,
          relevance_level: "VERY HIGH",
          affected_groups: ["EUR", "FX"],
          phase: "APPROACHING",
        },
      }),
    ),
  );
  await page.goto("/");
  await expandBackground(page);
  const banner = region(page, "Event risk mode");
  await expect(banner).toContainText("ECB rate decision");
  await expect(banner).toContainText("Impact8/10");
  await expect(banner).not.toContainText("relevance-based");
  await expect(banner).toContainText("Relevance85/100 · VERY HIGH");
});

test("no banner when risk mode is inactive; missing expectations are said once, with EVENT DATA INCOMPLETE", async ({
  page,
}) => {
  const cpi = fedEvent({
    event_id: "BLS-20260930-US_CPI",
    name: "US CPI",
    event_class: "US_CPI",
    category: "MACRO",
    kind: "RELEASE",
    is_decision: false,
    relevance_score: 78,
    relevance_level: "VERY HIGH",
    impact: 9,
    impact_10: 7.8,
    consensus: "NO CONSENSUS",
    market_pricing: "UNKNOWN",
    data_incomplete: ["NO CONSENSUS", "MARKET PRICING UNKNOWN"],
    sub_events: [],
    scenarios: [],
  });
  await open(page, () =>
    dashboard(
      eventRisk(cpi, {
        mode: { active: false },
        dominant_event: null,
        events: [cpi],
        // The backend flags it: VERY HIGH is one of its incomplete_flag_levels.
        data_incomplete: [
          {
            event_id: cpi.event_id,
            name: "US CPI",
            level: "VERY HIGH",
            flag: "EVENT DATA INCOMPLETE",
            reasons: ["NO CONSENSUS", "MARKET PRICING UNKNOWN"],
          },
        ],
      }),
      null,
    ),
  );
  await page.goto("/");
  await expandBackground(page);
  const card = region(page, "Next key event");
  await expect(card).toContainText("US CPI");
  // The calendar row's impact (9), the same number the calendar shows; relevance apart.
  await expect(card).toContainText("IMPACT 9/10");
  await expect(card).not.toContainText("7.8/10");
  await expect(card).not.toContainText("relevance-based");
  await expect(card.locator(".badge").filter({ hasText: /^VERY HIGH$/ })).toBeVisible();
  await expect(card).toContainText("Relevance 78/100");
  // Missing values stay missing, in plain words, on ONE line; the flag leads it and nothing is said twice.
  await expect(card.locator(".event-expect")).toHaveText(
    "EVENT DATA INCOMPLETEConsensus: not available · Market pricing: unknown",
  );
  await expect(card.locator(".event-expect")).toBeVisible();
  await expect(card).not.toContainText("NO CONSENSUS");
  await expect(card).not.toContainText("UNKNOWN");
  await expect(card.locator(".event-expectation")).toHaveCount(0);
  await expect(region(page, "Event risk mode")).toHaveCount(0);
  // No stored interpretation yet: said, not invented.
  await expect(region(page, "AI market interpretation")).toContainText("No interpretation has been stored yet.");
  await expect(page.getByRole("note", { name: "Market view", exact: true })).toHaveCount(0);
});

test("a HIGH earnings row with a Finnhub estimate: label kept, no EVENT DATA INCOMPLETE below the flag levels", async ({
  page,
}) => {
  const aapl = fedEvent({
    event_id: "EARN-20260930-AAPL-EARNINGS",
    name: "AAPL earnings",
    event_class: "EARNINGS",
    category: "EARNINGS",
    kind: "EARNINGS",
    is_decision: false,
    symbol: "AAPL",
    status: "APPROACHING",
    phase: "APPROACHING",
    time_utc: "2026-09-30T20:00:00+00:00",
    time_bucharest: null,
    date_bucharest: "2026-09-30",
    time_known: false,
    time_basis: "after close",
    minutes_until_event: null,
    relevance_score: 66,
    relevance_level: "HIGH",
    impact_10: 6.6,
    dominance_score: 60,
    consensus: {
      label: "Finnhub estimate",
      values: [
        { name: "EPS", value: 1.45, unit: "USD" },
        { name: "Revenue", value: 9.4e10, unit: "USD" },
      ],
      source: "finnhub",
    },
    market_pricing: "UNKNOWN",
    affected_assets: [
      { symbol: "AAPL", level: "very_high", weight: 1 },
      { symbol: "NASDAQ", level: "high", weight: 0.75 },
    ],
    sub_events: [],
    scenarios: [],
    data_incomplete: ["EXACT TIME UNKNOWN"],
  });
  const block = eventRisk(aapl, {
    mode: { active: false },
    dominant_event: null,
    events: [aapl],
    data_incomplete: [],
    trade_watch: [
      {
        asset: "NASDAQ",
        symbol: "NASDAQ",
        trade_status: "CONDITIONAL LONG",
        decision_score: 44,
        event_risk: { score: 49.5, level: "MODERATE", event_id: aapl.event_id },
        upcoming_event: brief(aapl),
      },
    ],
  });
  await open(page, () => dashboard(block, interpretation({ output: { ai_view: "Earnings tonight." } })));
  await page.goto("/");
  await expandBackground(page);
  const card = region(page, "Next key event");
  await expect(card).toContainText("Wed 30 Sep · time TBA");
  await expect(card.locator(".event-expect")).toHaveText(
    "Finnhub estimate: EPS 1.45 USD, Revenue 94B USD · Market pricing: unknown",
  );
  await expect(card).toContainText("Market expectationFinnhub estimateEPS1.45 USDRevenue94B USDSource: finnhub");
  await expect(card).not.toContainText("EVENT DATA INCOMPLETE");
  await expect(card).toContainText("Data notes: EXACT TIME UNKNOWN");
  await expect(card.getByRole("timer")).toHaveCount(0);
  // Date-only: the time TBA is said once, with no countdown after it.
  const watch = region(page, "Today's trade watch");
  await expect(watch).toContainText("AAPL earnings · Wed 30 Sep · time TBA");
  await expect(watch).not.toContainText("Time TBA");
});

test("trade watch: event-aware statuses, metrics, upcoming event, local stale downgrade", async ({ page }) => {
  await open(page, () => dashboard());
  await page.goto("/");
  await expandBackground(page);
  const watch = region(page, "Today's trade watch");
  const cards = watch.locator(".watch-card");
  await expect(cards.locator(".watch-status")).toHaveText([
    "WAIT FOR FED",
    "CONDITIONAL LONG",
    "WAIT FOR FED",
    "WAIT",
    "SHORT",
  ]);
  await expect(cards.nth(1)).toHaveAttribute("data-status", "conditional-long");
  const btc = cards.nth(0);
  await expect(btc).toContainText("Decision score+54");
  await expect(btc).toContainText("Regime favorability78/100");
  await expect(btc).toContainText("Evidence confidence91%");
  await expect(btc).toContainText("Market confirmation63%");
  await expect(btc).toContainText("Event risk71/100HIGH");
  // A conviction on the decision-score scale, never a percent.
  await expect(btc).toContainText("Adjusted conviction42/100not a probability");
  await expect(btc).not.toContainText("42%");
  await expect(btc).toContainText("Horizon4 hours");
  // Not calibrated: no historical probability at all.
  await expect(btc).not.toContainText("Historical probability");
  await expect(btc).toContainText("Published signal LONG SETUP");
  await expect(btc).toContainText("FOMC rate decision · 21:00 Bucharest · in 47 min");
  await expect(btc).toContainText(
    "BTC breaks calculated resistance AND Fed reaction leaves yields and DXY stable or lower",
  );
  await expect(btc).toContainText("Hawkish Fed reaction OR BTC breaks calculated support");
  // A model wrote this comment: labelled as an AI note.
  await expect(btc.locator(".watch-comment")).toHaveText("AI note Provisional until the Fed is resolved.");
  // Worded like the asset page (components/asset/signal.ts probabilityLabel).
  await expect(cards.nth(1)).toContainText("Historical probability61% · n=212 · horizon 1 day");
  // The trade watch replaces the plain setups list; nothing published is dropped.
  await expect(page.getByRole("heading", { name: "Today's best setups" })).toHaveCount(0);

  // Quotes expire locally: sides are held back, waits stay waits.
  await page.clock.fastForward(16000);
  await expect(cards.locator(".watch-status")).toHaveText(["WAIT FOR FED", "WAIT", "WAIT FOR FED", "WAIT", "WAIT"]);
  await expect(cards.nth(1)).toContainText("Held back from CONDITIONAL LONG");
  await expect(cards.nth(4)).toContainText("Held back from SHORT");
});

test("a cached conditional direction is held at WAIT after the underlying decision is blocked", async ({ page }) => {
  await open(page, () => dashboard(eventRisk(), interpretation(), {
    assets: [asset("NASDAQ", "index", 18000, {
      direction: "WAIT", category: "WAIT / DATA INSUFFICIENT", score: 44, audit_status: "BLOCK",
    })],
  }));
  await page.goto("/");
  await expandBackground(page);
  const card = region(page, "Today's trade watch").locator(".watch-card").nth(1);
  await expect(card.locator(".watch-status")).toHaveText("WAIT");
  await expect(card).toContainText("Held back from CONDITIONAL LONG");
});

test("an IN_PROGRESS event reads 'In progress' in the trade watch and on the asset page, never 'awaiting release'", async ({
  page,
}) => {
  const live = liveFed();
  const block = eventRisk(live, {
    trade_watch: [
      {
        asset: "BTC",
        symbol: "BTC",
        trade_status: "WAIT FOR FED",
        status_reason: "FED in progress",
        decision_score: 54,
        event_risk: { score: 71.3, level: "HIGH", event_id: FED },
        upcoming_event: brief(live),
      },
    ],
  });
  const btc = {
    ...asset("BTC", "crypto", 65000, { direction: "LONG", category: "LONG SETUP", score: 54 }),
    event_risk: { score: 71.3, level: "HIGH", event_id: FED, minutes_until: -5, trade_status: "WAIT FOR FED" },
  };
  await open(page, () => dashboard(block), { "/api/assets/BTC": btc });
  await page.goto("/");
  await expandBackground(page);
  const card = region(page, "Today's trade watch").locator(".watch-card").first();
  await expect(card).toContainText("FOMC rate decision · 20:08 Bucharest · In progress");
  await expect(card).not.toContainText("Due now");
  await expect(card).not.toContainText("awaiting release");
  await expect(region(page, "Post-event reaction")).toHaveCount(0);
  // The next-event card shows the backend phase and the decision headline (FOMC is a decision).
  const next = region(page, "Next key event");
  await expect(next).toContainText("In progress");
  await expect(next).toContainText("Decision published — press conference in 25 min");

  await page.goto("/assets/BTC");
  const panel = page.locator(".panel").filter({ has: page.getByRole("heading", { name: "Event risk", exact: true }) });
  await expect(panel).toContainText("FOMC rate decision · 20:08 Bucharest · In progress");
  await expect(panel).not.toContainText("awaiting release");
});

test("central-bank events that are not decisions never say 'Decision published'", async ({ page }) => {
  const testimony = fedEvent({
    event_id: "FED-20260930-FED_TESTIMONY",
    name: "Testimony -- Chair Powell",
    event_class: "FED_TESTIMONY",
    kind: "TESTIMONY",
    is_decision: false,
    speaker_role: "CHAIR",
    phase: "IN_PROGRESS",
    status: "IN_PROGRESS",
    time_utc: iso(-30),
    minutes_until_event: -30,
    consensus: "NO CONSENSUS",
    market_pricing: "UNKNOWN",
    sub_events: [],
    scenarios: [],
  });
  const block = eventRisk(testimony, {
    mode: { active: false },
    post_event: [
      {
        event_id: testimony.event_id,
        name: testimony.name,
        event_class: "FED_TESTIMONY",
        phase: "IN_PROGRESS",
        reactions: { US2Y: { "1m": { change: 1, unit: "bp", status: "NOISE" } } },
        confirmation: null,
        divergence: false,
        summary_facts: ["Market confirmation not graded: surprise direction unknown"],
      },
    ],
  });
  await open(page, () => dashboard(block));
  await page.goto("/");
  await expandBackground(page);
  await expect(region(page, "Next key event")).toContainText("Testimony under way");
  await expect(region(page, "Post-event reaction")).toContainText("Testimony under way");
  await expect(page.locator("body")).not.toContainText("Decision published");
});

test("positioning, what can change this view and the AI interpretation with collapsible scenarios", async ({
  page,
}) => {
  await open(page, () => dashboard());
  await page.goto("/");
  await expandBackground(page);
  const favored = region(page, "Favored now");
  // Asset chips: the symbol links to its page; the event-risk level is a dot, read out for screen readers.
  await expect(favored.getByRole("listitem")).toHaveText(["NASDAQEvent risk VERY HIGH", "BTCEvent risk HIGH"]);
  await expect(favored.getByRole("link", { name: "NASDAQ", exact: true })).toHaveAttribute("href", "/assets/NASDAQ");
  await expect(favored.locator(".risk-dot")).toHaveCount(2);
  // The event behind the risk and the asset's own reasons are the chip's tooltip, never boilerplate under each asset.
  await expect(favored.getByRole("listitem").first()).toHaveAttribute(
    "title",
    "Event risk VERY HIGH\nFOMC rate decision · 21:00 Bucharest\nRisk-on: NASDAQ ↑\nUS10Y -6 bp",
  );
  // Per-asset comments are no longer part of the interpretation's output: nothing is rendered for them.
  await expect(favored).not.toContainText("Cleanest macro tailwind.");
  await expect(region(page, "Positioning")).not.toContainText("Not strongly confirming.");
  // An empty group says "none" in one word.
  await expect(region(page, "Under pressure").locator(".positioning-none")).toHaveText("none");
  // What the group shares is written once, under the group.
  await expect(region(page, "Not confirming").getByRole("listitem")).toHaveText(["ETHEvent risk HIGH"]);
  await expect(region(page, "Not confirming").locator(".positioning-note")).toHaveText(
    "Current price change does not confirm (no move beyond noise in the implied direction)",
  );
  await expect(region(page, "Positioning").locator(".positioning-break")).toHaveText(
    "What can change this view: FED Hawkish (possible): US2Y↑ DXY↑ NASDAQ↓ · US10Y reversal higher · BTC below 62,000",
  );

  const ai = region(page, "AI market interpretation");
  await expect(ai).toContainText("AI · qwen3:8b");
  await expect(ai).toContainText("Market conditions are constructive.");
  await expect(ai).toContainText("FOMC rate decision — Largest cross-asset exposure today.");
  await expect(ai).toContainText("Hawkish guidance");
  const scenarios = ai.locator("details.scenario-set");
  await expect(scenarios).toHaveCount(1);
  await expect(scenarios.getByText("A higher dot plot lifts yields and the dollar.")).toBeHidden();
  await scenarios.locator("summary").click();
  await expect(scenarios.getByText("A higher dot plot lifts yields and the dollar.")).toBeVisible();
  await expect(scenarios).toContainText("UNDETERMINED");
  await expect(scenarios).toContainText("Scenario B · Hawkish");
  await expect(scenarios).toContainText("US2Y↑");
  await expect(scenarios).toContainText("Scenarios are possibilities, not certainties.");
});

test("AI scenario explanations for an active unscheduled event are shown", async ({ page }) => {
  const interp = interpretation();
  const output = interp.output as Block;
  output.next_events = [
    ...(output.next_events as Block[]),
    {
      event_id: hormuz.event_id,
      why_it_matters: "Oil supply risk through the strait.",
      bull_case: "De-escalation lets crude fade.",
      bear_case: "A closure lifts crude and yields.",
      surprise_assessment: "NOT_APPLICABLE",
    },
  ];
  await open(page, () => dashboard(eventRisk(), interp));
  await page.goto("/");
  await expandBackground(page);
  const scenarios = region(page, "AI market interpretation").locator("details.scenario-set");
  await expect(scenarios).toHaveCount(2);
  const geo = scenarios.filter({ hasText: "Iran / Hormuz escalation" });
  await expect(geo.locator("summary")).toContainText("Iran / Hormuz escalation · Timing ANYTIME");
  await geo.locator("summary").click();
  await expect(geo).toContainText("A closure lifts crude and yields.");
});

test("the short interpretation output renders no empty headings, lists or placeholders", async ({ page }) => {
  // What the interpreter writes now: two sentences of view, a summary, a few risks, one sentence per event.
  // The per-asset comment lists arrive empty and the per-event cases are gone.
  const short = interpretation({
    output: {
      ai_view: "Risk-on conditions favor Nasdaq and BTC. The Fed decision is the one thing to watch.",
      market_summary: "Conditions are constructive. The Fed decision is the dominant event.",
      key_risks: [],
      dominant_event: null,
      next_events: [{ event_id: FED, why_it_matters: "The rate path drives the dollar and yields.", surprise_assessment: "UNDETERMINED", evidence_ids: [] }],
      favored_assets: [],
      pressured_assets: [],
      non_confirming_assets: [],
      trade_watch: [],
    },
  });
  // A labour-style scenario pair: equities and crypto have competing paths, so they carry no direction (EVR-1).
  const fed = fedEvent({
    scenarios: [
      { id: "A", label: "Stronger than consensus", expected: { DXY: 1, US2Y: 1 }, unresolved: ["SP500", "NASDAQ", "BTC"] },
      { id: "B", label: "Weaker than consensus", expected: { DXY: -1, US2Y: -1 }, unresolved: ["SP500", "NASDAQ", "BTC"] },
    ],
  });
  await open(page, () => dashboard(eventRisk(fed), short));
  await page.goto("/");
  await expect(page.getByRole("note", { name: "Market view", exact: true })).toContainText("Risk-on conditions favor Nasdaq and BTC.");
  await expandBackground(page);
  const ai = region(page, "AI market interpretation");
  await expect(ai.locator(".ai-summary")).toHaveText("Conditions are constructive. The Fed decision is the dominant event.");
  // No heading without content, and no placeholder for a list the model no longer writes.
  await expect(ai.getByRole("heading", { level: 3 })).toHaveText(["Event scenarios"]);
  await expect(ai).not.toContainText("Key risks");
  await expect(ai).not.toContainText("Dominant event");
  const scenarios = ai.locator("details.scenario-set");
  await expect(scenarios).toHaveCount(1);
  await scenarios.locator("summary").click();
  await expect(scenarios.locator(".scenario-cases dt")).toHaveText(["Why it matters"]);
  for (const gone of ["Bull case", "Bear case", "Neutral case", "What is priced", "What is not priced", "Confirmation after the event"])
    await expect(ai).not.toContainText(gone);
  // Each scenario gives a direction only where the causal paths agree; the rest are named as unresolved.
  const items = scenarios.getByRole("listitem");
  await expect(items.nth(0)).toContainText("DXY↑ upUS2Y↑ up");
  await expect(items.nth(0)).toContainText("No clear direction (competing effects): SP500, NASDAQ, BTC");
  await expect(items.nth(1)).toContainText("DXY↓ downUS2Y↓ down");
  await expect(items.nth(0).locator(".scenario-arrows")).not.toContainText("SP500");
  // Nothing empty anywhere in the open sections.
  const hollow = await page.evaluate(() =>
    [...document.querySelectorAll("main h2, main h3, main h4, main dl, main ul, main ol, main dt")]
      .filter((el) => !(el.textContent ?? "").trim())
      .map((el) => el.tagName + "." + el.className),
  );
  expect(hollow).toEqual([]);
  // The trade watch and positioning carry no per-asset comment.
  await expect(page.locator(".watch-comment")).toHaveCount(0);
});

/** A HIGH release before the CRITICAL FOMC: the next event, but not the dominant one. */
const claimsEvent = () =>
  fedEvent({
    event_id: "DOL-20260930-US_CLAIMS",
    name: "Initial jobless claims",
    event_class: "US_CLAIMS",
    category: "MACRO",
    kind: "RELEASE",
    is_decision: false,
    time_utc: iso(17),
    time_bucharest: "20:30",
    minutes_until_event: 17,
    relevance_score: 66,
    relevance_level: "HIGH",
    impact_10: 6.6,
    dominance_score: 55,
    consensus: { label: "Consensus", values: [{ name: "Initial claims", value: 225, unit: "K" }], source: "fmp" },
    sub_events: [],
    scenarios: [],
  });

test("the dominant event is surfaced at the top when it is not the next event", async ({ page }) => {
  const claims = claimsEvent();
  const fed = fedEvent();
  await open(page, () =>
    dashboard(eventRisk(claims, { dominant_event: fed, events: [claims, fed], mode: { active: false } })),
  );
  await page.goto("/");
  await expandBackground(page);
  const card = region(page, "Next key event");
  await expect(card.getByRole("heading", { name: "Initial jobless claims" })).toBeVisible();
  await expect(card).toContainText("Initial claims225 K");
  const dominant = card.getByRole("note", { name: "Dominant event" });
  await expect(dominant).toContainText("DOMINANT EVENT");
  await expect(dominant).toContainText("FOMC rate decision");
  await expect(dominant).toContainText("21:00 Bucharest · in 47 min");
  await expect(dominant).toContainText("IMPACT 10/10");
  await expect(dominant).toContainText("CRITICAL");
});

test("deterministic interpretation is labelled as such and never repeats what the dashboard shows", async ({
  page,
}) => {
  const base = interpretation({ source: "deterministic", model: null });
  const output = base.output as Block;
  // The fallback builds key risks from What can change this view; only what that list lacks is shown again.
  output.key_risks = [
    "US10Y reversal higher",
    "BTC below 62,000",
    "EVENT DATA INCOMPLETE: US CPI (NO CONSENSUS)",
  ];
  const fed = fedEvent({
    scenarios: [
      { id: "A", label: "Below consensus", condition: "Below consensus", expected: { US2Y: -1 } },
      { id: "B", label: "Hawkish", condition: "Hike with a higher rate path", expected: { US2Y: 1 } },
    ],
  });
  await open(page, () => dashboard(eventRisk(fed), base));
  await page.goto("/");
  await expandBackground(page);
  const source = page.locator('.now-eyebrow [data-source="deterministic"]');
  await expect(source).toHaveText("Deterministic");
  await expect(source).toHaveAttribute("title", "Deterministic summary — AI unavailable");
  // The source is tagged once on the opening screen.
  await expect(page.locator(".regime")).not.toContainText("AI unavailable");
  await expect(region(page, "Next key event")).not.toContainText("Deterministic");
  const ai = region(page, "AI market interpretation");
  await expect(ai).toContainText("Deterministic summary — AI unavailable");
  await expect(ai).not.toContainText("qwen3");
  await expect(ai.locator(".break-list li")).toHaveText(["EVENT DATA INCOMPLETE: US CPI (NO CONSENSUS)"]);
  // A scenario whose condition only repeats its label shows it once.
  const scenarios = ai.locator("details.scenario-set").first();
  await scenarios.locator("summary").click();
  const first = scenarios.getByRole("listitem").first();
  await expect(first).toContainText("Scenario A · Below consensus");
  await expect(first.getByText("Below consensus", { exact: true })).toHaveCount(0);
  await expect(scenarios.getByRole("listitem").nth(1)).toContainText("Hike with a higher rate path");
  // The fallback's per-asset notes would only repeat each card's status, event, confirmation and invalidation.
  const watch = region(page, "Today's trade watch");
  await expect(watch.locator(".watch-comment")).toHaveCount(0);
  await expect(watch).not.toContainText("Deterministic note");
  await expect(watch).not.toContainText("AI note");
  // Positioning never shows per-asset comments.
  await expect(region(page, "Favored now")).toContainText("NASDAQ");
  await expect(region(page, "Favored now")).not.toContainText("Cleanest macro tailwind.");
});

test("post-event panel: reactions T+1m..T+1h in their units, confirmation and MARKET DIVERGENCE", async ({ page }) => {
  const live = liveFed({ time_utc: iso(-3), sub_events: [{ sub_event: "PRESS_CONFERENCE", time_utc: iso(27), phase: "SCHEDULED" }] });
  await open(page, () =>
    dashboard(
      eventRisk(live, {
        post_event: [
          {
            event_id: FED,
            name: "FOMC rate decision",
            event_class: "FOMC",
            phase: "IN_PROGRESS",
            // §8 cells: {change, unit, status}; yields always in bp.
            reactions: {
              US2Y: {
                "1m": { change: 4, unit: "bp", status: "AGAINST" },
                "5m": { change: 6, unit: "bp", status: "AGAINST" },
              },
              DXY: { "1m": { change: 0.2, unit: "pct", status: "ALIGNED" }, "5m": { change: 0.35, unit: "pct" } },
              NASDAQ: { "1m": { change: 0.4, unit: "pct" }, "5m": { change: 0.8, unit: "pct" } },
            },
            confirmation: 22,
            divergence: true,
            summary_facts: ["FED: surprise direction unknown"],
          },
        ],
      }),
    ),
  );
  await page.goto("/");
  await expect(region(page, "Post-event reaction").getByRole("alert")).toBeVisible();
  await expect(region(page, "Post-event reaction")).toHaveCount(1);
  await expandBackground(page);
  const post = region(page, "Post-event reaction");
  await expect(post).toContainText("IN PROGRESS");
  await expect(post).toContainText("Decision published — press conference in 27 min");
  await expect(post.getByRole("alert")).toContainText("MARKET DIVERGENCE");
  await expect(post.getByRole("alert")).toContainText("Do not act on the expected direction automatically");
  await expect(post).toContainText("Market confirmation 22/100");
  const table = post.getByRole("table", { name: "Reaction after the event" });
  await expect(table.getByRole("columnheader")).toHaveText(["Asset", "T+1m", "T+5m", "T+15m", "T+30m", "T+1h"]);
  await expect(table.getByRole("row").nth(1)).toHaveText("US2Y+4 bp+6 bpPendingPendingPending");
  await expect(table.getByRole("row").nth(2)).toContainText("+0.35%");
  await expect(post).toContainText("FED: surprise direction unknown");
  // While in progress the banner shows the phase, not a countdown.
  const banner = region(page, "Event risk mode");
  await expect(banner).toContainText("In progress");
  await expect(banner.getByRole("timer")).toHaveCount(0);
});

test("post-event bare numbers use the entry's units map: US10Y +11 bp is never +11.00%", async ({ page }) => {
  const live = liveFed();
  await open(page, () =>
    dashboard(
      eventRisk(live, {
        post_event: [
          {
            event_id: FED,
            name: "FOMC rate decision",
            phase: "IN_PROGRESS",
            // The backend shape before §8: bare floats plus units / statuses maps.
            reactions: { US10Y: { "1m": 4, "5m": 11 }, NASDAQ: { "1m": 0.4, "5m": -0.8 } },
            statuses: { US10Y: { "1m": "AGAINST", "5m": "AGAINST" }, NASDAQ: { "1m": "ALIGNED", "5m": "AGAINST" } },
            units: { US10Y: "bp", NASDAQ: "pct" },
            confirmation: null,
            divergence: false,
          },
        ],
      }),
    ),
  );
  await page.goto("/");
  await expandBackground(page);
  const table = region(page, "Post-event reaction").getByRole("table", { name: "Reaction after the event" });
  await expect(table.getByRole("row").nth(1)).toHaveText("US10Y+4 bp+11 bpPendingPendingPending");
  await expect(table.getByRole("row").nth(2)).toHaveText("NASDAQ+0.40%-0.80%PendingPendingPending");
  await expect(table).not.toContainText("+11.00%");
});

test("active event risk (ANYTIME) and possible events under verification", async ({ page }) => {
  await open(page, () => dashboard());
  await page.goto("/");
  await expandBackground(page);
  const active = page.locator("section").filter({
    has: page.getByRole("heading", { name: "Active event risk", exact: true }),
  });
  await expect(active).toContainText("Iran / Hormuz escalation");
  await expect(active).toContainText("Timing ANYTIME");
  await expect(active).toContainText("Status UNRESOLVED");
  await expect(active).toContainText("Impact 9.4/10 (relevance-based)");
  await expect(active).toContainText("BRENT↑");
  await expect(active).toContainText("NASDAQ↓");
  const possible = page.locator("section").filter({
    has: page.getByRole("heading", { name: "Possible event — under verification", exact: true }),
  });
  await expect(possible).toContainText("POTENTIAL EVENT · UNCONFIRMED");
  await expect(possible).toContainText("Reported missile strike on a tanker");
  await expect(possible).toContainText("does not change scores or trade statuses");
});

test("an unscheduled driving event reads 'Timing ANYTIME · UNRESOLVED' once, never 'Time TBA · Time TBA'", async ({
  page,
}) => {
  const block = eventRisk(fedEvent(), {
    trade_watch: [
      {
        asset: "NASDAQ",
        symbol: "NASDAQ",
        trade_status: "CONDITIONAL LONG",
        decision_score: 44,
        event_risk: { score: 70.5, level: "HIGH", event_id: hormuz.event_id },
        upcoming_event: brief(hormuz),
      },
    ],
    favored: [
      {
        symbol: "NASDAQ",
        stance: "FAVORED",
        event_risk: { score: 70.5, level: "HIGH", minutes_until: null },
        event_id: hormuz.event_id,
        reason_facts: ["Risk-on: NASDAQ ↑"],
      },
    ],
  });
  await open(page, () => dashboard(block));
  await page.goto("/");
  await expandBackground(page);
  const card = region(page, "Today's trade watch").locator(".watch-card").first();
  await expect(card).toContainText("Iran / Hormuz escalation · Timing ANYTIME · UNRESOLVED");
  const chip = region(page, "Favored now").getByRole("listitem").first();
  await expect(chip).toHaveAttribute("title", /^Event risk HIGH\nIran \/ Hormuz escalation · Timing ANYTIME\n/);
  await expect(chip).not.toHaveAttribute("title", /Time TBA/);
  await expect(page.locator("body")).not.toContainText("Time TBA · Time TBA");
});

test("risk mode from a verified unscheduled event shows timing ANYTIME, no countdown", async ({ page }) => {
  await open(page, () =>
    dashboard(
      eventRisk(fedEvent(), {
        mode: {
          active: true,
          event_id: hormuz.event_id,
          name: hormuz.name,
          time_utc: null,
          time_bucharest: null,
          minutes_until: null,
          impact_10: 9.4,
          affected_groups: ["Oil"],
          phase: "ACTIVE",
        },
      }),
    ),
  );
  await page.goto("/");
  await expandBackground(page);
  const banner = region(page, "Event risk mode");
  await expect(banner).toContainText("Iran / Hormuz escalation");
  await expect(banner).toContainText("Timing ANYTIME");
  await expect(banner).toContainText("UNRESOLVED");
  await expect(banner).toContainText("9.4/10");
  await expect(banner).toContainText("exposed trade statuses stay conditional while this event is unresolved");
  await expect(banner.getByRole("timer")).toHaveCount(0);
});

test("weekend: traditional markets awaiting reopen, crypto interpreted live", async ({ page }) => {
  const block = eventRisk(fedEvent(), {
    mode: { active: false },
    markets_open: { crypto: true, us_equities: false, us_rates: false, fx: false, commodities: false },
    favored: [
      {
        symbol: "BTC",
        stance: "FAVORED",
        event_risk: { score: 55, level: "MODERATE", minutes_until: null },
        event_id: hormuz.event_id,
        awaiting_reopen: false,
        reason_facts: ["Risk-off: BTC ↓"],
      },
      {
        symbol: "NASDAQ",
        stance: "FAVORED",
        event_risk: { score: 70.5, level: "HIGH", minutes_until: null },
        event_id: hormuz.event_id,
        awaiting_reopen: true,
        reason_facts: ["Risk-on: NASDAQ ↑"],
      },
    ],
    trade_watch: [
      {
        asset: "BTC",
        symbol: "BTC",
        trade_status: "LONG",
        decision_score: 54,
        event_risk: { score: 55, level: "MODERATE", event_id: hormuz.event_id },
        awaiting_reopen: false,
      },
      {
        asset: "NASDAQ",
        symbol: "NASDAQ",
        trade_status: "CONDITIONAL LONG",
        decision_score: 44,
        event_risk: { score: 70.5, level: "HIGH", event_id: hormuz.event_id },
        awaiting_reopen: true,
      },
    ],
  });
  await open(page, () => dashboard(block));
  await page.goto("/");
  await expandBackground(page);
  const view = region(page, "Positioning");
  await expect(view).toContainText(
    "US equities, US rates, FX, Commodities closed: their event implications cannot be confirmed until they reopen. Crypto market is open.",
  );
  // Said once for the group, naming only the assets it applies to.
  await expect(region(page, "Favored now")).toContainText("AWAITING REOPEN: NASDAQ");
  await expect(region(page, "Favored now")).not.toContainText("AWAITING REOPEN: BTC");
  const cards = region(page, "Today's trade watch").locator(".watch-card");
  await expect(cards.locator(".watch-status")).toHaveText(["LONG", "WAIT"]);
  await expect(cards.nth(1)).toContainText("Held back from CONDITIONAL LONG: Market closed — awaiting reopen");
});

test("equities closed on a weekday: the note names every market still interpreted live", async ({ page }) => {
  const block = eventRisk(fedEvent(), {
    markets_open: { crypto: true, us_equities: false, us_rates: true, fx: true, commodities: true },
  });
  await open(page, () => dashboard(block));
  await page.goto("/");
  await expandBackground(page);
  const view = region(page, "Positioning");
  await expect(view).toContainText(
    "US equities closed: their event implications cannot be confirmed until they reopen. Crypto, US rates, FX and Commodities markets are open.",
  );
});

test("event-risk failure: a quiet notice, the rest of the dashboard still works", async ({ page }) => {
  await open(page, () =>
    dashboard(null, interpretation(), { event_risk: null, event_risk_error: "event_exposure.json: invalid level" }),
  );
  await page.goto("/");
  await expandBackground(page);
  const notice = page.getByRole("status", { name: "Event risk unavailable" });
  await expect(notice).toContainText("Event risk is unavailable right now");
  await expect(notice).toContainText("Reason: event_exposure.json: invalid level");
  await expect(page.getByRole("heading", { name: "CONSTRUCTIVE · RISK ON" })).toBeVisible();
  await expect(page.getByRole("note", { name: "Market view", exact: true })).toContainText("Fed decision is today's dominant risk");
  await expect(page.getByRole("heading", { name: "Today's best setups" })).toBeVisible();
  await expect(region(page, "Next key event")).toHaveCount(0);
  await expect(region(page, "Event risk mode")).toHaveCount(0);
});

test("a WebSocket snapshot moves the phase; the card follows the backend", async ({ page }) => {
  await page.clock.install({ time: NOW });
  let socket: WebSocketRoute | undefined;
  await page.routeWebSocket("**/ws", (ws) => {
    socket = ws;
  });
  let body = dashboard(eventRisk(fedEvent({ phase: "IMMINENT" })));
  await serve(page, () => body);
  await page.goto("/");
  await expandBackground(page);
  const card = region(page, "Next key event");
  await expect(card).toContainText("Imminent");
  const released = fedEvent({ phase: "IN_PROGRESS", time_utc: iso(-1) });
  body = dashboard(eventRisk(released));
  await expect.poll(() => !!socket).toBe(true);
  socket!.send(JSON.stringify({ type: "snapshot", data: body }));
  await expect(card).toContainText("In progress");
  await expect(card).toContainText("Decision published — press conference in 1 h 17 min");
});

test("older backend without event fields keeps the previous dashboard", async ({ page }) => {
  await open(page, () => dashboard(null, null));
  await page.goto("/");
  await expandBackground(page);
  await expect(page.getByRole("heading", { name: "CONSTRUCTIVE · RISK ON" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Today's best setups" })).toBeVisible();
  await expect(region(page, "Next key event")).toHaveCount(0);
  await expect(region(page, "AI market interpretation")).toHaveCount(0);
  await expect(region(page, "Event risk mode")).toHaveCount(0);
  await expect(page.getByRole("status", { name: "Event risk unavailable" })).toHaveCount(0);
  await expect(page.locator(".watch-card")).toHaveCount(0);
});

test("asset page shows the asset's event risk and trade status", async ({ page }) => {
  const btc = {
    ...asset("BTC", "crypto", 65000, { direction: "LONG", category: "LONG SETUP", score: 54 }),
    event_risk: {
      score: 71.3,
      level: "HIGH",
      event_id: FED,
      minutes_until: 47,
      trade_status: "WAIT FOR FED",
      adjusted_confidence: 42,
    },
  };
  await open(page, () => dashboard(), { "/api/assets/BTC": btc });
  await page.goto("/assets/BTC");
  const panel = page.locator(".panel").filter({ has: page.getByRole("heading", { name: "Event risk", exact: true }) });
  await expect(panel).toContainText("71/100");
  await expect(panel).toContainText("HIGH");
  await expect(panel).toContainText("FOMC rate decision · 21:00 Bucharest · in 47 min");
  await expect(panel.locator(".asset-trade-status")).toHaveText("WAIT FOR FED");
  // The adjusted conviction is on the decision-score scale, never a percent.
  await expect(panel).toContainText("42/100 · published decision unchanged");
  await expect(panel).not.toContainText("42%");
  await expect(panel.getByRole("list", { name: "Events exposing BTC" })).toContainText("high exposure");
});

test("calendar rows show the backend phase, relevance level and how far away a timed release is", async ({ page }) => {
  const dateOnly = {
    id: "aapl",
    name: "AAPL earnings",
    country: "US",
    scheduled_utc: "2026-09-30T20:00:00Z",
    impact: 8,
    event_class: "EARNINGS",
    time_known: false,
    time_basis: "after close",
  };
  // The dashboard's "Next important events" never shows a clock for a date-only row either.
  await open(page, () => dashboard(eventRisk(), interpretation(), { calendar: [dateOnly] }));
  await page.goto("/");
  await expandBackground(page);
  const next = page.locator(".panel").filter({ has: page.getByRole("heading", { name: "Next important events" }) });
  await expect(next.locator("time")).toHaveText("Wed 30 Sep · time TBA");

  await serve(page, () => dashboard(), {
    "/api/calendar": [
      {
        id: "cpi",
        name: "CPI YoY",
        country: "US",
        scheduled_utc: "2026-10-01T12:30:00Z",
        impact: 9,
        consensus: 2.9,
        previous: 2.8,
        actual: null,
        unit: "%",
        event_class: "US_CPI",
        time_known: true,
        phase: "SCHEDULED",
        relevance_level: "VERY HIGH",
        relevance_score: 81,
      },
      {
        id: "aapl",
        name: "AAPL earnings",
        country: "US",
        scheduled_utc: "2026-09-30T20:00:00Z",
        impact: 8,
        consensus: null,
        actual: null,
        event_class: "EARNINGS",
        time_known: false,
        time_basis: "after close",
        phase: "APPROACHING",
        relevance_level: "HIGH",
      },
      {
        id: "ifo",
        name: "Ifo business climate",
        country: "DE",
        scheduled_utc: "2026-10-01T08:00:00Z",
        impact: 8,
        consensus: 88.0,
        previous: 87.5,
        actual: null,
      },
    ],
  });
  await page.goto("/calendar");
  const cpi = page.locator("article", { hasText: "CPI YoY" });
  await expect(cpi.locator("time")).toHaveText("15:30");
  await expect(cpi.locator(".calendar-meta")).toHaveText("in 19 h 17 minPhase ScheduledRelevance 81/100 · VERY HIGH");
  const aapl = page.locator("article", { hasText: "AAPL earnings" });
  // Date-only: no clock, no countdown.
  await expect(aapl.locator("time")).toHaveText("Time TBA");
  await expect(aapl.locator(".calendar-meta")).toHaveText("Phase ApproachingRelevance HIGHTime basis: after close");
  // Older rows without the extras keep the plain card (a relative time only).
  const ifo = page.locator("article", { hasText: "Ifo business climate" });
  await expect(ifo.locator(".calendar-meta")).toHaveText("in 14 h 47 min");
});

for (const [name, width, height] of [
  ["mobile", 390, 844],
  ["desktop", 1440, 1000],
] as const)
  test(`event-aware dashboard fits the ${name} viewport without horizontal scroll`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    const live = fedEvent();
    const claims = claimsEvent();
    await open(page, () =>
      dashboard(
        eventRisk(live, {
          // The dominant FOMC behind an earlier release: the dominant line is on screen too.
          next_event: claims,
          events: [claims, live],
          post_event: [
            {
              event_id: FED,
              phase: "POST_EVENT",
              reactions: { US2Y: { "1m": { change: 4, unit: "bp" } }, NASDAQ: { "1m": { change: 0.4, unit: "pct" } } },
              confirmation: null,
              divergence: true,
            },
          ],
        }),
      ),
    );
    await page.goto("/");
  await expandBackground(page);
    await expect(region(page, "Next key event")).toBeVisible();
    await expect(page.getByRole("note", { name: "Dominant event" })).toBeVisible();
    await expect(page.locator(".watch-card")).toHaveCount(5);
    await region(page, "AI market interpretation").locator("summary").first().click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/dashboard-event-risk-${name}.png`, fullPage: true });
  });

test("impact is the calendar's number on every surface; relevance is shown apart", async ({ page }) => {
  // PCE: calendar impact 9, relevance 69.6 (impact_10 7). Before, the card said 7/10 and the calendar 9/10.
  const pce = fedEvent({
    event_id: "BEA-20260930-US_PCE",
    row_id: "cal-pce",
    name: "US personal income & outlays (PCE)",
    event_class: "US_PCE",
    category: "MACRO",
    kind: "RELEASE",
    is_decision: false,
    impact: 9,
    impact_10: 7,
    relevance_score: 69.6,
    relevance_level: "HIGH",
    consensus: "NO CONSENSUS",
    market_pricing: "UNKNOWN",
    sub_events: [],
    scenarios: [],
  });
  const block = eventRisk(pce, {
    mode: {
      active: true,
      event_id: pce.event_id,
      name: pce.name,
      time_utc: pce.time_utc,
      minutes_until: 47,
      impact_10: 7,
      affected_groups: ["USD", "Treasuries"],
      phase: "APPROACHING",
    },
    dominant_event: pce,
    events: [pce],
  });
  const calendar = [
    { id: "cal-pce", name: "US personal income & outlays (PCE)", scheduled_utc: pce.time_utc, impact: 9, country: "US" },
  ];
  await open(page, () => dashboard(block, interpretation(), { calendar, calendar_min_impact: 5 }));
  await page.goto("/");
  await expandBackground(page);
  const card = region(page, "Next key event");
  await expect(card).toContainText("IMPACT 9/10");
  await expect(card).not.toContainText("7/10");
  await expect(card).toContainText("Relevance 70/100");
  await expect(card.locator(".badge").filter({ hasText: /^HIGH$/ })).toBeVisible();
  const banner = region(page, "Event risk mode");
  await expect(banner).toContainText("Impact9/10");
  await expect(banner).not.toContainText("relevance-based");
  await expect(banner).toContainText("Relevance70/100 · HIGH");
  const next = page.locator(".panel").filter({ has: page.getByRole("heading", { name: "Next important events" }) });
  await expect(next.getByRole("listitem").first()).toContainText("Impact 9/10");
});

test("WHY NOW flags a stale close with its date; current closes read as before", async ({ page }) => {
  const regime = {
    regimes: ["RISK_ON"],
    posture: "CONSTRUCTIVE",
    confidence: 82,
    coverage_pct: 90,
    basis: "last_close",
    basis_label: "last close 22 Sep–Fri 25 Sep",
    drivers: [
      {
        driver: "Risk appetite",
        direction: "up",
        arrow: "↑",
        input: "SP500 +0.51%, NASDAQ +0.48%, VIX -4.44%",
        inputs: [
          { feature: "SP500", field: "change_pct", value: 0.51, vote: 1, close_status: "CURRENT", close_date: "2026-09-25" },
          { feature: "NASDAQ", field: "change_pct", value: 0.48, vote: 1, close_status: "CURRENT", close_date: "2026-09-25" },
          { feature: "VIX", field: "change_pct", value: -4.44, vote: 1, close_status: "STALE", close_date: "2026-09-22" },
        ],
        basis: "last_close",
      },
      {
        // No close status on the input itself: the regime's feature_sources says it is stale.
        driver: "Rates",
        direction: "down",
        arrow: "↓",
        inputs: [{ feature: "US2Y", field: "change_bp", value: -6, vote: -1 }],
        basis: "last_close",
      },
    ],
    feature_sources: { US2Y: { basis: "last_close", close_status: "STALE", close_date: "2026-09-24" } },
  };
  await open(page, () => dashboard(eventRisk(), interpretation(), { regime }));
  await page.goto("/");
  await expandBackground(page);
  // The hero's arrows flag a late close; the numbers are the chip's tooltip and under the disclosure.
  const arrows = page.getByRole("list", { name: "Driver directions" }).getByRole("listitem");
  await expect(arrows).toHaveText(["Risk appetite↑upstale close", "Rates↓downstale close"]);
  await expect(arrows.nth(0).locator(".text-warning")).toHaveText("stale close");
  await expect(arrows.nth(0)).toHaveAttribute(
    "title",
    "S&P 500 +0.51%, NASDAQ +0.48%, VIX -4.44% (stale close 22 Sep) · last close",
  );
  await expect(page.locator(".now-eyebrow .badge").first()).toHaveText("from Friday's close");
  await openBasis(page);
  const chips = page.getByRole("list", { name: "Regime drivers" }).getByRole("listitem");
  await expect(chips.nth(0)).toContainText("S&P 500 +0.51%, NASDAQ +0.48%, VIX -4.44% (stale close 22 Sep)");
  const vix = chips.nth(0).getByText("VIX -4.44% (stale close 22 Sep)", { exact: true });
  await expect(vix).toHaveClass(/text-warning/);
  // What STALE means: not the latest expected close (never "older than the other inputs").
  await expect(vix).toHaveAttribute(
    "title",
    "Not the latest expected close: last close 22 Sep. It still counts while its market is closed, up to the configured maximum age.",
  );
  // Only the stale input is flagged; the current closes read as before.
  await expect(chips.nth(0).locator(".text-warning")).toHaveCount(1);
  await expect(chips.nth(1)).toContainText("US2Y -6 bp (stale close 24 Sep)");
});

/** The dashboard with decision times: BTC, NASDAQ and EURUSD are the first three trade-watch cards. */
function withDecisionTimes(times: Record<string, string>, analysis: Block): Block {
  const base = dashboard();
  const assets = (base.assets as Block[]).map((a) =>
    typeof a.symbol === "string" && a.symbol in times
      ? { ...a, decision: { ...(a.decision as Block), created_at: times[a.symbol] } }
      : a,
  );
  return { ...base, assets, analysis };
}

test("a decision the last successful analysis did not replace shows when it was made; a running cycle does not", async ({
  page,
}) => {
  // The cycle that started 8 min ago (more than one 5-minute cadence) succeeded; the next one is still running.
  const cycle = iso(-8);
  const analysis = { last_success_at: cycle, last_attempt_at: cycle, last_error_type: null, cadence_seconds: 300, stale: false };
  await open(page, () => withDecisionTimes({ BTC: iso(-303), NASDAQ: cycle }, analysis));
  await page.goto("/");
  await expandBackground(page);
  const cards = region(page, "Today's trade watch").locator(".watch-card");
  // 12:10 UTC = 15:10 Bucharest, 5 h 3 min before 20:13.
  const btc = cards.nth(0).locator(".watch-decided");
  await expect(btc).toHaveText("Decided 15:10 (5 h 3 min ago)");
  await expect(btc.locator("time")).toHaveAttribute("datetime", iso(-303));
  await expect(btc).toHaveAttribute(
    "title",
    "The last full analysis (20:05 Bucharest) published no new decision for this asset; this one is older.",
  );
  // From the last successful cycle: quiet, although that cycle started more than one cadence ago.
  await expect(cards.nth(1).locator(".watch-decided")).toHaveCount(0);
  // No decision time at all: nothing invented.
  await expect(cards.nth(2).locator(".watch-decided")).toHaveCount(0);
});

test("while the analysis is stale, every card says when its decision was made, with the header's reason", async ({
  page,
}) => {
  // The last attempt failed 3 min ago; the last success is 5 h old (the backend says stale, as the header shows).
  const analysis = {
    last_success_at: iso(-303),
    last_attempt_at: iso(-3),
    last_error_type: "RuntimeError",
    cadence_seconds: 300,
    stale: true,
  };
  await open(page, () => withDecisionTimes({ BTC: iso(-303), NASDAQ: iso(-303) }, analysis));
  await page.goto("/");
  await expandBackground(page);
  const cards = region(page, "Today's trade watch").locator(".watch-card");
  for (const i of [0, 1]) {
    const decided = cards.nth(i).locator(".watch-decided");
    await expect(decided).toHaveText("Decided 15:10 (5 h 3 min ago)");
    await expect(decided).toHaveAttribute("title", /^The analysis attempt at .+ failed; signals are from the last successful run\./);
  }
  await expect(cards.nth(2).locator(".watch-decided")).toHaveCount(0);
});

test("events timeline: the other scheduled events from now on, today's releases and the latest confirmed facts, each event once", async ({ page }) => {
  const fed = fedEvent();
  const DAY = 24 * 60;
  const scheduled = (id: string, name: string, minutes: number, extra: Block = {}) =>
    fedEvent({
      event_id: id,
      row_id: id,
      name,
      time_utc: iso(minutes),
      impact: 6,
      relevance_score: 45,
      relevance_level: "MODERATE",
      phase: "SCHEDULED",
      status: "SCHEDULED",
      sub_events: [],
      scenarios: [],
      ...extra,
    });
  const events = [
    // Due before the key event (the backend did not pick it as the key event): still coming up, listed first.
    scheduled("EXPIRY", "BTC options expiry", 10, { impact: 4 }),
    scheduled("DONE", "Already released", 50, { phase: "RELEASED" }),
    fed,
    scheduled("SIXTH", "Sixth release", 10 * DAY),
    scheduled("MINUTES", "FOMC minutes", 7 * DAY),
    scheduled("EIA", "EIA Weekly Petroleum Status Report", DAY + 30),
    scheduled("REFUNDING", "Treasury refunding", 8 * DAY, { time_known: false, date_bucharest: "2026-10-08" }),
    scheduled("FIFTH", "Fifth release", 9 * DAY),
  ];
  const fact = (id: string, minutes: number, extra: Block) => ({
    id,
    event_type: "NEWS",
    verification_status: "CONFIRMED",
    fact_confidence: 90,
    first_seen_utc: iso(minutes),
    last_updated_utc: iso(minutes),
    sources: ["Test fixture"],
    assets: [],
    interpretation: null,
    ...extra,
  });
  const news = [
    fact("custody", -200, { summary_fact: "The SEC approved a custody rule for crypto assets.", assets: ["BTC", "ETH"] }),
    fact("opec", -20, {
      topic: "OPEC",
      topic_basis: "rule",
      summary_fact: "OPEC agreed to cut output by 1 million barrels a day. More to follow.",
      assets: ["BRENT"],
      causal_paths: [{ active: true, effects: { BRENT: 1, WTI: 1, DXY: -1 } }],
    }),
    fact("partial", -5, { summary_fact: "A partly confirmed dollar story.", verification_status: "PARTIALLY_CONFIRMED", assets: ["DXY"] }),
    fact("impactless", -1, { summary_fact: "A confirmed story without market impact." }),
    // The official document of the released event: shown with that event, not a second time as news.
    fact("jolts", -190, { summary_fact: "Job openings were little changed at 7.2 million in August.", assets: ["US2Y"] }),
  ];
  const block = eventRisk(fed, {
    mode: { active: false },
    events,
    trade_watch: [],
    // A scheduled event released earlier today: it left `events`, its official release is the "jolts" fact below.
    released_today: [
      {
        event_id: "BLS-20260930-US_JOLTS",
        name: "US JOLTS job openings",
        time_utc: iso(-193),
        summary: "Job openings were little changed at 7.2 million in August",
        news_event_id: "jolts",
        affected_assets: [{ symbol: "US2Y" }, { symbol: "DXY" }, { symbol: "SP500" }],
      },
    ],
  });
  await open(page, () => dashboard(block, interpretation(), { events: news, assets: [] }));
  await page.goto("/");
  const timeline = region(page, "Events");
  // Soonest first, at most four; a date-only row has no clock; the zone is named once, in the page footer.
  await expect(timeline.getByRole("list", { name: "Coming up" }).getByRole("listitem")).toHaveText([
    "20:23BTC options expiry4/10",
    "Thu 1 Oct 20:43EIA Weekly Petroleum Status Report6/10",
    "Wed 7 Oct 20:13FOMC minutes6/10",
    "Thu 8 Oct · time TBATreasury refunding6/10",
  ]);
  // The key event has its own card: the timeline does not list it again.
  await expect(timeline).not.toContainText("FOMC rate decision");
  await expect(timeline).not.toContainText("Already released");
  await expect(timeline).not.toContainText("Fifth release");
  await expect(timeline).not.toContainText("Bucharest");
  await expect(region(page, "Next key event").getByRole("heading", { name: "FOMC rate decision" })).toBeVisible();
  // Confirmed facts with an identified asset impact, newest first: time, the first sentence, the assets.
  const latest = timeline.getByRole("list", { name: "Latest confirmed" }).getByRole("listitem");
  // First the scheduled event released today with what was published (EVR-4), then the news.
  await expect(latest).toHaveText([
    "Wed 30 Sep 17:00Released US JOLTS job openingsJob openings were little changed at 7.2 million in AugustUS2YDXYSP500",
    "Wed 30 Sep 19:53OPEC agreed to cut output by 1 million barrels a day.BRENT↑ upWTI↑ upDXY↓ down",
    "Wed 30 Sep 16:53The SEC approved a custody rule for crypto assets.BTCETH",
  ]);
  await expect(latest.first().getByRole("link")).toHaveAttribute("href", "/events/jolts");
  await expect(latest.nth(1).getByRole("link")).toHaveAttribute("href", "/events/opec");
  // The release document is told once, with its event.
  await expect(timeline.getByText(/Job openings were little changed/)).toHaveCount(1);
  await page.locator(".event-grid").screenshot({ path: "test-results/events-timeline.png" });
  await expect(timeline).not.toContainText("partly confirmed");
  await expect(timeline).not.toContainText("without market impact");
  // Nothing actionable 47 minutes before the key event: one line that refers to it without naming it again.
  await expect(page.locator(".setups-wait")).toHaveText(
    "Today's best setupsNo actionable setups— waiting for the next key event.WAIT",
  );
  // Both cards of the EVENTS row share one height on a desktop.
  const card = await region(page, "Next key event").boundingBox();
  const panel = await timeline.boundingBox();
  expect(Math.abs(card!.height - panel!.height)).toBeLessThan(2);
});

test("a released key event says what was published in its card, and the timeline does not list it again", async ({ page }) => {
  const fed = liveFed();
  const block = eventRisk(fed, {
    released_today: [
      {
        event_id: FED,
        name: "FOMC rate decision",
        time_utc: fed.time_utc,
        released_utc: iso(-5),
        summary: "Federal Reserve issues FOMC statement: the target range is unchanged at 5.00 to 5.25 percent",
        summary_basis: "official document",
        document: { event_id: "fomc-statement", title: "Federal Reserve issues FOMC statement", published_at: iso(-5) },
        news_event_id: "fomc-statement",
        affected_assets: [{ symbol: "US2Y" }, { symbol: "DXY" }],
        phase: "IN_PROGRESS",
      },
    ],
  });
  await open(page, () => dashboard(block));
  await page.goto("/");
  const card = region(page, "Next key event");
  await expect(card.locator(".next-event-published")).toHaveText(
    "Published: Federal Reserve issues FOMC statement: the target range is unchanged at 5.00 to 5.25 percent",
  );
  // Each event once: the card has it, so "Latest confirmed" does not repeat it.
  const timeline = region(page, "Events");
  await expect(timeline).not.toContainText("FOMC rate decision");
  await expect(timeline.locator("[data-released]")).toHaveCount(0);
  await expect(timeline).toContainText("No confirmed news with an identified asset impact yet.");
  // The key event has its card: an empty "Coming up" says there are no OTHER scheduled events.
  await expect(timeline).toContainText("No other scheduled events in this window.");
});
