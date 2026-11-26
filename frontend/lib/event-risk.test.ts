import { describe, expect, it } from "vitest";

import type { EventObject, EventRisk, Interpretation } from "./api";
import {
  closedMarkets,
  commentLabel,
  convictionText,
  DETERMINISTIC_LABEL,
  direction,
  distinctRisks,
  dominantBeyondNext,
  eventDataStale,
  eventRiskError,
  eventRiskOf,
  expectation,
  exposureList,
  findEvent,
  impact10,
  impactText,
  impactView,
  incompleteFor,
  incompleteReasons,
  interpretationOf,
  interpretationSource,
  levelFromScore,
  levelOf,
  levelTone,
  liveMarketsNote,
  livePostEvents,
  modeRelevance,
  modeTarget,
  NO_CONSENSUS,
  openMarkets,
  reactionCell,
  reactionUnitFor,
  relevanceText,
  riskBadge,
  rowImpact,
  scenarioCondition,
  scenarioSets,
  staleText,
  eventSurpriseView,
  surpriseView,
  UNKNOWN,
  unresolvedSymbols,
  viewItems,
} from "./event-risk";

it("labels a current deterministic snapshot without claiming the AI provider is unavailable", () => {
  expect(interpretationSource({ source: "deterministic", label: "Deterministic summary — current snapshot" }).label)
    .toBe("Deterministic summary — current snapshot");
});

const NOW = Date.parse("2026-09-26T12:00:00Z");
const at = (minutes: number) => new Date(NOW + minutes * 60_000).toISOString();

const fed: EventObject = {
  event_id: "FED-20260926-FOMC",
  name: "FOMC rate decision",
  category: "CENTRAL_BANK",
  phase: "IMMINENT",
  time_utc: at(47),
  time_known: true,
  relevance_score: 96,
  relevance_level: "CRITICAL",
  affected_assets: [
    { symbol: "GOLD", level: "high", weight: 0.75 },
    { symbol: "US2Y", level: "very_high", weight: 1 },
    { symbol: "BTC", level: "high", weight: 0.75 },
  ],
  consensus: { rate_change: "+25bp" },
  market_pricing: { hike_probability: 0.78, hold_probability: 0.22 },
};

const block: EventRisk = {
  generated_at: at(0),
  mode: { active: true, event_id: fed.event_id, impact_10: 10, minutes_until: 47 },
  next_event: fed,
  events: [fed, { event_id: "CPI", name: "US CPI", relevance_score: 64 }],
  asset_event_risk: { BTC: { score: 92, level: "CRITICAL", event_id: fed.event_id, minutes_until: 47 } },
};

