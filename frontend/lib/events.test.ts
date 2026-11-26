import { describe, expect, it } from "vitest";
import type { MarketEvent } from "./api";
import {
  annotate,
  clampText,
  confirmationState,
  directionLabel,
  eventCountText,
  eventRowAssets,
  eventRowSources,
  eventSearchFields,
  eventHeadline,
  eventTitle,
  evidenceCopyNote,
  evidenceNumbers,
  evidenceSummary,
  evidenceUrlKey,
  formatFactConfidence,
  groupEvidence,
  importanceBasisText,
  inactivePathsNote,
  inactiveReason,
  interpretationModel,
  lagText,
  localizeTimes,
  oneSentence,
  otherPublication,
  otherPublicationText,
  pathName,
  pathStrengthText,
  reactionStatusLabel,
  reactionTiming,
  ruleLabel,
  ruleTopic,
  publishedText,
  sortByPublication,
  sourceHome,
  sourceText,
  topicLabel,
  triggerBasisText,
  ungradedReaction,
} from "./events";
import { matchesSearch } from "./format";

const event = (extra: Partial<MarketEvent> = {}): MarketEvent => ({
  id: "e1",
  summary_fact: "Iran announced inspections of tankers in the Strait of Hormuz. Further details pending.",
  event_type: "GEOPOLITICAL_ENERGY",
  verification_status: "CONFIRMED",
  fact_confidence: 90,
  first_seen_utc: "2026-09-26T11:30:00Z",
  last_updated_utc: "2026-09-26T11:50:00Z",
  assets: [],
  sources: [],
  interpretation: null,
  ...extra,
});

describe("event titles in topic lists", () => {
  it("keeps a topic set by a causal rule as the title", () => {
    const title = eventTitle(event({ topic: "IRAN_HORMUZ", topic_basis: "rule" }));
    expect(title).toEqual({ text: "Iran / Hormuz", full: "Iran / Hormuz", headline: false });
    expect(eventTitle(event({ topic: "FED_POLICY", topic_basis: "rule" })).text).toBe("Fed / Policy");
  });
  it("uses the headline for a topic guessed from entities, a missing basis or no topic", () => {
    for (const extra of [
      { topic: "US_CHINA", topic_basis: "entities" },
      { topic: "US_CHINA" },
      { topic: "US_CHINA", topic_basis: null },
      { topic: null, topic_basis: null },
    ] as Partial<MarketEvent>[]) {
      const title = eventTitle(event({ ...extra, summary_fact: "Tariff report was denied by the ministry." }));
      expect(title).toEqual({
        text: "Tariff report was denied by the ministry.",
        full: "Tariff report was denied by the ministry.",
        headline: true,
      });
    }
  });
  it("clamps a long headline at a word boundary and keeps the full text", () => {
    const long = `${"Officials said the central bank would review its balance sheet policy ".repeat(4)}soon.`;
    const title = eventTitle(event({ topic: "CENTRAL_BANK", topic_basis: "entities", summary_fact: long }));
    expect(title.headline).toBe(true);
    expect(title.text.length).toBeLessThanOrEqual(140);
    expect(title.text.endsWith("…")).toBe(true);
    expect(title.text).not.toMatch(/\s…$/);
    expect(title.full).toBe(long.replace(/\s+/g, " ").trim());
    expect(clampText("short", 140)).toBe("short");
  });
  it("falls back to the topic or event type when there is no headline", () => {
    expect(eventTitle(event({ topic: "US_CHINA", topic_basis: "entities", summary_fact: "  " })).text).toBe(
      "US / China",
    );
    expect(eventTitle(event({ summary_fact: "" })).text).toBe("Geopolitical Energy");
    expect(topicLabel(event({ event_type: "" }))).toBe("Unclassified");
  });
  it("keeps the first sentence helper", () => {
    expect(oneSentence("One. Two.")).toBe("One.");
    expect(oneSentence("")).toBe("");
  });
});

