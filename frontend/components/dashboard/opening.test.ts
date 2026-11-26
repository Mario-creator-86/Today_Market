import { describe, expect, it } from "vitest";
import type { EventObject, EventRisk, Interpretation, MarketViewItem, Regime, RegimeDriver } from "../../lib/api";
import { expectation, NO_CONSENSUS, UNKNOWN } from "../../lib/event-risk";
import {
  basisTag,
  calendarUpcoming,
  clampSentences,
  driverBasisWord,
  eventWhy,
  expectationText,
  groupNotes,
  heroText,
  heroWarning,
  noSetupsReason,
  NO_SETUPS_REASON,
  otherIncompleteReasons,
  ownFacts,
  publishedSummary,
  regimeHeadline,
  releasedToday,
  sentences,
  upcomingEvents,
  weekdayName,
  whyLine,
} from "./opening";

const NOW = Date.parse("2026-10-02T07:30:00Z");
const NFP = "US employment situation (NFP, unemployment, AHE)";

/** The deterministic view the backend sent on 2 Oct 2026 (three sentences, every fact also shown elsewhere). */
const REAL_VIEW =
  "Mixed conditions driven by USD strength (mixed posture) describe historical context; DXY, USDJPY, EURUSD, GBPUSD and GOLD not confirming that view now. " +
  `${NFP} at 15:30 Bucharest is the dominant event and can change this view. ` +
  "US equities closed: event implications there cannot be confirmed until that market reopens; US rates, FX, commodities and crypto markets are open.";

const event = (extra: Partial<EventObject> = {}): EventObject => ({
  event_id: "BLS-20261002-US_NFP",
  name: NFP,
  phase: "APPROACHING",
  time_utc: "2026-10-02T12:30:00+00:00",
  time_known: true,
  impact: 10,
  ...extra,
});

describe("sentences", () => {
  it("splits on sentence ends, not on abbreviations or decimals", () => {
    expect(sentences("CPI rose 3.1% in the U.S. Bureau data. Yields fell vs. Friday. Done")).toEqual([
      "CPI rose 3.1% in the U.S. Bureau data.",
      "Yields fell vs. Friday.",
      "Done",
    ]);
    expect(sentences("  ")).toEqual([]);
    expect(sentences(null)).toEqual([]);
  });

  it("clamps to the first sentences as written", () => {
    expect(clampSentences("One. Two! Three?", 2)).toBe("One. Two!");
    expect(clampSentences("Only one", 2)).toBe("Only one");
  });
});

describe("hero text", () => {
  it("keeps at most two sentences", () => {
    expect(heroText("A is up. B is down. C is flat.")).toBe("A is up. B is down.");
  });

  it("drops what EVENTS and POSITIONING already state: the event sentence, the closed-markets sentence, the asset list", () => {
    expect(
      heroText(REAL_VIEW, {
        eventNames: [NFP],
        symbols: ["DXY", "USDJPY", "EURUSD", "GBPUSD", "GOLD"],
        closedNoteShown: true,
      }),
    ).toBe("Mixed conditions driven by USD strength (mixed posture) describe historical context.");
  });

  it("keeps the closed-markets sentence when no positioning note carries it", () => {
    expect(heroText("Risk is on. US equities closed: confirm after they reopen.", { closedNoteShown: false })).toBe(
      "Risk is on. US equities closed: confirm after they reopen.",
    );
  });

  it("keeps a sentence that mentions fewer than three positioning symbols", () => {
    const text = "Dollar strength pressures EURUSD and GOLD while yields ease.";
    expect(heroText(text, { symbols: ["EURUSD", "GOLD", "DXY"] })).toBe(text);
  });

  it("never matches a symbol inside another word", () => {
    const text = "GOLDMAN, DXYZ and EURUSDT are not tracked symbols.";
    expect(heroText(text, { symbols: ["GOLD", "DXY", "EURUSD"] })).toBe(text);
  });

  it("keeps the first sentence as written when everything would be dropped", () => {
    const only = `${NFP} at 15:30 Bucharest is the dominant event.`;
    expect(heroText(only, { eventNames: [NFP] })).toBe(only);
    expect(heroText("", { eventNames: [NFP] })).toBe("");
    expect(heroText(undefined)).toBe("");
  });
});

