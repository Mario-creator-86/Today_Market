import { afterEach, describe, expect, it, vi } from "vitest";
import {
  adminHeaders,
  analysisView,
  api,
  ApiError,
  briefLateText,
  combineAssetQuotes,
  describeError,
  durationText,
  errorDetail,
  isTransientError,
  labelText,
  LOCAL_ADMIN,
  localTime,
  mergeAssetQuotes,
  NetworkError,
  normalizeApiUrl,
  number,
  parseAssetQuotes,
  retriesAfter,
  safeUrl,
  unreadAlertCount,
  type Asset,
} from "./api";
describe("honest data presentation", () => {
  it("says when a morning brief was published after its schedule (AFP-05)", () => {
    expect(briefLateText({ late: true, minutes_late: 47, scheduled_local: "09:00" })).toBe(
      "published 47 min after the 09:00 schedule",
    );
    expect(briefLateText({ late: true, minutes_late: 815, scheduled_local: "09:00" })).toBe(
      "published 13 h 35 min after the 09:00 schedule",
    );
    expect(briefLateText({ late: true, minutes_late: 120 })).toBe("published 2 h after its schedule");
    // On time, an intraday update, or a payload without the fields: nothing is said.
    expect(briefLateText({ late: false, minutes_late: 3, scheduled_local: "09:00" })).toBeNull();
    expect(briefLateText({ late: true, minutes_late: null })).toBeNull();
    expect(briefLateText({})).toBeNull();
    expect(briefLateText(null)).toBeNull();
  });
  it("accepts an origin or an explicit API URL without duplicating the prefix", () => {
    expect(normalizeApiUrl("http://127.0.0.1:8000")).toBe(
      "http://127.0.0.1:8000/api",
    );
    expect(normalizeApiUrl("http://127.0.0.1:8000/api/")).toBe(
      "http://127.0.0.1:8000/api",
    );
  });
  it("does not convert missing quotes into zero", () => {
    expect(number(null)).toBe("Unavailable");
    expect(number(NaN)).toBe("Unavailable");
    expect(number(0)).toBe("0");
  });
  it("rejects executable evidence links", () => {
    expect(safeUrl("javascript:alert(1)")).toBeUndefined();
    expect(safeUrl("https://example.com/evidence")).toBe(
      "https://example.com/evidence",
    );
  });
  it("handles missing and invalid timestamps", () => {
    expect(localTime(null)).toBe("Not recorded");
    expect(localTime("bad")).toBe("Not recorded");
    expect(localTime("2026-01-01T10:00:00Z")).toContain("12:00");
  });
  it("formats Bucharest times like event times: weekday, unpadded day, fixed month names", () => {
    // ICU may say "Sept" and pad the day ("01"); the app says "Sep" and "1" everywhere.
    expect(localTime("2026-09-27T23:10:49Z")).toBe("Mon 28 Sep 02:10");
    expect(localTime("2026-10-02T07:05:00Z")).toBe("Fri 2 Oct 10:05");
    expect(localTime("2026-12-31T22:30:00Z")).toBe("Fri 1 Jan 00:30");
    // One format with the event-time labels ("Thu 1 Oct 15:30 Bucharest").
    expect(localTime("2026-10-01T12:30:00Z")).toBe("Thu 1 Oct 15:30");
  });
});