describe("event list rules", () => {
  it("orders by publication, falling back to first sighting, newest first", () => {
    const list = sortByPublication([
      event({ id: "old", event_time: "2026-06-17T10:00:00Z", first_seen_utc: "2026-09-27T05:29:00Z" }),
      event({ id: "new", event_time: "2026-09-26T16:26:00Z", first_seen_utc: "2026-09-27T05:28:00Z" }),
      event({ id: "unpublished", event_time: undefined, first_seen_utc: "2026-09-27T01:00:00Z" }),
    ]);
    expect(list.map((e) => e.id)).toEqual(["unpublished", "new", "old"]);
  });
  it("never invents a clock time for a publication dated without one (ER-5)", () => {
    // An OFAC notice dated 2 Oct: stored at the start of the US Eastern day (04:00Z = 07:00 Bucharest).
    const dated = { event_time: "2026-10-02T04:00:00Z", first_seen_utc: "2026-10-02T19:35:00Z" };
    expect(publishedText(dated)).toBe("Fri 2 Oct 07:00");
    expect(publishedText({ ...dated, event_time_known: false })).toBe("Fri 2 Oct");
    expect(publishedText({ ...dated, event_time_known: true })).toBe("Fri 2 Oct 07:00");
    expect(publishedText({ ...dated, published_date_only: true })).toBe("Fri 2 Oct");
    // Without a publication time the first sighting is shown, whatever the flag says.
    expect(publishedText({ first_seen_utc: "2026-10-02T19:35:00Z", event_time_known: false })).toBe("Fri 2 Oct 22:35");
  });
  it("states the honest total, the matches and the shown page", () => {
    expect(eventCountText({ total: 308, matching: 308, shown: 50, filtered: false })).toBe("308 events, showing 50");
    expect(eventCountText({ total: 308, matching: 12, shown: 12, filtered: true })).toBe("12 of 308 events match");
    expect(eventCountText({ total: 1, matching: 1, shown: 1, filtered: true })).toBe("1 of 1 event matches");
    expect(eventCountText({ total: 500, matching: 500, shown: 50, filtered: false })).toBe(
      "500 events, showing 50 (the newest 500 are loaded; older events are not)",
    );
  });
  it("searches what a row shows, never ids or hidden provenance", () => {
    const e = event({
      id: "ac22cda5-6311-4f82-b115-2e15b5f5a7f9",
      summary_fact: "Minutes of the Monetary Policy Meeting",
      verification_status: "PARTIALLY_CONFIRMED",
      assets: ["USDJPY"],
      sources: ["Bank of Japan"],
      ...({ provenance: "provider_retrieval" } as Partial<MarketEvent>),
    });
    const match = (q: string) => matchesSearch(e, q, eventSearchFields);
    expect(match("monetary policy")).toBe(true);
    expect(match("partially confirmed")).toBe(true);
    expect(match("usdjpy")).toBe(true);
    expect(match("bank of japan")).toBe(true);
    expect(match("provider_retrieval")).toBe(false);
    expect(match("ac22cda5")).toBe(false);
  });
  it("searches only what the row renders: no event type, topic, hidden symbols or further sources", () => {
    const e = event({
      event_type: "OFFICIAL_RELEASE",
      topic: "FED_POLICY",
      topic_basis: "rule",
      assets: ["USDJPY"],
      affected_assets: ["NIKKEI"],
      sources: ["Fed", "BLS", "BEA", "Treasury"],
      evidence: [{ id: "x", source: "Hidden wire" }],
    });
    const match = (q: string) => matchesSearch(e, q, eventSearchFields);
    expect(match("official release")).toBe(false);
    expect(match("fed / policy")).toBe(false);
    expect(match("nikkei")).toBe(false);
    expect(match("treasury")).toBe(false);
    expect(match("hidden wire")).toBe(false);
    expect(match("bea")).toBe(true);
    expect(eventRowSources(e)).toEqual({ shown: ["Fed", "BLS", "BEA"], more: 1 });
    expect(eventRowAssets(e)).toEqual(["USDJPY"]);
    expect(eventRowAssets(event({ assets: [], affected_assets: ["BTC"] }))).toEqual(["BTC"]);
    expect(eventRowSources(event({ sources: [{ name: "ECB" }, "ECB", { source: "BoE" }, 7] }))).toEqual({
      shown: ["ECB", "BoE"],
      more: 0,
    });
  });
  it("shows a topic badge only for a topic a causal rule set", () => {
    expect(ruleTopic(event({ topic: "FED_POLICY", topic_basis: "rule" }))).toBe("Fed / Policy");
    expect(ruleTopic(event({ topic: "CHINA_CHINA", topic_basis: "entities" }))).toBeNull();
    expect(ruleTopic(event({ topic: "AMERICA_AMERICAN" }))).toBeNull();
  });
});

