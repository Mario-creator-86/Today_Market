import { describe, expect, it } from "vitest";
import {
  alertCountText,
  alertLink,
  asOfText,
  bankName,
  calendarSearchText,
  consensusView,
  fedRateText,
  fxDifferentialText,
  fxMeetings,
  fxSearchText,
  groupLabel,
  horizonLabel,
  isBasket,
  isDateOnly,
  kindLabel,
  matchesText,
  meetingView,
  otherRateText,
  counterfactualNotice,
  hitRates,
  outcomeMix,
  outcomeMixText,
  performanceSummary,
  policyIncomplete,
  providerLabels,
  quoteAsOfLine,
  readableBasis,
  releaseTiming,
  releaseWindow,
  relevanceText,
  shortList,
  sortFx,
  sourceView,
  splitMeasured,
  timeBasisText,
} from "./page-logic";
import type { CalendarItem, FxRow, PerformanceReport } from "./types";

const at = (iso: string) => Date.parse(iso);

describe("search over shown text", () => {
  it("matches visible values, never ids or hidden fields", () => {
    expect(matchesText(["US industrial production (G.17)", "US"], "production")).toBe(true);
    expect(matchesText(["US industrial production (G.17)", "US"], "ecb")).toBe(false);
    expect(matchesText([null, undefined, 1.14], "1.14")).toBe(true);
    expect(matchesText(["anything"], "  ")).toBe(true);
  });
  it("calendar search ignores the row id and raw keys", () => {
    const row: CalendarItem = { id: "ecb2ec95abc", name: "US industrial production (G.17)", country: "US", impact: 5 };
    expect(matchesText(calendarSearchText(row), "ecb")).toBe(false);
    expect(matchesText(calendarSearchText(row), "impact")).toBe(false);
    expect(matchesText(calendarSearchText({ ...row, category: "CENTRAL_BANK" }), "central bank")).toBe(true);
  });
  it("FX search ignores the hidden quote label", () => {
    const gbp: FxRow = {
      symbol: "GBPUSD",
      other_bank: "boe",
      quote: { label: "ECB reference rate (cross via EUR)", provider: "ecb" } as FxRow["quote"],
    };
    expect(matchesText(fxSearchText(gbp), "ecb")).toBe(false);
    expect(matchesText(fxSearchText(gbp), "gbp/usd")).toBe(true);
    expect(matchesText(fxSearchText(gbp), "boe")).toBe(true);
  });
});