describe("regime headline and basis tag", () => {
  it("writes the regime in plain words and leaves a MIXED posture out", () => {
    expect(regimeHeadline(["USD_STRENGTH"], "MIXED")).toBe("USD strength");
    expect(regimeHeadline(["RISK_ON"], "CONSTRUCTIVE")).toBe("Constructive · Risk on");
    expect(regimeHeadline(["RISK_OFF", "USD_STRENGTH"], "DEFENSIVE")).toBe("Defensive · Risk off / USD strength");
    expect(regimeHeadline([], "INSUFFICIENT_DATA")).toBeNull();
    expect(regimeHeadline(undefined, undefined)).toBeNull();
  });

  it("names the weekday of the newest close for a last-close reading", () => {
    const regime = {
      basis: "last_close",
      basis_label: "last close 29 Sep–Thu 1 Oct + proxies (IWM, PAXG)",
      drivers: [
        { driver: "Oil", direction: "down", inputs: [{ feature: "WTI", close_date: "2026-09-29" }] },
        { driver: "USD", direction: "up", inputs: [{ feature: "DXY", close_date: "2026-10-01" }] },
      ],
    } as unknown as Regime;
    expect(basisTag(regime)).toEqual({
      label: "from Thursday's close",
      historical: true,
      title: "last close 29 Sep–Thu 1 Oct + proxies (IWM, PAXG)",
    });
    expect(weekdayName("2026-10-02")).toBe("Friday");
    expect(weekdayName("2 Oct")).toBeNull();
  });

  it("falls back without a close date, and says live for live observations", () => {
    expect(basisTag({ basis: "last_close" } as Regime)?.label).toBe("from the last close");
    expect(basisTag({ basis: "live" } as Regime)).toEqual({ label: "live", historical: false, title: null });
    expect(basisTag(undefined)).toBeNull();
    expect(basisTag({} as Regime)).toBeNull();
  });
});

describe("driver basis", () => {
  const driver = (inputs: unknown[], basis: string | null = "live") =>
    ({ driver: "USD", direction: "up", inputs, basis }) as unknown as RegimeDriver;

  it("never calls observations read from proxies live", () => {
    expect(driverBasisWord(driver([{ feature: "DXY", via: "UUP", basis: "proxy" }]))).toBe("via proxies");
    expect(driverBasisWord(driver([{ feature: "SP500", via: "SPY" }, { feature: "NASDAQ", basis: "proxy" }]))).toBe(
      "via proxies",
    );
    expect(driverBasisWord(driver([{ feature: "SP500", via: "SPY" }, { feature: "VIX", via: null }]))).toBe(
      "live + proxies",
    );
  });

  it("keeps the driver's own basis for direct observations", () => {
    expect(driverBasisWord(driver([{ feature: "BTC", via: null }]))).toBe("live");
    expect(driverBasisWord(driver([{ feature: "US2Y" }], "last_close"))).toBe("last close");
    expect(driverBasisWord(driver([], null))).toBeNull();
    // A driver sent with its text only and a proxy basis.
    expect(driverBasisWord(driver([], "proxy"))).toBe("via proxies");
  });
});

describe("hero warning", () => {
  it("leads with the first conflict and counts the rest", () => {
    const regime = {
      basis: "last_close",
      divergences: [
        { feature: "DXY", text: "DXY -0.31% opposes the historical input +0.49%." },
        { feature: "WTI", text: "WTI +3.01% opposes the historical input -3.23%." },
      ],
    } as unknown as Regime;
    expect(heroWarning(regime)).toEqual({
      kind: "conflict",
      text: "Today's observations conflict with this view: DXY -0.31% opposes the historical input +0.49%.",
      more: 1,
    });
  });

  it("says a close-based view is not confirmed when nothing current was observed", () => {
    const empty = { driver: "USD", direction: "unavailable", inputs: [], input: null };
    expect(heroWarning({ basis: "last_close", current_drivers: [empty] } as unknown as Regime)?.kind).toBe("unconfirmed");
  });

  it("stays silent for a live view, or a close-based view with current observations and no conflict", () => {
    expect(heroWarning({ basis: "live" } as Regime)).toBeNull();
    const observed = { driver: "USD", direction: "up", inputs: [{ feature: "DXY", via: "UUP" }] };
    expect(heroWarning({ basis: "last_close", current_drivers: [observed] } as unknown as Regime)).toBeNull();
    expect(heroWarning(null)).toBeNull();
  });
});