describe("fact confidence", () => {
  it("uses one precision everywhere and never rounds up to certainty", () => {
    expect(formatFactConfidence(99.58)).toBe("99.5%");
    expect(formatFactConfidence(73.39)).toBe("73.3%");
    expect(formatFactConfidence(100)).toBe("100%");
    expect(formatFactConfidence(73)).toBe("73%");
    expect(formatFactConfidence(0)).toBe("0%");
    expect(formatFactConfidence(null)).toBe("Unavailable");
  });
});

describe("evidence documents", () => {
  const boj = (id: string, url: string, extra: Record<string, unknown> = {}) => ({
    id,
    url,
    source: "Bank of Japan",
    relation: "supports",
    ...extra,
  });
  it("collapses the http and https copies of one document and numbers the rest", () => {
    const groups = groupEvidence([
      boj("db63739a-0000-4000-8000-000000000001", "http://www.boj.or.jp/en/mopo/g260731.pdf"),
      boj("db63739a-0000-4000-8000-000000000002", "https://www.boj.or.jp/en/mopo/g260731.pdf"),
      boj("db63739a-0000-4000-8000-000000000003", "https://boj.or.jp/en/mopo/other.pdf"),
      boj("db63739a-0000-4000-8000-000000000004", "https://www.boj.or.jp/en/mopo/g260731.pdf", {
        relation: "contradicts",
      }),
      boj("no-url", ""),
    ]);
    expect(groups.map((g) => [g.number, g.copies.length])).toEqual([
      [1, 2],
      [2, 1],
      [3, 1],
      [4, 1],
    ]);
    expect(groups[0].item.url).toBe("https://www.boj.or.jp/en/mopo/g260731.pdf");
    const numbers = evidenceNumbers(groups);
    expect(numbers.get("db63739a-0000-4000-8000-000000000001")).toBe(1);
    expect(numbers.get("db63739a-0000-4000-8000-000000000002")).toBe(1);
    expect(numbers.get("no-url")).toBe(4);
    expect(evidenceUrlKey("http://www.Example.com/a/")).toBe(evidenceUrlKey("https://example.com/a"));
    expect(evidenceUrlKey("not a url")).toBeNull();
    expect(evidenceCopyNote(groups[0])).toBe(
      "2 copies of this address received (http/https or www variants), counted once",
    );
    expect(evidenceCopyNote(groups[1])).toBeNull();
  });
  it("names an updated version under the same address as such and shows the latest", () => {
    const url = "https://www.benzinga.com/markets/tech/26/09/62013860/chip-curbs";
    const [group] = groupEvidence([
      boj("v1", url, { title: "Chip Stocks Slide Amid US Curbs", ingested_at: "2026-09-28T06:45:00Z" }),
      boj("v2", url, { title: "Chip Stocks Slide Amid US Curbs (UPDATED)", ingested_at: "2026-09-28T07:50:00Z" }),
    ]);
    expect(group.copies).toHaveLength(2);
    expect(group.item.id).toBe("v2");
    expect(evidenceCopyNote(group)).toBe(
      "Updated version received Mon 28 Sep 10:50 (Bucharest); the latest is shown, counted once",
    );
    expect(evidenceCopyNote(group)).not.toContain("http/https");
    // The same file re-sent under the same address with the same headline is a copy, not a version.
    const [resent] = groupEvidence([boj("r1", url, { title: "A" }), boj("r2", url, { title: " a " })]);
    expect(evidenceCopyNote(resent)).toBe("2 copies of this document received, counted once");
    // A version plus an http copy: both are said, the https copy of the latest version is shown.
    const [mixed] = groupEvidence([
      boj("m1", url, { title: "A", ingested_at: "2026-09-28T06:45:00Z" }),
      boj("m2", url.replace("https:", "http:"), { title: "A (UPDATED)", ingested_at: "2026-09-28T07:50:00Z" }),
      boj("m3", url, { title: "A (UPDATED)", ingested_at: "2026-09-28T07:50:00Z" }),
    ]);
    expect(mixed.item.id).toBe("m3");
    expect(evidenceCopyNote(mixed)).toBe(
      "Updated version received Mon 28 Sep 10:50 (Bucharest), plus http/https or www copies of the address; the latest is shown, counted once",
    );
  });
  it("flags a document from another publication date only with the API's same-story window", () => {
    const doc = { published_at: "2026-09-14T08:00:00Z" };
    const release = { event_time: "2026-09-25T08:00:00Z", same_story_window_hours: 18 };
    const gap = otherPublication(doc, release);
    expect(gap).toEqual({ gapMs: 264 * 3_600_000, before: true });
    expect(otherPublicationText(gap!)).toBe(
      "Published 11 days before this event: probably a separate release, not another report of it",
    );
    // Within the window (a wire update hours later), no window sent, or a missing time: nothing is guessed.
    expect(otherPublication({ published_at: "2026-09-25T20:00:00Z" }, release)).toBeNull();
    expect(otherPublication(doc, { event_time: release.event_time })).toBeNull();
    expect(otherPublication({ published_at: null }, release)).toBeNull();
    expect(otherPublication(doc, { ...release, event_time: undefined })).toBeNull();
  });
  it("counts documents, not copies, the same way on every page", () => {
    const groups = groupEvidence([
      boj("a", "http://www.boj.or.jp/en/mopo/g260731.pdf", { group: "boj" }),
      boj("b", "https://www.boj.or.jp/en/mopo/g260731.pdf", { group: "boj" }),
      boj("c", "https://www.reuters.com/x", { group: "reuters", relation: "contradicts" }),
      boj("d", "https://example.com/y", { group: null, source_id: "wire", source: "Wire" }),
    ]);
    expect(evidenceSummary(groups)).toEqual({
      documents: 3,
      supporting: 2,
      contradicting: 1,
      duplicates: 1,
      independentGroups: 3,
    });
    expect(evidenceSummary([])).toEqual({
      documents: 0,
      supporting: 0,
      contradicting: 0,
      duplicates: 0,
      independentGroups: 0,
    });
  });
  it("turns extractor timestamps and evidence ids into Bucharest times and references", () => {
    const id = "b6a7e902-4bff-4d72-8852-daac446a5cad";
    const numbers = new Map([[id, 2]]);
    const parts = annotate(
      `Article published 2026-09-27T17:30:24+00:00; first seen 2026-09-27T17:35:29 UTC (evidence: ${id})`,
      numbers,
    );
    const shown = parts.map((p) => (p.kind === "text" ? p.text : `[${p.number}]`)).join("");
    expect(shown).toBe("Article published Sun 27 Sep 20:30 (Bucharest); first seen Sun 27 Sep 20:35 (Bucharest)[2]");
    expect(shown).not.toMatch(/2026-09-27T|b6a7e902/);
    const unknown = annotate(`Claim (evidence db63739a-1111-4000-8000-000000000009, ${id})`, numbers);
    expect(unknown.filter((p) => p.kind === "evidence")).toEqual([
      { kind: "evidence", id: "db63739a-1111-4000-8000-000000000009", number: null },
      { kind: "evidence", id, number: 2 },
    ]);
    // A zone-less timestamp is ambiguous: it stays as written.
    expect(localizeTimes("at 2026-09-27T17:30")).toBe("at 2026-09-27T17:30");
  });
});