describe("FX view", () => {
  it("orders majors, NZD/USD before DXY and DXY last", () => {
    const rows = ["DXY", "NZDUSD", "XAUUSD", "EURUSD", "USDCHF"].map((symbol) => ({ symbol }));
    expect(sortFx(rows).map((r) => r.symbol)).toEqual(["EURUSD", "USDCHF", "NZDUSD", "XAUUSD", "DXY"]);
  });
  it("counts a pair as incomplete when a differential or (when sent) the rate expectation is missing", () => {
    const full: FxRow = { symbol: "EURUSD", policy_differential: 2.25, yield_differential: -0.4 };
    expect(policyIncomplete(full)).toBe(false);
    expect(policyIncomplete({ ...full, repricing_differential: 0.1 })).toBe(false);
    expect(policyIncomplete({ ...full, repricing_differential: null })).toBe(true);
    expect(policyIncomplete({ ...full, yield_differential: null })).toBe(true);
    expect(policyIncomplete({ symbol: "GBPUSD" })).toBe(true);
  });
  it("names banks by display name and keeps the backend label", () => {
    expect(bankName("ecb")).toBe("ECB");
    expect(bankName("boj")).toBe("BoJ");
    expect(bankName("xyz")).toBe("XYZ");
    expect(bankName("ecb", "European Central Bank")).toBe("European Central Bank");
    expect(bankName(null)).toBeNull();
  });
  it("shows the Fed target range with its midpoint, never a rounded midpoint alone", () => {
    expect(fedRateText({ symbol: "EURUSD", fed_rate: 3.875 })).toBe("3.875%");
    expect(fedRateText({ symbol: "EURUSD", fed_rate: 3.875, fed_rate_range: [3.75, 4] })).toBe(
      "3.75–4.00% (midpoint 3.875%)",
    );
    // Shown after "Fed": "Fed rate unavailable", worded like "BoE rate unavailable".
    expect(fedRateText({ symbol: "EURUSD", fed_rate: null })).toBe("rate unavailable");
    expect(fedRateText({ symbol: "DXY" })).toBe("rate unavailable");
  });
  it("says which bank's rate is missing", () => {
    expect(otherRateText({ symbol: "EURUSD", other_bank: "ecb", other_rate: 2 })).toBe("ECB 2%");
    expect(otherRateText({ symbol: "GBPUSD", other_bank: "boe", other_rate: null })).toBe("BoE rate unavailable");
    expect(otherRateText({ symbol: "USDCAD", other_bank: null })).toBe("Unavailable");
  });
  it("says a basket has no counterpart bank and its differentials do not apply, never 'Unavailable'", () => {
    const dxy: FxRow = { symbol: "DXY", other_bank: null, other_rate: null, policy_differential: null };
    expect(isBasket(dxy)).toBe(true);
    expect(isBasket({ symbol: "EURUSD" })).toBe(false);
    expect(isBasket({ symbol: "", pair: "EUR/USD" })).toBe(false);
    expect(otherRateText(dxy)).toBe("Basket, no single counterpart bank");
    expect(fxDifferentialText(dxy, dxy.policy_differential)).toBe("Not applicable");
    // A pair's missing differential stays missing; a measured one is signed, in percentage points.
    expect(fxDifferentialText({ symbol: "GBPUSD" }, null)).toBe("Unavailable");
    expect(fxDifferentialText({ symbol: "EURUSD" }, 2.25)).toBe("+2.25 pp");
  });
  it("names the quote's source once, by its display name, never the provider id beside it", () => {
    const fix = {
      price: 1.1712,
      timestamp: "2026-09-25T12:15:00Z",
      provider: "ecb",
      market_status: "LAST CLOSE",
      freshness_status: "STALE",
      change_pct: null,
      as_of_label: "ECB 14:15 CET fix, 2026-09-25 (daily, not a tradable quote)",
      attribution: "Source: ECB",
    };
    expect(quoteAsOfLine(fix)).toBe("as of ECB 14:15 CET fix, Fri 25 Sep (daily, not a tradable quote) · ECB");
    // Without an attribution the provider id (and feed) stays the source, as elsewhere in the app.
    expect(quoteAsOfLine({ ...fix, as_of_label: null, attribution: null, provider: "alpaca", feed: "iex" })).toBe(
      "as of Fri 25 Sep 15:15 Bucharest · alpaca (IEX)",
    );
  });
  it("formats a date-only as-of as a date, never an invented clock time", () => {
    expect(isDateOnly("2026-09-27T00:00:00Z")).toBe(true);
    expect(isDateOnly("2026-09-27T00:00:00+00:00")).toBe(true);
    expect(isDateOnly("2026-09-27")).toBe(true);
    expect(isDateOnly("2026-09-27T12:15:00Z")).toBe(false);
    expect(asOfText("2026-09-27T00:00:00Z")).toBe("27 Sep");
    expect(asOfText("2026-09-25T12:15:00Z")).toBe("Fri 25 Sep 15:15 Bucharest");
    expect(asOfText(null)).toBeNull();
  });
  it("reads the next rate decision the API sends ({name, scheduled_at})", () => {
    const view = meetingView({ name: "FOMC rate decision", scheduled_at: "2026-10-28T18:00:00+00:00" });
    expect(view.lines).toEqual(["FOMC rate decision · Wed 28 Oct 20:00 Bucharest"]);
    expect(view.priced).toBe(false);
  });
  it("lists one decision per bank, soonest first, with pricing and date-only rows", () => {
    const view = meetingView([
      { bank: "boj", name: "Monetary policy meeting", scheduled_at: "2026-10-30T03:00:00Z", time_known: false },
      { bank: "fed", name: "FOMC rate decision", scheduled_at: "2026-10-28T18:00:00Z", expected_change_bp: -25 },
    ]);
    expect(view.lines).toEqual([
      "Fed: FOMC rate decision · Wed 28 Oct 20:00 Bucharest · -25 bp priced",
      "BoJ: Monetary policy meeting · 30 Oct (time TBA)",
    ]);
    expect(view.priced).toBe(true);
    expect(meetingView({ bank: "ecb", name: "ECB rate decision", scheduled_at: "2026-10-29T13:15:00Z" }).lines[0]).toMatch(
      /^ECB rate decision · /,
    );
  });
  it("reads each bank's next decision from next_meetings; a bank without one gives its reason, not a line", () => {
    const row: FxRow = {
      symbol: "NZDUSD",
      next_meeting: { bank: "fed", bank_name: "Fed", name: "FOMC rate decision", scheduled_at: "2026-10-28T18:00:00Z" },
      next_meetings: [
        { bank: "rbnz", bank_name: "RBNZ", name: null, scheduled_at: null, time_known: null, reason: "No rate-decision calendar source for the Reserve Bank of New Zealand" },
        { bank: "fed", bank_name: "Fed", name: "FOMC rate decision", scheduled_at: "2026-10-28T18:00:00Z", time_known: true, reason: null },
      ],
    };
    const view = meetingView(fxMeetings(row));
    expect(view.lines).toEqual(["Fed: FOMC rate decision · Wed 28 Oct 20:00 Bucharest"]);
    expect(view.notes).toEqual(["No rate-decision calendar source for the Reserve Bank of New Zealand"]);
    expect(fxMeetings({ symbol: "DXY", next_meeting: null, next_meetings: [] })).toBeNull();
    expect(otherRateText({ symbol: "NZDUSD", other_bank: "rbnz", other_bank_name: "RBNZ", other_rate: 5.25 })).toBe("RBNZ 5.25%");
  });
  it("keeps text as sent and gives nothing for a missing meeting", () => {
    expect(meetingView("Fed hold priced for 29 Oct").lines).toEqual(["Fed hold priced for 29 Oct"]);
    expect(meetingView(null).lines).toEqual([]);
    expect(meetingView({ bank: "rba", note: "No calendar source for the RBA" })).toEqual({
      lines: [],
      priced: false,
      notes: ["No calendar source for the RBA"],
    });
  });
});