describe("missing data stays missing", () => {
  it("NO CONSENSUS and UNKNOWN literals", () => {
    expect(expectation("NO CONSENSUS", NO_CONSENSUS)).toMatchObject({ missing: true, text: NO_CONSENSUS });
    expect(expectation(null, NO_CONSENSUS)).toMatchObject({ missing: true, text: NO_CONSENSUS });
    expect(expectation({}, NO_CONSENSUS)).toMatchObject({ missing: true, text: NO_CONSENSUS });
    expect(expectation(undefined, UNKNOWN)).toMatchObject({ missing: true, text: UNKNOWN });
    expect(expectation("UNKNOWN", UNKNOWN)).toMatchObject({ missing: true, text: UNKNOWN });
  });
  it("formats backend values without inventing any", () => {
    expect(expectation(fed.consensus, NO_CONSENSUS)).toEqual({
      missing: false,
      text: null,
      lines: [{ label: "Rate change", value: "+25bp" }],
      label: null,
      caption: null,
    });
    expect(expectation(fed.market_pricing, UNKNOWN).lines).toEqual([
      { label: "Hike probability", value: "78%" },
      { label: "Hold probability", value: "22%" },
    ]);
    const earnings = expectation({ eps_estimate: 1.42, source: "finnhub" }, NO_CONSENSUS);
    expect(earnings.lines).toEqual([{ label: "Eps estimate", value: "1.42" }]);
    expect(earnings.caption).toBe("Source: finnhub");
    expect(expectation({ headline: { value: 2.8, unit: "%" } }, NO_CONSENSUS).lines).toEqual([
      { label: "Headline", value: "2.8%" },
    ]);
    expect(expectation({ summary: "Hawkish outcome partially priced" }, UNKNOWN).text).toBe(
      "Hawkish outcome partially priced",
    );
  });
  it("§8 consensus: label kept, units attached, previous never an expectation", () => {
    // Contract shape: {label, values: [{name, value, unit}], source}.
    expect(
      expectation(
        {
          label: "Finnhub estimate",
          values: [
            { name: "EPS", value: 2.02, unit: "USD" },
            { name: "Revenue", value: 1.2e11, unit: "USD" },
          ],
          source: "finnhub",
        },
        NO_CONSENSUS,
      ),
    ).toEqual({
      missing: false,
      text: null,
      lines: [
        { label: "EPS", value: "2.02 USD" },
        { label: "Revenue", value: "120B USD" },
      ],
      label: "Finnhub estimate",
      caption: "Source: finnhub",
    });
    const cpi = expectation(
      { label: "Consensus", values: [{ name: "CPI YoY", value: 3.1, unit: "%" }], source: "fmp" },
      NO_CONSENSUS,
    );
    expect(cpi.lines).toEqual([{ label: "CPI YoY", value: "3.1%" }]);
    expect(cpi.label).toBe("Consensus");
    // No values at all is no consensus, whatever the label says.
    expect(expectation({ label: "Consensus", values: [], source: "fmp" }, NO_CONSENSUS)).toMatchObject({
      missing: true,
      text: NO_CONSENSUS,
    });
  });
  it("older flat consensus rows (real backend output before §8) are read the same way", () => {
    // backend consensus_of for an FMP CPI row: value and unit belong together; previous is last period's actual.
    const macro = expectation({ value: 3.1, unit: "%", previous: 3.0, label: "consensus", source: "fmp" }, NO_CONSENSUS);
    expect(macro.lines).toEqual([{ label: "Headline", value: "3.1%" }]);
    expect(macro.label).toBe("Consensus");
    expect(macro.caption).toBe("Source: fmp");
    expect(macro.text).toBeNull(); // nothing else, previous (3.0) included, is rendered
    // Finnhub earnings: the "Finnhub estimate" label survives the source key that follows it.
    const finnhub = expectation(
      {
        label: "Finnhub estimate",
        eps: 2.02,
        revenue: 1.2e11,
        note: "Finnhub's estimates include its own proprietary estimates; not Street consensus",
        source: "finnhub",
      },
      NO_CONSENSUS,
    );
    expect(finnhub.label).toBe("Finnhub estimate");
    expect(finnhub.caption).toBe("Source: finnhub");
    expect(finnhub.lines).toEqual([
      { label: "Eps", value: "2.02" },
      { label: "Revenue", value: "120B" },
    ]);
    expect(finnhub.text).toMatch(/not Street consensus/);
  });
  it("lists why event data is incomplete", () => {
    expect(incompleteReasons({ ...fed, data_incomplete: ["consensus_missing"] })).toEqual(["consensus missing"]);
    expect(incompleteReasons(fed)).toEqual([]);
    expect(incompleteFor({ data_incomplete: [{ event_id: fed.event_id, reasons: ["pricing_unknown"] }] }, fed)).toEqual({
      flagged: true,
      reasons: ["pricing unknown"],
    });
    expect(incompleteFor({ data_incomplete: [fed.event_id] }, fed).flagged).toBe(true);
    expect(incompleteFor({ data_incomplete: ["OTHER"] }, fed).flagged).toBe(false);
  });
  it("EVENT DATA INCOMPLETE is the backend's flag only (its configured levels), never the event's own reasons", () => {
    // A HIGH date-only earnings row carries EXACT TIME UNKNOWN; the backend does not flag it at HIGH.
    const earnings: EventObject = {
      event_id: "EARN-20261028-AAPL-EARNINGS",
      name: "AAPL earnings",
      relevance_level: "HIGH",
      data_incomplete: ["EXACT TIME UNKNOWN"],
    };
    expect(incompleteFor({ data_incomplete: [] }, earnings)).toEqual({
      flagged: false,
      reasons: ["EXACT TIME UNKNOWN"],
    });
    expect(incompleteFor({}, earnings).flagged).toBe(false);
    // The backend's entry (CRITICAL / VERY HIGH) flags it.
    expect(
      incompleteFor(
        { data_incomplete: [{ event_id: earnings.event_id, level: "VERY HIGH", reasons: ["EXACT TIME UNKNOWN"] }] },
        earnings,
      ),
    ).toEqual({ flagged: true, reasons: ["EXACT TIME UNKNOWN"] });
  });
});