describe("api() errors and bodies", () => {
  const reply = (status: number, body = "") =>
    vi.fn<typeof fetch>(async () => new Response(status === 204 ? null : body, { status }));
  afterEach(() => vi.unstubAllGlobals());

  it("reads FastAPI string and validation details", () => {
    expect(errorDetail({ detail: "Invalid administrative token" })).toBe("Invalid administrative token");
    expect(
      errorDetail({ detail: [{ loc: ["body", "values", "fred_api_key"], msg: "Input should be a valid string" }] }),
    ).toBe("fred_api_key: Input should be a valid string");
    expect(errorDetail({ detail: [{ loc: ["body"], msg: "fred_api_key: contains ${" }] })).toBe(
      "fred_api_key: contains ${",
    );
    expect(errorDetail(null)).toBe("");
    expect(errorDetail({ detail: [] })).toBe("");
  });
  it("reads a structured detail with the fields it names", () => {
    expect(
      errorDetail({
        detail: {
          message: "Re-enter the key: a stored key is never sent to a new address",
          fields: ["smtp_host", "smtp_password"],
        },
      }),
    ).toBe("Re-enter the key: a stored key is never sent to a new address Fields: smtp_host, smtp_password");
    expect(
      errorDetail({ detail: { message: "smtp_password must be re-entered with smtp_host", fields: ["smtp_host", "smtp_password"] } }),
    ).toBe("smtp_password must be re-entered with smtp_host");
    expect(errorDetail({ detail: { fields: ["ap_base_url", "ap_api_key"] } })).toBe(
      "Fields: ap_base_url, ap_api_key",
    );
    expect(errorDetail({ detail: {} })).toBe("");
  });
  it("puts the backend detail next to the status", async () => {
    vi.stubGlobal("fetch", reply(422, JSON.stringify({ detail: "fred_api_key: must not contain control characters" })));
    await expect(api("/x")).rejects.toThrow("Request failed (422): fred_api_key: must not contain control characters");
    vi.stubGlobal("fetch", reply(401, JSON.stringify({ detail: "Invalid administrative token" })));
    await expect(api("/x")).rejects.toThrow("Request failed (401): Invalid administrative token");
  });
  it("keeps the generic message when there is no JSON detail", async () => {
    vi.stubGlobal("fetch", reply(404, "Not Found"));
    await expect(api("/x")).rejects.toThrow("Request failed (404). Check the service");
    vi.stubGlobal("fetch", reply(500));
    await expect(api("/x")).rejects.toThrow("Request failed (500).");
  });
  it("returns undefined for empty bodies and JSON otherwise", async () => {
    vi.stubGlobal("fetch", reply(204));
    await expect(api("/x", { method: "PUT" })).resolves.toBeUndefined();
    vi.stubGlobal("fetch", reply(200, '{"ok":true}'));
    await expect(api("/x")).resolves.toEqual({ ok: true });
  });
  it("uses a default timeout but keeps a caller's signal", async () => {
    const fetch = reply(200, "[]");
    vi.stubGlobal("fetch", fetch);
    await api("/x");
    expect(fetch.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal);
    const signal = AbortSignal.timeout(45000);
    await api("/x", { signal, headers: { "X-Admin-Token": "t" } });
    const init = fetch.mock.calls[1][1];
    expect(init?.signal).toBe(signal);
    expect(init?.headers).toEqual({ "Content-Type": "application/json", "X-Admin-Token": "t" });
  });
  it("reports timeouts in plain words", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new DOMException("signal timed out", "TimeoutError");
      }),
    );
    await expect(api("/x")).rejects.toThrow("Request timed out.");
    await expect(api("/x")).rejects.toBeInstanceOf(NetworkError);
  });
  it("turns a failed connection into a plain NetworkError and keeps a caller's abort", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const failure = await api("/x").catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(NetworkError);
    expect((failure as NetworkError).timeout).toBe(false);
    expect((failure as Error).message).not.toContain("Failed to fetch");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new DOMException("aborted", "AbortError");
      }),
    );
    await expect(api("/x")).rejects.toMatchObject({ name: "AbortError" });
  });
  it("keeps the status and the backend's detail on the error", async () => {
    vi.stubGlobal("fetch", reply(404, JSON.stringify({ detail: "Asset not found" })));
    const failure = (await api("/x").catch((e: unknown) => e)) as ApiError;
    expect(failure).toBeInstanceOf(ApiError);
    expect(failure.status).toBe(404);
    expect(failure.detail).toBe("Asset not found");
  });
});