describe("calendar view", () => {
  const dateOnly: CalendarItem = {
    name: "3-Year Note auction",
    country: "US",
    scheduled_at: "2026-10-06T04:00:00+00:00",
    time_known: false,
    impact: 5,
  };
  it("keeps a date-only row due through its day instead of 'awaiting actual' at midnight", () => {
    expect(releaseTiming(dateOnly, at("2026-10-05T20:00:00Z"))).toBe("UPCOMING");
    expect(releaseTiming(dateOnly, at("2026-10-06T06:00:00Z"))).toBe("DUE");
    expect(releaseTiming(dateOnly, at("2026-10-07T04:00:00Z"))).toBe("AWAITING");
  });
  it("uses the backend release window when it is sent", () => {
    const amc: CalendarItem = {
      ...dateOnly,
      scheduled_at: "2026-10-27T20:00:00Z",
      date_window: { start_utc: "2026-10-27T20:00:00Z", end_utc: "2026-10-28T00:00:00Z", label: "after the US close" },
    };
    expect(releaseTiming(amc, at("2026-10-27T19:00:00Z"))).toBe("UPCOMING");
    expect(releaseTiming(amc, at("2026-10-27T22:00:00Z"))).toBe("DUE");
    expect(releaseTiming(amc, at("2026-10-28T01:00:00Z"))).toBe("AWAITING");
  });
  it("shows a session window as a Bucharest time range and a whole-day window as that day in its zone", () => {
    const amc: CalendarItem = {
      ...dateOnly,
      scheduled_at: "2026-10-27T20:00:00Z",
      date_window: {
        start_utc: "2026-10-27T20:00:00+00:00",
        end_utc: "2026-10-28T00:00:00+00:00",
        label: "after the US close",
        zone: "America/New_York",
      },
    };
    expect(releaseWindow(amc)).toEqual({
      text: "22:00–02:00 next day Bucharest (after the US close)",
      label: "after the US close",
    });
    // A window that ends at Bucharest midnight ends that day.
    const evening = { ...amc, date_window: { ...amc.date_window, end_utc: "2026-10-27T22:00:00Z" } };
    expect(releaseWindow(evening)?.text).toBe("22:00–00:00 Bucharest (after the US close)");
    // The publisher's local day: the day in its zone, never the internal label "publisher local day".
    const day: CalendarItem = {
      ...dateOnly,
      date_window: {
        start_utc: "2026-10-06T04:00:00+00:00",
        end_utc: "2026-10-07T04:00:00+00:00",
        label: "publisher local day",
        zone: "America/New_York",
      },
    };
    expect(releaseWindow(day)).toEqual({ text: "all day Tue 6 Oct, New York time", label: null });
    // A day with a DST change lasts 25 h; the BoJ statement day is a Tokyo day.
    const dst = { ...day, date_window: { ...day.date_window, start_utc: "2026-11-01T04:00:00Z", end_utc: "2026-11-02T05:00:00Z" } };
    expect(releaseWindow(dst)?.text).toBe("all day Sun 1 Nov, New York time");
    const boj = {
      ...day,
      date_window: { start_utc: "2026-10-29T15:00:00Z", end_utc: "2026-10-30T15:00:00Z", label: "statement day in Tokyo", zone: "Asia/Tokyo" },
    };
    expect(releaseWindow(boj)?.text).toBe("all day Fri 30 Oct, Tokyo time");
    // Timed rows and rows without a window have none.
    expect(releaseWindow({ ...amc, time_known: true })).toBeNull();
    expect(releaseWindow(dateOnly)).toBeNull();
    expect(matchesText(calendarSearchText(day), "new york")).toBe(true);
  });
  it("drops from the time basis what the release window already says", () => {
    const amc: CalendarItem = {
      ...dateOnly,
      date_window: { start_utc: "2026-10-27T20:00:00Z", end_utc: "2026-10-28T00:00:00Z", label: "after the US close" },
    };
    expect(timeBasisText({ ...amc, time_basis: "after the US close (Finnhub gives the session only; exact time not published)" })).toBe(
      "Finnhub gives the session only; exact time not published",
    );
    expect(timeBasisText({ ...amc, time_basis: "After the US close; exact time not published" })).toBe("exact time not published");
    expect(timeBasisText({ ...amc, time_basis: "after the US close" })).toBeNull();
    // Nothing repeated: the basis stays as sent (timed rows show no window).
    expect(timeBasisText({ ...dateOnly, time_basis: "date only; closing time not published" })).toBe(
      "date only; closing time not published",
    );
    expect(timeBasisText({ scheduled_utc: "2026-09-26T12:30:00Z", time_known: true, time_basis: "BLS release calendar (ICS)" })).toBe(
      "BLS release calendar (ICS)",
    );
    expect(timeBasisText(dateOnly)).toBeNull();
  });
  it("times a timed row by its clock and a row with an actual as released", () => {
    const cpi: CalendarItem = { scheduled_utc: "2026-09-26T12:30:00Z", time_known: true };
    expect(releaseTiming(cpi, at("2026-09-26T12:00:00Z"))).toBe("UPCOMING");
    expect(releaseTiming(cpi, at("2026-09-26T12:31:00Z"))).toBe("AWAITING");
    expect(releaseTiming({ ...cpi, actual: 2.9 }, at("2026-09-26T12:31:00Z"))).toBe("RELEASED");
  });
  it("says NO CONSENSUS for a missing consensus, never 'Unavailable'", () => {
    expect(consensusView({ consensus: null })).toMatchObject({ label: "Consensus", value: "NO CONSENSUS", missing: true });
    expect(consensusView({ consensus: "NO CONSENSUS" }).missing).toBe(true);
    expect(consensusView({ consensus: 2.8, unit: "%" })).toMatchObject({ label: "Consensus", value: "2.8%", missing: false });
  });
  it("shows Finnhub estimates under their own label, never as consensus", () => {
    const view = consensusView({
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
    });
    expect(view.label).toBe("Finnhub estimate");
    expect(view.value).toBe("EPS 0.4464 USD/share · Revenue 28.18B USD");
    expect(view.note).toMatch(/not Street consensus/);
    expect(view.missing).toBe(false);
  });
  it("labels relevance apart from impact", () => {
    expect(relevanceText({ relevance_score: 69.6, relevance_level: "HIGH" })).toBe("Relevance 70/100 · HIGH");
    expect(relevanceText({ relevance_level: "very_high" })).toBe("Relevance VERY HIGH");
    expect(relevanceText({})).toBeNull();
  });
  it("credits the source and links only http(s) URLs", () => {
    expect(sourceView({ attribution: "Source: U.S. Bureau of Economic Analysis", source_url: "https://www.bea.gov/x" })).toEqual({
      text: "Source: U.S. Bureau of Economic Analysis",
      href: "https://www.bea.gov/x",
    });
    expect(sourceView({ attribution: "Source: BLS", source_url: "javascript:alert(1)" })?.href).toBeUndefined();
    expect(sourceView({ source_url: "https://finnhub.io/docs" })?.text).toBe("Source: finnhub.io");
    expect(sourceView({})).toBeNull();
  });
});