describe("AI interpretation", () => {
  it("turns specialist hypotheses into cards instead of JSON", () => {
    const model = interpretationModel({
      hypotheses: [
        {
          asset: "JPY (Japanese yen, e.g., USD/JPY)",
          direction: "neutral",
          causal_path: ["The Bank of Japan published minutes", "No new policy signal"],
          evidence_ids: ["db63739a"],
          confirmation: "USD/JPY stable",
          invalidation: "Yen rallies 1%",
        },
      ],
      uncertainties: ["The minutes are backward-looking."],
    });
    expect(model?.heading).toBe("Hypotheses");
    expect(model?.cards).toEqual([
      {
        title: "JPY (Japanese yen, e.g., USD/JPY)",
        direction: "neutral",
        path: ["The Bank of Japan published minutes", "No new policy signal"],
        facts: [],
        lists: [],
        confirmation: "USD/JPY stable",
        invalidation: "Yen rallies 1%",
        evidenceIds: ["db63739a"],
      },
    ]);
    expect(model?.uncertainties).toEqual(["The minutes are backward-looking."]);
    expect(model?.other).toEqual({});
    expect(directionLabel("neutral")).toBe("Neutral");
    expect(directionLabel("USD-positive")).toBe("USD positive");
    expect(directionLabel("USD-negative")).toBe("USD negative");
    expect(directionLabel("neutral-wait")).toBe("Neutral (wait)");
    expect(directionLabel("NEUTRAL-WAIT")).toBe("Neutral (wait)");
    expect(directionLabel("Mildly supportive for USD")).toBe("Mildly supportive for USD");
  });
  it("labels FX and development fields and keeps unknown scalars for a plain list", () => {
    const fx = interpretationModel({
      pairs: [
        { pair: "EURUSD", bias: "USD-positive", base_bank: "ECB", yield_differential_view: "Widening", causal_path: ["a", "b"] },
      ],
      uncertainties: [],
      note: "extra",
    });
    expect(fx?.heading).toBe("Currency pairs");
    expect(fx?.cards[0].title).toBe("EURUSD");
    expect(fx?.cards[0].direction).toBe("USD-positive");
    expect(fx?.cards[0].facts).toEqual([
      { label: "Base bank", value: "ECB" },
      { label: "Yield differential", value: "Widening" },
    ]);
    expect(fx?.other).toEqual({ note: "extra" });
    const geo = interpretationModel({
      developments: [{ summary: "Strait closed", market_relevant: true, pathway: ["x", "y"], affected_assets: ["BRENT"] }],
      uncertainties: [],
    });
    expect(geo?.cards[0].lists).toEqual([{ label: "Affected assets", items: ["BRENT"] }]);
    expect(geo?.cards[0].path).toEqual(["x", "y"]);
    expect(interpretationModel(null)).toBeNull();
    expect(interpretationModel({ hypotheses: [], uncertainties: [] })?.cards).toEqual([]);
  });
});