describe("error wording", () => {
  it("says not found for a 404, never that the service is unreachable", () => {
    expect(describeError(new ApiError("Request failed (404): Asset not found", 404, "Asset not found"))).toEqual({
      kind: "not_found",
      title: "Not found.",
      detail: "Asset not found",
    });
    // FastAPI's bare route 404 adds nothing to the title.
    expect(describeError(new ApiError("Request failed (404): Not Found", 404, "Not Found")).detail).toBe(
      "The service has no record at this address.",
    );
  });
  it("names the HTTP status when the service answered with an error", () => {
    expect(describeError(new ApiError("Request failed (500): Injected failure", 500, "Injected failure"))).toEqual({
      kind: "service",
      title: "The service returned an error (HTTP 500).",
      detail: "Injected failure",
    });
    expect(describeError(new SyntaxError("Unexpected token")).kind).toBe("service");
  });
  it("says unreachable only when no answer arrived, without browser jargon", () => {
    expect(describeError(new NetworkError("Unable to reach the data service.", false))).toEqual({
      kind: "network",
      title: "Unable to reach the data service.",
      detail: "Check that the backend is running.",
    });
    expect(describeError(new NetworkError("Request timed out.", true)).detail).toBe("It did not answer in time.");
  });
  it("retries only what may succeed later", () => {
    expect(isTransientError(new NetworkError("down", false))).toBe(true);
    expect(isTransientError(new ApiError("x", 503))).toBe(true);
    expect(isTransientError(new ApiError("x", 429))).toBe(true);
    expect(isTransientError(new ApiError("x", 404))).toBe(false);
    expect(isTransientError(new ApiError("x", 401))).toBe(false);
    expect(isTransientError(new ApiError("x", 422))).toBe(false);
    expect(isTransientError(new Error("x"))).toBe(false);
  });
  it("asks a failed query again in the background only when its own retry rule would", () => {
    const down = new NetworkError("down", false);
    const refused = new ApiError("x", 401);
    // Default (no rule of its own): transient errors only.
    expect(retriesAfter(undefined, down)).toBe(true);
    expect(retriesAfter(undefined, new ApiError("x", 404))).toBe(false);
    // Opted out.
    expect(retriesAfter(false, down)).toBe(false);
    expect(retriesAfter(0, down)).toBe(false);
    // A rule decides, e.g. admin settings: no answer yes, a refused token or "admin not configured" (503) no.
    const admin = (n: number, e: Error) => n < 1 && isTransientError(e) && !(e instanceof ApiError && e.status === 503);
    expect(retriesAfter(admin, down)).toBe(true);
    expect(retriesAfter(admin, refused)).toBe(false);
    expect(retriesAfter(admin, new ApiError("x", 503))).toBe(false);
    expect(retriesAfter(admin, new ApiError("x", 500))).toBe(true);
    expect(retriesAfter(3, down)).toBe(true);
  });
});

describe("configuration labels", () => {
  it("formats spans of seconds without calling a delay a cadence", () => {
    expect(durationText(0)).toBe("0 s");
    expect(durationText(45)).toBe("45 s");
    expect(durationText(300)).toBe("5 min");
    expect(durationText(3600)).toBe("1 h");
    expect(durationText(90)).toBe("90 s");
    expect(durationText(86400)).toBe("1 day");
    expect(durationText(172800)).toBe("2 days");
    expect(durationText(null)).toBe("Unavailable");
  });
  it("turns keys and enum names into sentence-case words", () => {
    expect(labelText("second_opinion")).toBe("Second opinion");
    expect(labelText("LEVEL_BREAK")).toBe("Level break");
    expect(labelText("market_interpreter")).toBe("Market interpreter");
    expect(labelText("fee_bps")).toBe("Fee bps");
    expect(labelText("")).toBe("");
    // Known acronyms stay upper case (source weights, calendar keys).
    expect(labelText("sec_filing")).toBe("SEC filing");
    expect(labelText("company_ir")).toBe("Company IR");
    expect(labelText("US_CPI")).toBe("US CPI");
    expect(labelText("min_priority")).toBe("Min priority");
  });
});

describe("last full analysis freshness", () => {
  const NOW = Date.parse("2026-09-28T04:26:30Z");
  const minutesAgo = (m: number) => new Date(NOW - m * 60_000).toISOString();
  it("follows the backend's record: a failed attempt is stale, with the reason in plain words", () => {
    const view = analysisView(
      {
        last_analysis: minutesAgo(316),
        analysis: {
          last_success_at: minutesAgo(316),
          last_attempt_at: minutesAgo(2),
          last_error_type: "TypeError",
          cadence_seconds: 300,
          stale: true,
        },
      },
      NOW,
    );
    expect(view.stale).toBe(true);
    expect(view.at).toBe(minutesAgo(316));
    expect(view.ageMs).toBe(316 * 60_000);
    expect(view.reason).toContain("failed");
    expect(view.reason).toContain("every 5 min");
    expect(view.reason).not.toContain("TypeError");
  });
  it("is fresh when the backend says so, and uses the success time rather than any other job's", () => {
    const view = analysisView(
      {
        last_analysis: minutesAgo(1),
        analysis: { last_success_at: minutesAgo(3), last_attempt_at: minutesAgo(3), cadence_seconds: 300, stale: false },
      },
      NOW,
    );
    expect(view).toEqual({ at: minutesAgo(3), ageMs: 3 * 60_000, stale: false, reason: null });
  });
  it("judges the record itself when the flag is missing: overdue beyond twice the cadence", () => {
    const overdue = { last_analysis: null, analysis: { last_success_at: minutesAgo(11), cadence_seconds: 300 } };
    expect(analysisView(overdue, NOW).stale).toBe(true);
    const recent = { last_analysis: null, analysis: { last_success_at: minutesAgo(9), cadence_seconds: 300 } };
    expect(analysisView(recent, NOW).stale).toBe(false);
    const never = analysisView({ last_analysis: null, analysis: { last_success_at: null, cadence_seconds: 300 } }, NOW);
    expect(never).toMatchObject({ at: null, ageMs: null, stale: true });
    expect(never.reason).toContain("No full analysis has completed yet");
  });
  it("falls back to the age of last_analysis on older backends", () => {
    expect(analysisView({ last_analysis: minutesAgo(5) }, NOW).stale).toBe(false);
    const old = analysisView({ last_analysis: minutesAgo(20) }, NOW);
    expect(old.stale).toBe(true);
    expect(old.reason).toContain("15 min");
    expect(analysisView({ last_analysis: null }, NOW)).toEqual({ at: null, ageMs: null, stale: false, reason: null });
    expect(analysisView(undefined, NOW).stale).toBe(false);
  });
});