describe("performance view", () => {
  it("keeps numeric horizons and words the session ones", () => {
    expect(horizonLabel("4h")).toBe("4h");
    expect(horizonLabel("next_session")).toBe("Next session close");
    expect(horizonLabel("close")).toBe("Session close");
  });
  it("words breakdown groups instead of enum ids", () => {
    expect(groupLabel("event_type", "NONE")).toBe("No event");
    expect(groupLabel("confidence_bucket", "UNRECORDED")).toBe("Not recorded");
    expect(groupLabel("confidence_bucket", "0-25")).toBe("0–25%");
    expect(groupLabel("setup", "BREAKDOWN_BELOW_SUPPORT")).toBe("Breakdown below support");
    expect(groupLabel("score_bucket", "SETUP-")).toBe("Setup short");
    expect(groupLabel("asset", "BTC")).toBe("BTC");
    expect(groupLabel("source_combination", "bls+reuters")).toBe("bls + reuters");
  });
  it("keeps acronyms in plain-word labels", () => {
    expect(groupLabel("regime", "USD_STRENGTH")).toBe("USD strength");
    expect(groupLabel("regime", "RISK_ON")).toBe("Risk on");
    expect(groupLabel("event_type", "US_CPI")).toBe("US CPI");
    expect(groupLabel("event_type", "FED_SPEECH")).toBe("Fed speech");
    expect(kindLabel("ETF_FLOW")).toBe("ETF flow");
  });
  it("reads the backend's directional count beside decisions_recorded", () => {
    const empty = { observations: 0 };
    // Nothing directional measured: the backend says INSUFFICIENT_DATA.
    expect(
      performanceSummary({
        observations: 0,
        decisions_recorded: 676,
        status: "INSUFFICIENT_DATA",
        horizons: ["1h", "4h"],
        metrics: { "1h": empty, "4h": empty },
      }),
    ).toMatchObject({ decisions: 676, directional: 0, directionalAtBestHorizon: false, status: "NOT ENOUGH DATA", mixHorizon: null });
    // Decisions measured at different horizons: the backend's distinct count (15), not the best horizon (10).
    expect(
      performanceSummary({
        observations: 15,
        decisions_recorded: 20,
        status: "UNCALIBRATED",
        horizons: ["1h", "4h"],
        metrics: { "1h": { observations: 8 }, "4h": { observations: 10 } },
      }),
    ).toMatchObject({ decisions: 20, directional: 15, directionalAtBestHorizon: false, status: "UNCALIBRATED", mixHorizon: "4h" });
  });
  it("reads an older payload (observations = decisions recorded) with the best horizon's count", () => {
    const empty = { observations: 0 };
    const report: PerformanceReport = {
      observations: 676,
      status: "UNCALIBRATED",
      horizons: ["1h", "4h"],
      metrics: { "1h": empty, "4h": empty },
    };
    expect(performanceSummary(report)).toMatchObject({
      decisions: 676,
      directional: 0,
      directionalAtBestHorizon: true,
      status: "NOT ENOUGH DATA",
    });
    const measured = { ...report, metrics: { "1h": { observations: 3 }, "4h": { observations: 10 } } };
    expect(performanceSummary(measured)).toMatchObject({ directional: 10, status: "UNCALIBRATED" });
    expect(
      performanceSummary({ ...measured, decisions_recorded: 700, directional_observations: 12 }),
    ).toMatchObject({ decisions: 700, directional: 12, directionalAtBestHorizon: false });
  });
  it("labels counterfactual outcomes: WAIT and lean results are never presented as a signal hit rate (AFP-03)", () => {
    // The live shape on 2 Oct: 148 decisions measured, none as a published signal.
    const counterfactual = (n: number, rate: number) => ({
      observations: n,
      actionable: 0,
      counterfactual: n,
      counterfactual_only: true,
      hit_rate: null,
      hit_rate_basis: null,
      counterfactual_hit_rate: rate,
      false_positive_rate: null,
    });
    const report: PerformanceReport = {
      observations: 148,
      decisions_recorded: 1514,
      published_signals: 0,
      counterfactual_decisions: 148,
      status: "UNCALIBRATED",
      population: "all",
      horizons: ["5m", "4h"],
      metrics: { "5m": counterfactual(148, 0.554), "4h": counterfactual(60, 0.433) },
    };
    const summary = performanceSummary(report);
    // The service's own decision counts: not one horizon's.
    expect(summary.mixHorizon).toBeNull();
    expect(summary.mix).toEqual({ published: 0, counterfactual: 148, basis: "counterfactual" });
    expect(outcomeMixText(summary.mix)).toBe("0 / 148");
    expect(hitRates(report.metrics!["5m"], "all")).toEqual({ signal: null, counterfactual: 0.554, signalIncludesCounterfactual: false });
    expect(counterfactualNotice(summary.mix, "all")).toMatch(/^No published signal has a measured outcome yet\. Every figure below is counterfactual/);
    expect(counterfactualNotice(summary.mix, "counterfactual")).toMatch(/^Counterfactual outcomes:/);
    // Published signals only: the signal rate, no counterfactual rate, no notice.
    const published = { observations: 4, actionable: 4, counterfactual: 0, counterfactual_only: false, hit_rate: 0.75, hit_rate_basis: "published", counterfactual_hit_rate: null };
    expect(outcomeMix(published, "all").basis).toBe("published");
    expect(hitRates(published, "all")).toEqual({ signal: 0.75, counterfactual: null, signalIncludesCounterfactual: false });
    expect(counterfactualNotice(outcomeMix(published, "all"), "all")).toBeNull();
    // Both in one row: the two rates stay apart and the notice says what the other figures cover.
    const both = { observations: 10, actionable: 3, counterfactual: 7, hit_rate: 2 / 3, hit_rate_basis: "published", counterfactual_hit_rate: 0.43 };
    const mixed = outcomeMix(both, "all");
    expect(mixed.basis).toBe("mixed");
    expect(hitRates(both, "all")).toEqual({ signal: 2 / 3, counterfactual: 0.43, signalIncludesCounterfactual: false });
    expect(counterfactualNotice(mixed, "all")).toBe(
      'Returns, MFE, MAE and drawdown below cover 3 published signal outcomes and 7 counterfactual ones (WAIT and lean decisions measured on their score sign). Choose "Actionable only" for published signals alone.',
    );
  });
  it("reads the basis defensively: an older single hit rate, a payload without counts, nothing measured", () => {
    // An older service: one rate over every measured outcome. It is never shown as a pure signal rate
    // when the row holds counterfactual outcomes.
    expect(hitRates({ observations: 148, actionable: 0, counterfactual: 148, hit_rate: 0.554 }, "all")).toEqual({
      signal: null,
      counterfactual: 0.554,
      signalIncludesCounterfactual: false,
    });
    expect(hitRates({ observations: 10, actionable: 3, counterfactual: 7, hit_rate: 0.5 }, "all")).toEqual({
      signal: 0.5,
      counterfactual: null,
      signalIncludesCounterfactual: true,
    });
    expect(hitRates({ observations: 4, actionable: 4, counterfactual: 0, hit_rate: 0.75 }, "all").signalIncludesCounterfactual).toBe(false);
    expect(hitRates({ observations: 5, hit_rate: 0.6 }, "counterfactual")).toMatchObject({ signal: null, counterfactual: 0.6 });
    expect(hitRates(null)).toEqual({ signal: null, counterfactual: null, signalIncludesCounterfactual: false });
    // The explicit flag wins over counts.
    expect(outcomeMix({ observations: 5, counterfactual_only: true }, "all").basis).toBe("counterfactual");
    // No counts: the requested population decides; "all" is never presented as signals.
    expect(outcomeMix({ observations: 5, hit_rate: 0.6 }, "all")).toEqual({ published: null, counterfactual: null, basis: "unknown" });
    expect(outcomeMix({ observations: 5 }, "counterfactual").basis).toBe("counterfactual");
    expect(outcomeMix({ observations: 5 }, "actionable").basis).toBe("published");
    expect(outcomeMixText(outcomeMix({ observations: 5 }, "all"))).toBeNull();
    expect(counterfactualNotice(outcomeMix({ observations: 5 }, "all"), "all")).toBeNull();
    // Malformed counts are not counts.
    expect(outcomeMix({ observations: 5, actionable: -1 as number, counterfactual: Number.NaN }, "all").basis).toBe("unknown");
    // Without the service's decision counts: the outcome counts of the horizon with the most outcomes.
    const older = performanceSummary({
      observations: 148,
      decisions_recorded: 1514,
      horizons: ["5m", "4h"],
      metrics: { "5m": { observations: 148, actionable: 0, counterfactual: 148 }, "4h": { observations: 60, actionable: 0, counterfactual: 60 } },
    });
    expect(older.mixHorizon).toBe("5m");
    expect(older.mix).toEqual({ published: 0, counterfactual: 148, basis: "counterfactual" });
    // Nothing measured at any horizon: no basis, no notice.
    const empty = performanceSummary({ observations: 0, decisions_recorded: 3, published_signals: 0, counterfactual_decisions: 0, population: "counterfactual", horizons: ["1h"], metrics: { "1h": { observations: 0 } } });
    expect(empty.mix.basis).toBe("unknown");
    expect(counterfactualNotice(empty.mix, "counterfactual")).toBeNull();
  });
  it("splits measured rows from unmeasured ones", () => {
    const rows = [
      { group: "BTC", summary: { observations: 0 } },
      { group: "ETH", summary: { observations: 4 } },
    ];
    expect(splitMeasured(rows, (r) => r.group)).toEqual({ measured: [rows[1]], unmeasured: ["BTC"] });
    expect(shortList(["a", "b", "c"], 2)).toBe("a, b and 1 more");
  });
  it("formats the paper basis without raw config ids", () => {
    expect(readableBasis("Net of configured fees and slippage; equal_notional sizing on 100000 initial virtual equity")).toBe(
      "Net of configured fees and slippage; equal-notional sizing on 100,000 initial virtual equity",
    );
  });
});