describe("levels and impact", () => {
  it("uses the backend bands", () => {
    expect([95, 80, 65, 45, 10].map(levelFromScore)).toEqual(["CRITICAL", "VERY HIGH", "HIGH", "MODERATE", "LOW"]);
    expect(levelOf("VERY_HIGH")).toBe("VERY HIGH");
    expect(levelOf(null, 91)).toBe("CRITICAL");
    expect(levelOf(null, null)).toBeNull();
    expect(levelTone("CRITICAL")).toBe("negative");
    expect(levelTone("HIGH")).toBe("neutral");
    expect(levelTone("MODERATE")).toBe("info");
  });
  it("impact out of 10 comes from the backend", () => {
    expect(impact10(fed, block.mode)).toBe(10);
    expect(impact10({ ...fed, impact_10: 9.4 })).toBe(9.4);
    expect(impact10({ event_id: "x", name: "x", relevance_score: 64 })).toBe(6.4);
    expect(impact10({ event_id: "x", name: "x" })).toBeNull();
    expect(impactText(10)).toBe("10/10");
    expect(impactText(9.44)).toBe("9.4/10");
    expect(impactText(null)).toBeNull();
  });
  it("IMPACT is the calendar row's impact, the same number the calendar shows", () => {
    // PCE: calendar impact 9, relevance 69.6 → impact_10 7. The card must say 9, not 7.
    const pce = { event_id: "BEA-PCE", name: "US PCE", impact: 9, impact_10: 7, relevance_score: 69.6 } as EventObject;
    expect(rowImpact(pce)).toBe(9);
    expect(impactView(pce, null)).toEqual({ text: "9/10", relevanceBased: false });
    expect(relevanceText(pce)).toBe("70/100");
    // A zero impact is a measured value, not a missing one.
    expect(impactView({ ...pce, impact: 0 } as EventObject)).toEqual({ text: "0/10", relevanceBased: false });
  });
  it("without a calendar impact, relevance / 10 is used and flagged as relevance-based", () => {
    expect(rowImpact(fed)).toBeNull();
    expect(impactView({ ...fed, impact_10: 9.4 })).toEqual({ text: "9.4/10", relevanceBased: true });
    // The mode's impact_10 for the same event, else relevance / 10.
    expect(impactView(fed, block.mode)).toEqual({ text: "10/10", relevanceBased: true });
    expect(impactView({ event_id: "x", name: "x", relevance_score: 64 })).toEqual({ text: "6.4/10", relevanceBased: true });
    expect(impactView({ event_id: "x", name: "x" })).toEqual({ text: null, relevanceBased: false });
    expect(relevanceText({ relevance_score: null })).toBeNull();
  });
  it("a mode whose event the block does not list uses its own impact, else its impact_10", () => {
    const mode = { active: true, event_id: "GONE", impact_10: 8.2 };
    expect(impactView(null, mode)).toEqual({ text: "8.2/10", relevanceBased: true });
    expect(impactView(null, { ...mode, impact: 10 } as typeof mode)).toEqual({ text: "10/10", relevanceBased: false });
    // Another event's mode never lends its impact.
    expect(impactView({ event_id: "x", name: "x", relevance_score: 50 }, { ...mode, impact: 10 } as typeof mode)).toEqual({
      text: "5/10",
      relevanceBased: true,
    });
  });
  it("the risk-mode banner's relevance: the mode's own, else the listed event's", () => {
    // The backend mode carries impact, relevance_score and relevance_level (C10).
    const mode = { active: true, event_id: "GONE", impact: 8, impact_10: 8.5, relevance_score: 84.6, relevance_level: "VERY HIGH" };
    expect(modeRelevance(mode, null)).toEqual({ text: "85/100", level: "VERY HIGH" });
    expect(impactView(null, mode)).toEqual({ text: "8/10", relevanceBased: false });
    // An older mode without relevance fields: the listed event's.
    expect(modeRelevance(block.mode, fed)).toEqual({ text: "96/100", level: "CRITICAL" });
    // A level alone, or a score alone (its band).
    expect(modeRelevance({ active: true, relevance_level: "HIGH" } as typeof mode, null)).toEqual({ text: null, level: "HIGH" });
    expect(modeRelevance({ active: true, relevance_score: 91 } as typeof mode, null)).toEqual({ text: "91/100", level: "CRITICAL" });
    // Nothing known: nothing shown.
    expect(modeRelevance({ active: true }, null)).toEqual({ text: null, level: null });
    expect(modeRelevance(null, null)).toEqual({ text: null, level: null });
  });
  it("adjusted conviction is on the 0-100 score scale, never a percent", () => {
    expect(convictionText(42)).toBe("42/100");
    expect(convictionText(0)).toBe("0/100");
    expect(convictionText(41.6)).toBe("42/100");
    expect(convictionText(null)).toBeNull();
    expect(convictionText(undefined)).toBeNull();
  });
  it("most exposed assets first", () => {
    expect(exposureList(fed).map((a) => a.symbol)).toEqual(["US2Y", "GOLD", "BTC"]);
  });
});