describe("adminHeaders", () => {
  it("sends only the local marker in local mode and adds a typed token otherwise", () => {
    expect(adminHeaders(LOCAL_ADMIN)).toEqual({ "X-Local-Admin": "1" });
    expect(adminHeaders("")).toEqual({ "X-Local-Admin": "1" });
    expect(adminHeaders("abc")).toEqual({ "X-Local-Admin": "1", "X-Admin-Token": "abc" });
  });
});

describe("live asset_quotes frames (WebSocket)", () => {
  const quote = (price: number) => ({
    price,
    timestamp: "2026-09-28T10:00:00Z",
    provider: "binance",
    market_status: "LIVE",
    freshness_status: "FRESH",
    change_pct: 1.2,
  });
  const proxy = { symbol: "PAXG", price: 2650, timestamp: "2026-09-28T10:00:00Z", provider: "binance" };
  const asset = (symbol: string, price: number | null) => ({
    symbol,
    name: symbol,
    asset_class: "crypto",
    quote: price === null ? null : quote(price),
    decision: null,
  });
  const dashboard: { as_of: string; assets: Asset[]; events: { id: string }[] } = {
    as_of: "2026-09-28T09:59:55Z",
    assets: [asset("BTC", 100), asset("ETH", 10), asset("GOLD", null)],
    events: [{ id: "e1" }],
  };

  it("accepts only object payloads and keeps quote and proxy objects or null per symbol", () => {
    expect(parseAssetQuotes(null)).toBeNull();
    expect(parseAssetQuotes([])).toBeNull();
    expect(parseAssetQuotes("x")).toBeNull();
    const frame = parseAssetQuotes({
      as_of: "2026-09-28T10:00:00Z",
      quotes: { BTC: quote(200), ETH: null, BAD: 5, LIST: [] },
      proxies: "nope",
    });
    expect(frame).toEqual({ as_of: "2026-09-28T10:00:00Z", quotes: { BTC: quote(200), ETH: null }, proxies: {} });
    expect(parseAssetQuotes({ as_of: "not a time" })).toEqual({ as_of: null, quotes: {}, proxies: {} });
    expect(Object.getPrototypeOf(parseAssetQuotes(JSON.parse('{"quotes":{"__proto__":{"price":1}}}'))!.quotes)).toBe(
      Object.prototype,
    );
  });

  it("replaces the named assets' quote and proxy and moves as_of, leaving everything else as it was", () => {
    const frame = parseAssetQuotes({
      as_of: "2026-09-28T10:00:05Z",
      quotes: { BTC: quote(64000), ETH: null, UNKNOWN: quote(1) },
      proxies: { GOLD: proxy },
    })!;
    const merged = mergeAssetQuotes(dashboard, frame)!;
    expect(merged.as_of).toBe("2026-09-28T10:00:05Z");
    expect(merged.assets[0].quote?.price).toBe(64000);
    expect(merged.assets[0].decision).toBeNull();
    expect(merged.assets[1].quote).toBeNull();
    expect(merged.assets[2].quote).toBeNull();
    expect(merged.assets[2].proxy).toEqual(proxy);
    expect("proxy" in merged.assets[0]).toBe(false);
    expect(merged.events).toBe(dashboard.events);
    expect(merged.assets).toHaveLength(3);
    // The cached object itself is never modified.
    expect(dashboard.assets[0].quote?.price).toBe(100);
    expect(dashboard.as_of).toBe("2026-09-28T09:59:55Z");
  });

  it("keeps untouched assets by reference and does nothing without a dashboard", () => {
    const frame = parseAssetQuotes({ quotes: { BTC: quote(1) } })!;
    const merged = mergeAssetQuotes(dashboard, frame)!;
    expect(merged.assets[1]).toBe(dashboard.assets[1]);
    expect(merged.as_of).toBe(dashboard.as_of);
    expect(mergeAssetQuotes(undefined, frame)).toBeUndefined();
    const empty = parseAssetQuotes({ quotes: { XYZ: quote(1) } })!;
    expect(mergeAssetQuotes(dashboard, empty)).toBe(dashboard);
  });

  it("combines frames held back by the throttle: the later one wins per symbol", () => {
    const first = parseAssetQuotes({ as_of: "2026-09-28T10:00:00Z", quotes: { BTC: quote(1), ETH: quote(2) } })!;
    const second = parseAssetQuotes({ as_of: "2026-09-28T10:00:01Z", quotes: { BTC: quote(3) }, proxies: { GOLD: null } })!;
    expect(combineAssetQuotes(first, second)).toEqual({
      as_of: "2026-09-28T10:00:01Z",
      quotes: { BTC: quote(3), ETH: quote(2) },
      proxies: { GOLD: null },
    });
    expect(combineAssetQuotes(second, parseAssetQuotes({})!).as_of).toBe("2026-09-28T10:00:01Z");
  });
});