describe("why it matters", () => {
  it("drops the provenance and the clauses that restate the card's chips", () => {
    expect(
      whyLine(
        "VERY HIGH relevance; most exposed: DXY, US2Y, BTC, SP500, NASDAQ. Known: Source: FRED release calendar, Federal Reserve Bank of St. Louis; Time basis: FRED release calendar date.",
      ),
    ).toBe("");
    expect(whyLine("Jobs data resets rate expectations; most exposed: DXY, US2Y. Known: Source: BLS.")).toBe(
      "Jobs data resets rate expectations.",
    );
  });

  it("keeps one sentence of a model's explanation", () => {
    expect(whyLine("A weak print would reverse the dollar bid. It would also lift gold.")).toBe(
      "A weak print would reverse the dollar bid.",
    );
    expect(whyLine(null)).toBe("");
  });

  it("falls back to the reason the event is dominant, for that event only", () => {
    const interpretation = {
      source: "deterministic",
      output: {
        next_events: [{ event_id: "BLS-20261002-US_NFP", why_it_matters: "VERY HIGH relevance; most exposed: DXY, US2Y." }],
        dominant_event: {
          event_id: "BLS-20261002-US_NFP",
          reason:
            "Dominance score 88/100 with VERY HIGH relevance; it can matter more than the current regime for the next directional move.",
        },
      },
    } as Interpretation;
    expect(eventWhy(event(), interpretation)).toBe(
      "It can matter more than the current regime for the next directional move.",
    );
    expect(eventWhy(event({ event_id: "OTHER" }), interpretation)).toBe("");
    expect(eventWhy(event(), null)).toBe("");
    expect(eventWhy(null, interpretation)).toBe("");
  });
});

describe("expectations line", () => {
  it("says what is missing in plain words, never a value", () => {
    expect(expectationText("Consensus", expectation("NO CONSENSUS", NO_CONSENSUS), "not available")).toBe(
      "Consensus: not available",
    );
    expect(expectationText("Market pricing", expectation(null, UNKNOWN), "unknown")).toBe("Market pricing: unknown");
  });

  it("shows reported values under the provider's own label", () => {
    const value = expectation(
      { label: "Finnhub estimate", values: [{ name: "EPS", value: 1.42, unit: "USD" }], source: "finnhub" },
      NO_CONSENSUS,
    );
    expect(expectationText("Consensus", value, "not available")).toBe("Finnhub estimate: EPS 1.42 USD");
    const priced = expectation({ hike_probability: 0.78, summary: "Hawkish outcome partially priced" }, UNKNOWN);
    expect(expectationText("Market pricing", priced, "unknown")).toBe(
      "Market pricing: Hike probability 78% — Hawkish outcome partially priced",
    );
  });

  it("does not repeat the missing consensus among the other data-quality reasons", () => {
    expect(otherIncompleteReasons(["NO CONSENSUS", "time not confirmed", "UNKNOWN"])).toEqual(["time not confirmed"]);
  });
});