describe("lookups", () => {
  it("parses blocks defensively", () => {
    expect(eventRiskOf({})).toBeNull();
    expect(eventRiskOf({ event_risk: "nope" })).toBeNull();
    expect(eventRiskOf({ event_risk: block })).toBe(block);
    expect(interpretationOf({ interpretation: { output: {} } })).toBeNull();
  });
  it("finds events and the risk-mode target from time_utc", () => {
    expect(findEvent(block, "CPI")?.name).toBe("US CPI");
    expect(findEvent(block, "missing")).toBeNull();
    expect(modeTarget(block)).toBe(NOW + 47 * 60_000);
    // Without the event, generated_at + minutes_until (both backend values).
    expect(modeTarget({ ...block, next_event: null, events: [] })).toBe(NOW + 47 * 60_000);
    expect(modeTarget({ ...block, mode: { active: false } })).toBeNull();
    // The mode's own time_utc (§8) is the target.
    expect(modeTarget({ ...block, mode: { ...block.mode!, time_utc: at(30) } })).toBe(NOW + 30 * 60_000);
    // Unscheduled events never count down.
    const hormuz: EventObject = { event_id: "GEO-1", name: "Hormuz", timing: "ANYTIME", time_known: false };
    expect(
      modeTarget({ ...block, active_events: [hormuz], mode: { active: true, event_id: "GEO-1", minutes_until: 5 } }),
    ).toBeNull();
  });
  it("surfaces the dominant event only when it is not the next one", () => {
    expect(dominantBeyondNext({ ...block, dominant_event: fed })).toBeNull();
    const claims: EventObject = { event_id: "US-CLAIMS", name: "Initial jobless claims", relevance_score: 66 };
    expect(dominantBeyondNext({ ...block, next_event: claims, dominant_event: fed })).toBe(fed);
    expect(dominantBeyondNext({ ...block, next_event: null, dominant_event: fed })).toBe(fed);
    expect(dominantBeyondNext({ ...block, dominant_event: null })).toBeNull();
  });
  it("graceful degradation: an event-risk failure is a quiet notice, never a crash", () => {
    expect(eventRiskError({ event_risk: null, event_risk_error: "rules fragment invalid" })).toBe("rules fragment invalid");
    expect(eventRiskError({ event_risk: null, event_risk_error: "" })).toBe("");
    expect(eventRiskError({ event_risk: null, event_risk_error: "x".repeat(500) })?.length).toBe(200);
    // Nothing failed: no notice (older backends without the layer included).
    expect(eventRiskError({})).toBeNull();
    expect(eventRiskError({ event_risk: block, event_risk_error: "ignored" })).toBeNull();
    const failed = { event_risk: null, event_risk_error: "boom" };
    expect(eventRiskOf(failed)).toBeNull();
  });
  it("per-asset risk badges with the driving event", () => {
    expect(riskBadge(block, "BTC")).toEqual({ score: 92, level: "CRITICAL", event: fed });
    expect(riskBadge(block, "BTC", 70)?.level).toBe("HIGH");
    expect(riskBadge(block, "ETH")).toBeNull();
  });
  it("flags stalled event data", () => {
    expect(eventDataStale(block, NOW + 60_000)).toBe(false);
    expect(eventDataStale(block, NOW + 10 * 60_000)).toBe(true);
  });
});