describe("alerts view", () => {
  it("links provider alerts to Provider health under the provider's label", () => {
    expect(alertLink({ id: "a", kind: "PROVIDER", label: "Finnhub (equities)", metrics: { provider: "finnhub" } })).toEqual({
      href: "/providers",
      text: "Finnhub (equities)",
    });
    // An alert stored with the provider id only is named from the provider list, never by its raw id.
    const labels = providerLabels([
      { name: "ollama", label: "Ollama (local AI)" },
      { name: "gdelt_events", label: " " },
      { label: "No id" },
      null,
    ]);
    expect([...labels]).toEqual([["ollama", "Ollama (local AI)"]]);
    expect(alertLink({ id: "b", kind: "PROVIDER", metrics: { provider: "ollama" } }, labels)).toEqual({
      href: "/providers",
      text: "Ollama (local AI)",
    });
    expect(alertLink({ id: "b2", kind: "PROVIDER", metrics: { provider: "gdelt_events" } }, labels)?.text).toBe("Provider health");
    expect(alertLink({ id: "b3", kind: "PROVIDER", metrics: { provider: "ollama" } })?.text).toBe("Provider health");
    expect(alertLink({ id: "b4", kind: "ANOMALY", metrics: { provider: "deribit" } })).toEqual({
      href: "/providers",
      text: "Provider health",
    });
    expect(providerLabels(undefined).size).toBe(0);
    expect(alertLink({ id: "c", symbol: "BTC" })).toEqual({ href: "/assets/BTC", text: "BTC" });
    expect(alertLink({ id: "d", kind: "SETUP" })).toBeNull();
  });
  it("states the honest count", () => {
    expect(alertCountText(50, 151, 151)).toBe("Showing 50 of 151 alerts");
    expect(alertCountText(26, 26, 151)).toBe("26 alerts");
    expect(alertCountText(50, 500, 500)).toBe("Showing 50 of 500 alerts · only the newest 500 are loaded");
    expect(kindLabel("LEVEL_BREAK")).toBe("Level break");
  });
});