describe("reaction timing and confirmation", () => {
  it("flags reactions of an event first seen after the configured publication lag", () => {
    const late = reactionTiming({
      event_time: "2026-09-24T14:46:00Z",
      first_seen_utc: "2026-09-27T05:29:56Z",
      reactions: [
        { symbol: "BTC", horizon: "15m", status: "DIVERGENT", baseline_timestamp: "2026-09-27T05:29:50Z" },
        { symbol: "ETH", horizon: "15m", status: "DIVERGENT", baseline_timestamp: "2026-09-27T05:29:56Z" },
      ],
    });
    expect(late.measuredFrom).toBe("2026-09-27T05:29:56.000Z");
    expect(late.late).toBe(true);
    expect(lagText(late.publicationLagMs ?? 0)).toBe("62 h");
    expect(lagText(late.baselineLagMs ?? 0)).toBe("62 h");
    // Without the API value the documented default (900 s) applies.
    expect(late.maxLagMs).toBe(900_000);
    const prompt = reactionTiming({
      event_time: "2026-09-27T05:25:00Z",
      first_seen_utc: "2026-09-27T05:29:00Z",
      reactions: [{ symbol: "BTC", horizon: "1m", status: "CONFIRMED", baseline_timestamp: "2026-09-27T05:29:00Z" }],
    });
    expect(prompt.late).toBe(false);
    expect(lagText(prompt.baselineLagMs ?? 0)).toBe("4 min");
    expect(
      reactionTiming({ event_time: "2026-09-27T05:25:00Z", first_seen_utc: "2026-09-27T05:26:00Z", reactions: [] }),
    ).toEqual({
      measuredFrom: null,
      baselineLagMs: null,
      publicationLagMs: 60_000,
      maxLagMs: 900_000,
      late: false,
    });
    expect(lagText(30_000)).toBe("under 1 min");
    expect(lagText(4 * 3_600_000 + 10 * 60_000)).toBe("4 h 10 min");
    expect(lagText(293 * 3_600_000)).toBe("12 days");
  });
  it("uses the backend's limit: 15 to 30 min after publication is already late", () => {
    const base = { event_time: "2026-09-27T05:00:00Z", first_seen_utc: "2026-09-27T05:20:00Z" };
    expect(reactionTiming(base).late).toBe(true);
    expect(reactionTiming({ ...base, first_seen_utc: "2026-09-27T05:15:00Z" }).late).toBe(false);
    // The API's configured limit and lag win over the default and the client-side computation.
    expect(reactionTiming({ ...base, max_publication_lag_seconds: 1800 }).late).toBe(false);
    expect(reactionTiming({ ...base, max_publication_lag_seconds: 1800 }).maxLagMs).toBe(1_800_000);
    expect(reactionTiming({ ...base, publication_lag_seconds: 60 }).late).toBe(false);
    // Without a publication time there is no lag, as in the backend.
    expect(reactionTiming({ first_seen_utc: "2026-09-27T05:20:00Z" }).late).toBe(false);
  });
  it("tells 'nothing expected' apart from 'expected but not measured'", () => {
    expect(confirmationState({ horizon: "1h", confirmation_pct: 50 })).toBe("measured");
    expect(confirmationState({ horizon: "1h", confirmation_pct: null, expected: 0, measured: 0 })).toBe(
      "nothing-expected",
    );
    expect(
      confirmationState({ horizon: "1h", confirmation_pct: null, expected: 3, measured: 0, missing: ["BTC"] }),
    ).toBe("no-data");
    // Older payloads without `expected`: empty lists mean nothing was expected.
    expect(confirmationState({ horizon: "1h", confirmation_pct: null, confirming: [], missing: [] })).toBe(
      "nothing-expected",
    );
    expect(reactionStatusLabel("NOT_EXPECTED")).toBe("NO EXPECTATION");
    expect(reactionStatusLabel("NO_REACTION")).toBe("NO REACTION");
    expect(reactionStatusLabel("DIVERGENT")).toBe("DIVERGENT");
  });
});

