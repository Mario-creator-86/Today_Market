import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { test, expect, type Page } from "@playwright/test";

/*
 * The event-aware dashboard against REAL backend output: a serialized
 * `event_risk.snapshot()` written by the backend test suite to
 * tests/fixtures/event-risk.real.json, loaded verbatim (no field is edited
 * here). Hand-written fixtures in event-aware.spec.ts cannot catch a contract
 * mismatch; these specs can. Skipped until the file exists.
 *
 * Accepted file shapes: a dashboard payload ({event_risk, interpretation, ...}),
 * an event_risk block itself ({generated_at, mode, ...}), or a map of named
 * scenarios whose values are either of those.
 */
const FIXTURE = path.join(__dirname, "fixtures", "event-risk.real.json");

type Json = Record<string, unknown>;
type Scenario = { name: string; risk: Json; interpretation: Json | null; dashboard: Json | null };

const isObject = (v: unknown): v is Json => !!v && typeof v === "object" && !Array.isArray(v);
const isBlock = (v: unknown): v is Json => isObject(v) && ("mode" in v || "next_event" in v || "trade_watch" in v);

function scenario(name: string, raw: Json): Scenario | null {
  if (isBlock(raw.event_risk))
    return {
      name,
      risk: raw.event_risk,
      interpretation: isObject(raw.interpretation) ? raw.interpretation : null,
      dashboard: Array.isArray(raw.assets) ? raw : null,
    };
  if (isBlock(raw)) return { name, risk: raw, interpretation: null, dashboard: null };
  return null;
}

function load(): Scenario[] {
  if (!existsSync(FIXTURE)) return [];
  const raw = JSON.parse(readFileSync(FIXTURE, "utf8")) as unknown;
  if (!isObject(raw)) return [];
  const single = scenario("snapshot", raw);
  if (single) return [single];
  return Object.entries(raw)
    .map(([name, value]) => (isObject(value) ? scenario(name, value) : null))
    .filter((s): s is Scenario => !!s);
}

const SCENARIOS = load();

const events = (value: unknown): Json[] =>
  Array.isArray(value) ? value.filter((e): e is Json => isObject(e) && typeof e.event_id === "string") : [];

function allEvents(risk: Json): Json[] {
  return [risk.next_event, risk.dominant_event, ...events(risk.events), ...events(risk.active_events)].filter(
    (e): e is Json => isObject(e) && typeof e.event_id === "string",
  );
}

/** The dashboard to serve: the file's own payload when it is one, else a minimal shell around the real block. */
function payload(s: Scenario): Json {
  if (s.dashboard) return s.dashboard;
  const at = typeof s.risk.generated_at === "string" ? s.risk.generated_at : new Date().toISOString();
  return {
    as_of: at,
    timezone: "Europe/Bucharest",
    regimes: [],
    assets: [],
    events: [],
    calendar: [],
    providers: [],
    alerts: [],
    last_analysis: null,
    event_risk: s.risk,
    ...(s.interpretation ? { interpretation: s.interpretation } : {}),
  };
}