describe("market view and interpretation", () => {
  it("caps lists at five and never forces an empty one", () => {
    const many = ["A", "B", "C", "D", "E", "F"].map((symbol) => ({ symbol }));
    expect(viewItems(many)).toHaveLength(5);
    expect(viewItems([])).toEqual([]);
    expect(viewItems(undefined)).toEqual([]);
  });
  it("labels the source", () => {
    const ai: Interpretation = { source: "ai", model: "qwen3:8b" };
    expect(interpretationSource(ai)).toEqual({ label: "AI · qwen3:8b", deterministic: false });
    expect(interpretationSource({ source: "deterministic" })).toEqual({
      label: DETERMINISTIC_LABEL,
      deterministic: true,
    });
    expect(interpretationSource(null).deterministic).toBe(true);
  });
  it("labels per-asset comments by source: template text is never an AI note", () => {
    expect(commentLabel({ source: "ai", model: "m" })).toBe("AI note");
    expect(commentLabel({ source: "deterministic" })).toBe("Deterministic note");
    expect(commentLabel(null)).toBe("Deterministic note");
  });
  it("the STALE badge states the age, never that inputs changed", () => {
    const generated = at(-23);
    expect(staleText({ source: "ai", stale: false, generated_at: generated }, NOW)).toBeNull();
    expect(staleText({ source: "ai", stale: true, generated_at: generated }, NOW)).toBe("STALE · older than 23 min");
    expect(staleText({ source: "ai", stale: true, age_seconds: 1500 }, NOW)).toBe("STALE · older than 25 min");
    expect(
      staleText({ source: "ai", stale: true, generated_at: generated, stale_after_seconds: 600 }, NOW),
    ).toBe("STALE · older than 10 min");
    expect(staleText({ source: "ai", stale: true }, NOW)).toBe("STALE");
  });
  it("ages a cached fresh interpretation only after its backend deadline", () => {
    const value: Interpretation = { source: "ai", stale: false, generated_at: at(0), stale_after_seconds: 420 };
    expect(staleText(value, NOW + 420_000)).toBeNull();
    expect(staleText(value, NOW + 420_001)).toBe("STALE · older than 7 min");
    expect(value.stale).toBe(false);
    expect(staleText({ ...value, source: "deterministic" }, NOW + 420_001)).toBe("STALE · older than 7 min");
  });
  it.each([
    ["2026-03-29T02:55:00+02:00", "2026-03-29T04:05:01+03:00", 600, "10"],
    ["2026-10-25T03:50:00+03:00", "2026-10-25T03:10:01+02:00", 1200, "20"],
  ])("ages elapsed UTC time across Bucharest DST changes (%s)", (generated, current, seconds, minutes) => {
    expect(staleText({ source: "ai", stale: false, generated_at: generated, stale_after_seconds: seconds }, Date.parse(current)))
      .toBe(`STALE · older than ${minutes} min`);
  });
  it("preserves backend staleness without inventing a client threshold", () => {
    const value: Interpretation = { source: "ai", stale: false, generated_at: at(-120) };
    expect(staleText(value, NOW)).toBeNull();
    for (const limit of [null, -1, Number.NaN, Number.POSITIVE_INFINITY])
      expect(staleText({ ...value, stale_after_seconds: limit }, NOW)).toBeNull();
    expect(staleText({ ...value, generated_at: "invalid", stale_after_seconds: 600 }, NOW)).toBeNull();
    expect(staleText({ ...value, stale: true, generated_at: at(0), stale_after_seconds: 600 }, NOW)).not.toBeNull();
  });
  it("merges scenarios with AI explanations by event id only", () => {
    const sets = scenarioSets(
      { ...block, next_event: { ...fed, scenarios: [{ id: "A", label: "As expected" }] } },
      {
        source: "ai",
        output: {
          next_events: [
            { event_id: fed.event_id, why_it_matters: "Rate path." },
            { event_id: "INVENTED", why_it_matters: "Should be dropped." },
          ],
        },
      },
    );
    expect(sets).toHaveLength(1);
    expect(sets[0].explanation?.why_it_matters).toBe("Rate path.");
    expect(sets[0].scenarios).toHaveLength(1);
  });
  it("keeps AI explanations for active unscheduled events; never for unverified ones", () => {
    const hormuz: EventObject = { event_id: "GEO-HORMUZ", name: "Hormuz escalation", timing: "ANYTIME" };
    const rumour: EventObject = { event_id: "X-1", name: "Unverified post", timing: "ANYTIME" };
    const sets = scenarioSets(
      { ...block, active_events: [hormuz], potential_events: [rumour] },
      {
        source: "ai",
        output: {
          next_events: [
            { event_id: "GEO-HORMUZ", bull_case: "Supply fears fade." },
            { event_id: "X-1", bull_case: "Must be dropped." },
          ],
        },
      },
    );
    expect(sets.map((s) => s.event.event_id)).toEqual(["GEO-HORMUZ"]);
    expect(sets[0].explanation?.bull_case).toBe("Supply fears fade.");
  });
  it("surprise assessments", () => {
    expect(surpriseView("DOVISH_SURPRISE")).toEqual({ text: "DOVISH SURPRISE", tone: "positive" });
    expect(surpriseView("NOT_APPLICABLE")).toBeNull();
  });
  it("closed traditional markets", () => {
    expect(closedMarkets({ crypto: true, us_equities: false, us_rates: false, fx: true })).toEqual([
      "US equities",
      "US rates",
    ]);
    expect(closedMarkets(null)).toEqual([]);
  });
  it("open markets: only sessions explicitly reported open", () => {
    const weekday = { crypto: true, us_equities: false, us_rates: true, fx: true, commodities: true };
    expect(openMarkets(weekday)).toEqual(["Crypto", "US rates", "FX", "Commodities"]);
    expect(liveMarketsNote(weekday)).toBe("Crypto, US rates, FX and Commodities markets are open.");
    const weekend = { crypto: true, us_equities: false, us_rates: false, fx: false, commodities: false };
    expect(liveMarketsNote(weekend)).toBe("Crypto market is open.");
    // A market the backend does not report is never called live; crypto is live unless reported closed.
    expect(openMarkets({ us_equities: false, fx: true })).toEqual(["FX"]);
    expect(liveMarketsNote({ crypto: false, us_equities: false, fx: true })).toBe("FX market is open.");
    expect(liveMarketsNote({ crypto: false, us_equities: false })).toBeNull();
    expect(openMarkets(null)).toEqual([]);
    expect(liveMarketsNote(undefined)).toBeNull();
  });
  it("does not infer an open crypto session when session metadata is missing", () => {
    expect(openMarkets({})).toEqual([]);
    expect(liveMarketsNote({})).toBeNull();
    expect(liveMarketsNote({ us_equities: false })).toBeNull();
  });
});