describe("unread alert count", () => {
  const NOW = Date.parse("2026-09-26T12:00:00Z");
  const alerts = (n: number, offset = 0) =>
    Array.from({ length: n }, (_, i) => ({
      id: `a${i + offset}`,
      kind: "SETUP",
      priority: "LOW",
      title: `Alert ${i + offset}`,
      message: "",
      symbol: null,
      created_at: new Date(NOW - (i + offset + 1) * 60_000).toISOString(),
    }));
  const NEVER = Number.NaN;
  const LONG_AGO = NOW - 24 * 3600_000;

  it("uses the stored total when the list was never marked read, without the longer list", () => {
    expect(unreadAlertCount(alerts(30), NEVER, 201, undefined, 500)).toEqual({ unread: 201, more: false, needsLonger: false });
  });

  it("is exact from the short list when it holds every stored alert or some of it was read", () => {
    expect(unreadAlertCount(alerts(12), LONG_AGO, 12, undefined, 500)).toEqual({ unread: 12, more: false, needsLonger: false });
    // Read 10 minutes ago: the 9 newer are unread, the short list is not saturated.
    expect(unreadAlertCount(alerts(30), NOW - 10 * 60_000 + 1, 201, undefined, 500)).toEqual({
      unread: 9,
      more: false,
      needsLonger: false,
    });
    expect(unreadAlertCount([], NEVER, 0, undefined, 500)).toEqual({ unread: 0, more: false, needsLonger: false });
  });

  it("is a lower bound while the longer list is loading or failed, never an exact short-list count", () => {
    expect(unreadAlertCount(alerts(30), LONG_AGO, 201, undefined, 500)).toEqual({ unread: 30, more: true, needsLonger: true });
    // Older backends without a total, in a fresh browser.
    expect(unreadAlertCount(alerts(30), NEVER, undefined, undefined, 500)).toEqual({ unread: 30, more: true, needsLonger: true });
  });

  it("counts the longer list once loaded", () => {
    expect(unreadAlertCount(alerts(30), NEVER, undefined, alerts(45), 500)).toEqual({ unread: 45, more: false, needsLonger: true });
    expect(unreadAlertCount(alerts(30), LONG_AGO, 45, alerts(45), 500)).toEqual({ unread: 45, more: false, needsLonger: true });
    // Every loaded alert unread and more stored: at least that many.
    expect(unreadAlertCount(alerts(30), LONG_AGO, 201, alerts(45), 45)).toEqual({ unread: 45, more: true, needsLonger: true });
    // A loaded list is taken as it is, even an empty one.
    expect(unreadAlertCount(alerts(2), NEVER, undefined, [], 500)).toEqual({ unread: 2, more: false, needsLonger: true });
    // No total: a full longer list that is entirely unread is a lower bound.
    expect(unreadAlertCount(alerts(30), NEVER, null, alerts(500), 500)).toEqual({ unread: 500, more: true, needsLonger: true });
  });

  it("ignores an inconsistent total", () => {
    expect(unreadAlertCount(alerts(30), NEVER, 5, undefined, 500).needsLonger).toBe(true);
    expect(unreadAlertCount(alerts(30), NEVER, 12.5, undefined, 500).needsLonger).toBe(true);
  });
});