describe("coming up", () => {
  const block = (extra: Partial<EventRisk> = {}): EventRisk => ({
    next_event: event(),
    dominant_event: event(),
    events: [
      event({ event_id: "EXP", name: "BTC options expiry", time_utc: "2026-10-02T08:00:00+00:00", impact: 4 }),
      event(),
      event({ event_id: "MIN", name: "FOMC minutes", time_utc: "2026-10-07T18:00:00+00:00", impact: 6 }),
      event({ event_id: "EIA", name: "EIA report", time_utc: "2026-10-07T14:30:00+00:00", impact: 6 }),
      event({ event_id: "OLD", name: "Released", time_utc: "2026-10-03T10:00:00+00:00", phase: "RELEASED" }),
      event({ event_id: "ANY", name: "Unscheduled", time_utc: null, timing: "ANYTIME" }),
    ],
    ...extra,
  });

  it("lists the other scheduled events from now on, soonest first, never the key event itself", () => {
    // The expiry is due before the key event (NFP 15:30): it is listed first, not dropped (EVR-2).
    expect(upcomingEvents(block(), NOW)).toEqual([
      { key: "EXP", name: "BTC options expiry", when: "11:00", iso: "2026-10-02T08:00:00.000Z", impact: "4/10" },
      { key: "EIA", name: "EIA report", when: "Wed 7 Oct 17:30", iso: "2026-10-07T14:30:00.000Z", impact: "6/10" },
      { key: "MIN", name: "FOMC minutes", when: "Wed 7 Oct 21:00", iso: "2026-10-07T18:00:00.000Z", impact: "6/10" },
    ]);
  });

  it("lists today's lesser events when the key event is days away (Thu 8 Oct, key event CPI on 14 Oct)", () => {
    const morning = Date.parse("2026-10-08T06:00:00Z");
    const cpi = event({ event_id: "CPI", name: "US CPI", phase: "SCHEDULED", time_utc: "2026-10-14T12:30:00+00:00" });
    const rows = upcomingEvents(
      {
        next_event: cpi,
        dominant_event: cpi,
        events: [
          event({ event_id: "CLAIMS", name: "US initial jobless claims", time_utc: "2026-10-08T12:30:00+00:00", impact: 6 }),
          event({ event_id: "30Y", name: "30-Year Bond auction", phase: "SCHEDULED", time_utc: "2026-10-08T17:00:00+00:00", impact: 5 }),
          event({ event_id: "UMICH", name: "UMich sentiment", phase: "SCHEDULED", time_utc: "2026-10-09T14:00:00+00:00", impact: 5 }),
          cpi,
          event({ event_id: "PPI", name: "US PPI", phase: "SCHEDULED", time_utc: "2026-10-15T12:30:00+00:00", impact: 7 }),
          // Already due by the clock although the snapshot still calls it imminent: not "coming up".
          event({ event_id: "PAST", name: "Earlier today", phase: "IMMINENT", time_utc: "2026-10-08T05:30:00+00:00" }),
        ],
      },
      morning,
    );
    expect(rows.map((r) => [r.key, r.when])).toEqual([
      ["CLAIMS", "15:30"],
      ["30Y", "20:00"],
      ["UMICH", "Fri 9 Oct 17:00"],
      ["PPI", "Thu 15 Oct 15:30"],
    ]);
  });

  it("keeps a date-only row until its release window ends, whatever its anchor time", () => {
    const boj = event({
      event_id: "BOJ",
      name: "BoJ monetary policy decision",
      time_utc: "2026-10-30T03:00:00+00:00",
      time_known: false,
      date_bucharest: "2026-10-30",
      date_window: { start_utc: "2026-10-29T15:00:00+00:00", end_utc: "2026-10-30T15:00:00+00:00" },
      impact: 9,
    });
    const key = event({ event_id: "PCE", name: "US PCE", time_utc: "2026-10-30T12:30:00+00:00" });
    const risk = { next_event: key, dominant_event: key, events: [boj, key] };
    // 06:00Z: the anchor (03:00Z) has passed, the Tokyo statement day has not ended.
    expect(upcomingEvents(risk, Date.parse("2026-10-30T06:00:00Z")).map((r) => [r.key, r.when, r.iso])).toEqual([
      ["BOJ", "Fri 30 Oct · time TBA", null],
    ]);
    expect(upcomingEvents(risk, Date.parse("2026-10-30T15:01:00Z"))).toEqual([]);
    // Without a window the row counts as due for the 24 h after its anchor.
    const bare = { ...risk, events: [{ ...boj, date_window: null }, key] };
    expect(upcomingEvents(bare, Date.parse("2026-10-30T06:00:00Z")).map((r) => r.key)).toEqual(["BOJ"]);
    expect(upcomingEvents(bare, Date.parse("2026-10-31T03:01:00Z"))).toEqual([]);
  });

  it("leaves out a dominant event the card already shows, and honours the limit", () => {
    const dominant = event({ event_id: "MIN", name: "FOMC minutes", time_utc: "2026-10-07T18:00:00+00:00" });
    expect(upcomingEvents(block({ dominant_event: dominant }), NOW).map((r) => r.key)).toEqual(["EXP", "EIA"]);
    expect(upcomingEvents(block(), NOW, 1).map((r) => r.key)).toEqual(["EXP"]);
  });

  it("lists every upcoming event when no key event is timed", () => {
    expect(upcomingEvents(block({ next_event: null, dominant_event: null }), NOW).map((r) => r.key)).toEqual([
      "EXP",
      "BLS-20261002-US_NFP",
      "EIA",
      "MIN",
    ]);
    expect(upcomingEvents(null, NOW)).toEqual([]);
  });

  it("marks a date-only row as time TBA without a clock", () => {
    const tba = event({ event_id: "TBA", name: "Refunding", time_utc: "2026-10-28T12:00:00+00:00", time_known: false, date_bucharest: "2026-10-28", impact: null });
    const rows = upcomingEvents(block({ events: [tba] }), NOW);
    expect(rows).toEqual([{ key: "TBA", name: "Refunding", when: "Wed 28 Oct · time TBA", iso: null, impact: null }]);
  });

  it("keeps a scheduled event released today on screen with what was published (EVR-4)", () => {
    // Fri 2 Oct, evening: NFP resolved hours ago; the BLS release is the document of that event.
    const risk: EventRisk = {
      next_event: event({ event_id: "EIA", name: "EIA report", time_utc: "2026-10-07T14:30:00+00:00" }),
      released_today: [
        {
          event_id: "FOMC-MIN",
          name: "FOMC minutes",
          time_utc: "2026-10-02T09:00:00+00:00",
          document: { title: "Minutes of the Federal Open Market Committee", published_at: "2026-10-02T09:00:05+00:00" },
          affected_assets: [{ symbol: "US2Y", weight: 0.9 }, { symbol: "DXY" }],
        },
        {
          event_id: "BLS-20261002-US_NFP",
          name: NFP,
          time_utc: "2026-10-02T12:30:00+00:00",
          summary: "Both payroll employment (+29,000) and unemployment rate (4.2%) change little in September",
          news_event_id: "538f1cd8",
          affected_assets: ["DXY", "US2Y", "SP500", "DXY", ""],
        },
        { event_id: "BLS-20261002-US_NFP", name: "duplicate" },
        { event_id: "NAMELESS" },
        "not an entry",
      ] as never,
    };
    expect(releasedToday(risk)).toEqual([
      {
        key: "BLS-20261002-US_NFP",
        name: NFP,
        summary: "Both payroll employment (+29,000) and unemployment rate (4.2%) change little in September",
        iso: "2026-10-02T12:30:00.000Z",
        assets: ["DXY", "US2Y", "SP500"],
        newsId: "538f1cd8",
      },
      {
        key: "FOMC-MIN",
        name: "FOMC minutes",
        summary: "Minutes of the Federal Open Market Committee",
        iso: "2026-10-02T09:00:05.000Z",
        assets: ["US2Y", "DXY"],
        newsId: null,
      },
    ]);
    expect(releasedToday(risk, 1).map((r) => r.key)).toEqual(["BLS-20261002-US_NFP"]);
    // While the key-event card still shows the released event, the card states what was published
    // and the timeline does not list the event a second time.
    const live = { ...risk, next_event: event({ phase: "IN_PROGRESS" }) };
    expect(releasedToday(live).map((r) => r.key)).toEqual(["FOMC-MIN"]);
    expect(publishedSummary(live, live.next_event)).toBe(
      "Both payroll employment (+29,000) and unemployment rate (4.2%) change little in September",
    );
    expect(publishedSummary(risk, risk.next_event)).toBeNull();
    // A diverging reaction puts the post-event panel on the opening screen; its facts state the release.
    const post = (divergence: boolean) => ({
      ...risk,
      post_event: [{ event_id: "BLS-20261002-US_NFP", phase: "POST_EVENT", divergence, reactions: {} }],
    });
    expect(releasedToday(post(true)).map((r) => r.key)).toEqual(["FOMC-MIN"]);
    expect(releasedToday(post(false)).map((r) => r.key)).toEqual(["BLS-20261002-US_NFP", "FOMC-MIN"]);
    // Without a document summary the stored series values are the published text.
    const values = { released_today: [{ event_id: "X", name: "US NFP", values_text: "Nonfarm payrolls +29k; Unemployment rate 4.2%" }] };
    expect(releasedToday(values as never)[0].summary).toBe("Nonfarm payrolls +29k; Unemployment rate 4.2%");
    // A backend without the list, or a malformed one.
    expect(releasedToday({ next_event: null })).toEqual([]);
    expect(releasedToday({ released_today: "NFP" } as never)).toEqual([]);
    expect(releasedToday(null)).toEqual([]);
  });

  it("reads plain calendar rows for a backend without the event layer", () => {
    expect(
      calendarUpcoming(
        [
          { id: "a", name: "US CPI", scheduled_utc: "2026-10-02T12:30:00Z", impact: 9 },
          { id: "b", name: "Auction", scheduled_at: "2026-10-05T17:00:00Z", impact: "high" },
          { id: "c", name: "", scheduled_utc: "2026-10-05T17:00:00Z" },
          { id: "d", name: "No time" },
        ],
        NOW,
      ),
    ).toEqual([
      { key: "a", name: "US CPI", when: "15:30", iso: "2026-10-02T12:30:00.000Z", impact: "9/10" },
      { key: "b", name: "Auction", when: "Mon 5 Oct 20:00", iso: "2026-10-05T17:00:00.000Z", impact: "HIGH" },
    ]);
  });
});