describe("causal path captions", () => {
  it("names rules, explains strength and says why a path is inactive", () => {
    expect(ruleLabel("crypto-regulation")).toBe("Crypto regulation");
    expect(ruleLabel("us-inflation")).toBe("US inflation");
    expect(ruleLabel(undefined, "Path 1")).toBe("Path 1");
    // The rule's configured name wins over its id.
    expect(pathName({ rule_id: "crypto-regulation", label: "Tighter crypto regulation" }, 0)).toBe(
      "Tighter crypto regulation",
    );
    expect(pathName({ rule_id: "hormuz-oil", label: "  " }, 0)).toBe("Hormuz oil");
    expect(pathName({ label: null }, 2)).toBe("Path 3");
    expect(pathStrengthText({ strength: 60, decay: 0.08, half_life_hours: 24 })).toBe(
      "Strength 60 · 8% of the initial effect remains (half-life 24 h)",
    );
    expect(pathStrengthText({ strength: 70, decay: 1 })).toBe("Strength 70 · full effect");
    expect(pathStrengthText({ strength: 60, decay: 0 })).toBe(
      "Strength 60 · under 0.1% of the initial effect remains",
    );
    // `strength` is regime- and exception-adjusted; the configured rule strength is named when it differs.
    expect(pathStrengthText({ strength: 78, strength_prior: 60, regime_multiplier: 1.3, decay: 1 })).toBe(
      "Strength 78 (rule 60, adjusted for the current regime) · full effect",
    );
    expect(pathStrengthText({ strength: 30, strength_prior: 60, regime_multiplier: 1, exceptions_fired: ["x"] })).toBe(
      "Strength 30 (rule 60, adjusted by exceptions)",
    );
    expect(
      pathStrengthText({ strength: 39, strength_prior: 60, regime_multiplier: 1.3, exceptions_fired: ["x"] }),
    ).toBe("Strength 39 (rule 60, adjusted for the current regime and exceptions)");
    expect(pathStrengthText({ strength: 60, strength_prior: 60, regime_multiplier: 1 })).toBe("Strength 60");
    expect(pathStrengthText({})).toBeNull();
    expect(inactiveReason({ active: true })).toBeNull();
    expect(inactiveReason({ active: false, suppressed_by: "posture-risk-on" })).toBe(
      'Suppressed by the exception "Posture risk on".',
    );
    expect(inactiveReason({ active: false, suppressed_by: null, decay: 0 })).toBe(
      "Inactive: the effect has faded with time since publication.",
    );
  });
  it("qualifies the affected assets when no causal path is active", () => {
    const faded = { active: false, suppressed_by: null, decay: 0 };
    const suppressed = { active: false, suppressed_by: "posture-risk-on", decay: 1 };
    expect(inactivePathsNote([faded, { ...faded, decay: 0.2 }])).toBe("no active causal path: the effect has faded");
    expect(inactivePathsNote([suppressed])).toBe("no active causal path: suppressed by an exception");
    expect(inactivePathsNote([faded, suppressed])).toBe("no active causal path");
    expect(inactivePathsNote([faded, { active: true }])).toBeNull();
    expect(inactivePathsNote([])).toBeNull();
    expect(inactivePathsNote(undefined)).toBeNull();
  });
  it("captions importance and trigger bases in plain English", () => {
    expect(importanceBasisText("source tier")).toBe("source trust tier only; not assessed by the extractor");
    expect(importanceBasisText("source tier x extractor importance")).toBe(
      "source trust tier × importance assessed by the AI extractor",
    );
    expect(importanceBasisText(null)).toBeNull();
    expect(triggerBasisText("rule")).toBe("matched a trigger rule");
    expect(triggerBasisText("none")).toBeNull();
    expect(triggerBasisText("macro_surprise")).toBe("macro surprise");
  });
});

