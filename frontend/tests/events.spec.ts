import { test, expect, type Page, type Route } from "@playwright/test";

/*
 * Event intelligence (/events), event detail (/events/[id]), sources & verification (/sources) and the X feed (/social)
 * against intercepted, test-only API responses shaped like the backend's. Nothing here reaches a real backend.
 * Monday 28 Sep 2026 09:00 UTC = 12:00 Bucharest (EEST, UTC+3).
 */
const NOW = new Date("2026-09-28T09:00:00Z");
const hoursAgo = (h: number) => new Date(NOW.getTime() - h * 3_600_000).toISOString();

/** Only BTC is a tracked asset: symbols link to /assets/BTC, other symbols stay text. */
const dashboard = {
  providers: [],
  assets: [{ symbol: "BTC", name: "Bitcoin", asset_class: "crypto", quote: null, decision: null }],
  events: [],
  calendar: [],
  regimes: [],
};

type Reply = unknown | { status: number; json: unknown } | ((route: Route) => Promise<void>);

/** Routes keyed by pathname; the returned list records every API path + query the page requested. */
async function mockApi(page: Page, routes: Record<string, Reply>) {
  const hits: string[] = [];
  await page.clock.install({ time: NOW });
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    hits.push(`${url.pathname}${url.search}`);
    const reply = routes[url.pathname];
    if (typeof reply === "function") return (reply as (route: Route) => Promise<void>)(route);
    if (reply && typeof reply === "object" && "status" in reply && "json" in reply)
      return route.fulfill(reply as { status: number; json: unknown });
    if (reply !== undefined) return route.fulfill({ json: reply });
    return route.fulfill({ json: url.pathname === "/api/dashboard" ? dashboard : [] });
  });
  return hits;
}

/** Panel sections are titled by an h2; locate the card by its heading. */
function panel(page: Page, title: string) {
  return page.locator(".panel", { has: page.getByRole("heading", { name: title, exact: true }) });
}

function listEvent(i: number, extra: Record<string, unknown> = {}) {
  const t = hoursAgo(i + 1);
  return {
    id: `ev-${i}`,
    summary_fact: `Release number ${i}`,
    event_type: "OFFICIAL_RELEASE",
    verification_status: i % 2 ? "CONFIRMED" : "PARTIALLY_CONFIRMED",
    fact_confidence: 73.39,
    first_seen_utc: t,
    last_updated_utc: t,
    event_time: t,
    assets: i === 0 ? ["BTC"] : [],
    sources: ["Federal Reserve"],
    evidence: [],
    interpretation: null,
    provenance: "provider_retrieval",
    ...extra,
  };
}

/** 60 recent releases plus a backfilled old one that the API lists first (it was ingested last). */
const backfilled = listEvent(99, {
  id: "ev-backfill",
  summary_fact: "OFSI notice: Sabre Global Technologies designation",
  verification_status: "STALE",
  fact_confidence: 0,
  event_time: "2026-06-17T10:00:00Z",
  first_seen_utc: hoursAgo(0.01),
});
const manyEvents = [backfilled, ...Array.from({ length: 60 }, (_, i) => listEvent(i))];

test("events list loads every event newest publication first, pages, counts and searches honestly", async ({ page }) => {
  const hits = await mockApi(page, { "/api/events": manyEvents });
  await page.goto("/events");
  const rows = page.locator(".event-row");
  await expect(rows).toHaveCount(50);
  expect(hits).toContain("/api/events?limit=500");
  // An h2 sits between the page h1 and the h3 row titles.
  await expect(page.getByRole("heading", { level: 2, name: "Events", exact: true })).toBeVisible();
  const count = page.locator("#events-list-heading + p");
  await expect(count).toHaveText("61 events, showing 50");
  await expect(rows.first()).toContainText("Release number 0");
  await expect(rows.first().getByRole("link", { name: "Release number 0" })).toHaveAttribute("href", "/events/ev-0");
  await expect(rows.first().getByRole("link", { name: "BTC", exact: true })).toHaveAttribute("href", "/assets/BTC");
  await expect(rows.first()).toContainText("Fact confidence 73.3%");

  await page.getByRole("button", { name: "Show more events (11 remaining)" }).click();
  await expect(rows).toHaveCount(61);
  await expect(count).toHaveText("61 events");
  // The backfilled release is sorted by its publication date, not its ingestion time.
  await expect(rows.last()).toContainText("OFSI notice");
  await expect(rows.last()).toContainText("Fact confidence not current");

  const search = page.getByRole("textbox", { name: "Search records" });
  await search.fill("ofsi");
  await expect(rows).toHaveCount(1);
  await expect(count).toHaveText("1 of 61 events matches");
  // One count on the page (REG-events-7): the toolbar does not repeat it as "1 records".
  await expect(page.locator(".toolbar [role=status]")).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: /\d+ (events?|records)/ })).toHaveCount(1);
  // Hidden fields (provenance, ids) never match.
  await search.fill("provider_retrieval");
  await expect(rows).toHaveCount(0);
  await expect(page.getByText("No events match “provider_retrieval”")).toBeVisible();
  await expect(page.getByText("No verified events yet")).toHaveCount(0);

  await search.fill("");
  await page.getByLabel("Verification filter").selectOption("PARTIALLY_CONFIRMED");
  await expect(count).toHaveText("30 of 61 events match");

  // The list refreshes while open.
  const before = hits.filter((h) => h === "/api/events?limit=500").length;
  await page.clock.runFor(31_000);
  await expect.poll(() => hits.filter((h) => h === "/api/events?limit=500").length).toBeGreaterThan(before);
});