async function open(page: Page, s: Scenario) {
  const body = payload(s);
  const at = Date.parse(String(s.risk.generated_at ?? ""));
  if (Number.isFinite(at)) await page.clock.install({ time: new Date(at) });
  await page.routeWebSocket("**/ws", (ws) => ws.close());
  await page.route("**/api/**", (route) => {
    const p = new URL(route.request().url()).pathname;
    return route.fulfill({ json: p === "/api/dashboard" ? body : [] });
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Today's market" })).toBeVisible();
  await expect(page.locator(".regime")).toBeVisible();
  for (const label of ["More context and scenarios", "Active events and measured reactions"]) {
    await page.getByText(label, { exact: true }).click();
  }
}

const region = (page: Page, name: string) => page.getByRole("region", { name, exact: true });
const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

test.describe("real backend snapshot", () => {
  if (!SCENARIOS.length)
    test("loads tests/fixtures/event-risk.real.json", () => {
      test.skip(true, `no real snapshot at ${FIXTURE} yet`);
    });

  for (const s of SCENARIOS) {
    test(`${s.name}: next event, expectation literals and labels render from the real shape`, async ({ page }) => {
      await open(page, s);
      const next = isObject(s.risk.next_event) ? s.risk.next_event : null;
      const card = region(page, "Next key event");
      if (!next) {
        await expect(card).toContainText("No upcoming market-moving event in this window.");
        return;
      }
      await expect(card.getByRole("heading", { name: String(next.name), exact: true })).toBeVisible();
      const consensus = next.consensus;
      // Missing expectations are said in plain words on the card's one expectations line.
      const expect_ = card.locator(".event-expect");
      if (consensus === "NO CONSENSUS") await expect(expect_).toContainText("Consensus: not available");
      else if (isObject(consensus)) {
        // The label is shown as sent ("Consensus", "Finnhub estimate"), first letter capitalised.
        if (typeof consensus.label === "string" && consensus.label.trim())
          await expect(card).toContainText(new RegExp(escapeRegExp(consensus.label.trim()), "i"));
        for (const v of Array.isArray(consensus.values) ? consensus.values : [])
          if (isObject(v) && typeof v.name === "string" && v.value !== null && v.value !== undefined)
            await expect(card).toContainText(v.name);
      }
      if (next.market_pricing === "UNKNOWN") await expect(expect_).toContainText("Market pricing: unknown");
      // IMPACT is the calendar row's own number when the event carries one; else relevance / 10, labelled so.
      // Scoped to the next event's own impact: a dominant line for another (unscheduled) event may be relevance-based.
      const tenths = (n: number) => Math.max(0, Math.min(10, n)).toLocaleString("en-US", { maximumFractionDigits: 1 });
      const impact = card.locator(".next-event-impact");
      if (typeof next.impact === "number") {
        await expect(impact).toHaveText(`IMPACT ${tenths(next.impact)}/10`);
        await expect(impact).not.toContainText("relevance-based");
      } else if (typeof next.impact_10 === "number")
        await expect(impact).toHaveText(`IMPACT ${tenths(next.impact_10)}/10 (relevance-based)`);
      // EVENT DATA INCOMPLETE only where the backend flags it.
      const flagged = (Array.isArray(s.risk.data_incomplete) ? s.risk.data_incomplete : []).some(
        (e) => e === next.event_id || (isObject(e) && e.event_id === next.event_id),
      );
      if (flagged) await expect(card).toContainText("EVENT DATA INCOMPLETE");
      else await expect(card).not.toContainText("EVENT DATA INCOMPLETE");
      // Date-only rows never count down.
      if (next.time_known === false) await expect(card.getByRole("timer")).toHaveCount(0);
    });

    test(`${s.name}: decisions, units, statuses and timing are shown as the backend sends them`, async ({ page }) => {
      await open(page, s);
      const body = page.locator("body");
      // "Decision published" only when a live decision exists.
      const decisionLive = allEvents(s.risk).some(
        (e) => e.is_decision === true && ["RELEASED", "IN_PROGRESS", "POST_EVENT"].includes(String(e.phase)),
      );
      if (!decisionLive) await expect(body).not.toContainText("Decision published");
      await expect(body).not.toContainText("Time TBA · Time TBA");
      await expect(body).not.toContainText("inputs changed");
      if (!isObject(s.interpretation) || s.interpretation.source !== "ai") {
        await expect(body).not.toContainText("AI note");
        // The deterministic fallback's per-asset notes only restate the cards: none are shown.
        await expect(page.locator(".watch-comment")).toHaveCount(0);
      }
      // Adjusted conviction is never shown as a percent.
      await expect(body).not.toContainText("Adjusted confidence");

      // Post-event: basis-point rows never read as percent.
      const live = (Array.isArray(s.risk.post_event) ? s.risk.post_event : []).filter(
        (p): p is Json => isObject(p) && ["RELEASED", "IN_PROGRESS", "POST_EVENT"].includes(String(p.phase)),
      );
      for (const entry of live) {
        const reactions = isObject(entry.reactions) ? entry.reactions : {};
        const units = isObject(entry.units) ? entry.units : {};
        for (const [symbol, cells] of Object.entries(reactions)) {
          const cellUnits = isObject(cells)
            ? Object.values(cells).map((c) => (isObject(c) ? c.unit : undefined))
            : [];
          const bp = units[symbol] === "bp" || cellUnits.includes("bp");
          const measured = isObject(cells) && Object.values(cells).some((c) => typeof c === "number" || (isObject(c) && typeof c.change === "number"));
          if (!bp || !measured) continue;
          const row = region(page, "Post-event reaction")
            .getByRole("row")
            .filter({ has: page.getByRole("cell", { name: symbol, exact: true }) })
            .first();
          await expect(row).toContainText(" bp");
          await expect(row).not.toContainText("%");
        }
      }

      // Trade watch: one card per backend item (at most eight).
      const watch = Array.isArray(s.risk.trade_watch) ? s.risk.trade_watch.filter((t) => isObject(t) && t.asset) : [];
      if (watch.length) await expect(page.locator(".watch-card")).toHaveCount(Math.min(8, watch.length));

      // Unscheduled events: timing ANYTIME, never a clock.
      for (const e of events(s.risk.active_events))
        await expect(page.locator("section").filter({ hasText: "Active event risk" }).first()).toContainText(String(e.name));

      // Risk mode banner when the backend says so.
      if (isObject(s.risk.mode) && s.risk.mode.active) await expect(region(page, "Event risk mode")).toBeVisible();
      else await expect(region(page, "Event risk mode")).toHaveCount(0);
    });

    test(`${s.name}: fits 390 px without horizontal scroll`, async ({ page }) => {
      await page.setViewportSize({ width: 390, height: 844 });
      await open(page, s);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    });
  }
});

/*
 * The opening screen (sections NOW, EVENTS, MARKETS, POSITIONING, SETUPS) against the dashboard payload the
 * backend served on Friday 2 Oct 2026 at 10:34 Bucharest (tests/fixtures/dashboard.opening.json: trimmed, no value
 * edited): a quiet morning before the US employment report, a close-based regime, no current observations, five
 * assets "not confirming" with the same reasons, US equities closed, nothing actionable. The old layout needed
 * 2,720 px and about 1,000 words for it and said several facts two or three times.
 */
const OPENING = path.join(__dirname, "fixtures", "dashboard.opening.json");

test.describe("opening screen on a real dashboard payload", () => {
  const body = JSON.parse(readFileSync(OPENING, "utf8")) as Json;
  const risk = body.event_risk as Json;
  const next = risk.next_event as Json;

  async function openDashboard(page: Page, width: number, height: number) {
    await page.setViewportSize({ width, height });
    await page.clock.install({ time: new Date(String(body.as_of)) });
    await page.routeWebSocket("**/ws", (ws) => ws.close());
    await page.route("**/api/**", (route) => {
      const p = new URL(route.request().url()).pathname;
      return route.fulfill({ json: p === "/api/dashboard" ? body : [] });
    });
    await page.goto("/");
    await expect(page.locator(".regime")).toBeVisible();
    await expect(region(page, "Next key event")).toBeVisible();
  }

  /** Sections 1-5: the text a reader sees between the hero and the first collapsed section, and their height. */
  const opening = (page: Page) =>
    page.evaluate(() => {
      const main = document.querySelector("main")!;
      const start = main.querySelector(".regime")!.getBoundingClientRect().top;
      const end = main.querySelector("details.dashboard-details")!.getBoundingClientRect().top;
      const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
      const texts: string[] = [];
      while (walker.nextNode()) {
        const el = walker.currentNode.parentElement;
        const text = walker.currentNode.textContent?.trim();
        if (!el || !text || el.closest(".sr-only")) continue;
        // Text inside a closed disclosure is not on screen (its summary is).
        if (el.closest("details:not([open])") && !el.closest("summary")) continue;
        const box = el.getBoundingClientRect();
        if (!box.width || !box.height || box.top < start || box.top >= end) continue;
        texts.push(text);
      }
      const text = texts.join(" ");
      return { text, words: text.split(/\s+/).filter((w) => /[A-Za-z0-9]/.test(w)).length, height: Math.round(end - start) };
    });

  const count = (text: string, needle: string | RegExp) =>
    text.match(needle instanceof RegExp ? needle : new RegExp(escapeRegExp(needle), "g"))?.length ?? 0;

  test("each fact is said once", async ({ page }) => {
    await openDashboard(page, 1440, 1000);
    const { text } = await opening(page);
    // The next key event is named by its card only: not by the hero's view, the timeline or the setups line.
    expect(count(text, String(next.name))).toBe(1);
    await expect(page.getByRole("note", { name: "Market view", exact: true })).toHaveText(
      "Mixed conditions driven by USD strength (mixed posture) describe historical context.",
    );
    await expect(region(page, "Events").getByRole("list", { name: "Coming up" })).not.toContainText(String(next.name));
    // What the five "not confirming" assets share is written once under the group, never per asset.
    expect(count(text, /Historical context only/g)).toBe(1);
    expect(count(text, /own evidence does not confirm/gi)).toBe(1);
    const group = region(page, "Not confirming");
    await expect(group.getByRole("listitem")).toHaveCount(5);
    await expect(group.locator(".positioning-note")).toHaveText(
      "Own evidence does not confirm (no aligned decision side or move) · Historical context only; current-session confirmation is unavailable (except GOLD)",
    );
    for (const item of await group.getByRole("listitem").all()) expect(count((await item.textContent()) ?? "", /\S+/g)).toBeLessThanOrEqual(4);
    // Closed markets, the missing consensus, the exposure list and the source of the view: once each.
    expect(count(text, /\bclosed\b/g)).toBe(1);
    expect(count(text, /consensus/gi)).toBe(1);
    expect(count(text, /most exposed/gi)).toBe(1);
    expect(count(text, /Deterministic/g)).toBe(1);
    expect(count(text, /EVENT DATA INCOMPLETE/g)).toBe(1);
    // Provenance and enum literals are not opening-screen text.
    for (const literal of ["Known:", "Time basis", "NO CONSENSUS", "UNKNOWN", "FRED release calendar", "as of "])
      expect(text).not.toContain(literal);
    // No asset is listed twice inside POSITIONING, and no event twice in the timeline.
    const symbols = await region(page, "Positioning").getByRole("link").allTextContents();
    expect(new Set(symbols).size).toBe(symbols.length);
    const upcoming = await region(page, "Events").locator(".events-name").allTextContents();
    expect(new Set(upcoming).size).toBe(upcoming.length);
    // A market tile is symbol, ONE tag, level and change.
    const tiles = page.locator(".market-tile");
    await expect(tiles).toHaveCount(13);
    for (const tile of await tiles.all()) {
      await expect(tile.locator(".badge")).toHaveCount(1);
      expect(((await tile.textContent()) ?? "").length).toBeLessThan(45);
    }
    await expect(tiles.nth(2)).toHaveText("S&P 500CLOSE Thu7,666.45+0.19%");
    await expect(tiles.nth(10)).toHaveText(/^GOLDPROXY via PAXG—[+-]\d+\.\d\d%$/);
  });

  test("the warnings the screen must keep are on it", async ({ page }) => {
    await openDashboard(page, 1440, 1000);
    // A close-based regime nothing current confirms: the basis tag and the hero's one warning line.
    await expect(page.locator(".now-eyebrow .badge").first()).toHaveText("from Thursday's close");
    await expect(page.locator(".now-warning")).toHaveText("Today's direction is not confirmed: no current observations yet.");
    await expect(page.locator(".now-warning")).toHaveCount(1);
    // Deterministic, labelled as such.
    await expect(page.locator('.now-eyebrow [data-source="deterministic"]')).toBeVisible();
    // Missing expectations for a VERY HIGH event.
    const card = region(page, "Next key event");
    await expect(card.locator(".event-expect")).toHaveText(
      "EVENT DATA INCOMPLETEConsensus: not available · Market pricing: unknown",
    );
    await expect(card.locator(".event-expect")).toBeVisible();
    await expect(card.locator(".next-event-explanation")).toHaveText(
      "It can matter more than the current regime for the next directional move.",
    );
    // The source of the schedule is one click away, not in the card's text.
    await card.getByText("Event timing and relevance details", { exact: true }).click();
    await expect(card.getByText(/^Source: FRED release calendar/)).toBeVisible();
    // Closed markets, and the FRED notice in the footer.
    await expect(region(page, "Positioning")).toContainText(
      "US equities closed: their event implications cannot be confirmed until they reopen.",
    );
    await expect(page.getByRole("list", { name: "Data provider notices" })).toContainText("FRED® API");
    // Nothing actionable: one line that refers to the key event instead of naming it again.
    await expect(page.locator(".setups-wait")).toHaveText(
      "Today's best setupsNo actionable setups— waiting for the next key event.WAIT",
    );
    // The numbers behind the arrows are one click away.
    await page.getByText("Data coverage and basis", { exact: true }).click();
    await expect(page.getByRole("list", { name: "Regime drivers" }).getByRole("listitem").first()).toHaveText(
      "Rates↓downUS2Y -10 bp, US10Y -5 bp· last close",
    );
  });

  test("sections 1-5 stay within the height and word budget at 1440 px", async ({ page }) => {
    await openDashboard(page, 1440, 1000);
    const { words, height } = await opening(page);
    expect(height).toBeLessThanOrEqual(1700);
    expect(words).toBeLessThanOrEqual(350);
    // The collapsed sections stay collapsed.
    await expect(page.locator("details.dashboard-details")).toHaveCount(5);
    await expect(page.locator("details.dashboard-details[open]")).toHaveCount(0);
    // One h1, then h2 sections; no heading level is skipped.
    const levels = await page.locator("main").locator("h1:visible, h2:visible, h3:visible, h4:visible").evaluateAll((nodes) =>
      nodes.map((n) => Number(n.tagName[1])),
    );
    expect(levels[0]).toBe(1);
    for (let i = 1; i < levels.length; i += 1) expect(levels[i] - levels[i - 1]).toBeLessThanOrEqual(1);
    await page.screenshot({ path: "test-results/opening-real-desktop.png", fullPage: true });
  });

  test("the same screen fits a phone without horizontal scroll", async ({ page }) => {
    await openDashboard(page, 390, 844);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const { words } = await opening(page);
    expect(words).toBeLessThanOrEqual(350);
    await expect(page.locator(".now-warning")).toBeVisible();
    await expect(region(page, "Next key event").locator(".event-expect")).toBeVisible();
    await page.screenshot({ path: "test-results/opening-real-phone.png", fullPage: true });
  });
});