describe("positioning group notes", () => {
  const item = (symbol: string, facts: string[]): MarketViewItem => ({ symbol, reason_facts: facts });
  const HIST = "Historical context only; current-session confirmation is unavailable";
  const OWN = "current price change does not confirm (no move beyond noise in the implied direction)";
  const group = [
    item("DXY", ["USD strength: DXY ↑", "DXY +0.49% (last close Thu 1 Oct)", HIST, OWN]),
    item("USDJPY", ["USD strength: USDJPY ↑", "USDJPY +0.63% (last close Thu 1 Oct)", HIST, OWN]),
    item("EURUSD", ["USD strength: EURUSD ↓", "EURUSD -0.50% (last close Thu 1 Oct)", HIST, OWN]),
    item("GOLD", ["USD strength: GOLD ↓", "GOLD +0.43% (24h via PAXG)", OWN]),
  ];

  it("writes what the group shares once, most widely shared first, and names the exceptions", () => {
    expect(groupNotes(group)).toEqual([
      "Current price change does not confirm (no move beyond noise in the implied direction)",
      `${HIST} (except GOLD)`,
    ]);
  });

  it("lists the assets a note applies to when most of the group is excepted", () => {
    const wide = [...group, item("BTC", [OWN]), item("ETH", [OWN])];
    expect(groupNotes(wide)[1]).toBe(`${HIST} (DXY, USDJPY, EURUSD)`);
  });

  it("never repeats per-asset facts, and keeps a single asset's general reasons", () => {
    expect(groupNotes(group).join(" ")).not.toMatch(/DXY \+0\.49%|USD strength/);
    expect(groupNotes([item("BTC", ["Rates ↓ support BTC", "live quote confirms"])])).toEqual(["Live quote confirms"]);
    expect(groupNotes([])).toEqual([]);
  });

  it("keeps for an asset's tooltip every reason the group note does not state", () => {
    expect(ownFacts(group[3], group)).toEqual(["USD strength: GOLD ↓", "GOLD +0.43% (24h via PAXG)"]);
    // A general reason only one asset carries is not a group note: it stays with that asset.
    const pair = [item("NASDAQ", ["Risk-on: NASDAQ ↑", "US10Y -6 bp"]), item("BTC", ["Risk-on: BTC ↑"])];
    expect(groupNotes(pair)).toEqual([]);
    expect(ownFacts(pair[0], pair)).toEqual(["Risk-on: NASDAQ ↑", "US10Y -6 bp"]);
  });
});

describe("no actionable setups", () => {
  it("waits for the next key event while one is pending", () => {
    expect(noSetupsReason({ next_event: event() }, NOW)).toBe("waiting for the next key event");
    expect(noSetupsReason({ mode: { active: true } }, NOW)).toBe("waiting for the next key event");
    expect(noSetupsReason({ next_event: event({ timing: "ANYTIME", time_utc: null }) }, NOW)).toBe(
      "waiting for the next key event",
    );
  });

  it("gives the standing reason when no event is near, was released, or the layer is absent", () => {
    expect(noSetupsReason({ next_event: event({ time_utc: "2026-10-07T18:00:00+00:00" }) }, NOW)).toBe(NO_SETUPS_REASON);
    expect(noSetupsReason({ next_event: event({ phase: "RELEASED" }) }, NOW)).toBe(NO_SETUPS_REASON);
    expect(noSetupsReason({ next_event: null }, NOW)).toBe(NO_SETUPS_REASON);
    expect(noSetupsReason(null, NOW)).toBe(NO_SETUPS_REASON);
  });
});