test("events list shows no empty state or record count while loading", async ({ page }) => {
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await mockApi(page, {
    "/api/events": async (route: Route) => {
      await held;
      await route.fulfill({ json: [listEvent(0)] });
    },
  });
  await page.goto("/events");
  await expect(page.getByRole("heading", { level: 1, name: "Event intelligence" })).toBeVisible();
  await expect(page.getByText("No verified events yet")).toHaveCount(0);
  await expect(page.getByText(/\d+ records/)).toHaveCount(0);
  release();
  await expect(page.locator(".event-row")).toHaveCount(1);
});

const EV_A = "db63739a-0000-4000-8000-00000000000a";
const EV_B = "db63739a-0000-4000-8000-00000000000b";

/** A BoJ release first seen 62 h after publication, with one document received over http and https. */
const bojEvent = {
  id: "ev-boj",
  summary_fact: "Minutes of the Monetary Policy Meeting on July 30 and 31, 2026",
  event_type: "OFFICIAL_RELEASE",
  verification_status: "PARTIALLY_CONFIRMED",
  fact_confidence: 99.58,
  event_time: "2026-09-24T14:46:00Z",
  first_seen_utc: "2026-09-27T05:29:56Z",
  // Stored a few seconds before the first sighting (EV-16): the page never shows an update before first seen.
  last_updated_utc: "2026-09-27T05:28:30Z",
  topic: "CHINA_CHINA",
  topic_basis: "entities",
  assets: ["BTC", "ETH"],
  affected_assets: ["BTC", "ETH"],
  importance: 1,
  importance_basis: "source tier",
  trigger: "CRYPTO_REGULATION",
  trigger_basis: "rule",
  sources: ["Bank of Japan"],
  extraction: {
    who: ["Bank of Japan"],
    what: "Published the minutes of the July meeting",
    when: "Minutes published 2026-09-27T23:50:00+00:00",
    where: "Tokyo",
    direct_claims: [`Members kept the policy rate unchanged (evidence: ${EV_A})`],
    new_information: [],
  },
  evidence: [EV_A, EV_B].map((id, i) => ({
    id,
    source: "Bank of Japan",
    url: `${i ? "https" : "http"}://www.boj.or.jp/en/mopo/g260731.pdf`,
    title: "Minutes of the Monetary Policy Meeting",
    excerpt: "",
    published_at: "2026-09-24T14:46:00Z",
    ingested_at: "2026-09-27T05:29:56Z",
    relation: "supports",
    official: true,
    direct: true,
    group: "boj",
    tier: 1,
  })),
  causal_paths: [
    {
      rule_id: "crypto-regulation",
      label: "Tighter crypto regulation",
      trigger: "CRYPTO_REGULATION",
      steps: [{ variable: "crypto_regulatory_risk", direction: 1, label: "Crypto regulatory risk" }],
      effects: { BTC: -1, ETH: -1 },
      // Regime-adjusted; the rule itself is configured at 60.
      strength: 78,
      strength_prior: 60,
      regime_multiplier: 1.3,
      exceptions_fired: [],
      decay: 0,
      half_life_hours: 24,
      active: false,
      suppressed_by: null,
    },
  ],
  reactions: [
    { symbol: "BTC", horizon: "15m", change: 0.18, unit: "pct", expected_direction: 0, status: "DIVERGENT", baseline_timestamp: "2026-09-27T05:29:56Z" },
    { symbol: "ETH", horizon: "15m", change: 0.01, unit: "pct", expected_direction: 0, status: "NOT_EXPECTED", baseline_timestamp: "2026-09-27T05:29:50Z" },
  ],
  baskets: [
    { horizon: "15m", confirmation_pct: null, expected: 0, measured: 0, confirming: [], partial: [], diverging: [], missing: [], no_reaction: [] },
  ],
  interpretation: {
    hypotheses: [
      {
        asset: "JPY (Japanese yen, e.g., USD/JPY)",
        direction: "neutral",
        causal_path: [`The Bank of Japan published minutes (evidence ${EV_B})`, "No new policy signal"],
        evidence_ids: [EV_A],
        confirmation: "USD/JPY stays in its range",
        invalidation: "The yen rallies 1% in a session",
      },
    ],
    uncertainties: ["The minutes are backward-looking."],
  },
  interpretation_role: "fx",
};