describe("source registry homes", () => {
  it("links the X account of an X source and the listed homepage of any other, never the feed", () => {
    expect(sourceHome({ x_account: "@fed", homepage: null })).toEqual({
      href: "https://x.com/fed",
      label: "@fed on X",
    });
    expect(sourceHome({ homepage: "https://www.boj.or.jp/en/" })).toEqual({
      href: "https://www.boj.or.jp/en/",
      label: "boj.or.jp",
    });
    expect(sourceHome({ homepage: " https://sanctionslistservice.ofac.treas.gov/ " })).toEqual({
      href: "https://sanctionslistservice.ofac.treas.gov/",
      label: "sanctionslistservice.ofac.treas.gov",
    });
    expect(sourceHome({ homepage: " " })).toBeUndefined();
    expect(sourceHome({ homepage: "javascript:alert(1)" })).toBeUndefined();
    expect(sourceHome({})).toBeUndefined();
  });
  it("never builds a link from the bare domain (boj.or.jp, example.invalid have no site there)", () => {
    const registryRow = { domain: "boj.or.jp", homepage: null, x_account: null };
    expect(sourceHome(registryRow)).toBeUndefined();
    const devStub = { domain: "example.invalid", homepage: null };
    expect(sourceHome(devStub)).toBeUndefined();
  });
});

describe("feed text on the event pages", () => {
  it("shows headlines as plain text wherever an event title is used", () => {
    const e = event({
      summary_fact: "Inquiry Into Apple's Competitor Dynamics In Technology Hardware, Storage &amp; Peripherals Industry",
    });
    expect(eventHeadline(e)).toBe(
      "Inquiry Into Apple's Competitor Dynamics In Technology Hardware, Storage & Peripherals Industry",
    );
    expect(eventTitle(e).full).not.toContain("&amp;");
    expect(oneSentence("S&amp;P <b>rallies</b>. More text.")).toBe("S&P rallies.");
    expect(matchesSearch(e, "storage & peripherals", eventSearchFields)).toBe(true);
    expect(matchesSearch(e, "&amp;", eventSearchFields)).toBe(false);
  });
  it("strips excerpt markup, names topic codes and localises timestamps", () => {
    expect(
      sourceText(
        "<p>For over 250 years, America’s hunters … lands and waters. &#160;On National Hunting and Fishing Day&#8230;</p>",
      ),
    ).toBe("For over 250 years, America’s hunters … lands and waters. On National Hunting and Fishing Day…");
    expect(sourceText('Posted <time datetime="2026-07-22T14:38:46-04:00">July 22, 2026</time>')).toBe(
      "Posted July 22, 2026",
    );
    expect(sourceText("Programs matched: SANCTIONS_RUSSIA, SANCTIONS_VENEZUELA")).toBe(
      "Programs matched: Sanctions / Russia, Sanctions / Venezuela",
    );
    expect(
      sourceText("OFAC Sanctions List Service publication 984 (Standard Action) at 2026-09-23T14:07:28.650545+00:00: 2 removals"),
    ).toBe("OFAC Sanctions List Service publication 984 (Standard Action) at Wed 23 Sep 17:07 (Bucharest): 2 removals");
    expect(sourceText(null)).toBe("");
  });
});

describe("ungradedReaction", () => {
  it("flags only NOT_EXPECTED reactions of events classified on read that a rule now expects to move", () => {
    const legacy = { classified_on_read: true };
    expect(ungradedReaction(legacy, "NOT_EXPECTED", 1)).toBe(true);
    expect(ungradedReaction(legacy, "NOT_EXPECTED", 0)).toBe(false);
    expect(ungradedReaction(legacy, "CONFIRMED", 1)).toBe(false);
    expect(ungradedReaction({}, "NOT_EXPECTED", -1)).toBe(false);
  });
});