describe("interpretation text without repeats", () => {
  it("key risks drop items already listed under What can break this view", () => {
    const breaks = [{ text: "US2Y reversal higher", kind: "driver" }, "VIX reversal higher", { text: "  " }];
    expect(
      distinctRisks(
        ["US2Y reversal higher", "vix  reversal higher", "EVENT DATA INCOMPLETE: US CPI (NO CONSENSUS)", "Hawkish guidance", "Hawkish guidance", 3, ""],
        breaks,
      ),
    ).toEqual(["EVENT DATA INCOMPLETE: US CPI (NO CONSENSUS)", "Hawkish guidance"]);
    expect(distinctRisks(["Hawkish guidance"], null)).toEqual(["Hawkish guidance"]);
    expect(distinctRisks(null, breaks)).toEqual([]);
  });
  it("a scenario condition that repeats its label is not shown twice", () => {
    expect(scenarioCondition({ label: "Below consensus", condition: "Below consensus" })).toBeNull();
    expect(scenarioCondition({ label: "Below consensus", condition: " below  CONSENSUS " })).toBeNull();
    expect(scenarioCondition({ label: "Hawkish", condition: "Hike with a higher rate path" })).toBe("Hike with a higher rate path");
    expect(scenarioCondition({ condition: "Hike" })).toBe("Hike");
    expect(scenarioCondition({ label: "Hold" })).toBeNull();
  });
  it("names the assets a scenario leaves unresolved, never one that carries a direction (EVR-1)", () => {
    // NFP "stronger": USD and yields up; equities and crypto have competing rate and growth paths.
    const stronger = { expected: { DXY: 1, US2Y: 1, EURUSD: -1, SP500: 0 }, unresolved: ["SP500", "NASDAQ", "BTC", "BTC", " ", 7, "DXY"] };
    expect(unresolvedSymbols(stronger as never)).toEqual(["SP500", "NASDAQ", "BTC"]);
    // A backend without the list, or a malformed one.
    expect(unresolvedSymbols({ expected: { DXY: 1 } })).toEqual([]);
    expect(unresolvedSymbols({ unresolved: "SP500" } as never)).toEqual([]);
    expect(unresolvedSymbols(null)).toEqual([]);
  });
});