test("event detail renders the interpretation, citations, duplicates and late reactions in plain language", async ({ page }) => {
  await mockApi(page, { "/api/events/ev-boj": bojEvent });
  await page.goto("/events/ev-boj");
  await expect(page).toHaveTitle(/^Minutes of the Monetary Policy Meeting.* · Event evidence · Market Intelligence$/);

  const fact = page.getByRole("region", { name: "Fact", exact: true });
  await expect(fact).toContainText("Fact confidence: 99.5%");
  await expect(fact).not.toContainText("CHINA"); // entity-joined topic: no badge
  await expect(fact).toContainText("Importance 100/100 (source trust tier only; not assessed by the extractor)");
  await expect(fact).toContainText("Trigger Crypto regulation (matched a trigger rule)");
  await expect(fact).toContainText("First seen Sun 27 Sep 08:29 · Updated Sun 27 Sep 08:29");
  // Every causal path has faded: the listed assets are qualified, not presented as expected movers.
  await expect(fact).toContainText("Affected assets: BTC / ETH (no active causal path: the effect has faded)");
  await expect(fact.getByRole("link", { name: "BTC", exact: true })).toHaveAttribute("href", "/assets/BTC");
  await expect(fact.getByRole("link", { name: "ETH", exact: true })).toHaveCount(0);

  const what = panel(page, "What happened");
  await expect(what).toContainText("Minutes published Mon 28 Sep 02:50 (Bucharest)");
  await expect(what).not.toContainText("2026-09-27T");
  await expect(what).not.toContainText(EV_A);
  await expect(what.getByRole("link", { name: "Evidence 1" })).toHaveAttribute("href", "#evidence-1");

  const verification = panel(page, "Verification status");
  await expect(verification).toContainText("99.5%");
  await expect(verification).toContainText(
    "1 document from 1 independent source group (1 duplicate copy of the same address not counted)",
  );
  await expect(verification).toContainText("1 official · 1 direct");

  const sources = panel(page, "Sources");
  await expect(sources.locator(".evidence")).toHaveCount(1);
  await expect(sources).toContainText("2 copies of this address received");
  await expect(sources).toContainText("No excerpt captured");
  await expect(sources.getByText("Minutes of the Monetary Policy Meeting", { exact: true })).toHaveCount(1);
  await expect(sources.getByRole("link", { name: "Read source" })).toHaveAttribute(
    "href",
    "https://www.boj.or.jp/en/mopo/g260731.pdf",
  );

  const chain = panel(page, "Causal chain");
  // The rule's configured name, not its id.
  const path = chain.getByRole("region", { name: "Causal path Tighter crypto regulation" });
  await expect(path.getByRole("heading", { name: "Tighter crypto regulation" })).toBeVisible();
  await expect(path).toContainText("INACTIVE");
  await expect(path).toContainText("Inactive: the effect has faded with time since publication.");
  await expect(path).toContainText(
    "Strength 78 (rule 60, adjusted for the current regime) · under 0.1% of the initial effect remains (half-life 24 h)",
  );
  await expect(path).not.toContainText("DIVERGENT");
  await expect(path).not.toContainText("NOT EXPECTED");
  await expect(chain).toContainText("Measured from the prices at Sun 27 Sep 08:29 (Bucharest), 62 h after publication");
  // The late-sighting warning is written once, under the reaction table; the chain only points to it.
  await expect(chain).not.toContainText("not the market's reaction");
  await expect(chain).toContainText("see the timing note under Expected vs actual reaction");
  await expect(page.getByText(/first seen 62 h after it was published/)).toHaveCount(1);

  const table = page.getByRole("table", { name: "Expected and actual market reaction" });
  await expect(table.getByRole("row", { name: /BTC/ })).toContainText("+0.18%");
  await expect(table).not.toContainText("DIVERGENT");
  await expect(table).not.toContainText("NOT EXPECTED");
  await expect(panel(page, "Expected vs actual reaction")).toContainText(
    "first seen 62 h after it was published (reactions are measured only for events first seen within 15 min of publication), so these moves are not the market's reaction to the release",
  );
  await expect(panel(page, "Market confirmation")).toContainText("Not applicable");
  await expect(panel(page, "Market confirmation")).not.toContainText("Not enough data");

  const ai = panel(page, "AI interpretation");
  await expect(ai).toContainText("JPY (Japanese yen, e.g., USD/JPY)");
  await expect(ai).toContainText("NEUTRAL");
  await expect(ai).toContainText("No new policy signal");
  await expect(ai).toContainText("Confirmed if");
  await expect(ai).toContainText("USD/JPY stays in its range");
  await expect(ai).toContainText("The minutes are backward-looking.");
  await expect(ai).toContainText("by the FX specialist");
  await expect(ai).not.toContainText('"asset"');
  await expect(ai).not.toContainText("db63739a");
  await ai.getByRole("link", { name: "Evidence 1" }).first().click();
  await expect(page).toHaveURL(/#evidence-1$/);
  await expect(page.locator("#evidence-1")).toBeVisible();
});

/** A release first seen 20 min after publication with one confirmed reaction (BTC expected down, measured down). */
function laggedEvent(id: string, extra: Record<string, unknown> = {}) {
  return {
    ...listEvent(3),
    id,
    event_time: "2026-09-28T06:00:00Z",
    first_seen_utc: "2026-09-28T06:20:00Z",
    trigger: "CRYPTO_REGULATION",
    trigger_basis: "rule",
    causal_paths: [
      { rule_id: "crypto-regulation", effects: { BTC: -1 }, strength: 60, strength_prior: 60, decay: 0.9, active: true, suppressed_by: null },
    ],
    reactions: [
      { symbol: "BTC", horizon: "1h", change: -1.2, unit: "pct", expected_direction: -1, status: "CONFIRMED", baseline_timestamp: "2026-09-28T06:20:00Z" },
    ],
    baskets: [{ horizon: "1h", confirmation_pct: 100, expected: 1, measured: 1, confirming: ["BTC"], diverging: [], missing: [], no_reaction: [] }],
    ...extra,
  };
}

test("reaction verdicts follow the backend's publication-lag limit, from the API when it sends one", async ({ page }) => {
  await mockApi(page, {
    "/api/events/ev-lag-default": laggedEvent("ev-lag-default"),
    "/api/events/ev-lag-config": laggedEvent("ev-lag-config", { max_publication_lag_seconds: 1800, publication_lag_seconds: 1200 }),
    "/api/events/ev-lag-unmeasured": laggedEvent("ev-lag-unmeasured", {
      first_seen_utc: "2026-09-28T08:00:00Z",
      reactions: [],
      baskets: [],
    }),
  });
  // 20 min is over the default limit (15 min): verdicts are hidden, the limit is named.
  await page.goto("/events/ev-lag-default");
  const reactions = panel(page, "Expected vs actual reaction");
  await expect(reactions).toContainText(
    "first seen 20 min after it was published (reactions are measured only for events first seen within 15 min of publication)",
  );
  await expect(reactions.getByRole("table")).not.toContainText("CONFIRMED");
  await expect(panel(page, "Market confirmation")).toContainText("Not the release reaction");
  await expect(panel(page, "Market confirmation")).toContainText("see the timing note under Expected vs actual reaction");
  await expect(panel(page, "Market confirmation")).not.toContainText("first seen 20 min");
  await expect(panel(page, "Market confirmation")).not.toContainText("100%");

  // A configured limit of 30 min (sent by the API) makes the same lag acceptable.
  await page.goto("/events/ev-lag-config");
  await expect(panel(page, "Expected vs actual reaction").getByRole("table")).toContainText("CONFIRMED");
  await expect(panel(page, "Expected vs actual reaction")).not.toContainText("not the market's reaction");
  await expect(panel(page, "Market confirmation")).toContainText("100%");

  // A late event is no longer measured: say why instead of "not measured yet".
  await page.goto("/events/ev-lag-unmeasured");
  await expect(panel(page, "Expected vs actual reaction")).toContainText("Not measured: this event was first seen 2 h after it was published");
  await expect(panel(page, "Expected vs actual reaction")).not.toContainText("No reaction has been measured yet");
  await expect(panel(page, "Market confirmation")).toContainText("Not measured");
  await expect(panel(page, "Market confirmation")).not.toContainText("Not computed yet");
});

test("FX specialist biases read as words, never as machine tokens", async ({ page }) => {
  await mockApi(page, {
    "/api/events/ev-fx": {
      ...listEvent(4),
      id: "ev-fx",
      interpretation: {
        pairs: [
          { pair: "EURUSD", bias: "neutral-wait", base_bank: "ECB", causal_path: ["ECB on hold", "Fed on hold"] },
          { pair: "USDJPY", bias: "USD-positive", causal_path: ["Rate gap widens"] },
        ],
        uncertainties: [],
      },
      interpretation_role: "fx",
    },
  });
  await page.goto("/events/ev-fx");
  const ai = panel(page, "AI interpretation");
  await expect(ai).toContainText("Currency pairs");
  await expect(ai).toContainText("NEUTRAL (WAIT)");
  await expect(ai).toContainText("USD POSITIVE");
  await expect(ai).not.toContainText("NEUTRAL-WAIT");
  await expect(ai).not.toContainText("USD-POSITIVE");
});

test("PARTIALLY CONFIRMED has one colour on the event page and the sources page", async ({ page }) => {
  await mockApi(page, { "/api/events/ev-boj": bojEvent, "/api/events": [bojEvent], "/api/sources": [] });
  await page.goto("/events/ev-boj");
  const fact = page.getByRole("region", { name: "Fact", exact: true });
  await expect(fact.locator(".badge", { hasText: "PARTIALLY CONFIRMED" })).toHaveAttribute("data-variant", "info");
  await page.goto("/sources");
  const header = page.locator("article header", { hasText: "Minutes of the Monetary Policy Meeting" });
  await expect(header.locator(".badge", { hasText: "PARTIALLY CONFIRMED" })).toHaveAttribute("data-variant", "info");
});

test("causal chain copy separates 'no rule covers it' from 'not classified'", async ({ page }) => {
  const base = { ...listEvent(1), id: "ev-rule-less", causal_paths: [], reactions: [], baskets: [] };
  await mockApi(page, {
    "/api/events/ev-rule-less": { ...base, trigger: null, trigger_basis: "none" },
    "/api/events/ev-legacy": { ...base, id: "ev-legacy" },
  });
  await page.goto("/events/ev-rule-less");
  const chain = panel(page, "Causal chain");
  await expect(chain).toContainText("No causal rule covers this event");
  await expect(chain).toContainText("That does not mean no effect is expected.");
  await expect(chain).not.toContainText("no market effect is expected");
  await page.goto("/events/ev-legacy");
  await expect(panel(page, "Causal chain")).toContainText("Causal chain unavailable");
  await expect(panel(page, "Causal chain")).toContainText("has not been classified");
});

test("market confirmation lists assets that did not react and how many were measured", async ({ page }) => {
  await mockApi(page, {
    "/api/events/ev-measured": {
      ...listEvent(2),
      id: "ev-measured",
      trigger: "GEOPOLITICAL_ENERGY",
      causal_paths: [],
      reactions: [
        { symbol: "BRENT", horizon: "1h", change: 2.1, unit: "pct", expected_direction: 1, status: "CONFIRMED", baseline_timestamp: hoursAgo(3) },
      ],
      baskets: [
        {
          horizon: "1h",
          confirmation_pct: 33.3,
          confirming: ["BRENT"],
          partial: [],
          diverging: ["BTC"],
          missing: [],
          no_reaction: ["ETH"],
          divergence: false,
          measured: 3,
          expected: 3,
        },
      ],
    },
  });
  await page.goto("/events/ev-measured");
  const confirmation = panel(page, "Market confirmation");
  await expect(confirmation).toContainText("33%");
  await expect(confirmation).toContainText("Measured 3 of 3 expected assets");
  await expect(confirmation.locator("dt", { hasText: "No reaction" })).toBeVisible();
  await expect(confirmation.locator("dd").nth(2)).toHaveText("ETH");
  await expect(panel(page, "Expected vs actual reaction")).toContainText("Measured from the prices at");
  await expect(panel(page, "Expected vs actual reaction")).not.toContainText("not the market's reaction");
});

test("an unknown event shows a not-found state and is not polled again", async ({ page }) => {
  const hits = await mockApi(page, {
    "/api/events/missing": { status: 404, json: { detail: "Event not found" } },
  });
  await page.goto("/events/missing");
  const missing = page.getByRole("region", { name: "Event not found" });
  await expect(missing).toBeVisible();
  await expect(missing.getByRole("link", { name: "Open the events list" })).toHaveAttribute("href", "/events");
  await expect(page.getByText("Unable to reach the data service")).toHaveCount(0);
  await expect(page).toHaveTitle("Event not found · Market Intelligence");
  await page.clock.runFor(65_000);
  expect(hits.filter((h) => h === "/api/events/missing")).toHaveLength(1);
});

const registry = [
  {
    id: "fed",
    name: "Federal Reserve",
    domain: "federalreserve.gov",
    kind: "central_bank",
    tier: 1,
    official: true,
    independence_group: "fed",
    rss: "https://www.federalreserve.gov/feeds/press_all.xml",
    homepage: "https://www.federalreserve.gov/",
    trust_weight: 1,
    status: "ONLINE",
    last_success: hoursAgo(1),
  },
  {
    id: "x-fed",
    name: "Federal Reserve (X)",
    domain: "x.com",
    kind: "central_bank",
    tier: 1,
    official: true,
    independence_group: "fed",
    x_account: "federalreserve",
    homepage: null,
    trust_weight: 1,
    status: "NOT_CONFIGURED",
    last_success: null,
  },
  // No homepage known and no provider status: the domain is text, never https://boj.or.jp/ (no site there).
  {
    id: "boj",
    name: "Bank of Japan",
    domain: "boj.or.jp",
    kind: "central_bank",
    tier: 1,
    official: true,
    independence_group: "boj",
    rss: "https://www.boj.or.jp/en/rss/whatsnew.xml",
    homepage: null,
    trust_weight: 1,
    status: null,
    last_success: null,
  },
];

test("sources page: honest confidence, source homes, status and filtered-empty copy", async ({ page }) => {
  const hits = await mockApi(page, {
    "/api/sources": registry,
    "/api/events": [listEvent(0), backfilled],
  });
  await page.goto("/sources");
  await expect.poll(() => hits).toContain("/api/events?limit=500");
  const fresh = page.locator("article", { hasText: "Release number 0" });
  await expect(fresh.locator("header")).toContainText("Fact confidence 73.3%");
  const stale = page.locator("article", { hasText: "OFSI notice" });
  await expect(stale.locator("header")).toContainText("Fact confidence not current");
  await expect(stale.locator("header")).not.toContainText("Fact confidence 0");

  const table = page.locator("section", { has: page.getByRole("heading", { name: "Source registry" }) });
  await expect(table.getByRole("link", { name: "federalreserve.gov" })).toHaveAttribute("href", "https://www.federalreserve.gov/");
  await expect(table.getByRole("link", { name: "Federal Reserve feed" })).toHaveAttribute(
    "href",
    "https://www.federalreserve.gov/feeds/press_all.xml",
  );
  await expect(table.getByRole("link", { name: "@federalreserve on X" })).toHaveAttribute("href", "https://x.com/federalreserve");
  await expect(table.locator("tr", { hasText: "Federal Reserve (X)" })).toContainText("NOT CONFIGURED");
  await expect(table.locator("tr", { hasText: "Federal Reserve (X)" })).not.toContainText("Last success");
  await expect(table.locator("tr", { hasText: "federalreserve.gov" })).toContainText("ONLINE");
  await expect(table.locator("tr", { hasText: "federalreserve.gov" })).toContainText("Last success Mon 28 Sep 11:00");
  // No homepage: the domain is plain text; no provider status is not an outage.
  const boj = table.locator("tr", { hasText: "Bank of Japan" });
  await expect(boj).toContainText("boj.or.jp");
  await expect(boj.getByRole("link", { name: "boj.or.jp" })).toHaveCount(0);
  await expect(boj.getByRole("link", { name: "Bank of Japan feed" })).toHaveAttribute(
    "href",
    "https://www.boj.or.jp/en/rss/whatsnew.xml",
  );
  await expect(boj).toContainText("No provider status");
  await expect(boj).not.toContainText("Unavailable");
  expect(await table.locator('a[href="https://boj.or.jp/"], a[href="https://example.invalid/"]').count()).toBe(0);

  const search = page.getByRole("textbox", { name: "Search records" });
  await search.fill("zzqqnomatch");
  await expect(page.getByText("No events match “zzqqnomatch”")).toBeVisible();
  await expect(page.getByText("No sources match “zzqqnomatch”")).toBeVisible();
  await expect(page.getByText("No events to verify")).toHaveCount(0);
  await expect(page.getByText("No sources registered")).toHaveCount(0);

  // Only shown text matches: the event type and a registry id that has a name are not on the page.
  await search.fill("official release");
  await expect(page.getByText("No events match “official release”")).toBeVisible();
  await search.fill("x-fed");
  await expect(page.getByText("No sources match “x-fed”")).toBeVisible();
  await search.fill("@federalreserve on x");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  // One count on the page: the panel header, not a second "N records" in the toolbar.
  await search.fill("");
  await expect(page.locator(".toolbar [role=status]")).toHaveCount(0);
  await expect(panel(page, "Event verification").getByRole("status")).toHaveText("2 events");
});

test("sources page counts one document once, as the event page does", async ({ page }) => {
  await mockApi(page, { "/api/sources": registry, "/api/events": [bojEvent] });
  await page.goto("/sources");
  const block = page.locator("article", { hasText: "Minutes of the Monetary Policy Meeting" });
  await expect(block).toContainText(
    "1 supporting · 0 contradicting · 1 independent source group (1 duplicate copy of the same address not counted)",
  );
  const evidence = block.getByRole("table", { name: /^Evidence for / });
  await expect(evidence.locator("tbody tr")).toHaveCount(1);
  await expect(evidence).toContainText("2 copies of this address received (http/https or www variants), counted once");
  await expect(evidence.getByRole("link", { name: "Bank of Japan" })).toHaveAttribute(
    "href",
    "https://www.boj.or.jp/en/mopo/g260731.pdf",
  );
});

test("sources page shows no record count while the events are still loading", async ({ page }) => {
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await mockApi(page, {
    "/api/sources": registry,
    "/api/events": async (route: Route) => {
      await held;
      await route.fulfill({ json: [listEvent(0)] });
    },
  });
  await page.goto("/sources");
  // The registry answered first: its table is there, but no "0 records" beside the loading notice.
  await expect(page.getByRole("heading", { name: "Source registry" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Search records" })).toBeVisible();
  await expect(page.getByText(/\d+ records/)).toHaveCount(0);
  release();
  await expect(panel(page, "Event verification").getByRole("status")).toHaveText("1 event");
  await expect(page.getByText(/\d+ records/)).toHaveCount(0);
});

test("sources page keeps event titles readable on a phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page, {
    "/api/sources": registry,
    "/api/events": [
      listEvent(0, { summary_fact: "Circle’s Big Leadership Shift—New CFO appointed as the company expands" }),
    ],
  });
  await page.goto("/sources");
  const title = page.locator("article header h3").first();
  await expect(title).toBeVisible();
  const box = await title.boundingBox();
  expect(box?.width ?? 0).toBeGreaterThan(250);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("a development-stub X post is marked and has no live link", async ({ page }) => {
  await mockApi(page, {
    "/api/social": [
      {
        id: "stub-1",
        text: "Synthetic development post",
        author_username: "federalreserve",
        created_at: hoursAgo(2),
        url: "https://x.com/federalreserve/status/stub-1",
        official: true,
        stub: true,
        verification_status: "OFFICIAL STATEMENT",
      },
      {
        id: "1790000000000000000",
        text: "FOMC statement released",
        author_username: "federalreserve",
        created_at: hoursAgo(1),
        url: "https://x.com/federalreserve/status/1790000000000000000",
        official: true,
        stub: false,
        verification_status: "OFFICIAL STATEMENT",
      },
    ],
  });
  await page.goto("/social");
  const stub = page.locator("article", { hasText: "Synthetic development post" });
  await expect(stub).toContainText("DEVELOPMENT STUB");
  await expect(stub.getByRole("link", { name: "Open on X" })).toHaveCount(0);
  const real = page.locator("article", { hasText: "FOMC statement released" });
  await expect(real).not.toContainText("DEVELOPMENT STUB");
  await expect(real.getByRole("link", { name: "Open on X" })).toHaveAttribute(
    "href",
    "https://x.com/federalreserve/status/1790000000000000000",
  );
  await expect(page.getByText("at least one trusted source partially supports it", { exact: false })).toBeVisible();
});

/** A Benzinga headline stored with a character reference and official excerpts stored as feed HTML (REG-events-1, -6). */
const markupEvent = {
  ...listEvent(5),
  id: "ev-markup",
  summary_fact: "Inquiry Into Apple's Competitor Dynamics In Technology Hardware, Storage &amp; Peripherals Industry",
  sources: ["The White House"],
  evidence: [
    {
      id: "doc-wh",
      source: "The White House",
      url: "https://www.whitehouse.gov/presidential-actions/2026/09/hunting-and-fishing-day/",
      title: "National Hunting and Fishing Day, 2026 &#8211; Proclamation",
      excerpt:
        "<p>For over 250 years, America’s hunters … lands and waters. &#160;On National Hunting and Fishing Day&#8230;</p><p>Posted <time datetime=\"2026-07-22T14:38:46-04:00\">July 22, 2026</time></p>",
      published_at: hoursAgo(6),
      ingested_at: hoursAgo(6),
      relation: "supports",
      official: true,
      group: "whitehouse",
      tier: 1,
    },
    {
      id: "doc-sls",
      source: "OFAC Sanctions List Service",
      url: "https://sanctionslistservice.ofac.treas.gov/changes/984",
      title: "",
      excerpt:
        "OFAC Sanctions List Service publication 984 (Standard Action) at 2026-09-23T14:07:28.650545+00:00: 2 removals. Programs matched: SANCTIONS_RUSSIA, SANCTIONS_VENEZUELA",
      published_at: hoursAgo(6),
      ingested_at: hoursAgo(6),
      relation: "supports",
      official: true,
      group: "ofac",
      tier: 1,
    },
  ],
};

test("feed markup, character references and topic codes never reach the event pages", async ({ page }) => {
  await mockApi(page, { "/api/events": [markupEvent], "/api/events/ev-markup": markupEvent, "/api/sources": [] });
  await page.goto("/events");
  const row = page.locator(".event-row").first();
  await expect(row).toContainText("Storage & Peripherals Industry");
  await expect(row).not.toContainText("&amp;");

  await page.goto("/events/ev-markup");
  const fact = page.getByRole("region", { name: "Fact", exact: true });
  await expect(fact.getByRole("heading", { level: 2 })).toHaveText(
    "Inquiry Into Apple's Competitor Dynamics In Technology Hardware, Storage & Peripherals Industry",
  );
  // The tab title clamps the headline to 70 characters; it carries no character references either.
  await expect(page).toHaveTitle(
    /^Inquiry Into Apple's Competitor Dynamics In Technology Hardware… · Event evidence · Market Intelligence$/,
  );
  await expect(page).not.toHaveTitle(/&amp;/);
  const sources = panel(page, "Sources");
  await expect(sources).toContainText(
    "For over 250 years, America’s hunters … lands and waters. On National Hunting and Fishing Day… Posted July 22, 2026",
  );
  await expect(sources).toContainText("National Hunting and Fishing Day, 2026 – Proclamation");
  await expect(sources).toContainText(
    "at Wed 23 Sep 17:07 (Bucharest): 2 removals. Programs matched: Sanctions / Russia, Sanctions / Venezuela",
  );
  for (const raw of ["<p>", "<time", "&#160;", "&#8230;", "&#8211;", "SANCTIONS_", "2026-09-23T"])
    await expect(page.locator("main")).not.toContainText(raw);
  // The "What happened" fallback (no extraction) reads the same plain text.
  await expect(panel(page, "What happened")).toContainText("For over 250 years");
  await expect(panel(page, "What happened")).not.toContainText("<p>");

  await page.goto("/sources");
  await expect(page.locator("article header h3").first()).toHaveText(
    "Inquiry Into Apple's Competitor Dynamics In Technology Hardware, Storage & Peripherals Industry",
  );
});

/** Two versions of one Benzinga story under one address, and a note merged in from 11 days earlier (REG-events-5, EV-05). */
const BENZINGA = "https://www.benzinga.com/markets/tech/26/09/62013860/chip-stocks-slide-amid-us-curbs";
const revisedEvent = {
  ...listEvent(6),
  id: "ev-revised",
  summary_fact: "Chip Stocks Slide Amid US Curbs",
  event_time: "2026-09-28T06:40:00Z",
  first_seen_utc: "2026-09-28T06:45:00Z",
  same_story_window_hours: 18,
  evidence: [
    {
      id: "rev-1",
      source: "Benzinga",
      url: BENZINGA,
      title: "Chip Stocks Slide Amid US Curbs",
      excerpt: "First version.",
      published_at: "2026-09-28T06:40:00Z",
      ingested_at: "2026-09-28T06:45:00Z",
      relation: "supports",
      group: "benzinga",
      tier: 4,
    },
    {
      id: "rev-2",
      source: "Benzinga",
      url: BENZINGA,
      title: "Chip Stocks Slide Amid US Curbs (UPDATED)",
      excerpt: "Updated version.",
      published_at: "2026-09-28T06:40:00Z",
      ingested_at: "2026-09-28T07:50:00Z",
      relation: "supports",
      group: "benzinga",
      tier: 4,
    },
    {
      id: "old-release",
      source: "Benzinga",
      url: "https://www.benzinga.com/markets/tech/26/09/61000000/chip-stocks-weekly",
      title: "Chip stocks weekly",
      excerpt: "An earlier weekly note.",
      published_at: "2026-09-17T06:40:00Z",
      ingested_at: "2026-09-27T05:29:00Z",
      relation: "supports",
      group: "benzinga",
      tier: 4,
    },
  ],
};

test("an updated version is named as such and a document from another date is flagged", async ({ page }) => {
  await mockApi(page, { "/api/events/ev-revised": revisedEvent, "/api/events": [revisedEvent], "/api/sources": [] });
  await page.goto("/events/ev-revised");
  const sources = panel(page, "Sources");
  await expect(sources.locator(".evidence")).toHaveCount(2);
  const revised = sources.locator(".evidence").first();
  await expect(revised).toContainText(
    "Updated version received Mon 28 Sep 10:50 (Bucharest); the latest is shown, counted once",
  );
  await expect(revised).not.toContainText("http/https");
  await expect(revised).toContainText("Chip Stocks Slide Amid US Curbs (UPDATED)");
  await expect(revised).toContainText("Updated version.");
  await expect(revised.getByRole("note")).toHaveCount(0);
  const older = sources.locator(".evidence").last();
  await expect(older.getByRole("note")).toHaveText(
    "Published 11 days before this event: probably a separate release, not another report of it.",
  );
  await expect(panel(page, "Verification status")).toContainText(
    "1 document was published on another date: probably a separate release, not another report of this event.",
  );

  await page.goto("/sources");
  const evidence = page.getByRole("table", { name: /^Evidence for / });
  await expect(evidence.locator("tbody tr")).toHaveCount(2);
  await expect(evidence.locator("tbody tr").first()).toContainText(
    "Updated version received Mon 28 Sep 10:50 (Bucharest)",
  );
  await expect(evidence.locator("tbody tr").last()).toContainText("Published 11 days before this event");
});

test("without the API's same-story window no document is flagged as another release", async ({ page }) => {
  const legacy = { ...revisedEvent, id: "ev-no-window", same_story_window_hours: undefined };
  await mockApi(page, { "/api/events/ev-no-window": legacy });
  await page.goto("/events/ev-no-window");
  await expect(panel(page, "Sources").locator(".evidence")).toHaveCount(2);
  await expect(page.getByText("probably a separate release")).toHaveCount(0);
});

test("the X feed shows no count and no empty state while loading", async ({ page }) => {
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await mockApi(page, {
    "/api/social": async (route: Route) => {
      await held;
      await route.fulfill({
        json: [
          { id: "1790000000000000001", text: "Rates unchanged", author_username: "federalreserve", created_at: hoursAgo(1) },
        ],
      });
    },
  });
  await page.goto("/social");
  await expect(page.getByText("Loading market intelligence…")).toBeVisible();
  await expect(page.getByText(/\d+ records/)).toHaveCount(0);
  await expect(page.getByText("No posts", { exact: true })).toHaveCount(0);
  release();
  await expect(page.locator("article", { hasText: "Rates unchanged" })).toBeVisible();
  await expect(page.locator(".toolbar [role=status]")).toHaveText("1 records");
});

test("the X feed states the error, not '0 records' or 'No posts'", async ({ page }) => {
  await mockApi(page, { "/api/social": { status: 500, json: { detail: "boom" } } });
  await page.goto("/social");
  // The feed retries twice (1 s, then 2 s) before it reports the failure.
  await expect(page.getByRole("main").getByRole("alert").first()).toContainText("HTTP 500", { timeout: 15_000 });
  await expect(page.getByText(/\d+ records/)).toHaveCount(0);
  await expect(page.getByText("No posts", { exact: true })).toHaveCount(0);
});

test("asset tags in an event row have a 24 px tap area on a phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const eth = { symbol: "ETH", name: "Ether", asset_class: "crypto", quote: null, decision: null };
  await mockApi(page, {
    "/api/dashboard": { ...dashboard, assets: [...dashboard.assets, eth] },
    "/api/events": [listEvent(0, { assets: ["BTC", "ETH"] })],
  });
  await page.goto("/events");
  const row = page.locator(".event-row").first();
  const tag = row.getByRole("link", { name: "BTC", exact: true });
  await expect(tag).toHaveAttribute("href", "/assets/BTC");
  await expect(row.getByRole("link", { name: "ETH", exact: true })).toHaveAttribute("href", "/assets/ETH");
  const box = (await tag.boundingBox())!;
  // A near miss (4 px outside the text, as a finger lands) still opens the asset, not the event behind it.
  const hrefAt = (x: number, y: number) =>
    page.evaluate(
      ([px, py]) => document.elementFromPoint(px, py)?.closest("a")?.getAttribute("href") ?? null,
      [x, y],
    );
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  expect(await hrefAt(cx, box.y + box.height + 4)).toBe("/assets/BTC");
  expect(await hrefAt(cx, box.y - 4)).toBe("/assets/BTC");
  expect(await hrefAt(box.x + box.width + 4, cy)).toBe("/assets/BTC");
  const area = await tag.evaluate((el) => {
    const after = getComputedStyle(el, "::after");
    return { width: parseFloat(after.width), height: parseFloat(after.height) };
  });
  expect(area.width).toBeGreaterThanOrEqual(24);
  expect(area.height).toBeGreaterThanOrEqual(24);
  // Neighbouring tags keep their areas apart: the point halfway between them opens neither asset.
  const next = (await row.getByRole("link", { name: "ETH", exact: true }).boundingBox())!;
  expect(next.x - (box.x + box.width)).toBeGreaterThanOrEqual(12);
  expect(await hrefAt((box.x + box.width + next.x) / 2, cy)).toBe("/events/ev-0");
  // Well away from the tags, the row still opens the event.
  expect(await hrefAt(next.x + next.width + 40, cy)).toBe("/events/ev-0");
});