describe("reactions", () => {
  it("formats measured cells and leaves unmeasured ones pending", () => {
    expect(reactionCell(0.7, "pct")).toEqual({ text: "+0.70%", sign: 1 });
    expect(reactionCell({ change: 11, unit: "bp" })).toEqual({ text: "+11 bp", sign: 1 });
    expect(reactionCell({ change: 11, unit: "bp", status: "CONFIRMS" })).toEqual({ text: "+11 bp", sign: 1 });
    expect(reactionCell({ change: -1.2, unit: "pct" })).toEqual({ text: "-1.20%", sign: -1 });
    expect(reactionCell({ change_pct: 0.35 })).toEqual({ text: "+0.35%", sign: 1 });
    expect(reactionCell(null)).toEqual({ text: "Pending", sign: null });
    expect(reactionCell({ status: "AWAITING_REOPEN" })).toEqual({ text: "AWAITING REOPEN", sign: null });
  });
  it("basis-point yield moves are never shown as percent", () => {
    // Real backend post_event: bare floats plus a units map; US10Y +11 bp.
    const entry = {
      event_id: "FED",
      reactions: { US10Y: { "5m": 11 }, NASDAQ: { "5m": 0.8 } },
      units: { US10Y: "bp", NASDAQ: "pct" },
    };
    expect(reactionCell(entry.reactions.US10Y["5m"], reactionUnitFor(entry, "US10Y"))).toEqual({
      text: "+11 bp",
      sign: 1,
    });
    expect(reactionCell(entry.reactions.NASDAQ["5m"], reactionUnitFor(entry, "NASDAQ")).text).toBe("+0.80%");
    // The cell's own unit wins over the map; an unreported unit is not guessed as percent.
    expect(reactionCell({ change: -8, unit: "bp" }, "pct").text).toBe("-8 bp");
    expect(reactionCell(11).text).toBe("+11.00");
    expect(reactionUnitFor(entry, "DXY")).toBeNull();
    expect(reactionUnitFor({ event_id: "x" }, "US10Y")).toBeNull();
  });
  it("keeps live post-event entries only", () => {
    const post = livePostEvents({
      post_event: [
        { event_id: "a", phase: "IN_PROGRESS" },
        { event_id: "b", phase: "RESOLVED" },
        { event_id: "c", phase: "POST_EVENT" },
      ],
    });
    expect(post.map((p) => p.event_id)).toEqual(["a", "c"]);
  });
  it("directions", () => {
    expect(direction(1)?.arrow).toBe("↑");
    expect(direction("down")?.word).toBe("down");
    expect(direction("sideways")).toBeNull();
  });
});

describe("eventSurpriseView", () => {
  it("shows only validated, evidence-backed classifications as findings", () => {
    const validated = { surprise: { assessment: "DOVISH_SURPRISE", source: "ai", evidence_ids: ["ev-1"], validated: true } };
    expect(eventSurpriseView(validated, "DOVISH_SURPRISE")).toEqual({ text: "DOVISH SURPRISE", tone: "positive" });
    const unvalidated = { surprise: { assessment: "UNDETERMINED", source: "none", evidence_ids: [], validated: false } };
    expect(eventSurpriseView(unvalidated, "HAWKISH_SURPRISE")).toEqual({
      text: "HAWKISH SURPRISE · not validated",
      tone: "outline",
    });
    expect(eventSurpriseView(null, "NEUTRAL_PRICED")).toEqual({ text: "NEUTRAL PRICED", tone: "outline" });
    expect(eventSurpriseView(null, "NOT_APPLICABLE")).toBeNull();
  });
});
