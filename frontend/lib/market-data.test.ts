import { describe, expect, it } from "vitest";
import type { Asset, Quote } from "./api";
import {
  asOfLabel,
  asOfLine,
  basisText,
  calendarDay,
  changeLine,
  coverageText,
  dataChip,
  disabledSources,
  gapKind,
  hasClockTime,
  horizonLabel,
  isCurrentClose,
  isOfficialPublisher,
  isStub,
  levelText,
  marketState,
  mergeNotices,
  missingKeysText,
  noticesFor,
  parseNotices,
  proxyChangeTitle,
  proxyLine,
  proxyStale,
  readableLabel,
  sessionBasisText,
  sessionBadge,
  sessionLabel,
  asOfTime,
  shortDate,
  shortSourceName,
  sourceName,
  venueName,
  withoutSessionState,
  zonedTime,
} from "./market-data";
// Pure asset-page modules (relative runtime imports only), collected here because vitest runs lib/**.
import {
  crossAssetModel,
  dailyPoints,
  priceDigits,
  seriesChange,
  sessionCountText,
  sessionDay,
  toPoints,
} from "../components/asset/cross-asset";
import { driverModel } from "../components/asset/drivers";
import { basisLabel, levelRows, scopeText, storedSessions } from "../components/asset/levels";
import { probabilityLabel } from "../components/asset/signal";
import { canonicalSymbol, exactSymbol, isAssetSymbol } from "../components/asset/symbol";

const quote = (extra: Partial<Quote> = {}): Quote => ({
  price: 4.81,
  timestamp: "2026-09-25T20:00:00Z",
  provider: "treasury",
  market_status: "FRIDAY CLOSE",
  freshness_status: "STALE",
  change_pct: null,
  ...extra,
});

const asset = (extra: Partial<Asset> = {}): Asset => ({
  symbol: "US2Y",
  name: "US 2Y",
  asset_class: "bond",
  quote: null,
  decision: null,
  ...extra,
});

describe("change with its basis", () => {
  it("labels yields in bp against the previous close date", () => {
    const q = quote({
      change_bp: -6,
      change_basis: "previous_close",
      previous_close_at: "2026-09-24T20:00:00Z",
    });
    expect(changeLine(q)?.text).toBe("-6 bp vs prev close 24 Sep");
    expect(changeLine(q)?.sign).toBe(-1);
  });
  it("labels ECB fixes, rolling 24h crypto and provider-reported changes", () => {
    expect(
      changeLine(
        quote({
          provider: "ecb",
          change_pct: 0.317,
          change_basis: "previous_fix",
          previous_close_at: "2026-09-24T12:15:00Z",
        }),
      )?.text,
    ).toBe("+0.32% vs prev ECB fix 24 Sep");
    expect(
      changeLine(quote({ change_pct: 0.711, change_basis: "rolling_24h" }))
        ?.text,
    ).toBe("+0.71% 24h");
    expect(basisText({ change_basis: "provider", provider: "finnhub" })).toBe(
      "per finnhub",
    );
    // No basis reported: the number alone, never an invented basis.
    expect(changeLine(quote({ change_pct: 1.2 }))?.text).toBe("+1.20%");
  });
  it("never turns a missing change into zero", () => {
    expect(changeLine(quote())).toBeNull();
    expect(shortDate(null)).toBeNull();
    expect(shortDate("bad")).toBeNull();
  });
});

describe("data chip", () => {
  it("shows a current official close as DAILY CLOSE, not STALE", () => {
    const chip = dataChip(
      asset({ quote: quote({ data_class: "DAILY_CLOSE", close_status: "CURRENT" }) }),
    );
    expect(chip).toEqual({ label: "DAILY CLOSE", tone: "info" });
  });
  it("keeps a genuinely late close prominent", () => {
    const chip = dataChip(
      asset({ quote: quote({ data_class: "DAILY_CLOSE", close_status: "STALE" }) }),
    );
    expect(chip.label).toBe("STALE");
    expect(chip.tone).toBe("negative");
    expect(chip.prominent).toBe(true);
  });
  it("labels derived daily values and live data", () => {
    expect(
      dataChip(
        asset({
          quote: quote({
            provider: "ecb-derived",
            data_class: "DERIVED_DAILY",
            derived: true,
            close_status: "CURRENT",
          }),
        }),
      ).label,
    ).toBe("DERIVED");
    expect(
      dataChip(asset({ quote: quote({ freshness_status: "FRESH", data_class: "LIVE" }) }))
        .label,
    ).toBe("LIVE");
    // A live quote that went late while its market trades (currentAsset sets freshness STALE; the venue stays LIVE).
    const late = dataChip(
      asset({ quote: quote({ data_class: "LIVE", market_status: "LIVE", freshness_status: "STALE" }) }),
    );
    expect(late).toEqual({ label: "STALE", tone: "negative", prominent: true });
    expect(
      dataChip(asset({ quote: quote({ data_class: "LIVE", market_status: "LIVE" }) }))
        .label,
    ).toBe("STALE");
    // Unknown session: stay conservative.
    expect(
      dataChip(asset({ quote: quote({ data_class: "LIVE", market_status: "UNKNOWN" }) }))
        .label,
    ).toBe("STALE");
  });
  it("shows the last print of a closed session quietly, not as a red STALE", () => {
    for (const state of ["FRIDAY CLOSE", "LAST CLOSE", "AFTER HOURS", "PREMARKET"]) {
      const chip = dataChip(
        asset({ quote: quote({ data_class: "LIVE", market_status: state }) }),
      );
      expect(chip).toEqual({ label: "LAST PRINT", tone: "outline" });
    }
    // The session of the asset counts when the quote carries none.
    expect(
      dataChip(
        asset({
          session: { state: "FRIDAY CLOSE" },
          quote: quote({ market_status: "", freshness_status: "STALE" }),
        }),
      ).label,
    ).toBe("LAST PRINT");
    // A daily close keeps its own close status, never LAST PRINT.
    expect(
      dataChip(asset({ quote: quote({ data_class: "DAILY_CLOSE", close_status: "STALE" }) }))
        .label,
    ).toBe("STALE");
  });
  it("labels a development stub STUB, never LIVE", () => {
    const stub = quote({
      provider: "massive-stub",
      market_status: "LIVE",
      freshness_status: "FRESH",
      data_class: "LIVE",
    });
    expect(dataChip(asset({ quote: stub }))).toEqual({ label: "STUB", tone: "outline" });
    expect(
      dataChip(asset({ quote: quote({ stub: true, freshness_status: "FRESH" }) })).label,
    ).toBe("STUB");
    expect(isStub({ name: "twelvedata-stub" })).toBe(true);
    expect(isStub({ provider: "massive" })).toBe(false);
    expect(isStub(null)).toBe(false);
  });
  it("says DISABLED (switched off in Settings) when every source is switched off", () => {
    const chip = dataChip(
      asset({ coverage: { status: "DISABLED", providers: [], disabled: ["ecb_data"] } }),
    );
    expect(chip).toEqual({
      label: "DISABLED",
      tone: "outline",
      detail: "switched off in Settings",
    });
    expect(coverageText({ status: "DISABLED" })).toBe("Sources switched off in Settings");
    expect(disabledSources({ status: "OK", disabled: ["ecb_data", "ecb_data", "", 3 as unknown as string] })).toEqual([
      "ecb_data",
    ]);
    expect(disabledSources(null)).toEqual([]);
  });
  it("names the key that would fill an unconfigured asset", () => {
    const chip = dataChip(
      asset({
        coverage: {
          status: "NOT_CONFIGURED",
          providers: ["fred", "massive"],
          missing_keys: ["FRED_API_KEY", "MASSIVE_API_KEY", "FRED_API_KEY"],
        },
      }),
    );
    expect(chip.label).toBe("NOT CONFIGURED");
    expect(chip.detail).toBe("needs FRED_API_KEY or MASSIVE_API_KEY");
    expect(missingKeysText([])).toBeNull();
  });
  it("names every key of a source that needs several, joined", () => {
    expect(
      missingKeysText(["FINNHUB_API_KEY", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"]),
    ).toBe("needs FINNHUB_API_KEY or ALPACA_API_KEY_ID + ALPACA_API_SECRET_KEY");
    expect(missingKeysText(["ALPACA_API_SECRET_KEY"])).toBe(
      "needs ALPACA_API_SECRET_KEY",
    );
  });
  it("labels a proxy-only asset as PROXY and an absent asset as not delivered", () => {
    const proxy = {
      symbol: "SPY",
      price: 655.2,
      change_pct: 0.42,
      timestamp: "2026-09-25T20:00:00Z",
      provider: "finnhub",
    };
    expect(
      dataChip(asset({ proxy, coverage: { status: "PROXY_ONLY" } })).label,
    ).toBe("PROXY");
    // No basis reported: the number alone, never an invented basis.
    expect(proxyLine(proxy).text).toBe("+0.42% via SPY");
    expect(dataChip(undefined).label).toBe("NOT DELIVERED");
    expect(dataChip(asset({ coverage: { status: "NO_DATA_YET" } })).label).toBe(
      "NO DATA YET",
    );
  });
  it("gives a proxy change its basis on the tile and in the tooltip", () => {
    const paxg = {
      symbol: "PAXG",
      price: 3772.5,
      change_pct: 0.42,
      change_basis: "rolling_24h",
      timestamp: "2026-09-26T11:59:00Z",
      provider: "binance",
      market_status: "LIVE",
      freshness_status: "FRESH",
    };
    expect(proxyLine(paxg)).toEqual({ text: "+0.42% 24h via PAXG", sign: 1 });
    expect(proxyChangeTitle(paxg)).toBe(
      "Proxy change: +0.42% over a rolling 24 hours (PAXG; not since a close)",
    );
    const spy = {
      symbol: "SPY",
      price: 655.2,
      change_pct: 0.51,
      change_basis: "previous_close",
      timestamp: "2026-09-25T20:00:00Z",
      provider: "finnhub",
    };
    expect(proxyLine(spy).text).toBe("+0.51% vs prev close via SPY");
    expect(proxyChangeTitle(spy)).toBe("Proxy change: +0.51% vs prev close (SPY)");
    expect(proxyLine({ ...spy, change_pct: null }).text).toBe(
      "via SPY · no change reported",
    );
  });
  it("flags a proxy as stale only while its market trades", () => {
    expect(
      proxyStale({ freshness_status: "STALE", market_status: "FRIDAY CLOSE" }),
    ).toBe(false);
    expect(proxyStale({ freshness_status: "STALE", market_status: "LIVE" })).toBe(
      true,
    );
    expect(proxyStale({ freshness_status: "STALE" })).toBe(true);
    expect(proxyStale({ freshness_status: "FRESH", market_status: "LIVE" })).toBe(
      false,
    );
  });
});

describe("session and as-of", () => {
  it("uses the asset session when there is no quote", () => {
    expect(marketState(asset({ session: { state: "FRIDAY CLOSE" } }))).toBe(
      "FRIDAY CLOSE",
    );
    expect(marketState(asset())).toBe("UNKNOWN");
  });
  it("never reports STALE as a session state: freshness is the data chip's word", () => {
    const open = { state: "LIVE" };
    // A status carrying a freshness word (an older reader wrote it there) falls back to the venue state.
    const stale = asset({ session: open, quote: quote({ data_class: "LIVE", market_status: "STALE", freshness_status: "STALE" }) });
    expect(marketState(stale)).toBe("LIVE");
    expect(sessionLabel(stale)).toBe("LIVE");
    expect(dataChip(stale).label).toBe("STALE");
    // Beside a STALE chip the badge says the venue is open, never LIVE and never STALE.
    expect(sessionBadge(stale)).toBe("MARKET OPEN");
    const late = asset({ session: open, quote: quote({ data_class: "LIVE", market_status: "LIVE", freshness_status: "STALE" }) });
    expect(sessionBadge(late)).toBe("MARKET OPEN");
    // A fresh live quote: the chip already says LIVE, the badge is left out.
    const fresh = asset({ session: open, quote: quote({ data_class: "LIVE", market_status: "LIVE", freshness_status: "FRESH" }) });
    expect(sessionBadge(fresh)).toBeNull();
    // Closed sessions and daily closes keep their venue state next to the chip.
    expect(sessionBadge(asset({ quote: quote({ data_class: "LIVE" }) }))).toBe("FRIDAY CLOSE");
    expect(
      sessionBadge(asset({ session: open, quote: quote({ market_status: "LIVE", data_class: "DAILY_CLOSE", close_status: "CURRENT" }) })),
    ).toBe("MARKET OPEN");
    expect(sessionBadge(asset())).toBeNull();
  });
  it("prefers the service's as-of text and names the source by its attribution", () => {
    expect(
      asOfLine(
        quote({
          as_of_label: "Fri 25 Sep 16:00 ET",
          attribution: "Source: U.S. Department of the Treasury",
        }),
      ),
    ).toBe("as of Fri 25 Sep 16:00 ET · U.S. Department of the Treasury");
    expect(asOfLine(quote({ provider: "binance", attribution: "Source: Binance" }))).toBe(
      "as of Fri 25 Sep 23:00 Bucharest · Binance",
    );
    // Without an attribution the provider id stays the source. 20:00 UTC = 23:00 Europe/Bucharest (EEST);
    // a time this app formats names its zone.
    expect(asOfLine(quote({ provider: "alpaca", feed: "iex" }))).toBe(
      "as of Fri 25 Sep 23:00 Bucharest · alpaca (IEX)",
    );
    expect(zonedTime("2026-09-25T20:00:00Z")).toBe("Fri 25 Sep 23:00 Bucharest");
    expect(zonedTime("bad")).toBeNull();
  });
  it("never says 'as of' twice", () => {
    expect(
      asOfLine(
        quote({
          provider: "eia",
          as_of_label: "EIA spot, as of Tue 22 Sep",
          attribution: "Source: U.S. Energy Information Administration",
        }),
      ),
    ).toBe("EIA spot, as of Tue 22 Sep · U.S. Energy Information Administration");
    expect(asOfLine(quote({ as_of_label: "As of Fri 25 Sep" }))).toBe("As of Fri 25 Sep · treasury");
  });
  it("leaves the session state out of the as-of text: the page shows the current one", () => {
    expect(withoutSessionState("PREMARKET (last trade Fri 25 Sep 16:00 ET)")).toBe(
      "last trade Fri 25 Sep 16:00 ET",
    );
    expect(withoutSessionState("FRIDAY CLOSE (IEX regular-session close Fri 25 Sep 23:00 Bucharest)")).toBe(
      "IEX regular-session close Fri 25 Sep 23:00 Bucharest",
    );
    expect(withoutSessionState("real-time (Finnhub /quote last price), AFTER HOURS")).toBe(
      "real-time (Finnhub /quote last price)",
    );
    // Not a session state: kept as sent.
    expect(withoutSessionState("ECB 14:15 CET fix, Fri 25 Sep (daily, not a tradable quote)")).toBe(
      "ECB 14:15 CET fix, Fri 25 Sep (daily, not a tradable quote)",
    );
    expect(withoutSessionState("STALE: no trade for 12 min while the market is open (last trade 16:00 ET)")).toBe(
      "STALE: no trade for 12 min while the market is open (last trade 16:00 ET)",
    );
    expect(
      asOfLine(
        quote({
          provider: "finnhub",
          as_of_label: "PREMARKET (last trade Fri 25 Sep 16:00 ET)",
          attribution: "Source: Finnhub",
        }),
      ),
    ).toBe("as of last trade Fri 25 Sep 16:00 ET · Finnhub");
    expect(asOfLabel(quote({ as_of_label: "  " }))).toBeNull();
  });
  it("tells whether a label names a clock time", () => {
    expect(hasClockTime("last trade Fri 25 Sep 16:00 ET")).toBe(true);
    expect(hasClockTime("IEX last trade Fri 25 Sep 23:59 Bucharest")).toBe(true);
    expect(hasClockTime("FRED SP500, close Fri 25 Sep (daily; FRED lags its source)")).toBe(false);
    expect(hasClockTime("EIA spot, as of Tue 22 Sep")).toBe(false);
  });
  it("keeps the word LIVE for live data: an open market without it reads MARKET OPEN", () => {
    const open = { state: "LIVE" };
    // A current daily close while the venue trades.
    expect(
      sessionLabel(
        asset({
          session: open,
          quote: quote({ market_status: "LIVE", data_class: "DAILY_CLOSE", close_status: "CURRENT" }),
        }),
      ),
    ).toBe("MARKET OPEN");
    // No quote, or only a stub.
    expect(sessionLabel(asset({ session: open }))).toBe("MARKET OPEN");
    expect(
      sessionLabel(asset({ session: open, quote: quote({ market_status: "LIVE", stub: true }) })),
    ).toBe("MARKET OPEN");
    // Live data keeps LIVE; closed sessions keep their own state.
    expect(
      sessionLabel(
        asset({ session: open, quote: quote({ market_status: "LIVE", data_class: "LIVE" }) }),
      ),
    ).toBe("LIVE");
    expect(sessionLabel(asset({ session: { state: "FRIDAY CLOSE" } }))).toBe("FRIDAY CLOSE");
  });
  it("classifies why a regime input is missing from its coverage", () => {
    expect(gapKind({ status: "NOT_CONFIGURED" })).toBe("not_configured");
    expect(gapKind({ status: "DISABLED" })).toBe("disabled");
    expect(gapKind({ status: "NO_DATA_YET" })).toBe("no_data");
    // Data exists (a stale close, an old proxy) but the service could not use it.
    expect(gapKind({ status: "OK" })).toBe("unusable");
    expect(gapKind({ status: "DAILY_ONLY" })).toBe("unusable");
    expect(gapKind({ status: "PROXY_ONLY" })).toBe("unusable");
    expect(gapKind(null)).toBe("unknown");
    expect(gapKind({ status: "SOMETHING_NEW" })).toBe("unknown");
  });
  it("only a daily close with CURRENT status is a current close", () => {
    expect(isCurrentClose(quote({ daily_close: true, close_status: "CURRENT" }))).toBe(
      true,
    );
    expect(isCurrentClose(quote({ data_class: "LIVE", close_status: "CURRENT" }))).toBe(
      false,
    );
    expect(isCurrentClose(null)).toBe(false);
  });
});

describe("provider notices", () => {
  const FRED =
    "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.";
  it("keeps well-formed notices only", () => {
    expect(parseNotices(null)).toEqual([]);
    expect(
      parseNotices([
        { id: "fred_api", text: FRED, providers: ["fred"] },
        { id: "empty", text: "  " },
        "not an object",
        { text: "No id", providers: ["x", 1] },
      ]),
    ).toEqual([
      { id: "fred_api", text: FRED, providers: ["fred"] },
      { id: "notice-3", text: "No id", providers: ["x"] },
    ]);
  });
  it("shows a notice on an asset only when its quote or proxy comes from that provider", () => {
    const notices = parseNotices([{ id: "fred_api", text: FRED, providers: ["fred"] }]);
    expect(noticesFor(notices, asset({ quote: quote({ provider: "fred" }) }))).toHaveLength(1);
    expect(
      noticesFor(
        notices,
        asset({
          proxy: {
            symbol: "X",
            price: 1,
            timestamp: "2026-09-25T20:00:00Z",
            provider: "FRED",
          },
        }),
      ),
    ).toHaveLength(1);
    expect(noticesFor(notices, asset({ quote: quote({ provider: "treasury" }) }))).toEqual([]);
    expect(noticesFor(notices, asset())).toEqual([]);
    expect(noticesFor(notices, undefined)).toEqual([]);
  });
  it("merges the asset's own notices with the dashboard's, each shown once", () => {
    const fred = { id: "fred_api", text: FRED, providers: ["fred"] };
    const other = { id: "other", text: "Other terms", providers: ["x"] };
    // A FRED overlay on an Alpaca-quoted asset: only the asset's own list names FRED.
    expect(mergeNotices([fred], [])).toEqual([fred]);
    expect(mergeNotices([fred], [fred, other])).toEqual([fred, other]);
    // The same text under another id is not repeated either.
    expect(mergeNotices([fred], [{ ...fred, id: "notice-0" }])).toEqual([fred]);
    expect(mergeNotices([], [])).toEqual([]);
  });
});

describe("readable service labels", () => {
  it("turns raw ISO times inside a label into Bucharest times and session dates", () => {
    expect(
      readableLabel("FRIDAY CLOSE (IEX last trade 2026-09-25T20:59:34.720445+00:00)"),
    ).toBe("FRIDAY CLOSE (IEX last trade Fri 25 Sep 23:59 Bucharest)");
    expect(readableLabel("close 2026-09-25")).toBe("close Fri 25 Sep");
    expect(readableLabel("Fri 25 Sep 16:00 ET")).toBe("Fri 25 Sep 16:00 ET");
    expect(
      asOfLine(
        quote({
          provider: "alpaca",
          feed: "iex",
          as_of_label: "FRIDAY CLOSE (IEX last trade 2026-09-25T20:00:00+00:00)",
        }),
      ),
    ).toBe("as of IEX last trade Fri 25 Sep 23:00 Bucharest · alpaca (IEX)");
  });
  it("never zero-pads the day of a formatted time", () => {
    // 1 Oct 2026 09:30 Bucharest.
    expect(shortDate("2026-10-01T06:30:00Z")).toBe("1 Oct");
    expect(asOfTime("2026-10-01T06:30:00Z")).toBe("Thu 1 Oct 09:30");
    expect(zonedTime("2026-10-01T06:30:00Z")).toBe("Thu 1 Oct 09:30 Bucharest");
    expect(asOfTime("2026-09-25T20:00:00Z")).toBe("Fri 25 Sep 23:00");
  });
  it("formats a calendar date as that date, never shifted by a zone", () => {
    expect(calendarDay("2026-09-25")).toBe("Fri 25 Sep");
    expect(calendarDay("2026-02-30")).toBeNull();
    expect(calendarDay(null)).toBeNull();
  });
  it("names venues and drops the calendar library from the session basis", () => {
    expect(venueName("XNYS")).toBe("NYSE");
    expect(venueName("US_TREASURY")).toBe("US Treasury (CBOT futures hours)");
    expect(venueName("CBOE_VIX")).toBe("Cboe (VIX)");
    expect(venueName("NEW_VENUE")).toBe("NEW VENUE");
    expect(venueName(null)).toBeNull();
    expect(sessionBasisText("ICE futures session calendar (exchange_calendars IEPA)")).toBe(
      "ICE futures session calendar",
    );
  });
  it("quotes yields in percent and other levels as numbers", () => {
    expect(levelText(5.17, "bond")).toBe("5.17%");
    expect(levelText(83442.5, "crypto")).toBe("83,442.5");
    expect(levelText(null, "bond")).toBe("Unavailable");
  });
  it("names the source by its attribution, with the feed, once", () => {
    expect(sourceName({ provider: "binance", attribution: "Source: Binance" })).toBe("Binance");
    expect(
      sourceName({ provider: "alpaca", feed: "iex", attribution: "Source: Alpaca Market Data" }),
    ).toBe("Alpaca Market Data (IEX)");
    expect(sourceName({ provider: "alpaca", feed: "iex" })).toBe("alpaca (IEX)");
  });
  it("calls only official publishers' daily values official", () => {
    for (const p of ["treasury", "fred", "ECB", "eia"]) expect(isOfficialPublisher(p)).toBe(true);
    for (const p of ["alpaca", "finnhub", "ecb-derived", undefined])
      expect(isOfficialPublisher(p)).toBe(false);
  });
  it("words evaluation horizons", () => {
    expect(horizonLabel("5m")).toBe("5 min");
    expect(horizonLabel("1h")).toBe("1 hour");
    expect(horizonLabel("4h")).toBe("4 hours");
    expect(horizonLabel("close")).toBe("At close");
    expect(horizonLabel("next_session")).toBe("Next session");
  });
});

const minute = (i: number) => new Date(Date.UTC(2026, 8, 26, 10, i)).toISOString();

describe("asset price and cross-asset series", () => {
  it("keys daily closes by New York session date", () => {
    // A 16:00 ET close (20:00 UTC) and a date-only bar are both 25 Sep.
    const sep25 = Date.UTC(2026, 8, 25) / 1000;
    expect(sessionDay("2026-09-25T20:00:00Z")).toBe(sep25);
    expect(sessionDay("2026-09-25")).toBe(sep25);
    // 02:00 UTC on 26 Sep is still 25 Sep in New York.
    expect(sessionDay("2026-09-26T02:00:00Z")).toBe(sep25);
    expect(
      dailyPoints([
        { time: "2026-09-24T20:00:00Z", close: 1 },
        { time: "2026-09-25T14:00:00Z", close: 2 },
        { time: "2026-09-25T20:00:00Z", close: 3 },
      ]).map((p) => p.value),
    ).toEqual([1, 3]);
  });
  it("lists only the unusable series as missing, never the plotted subject", () => {
    const model = crossAssetModel({
      symbol: "BTC",
      name: "Bitcoin",
      bars: [0, 1, 2].map((i) => ({ time: minute(i), close: 100 + i })),
      cross_asset: { DXY: [], US10Y: [], NASDAQ: null },
    });
    expect(model.lines).toEqual([]);
    expect(model.missing.map((m) => m.key)).toEqual(["DXY", "US10Y", "NASDAQ"]);
    expect(model.empty).toBe("no_overlays");
    expect(model.subjectPoints).toBe(3);
  });
  it("compares on daily closes when the overlays only have daily closes", () => {
    const daily = (from: number) =>
      ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"].map((time, i) => ({
        time,
        close: from + i,
      }));
    const model = crossAssetModel({
      symbol: "NVDA",
      name: "NVIDIA",
      bars: [],
      daily_bars: daily(200),
      cross_asset: { DXY: daily(97), US10Y: daily(4) },
      cross_asset_interval: { DXY: 86400, US10Y: 86400 },
    });
    expect(model.daily).toBe(true);
    expect(model.lines.map((l) => l.key)).toEqual(["NVDA", "DXY", "US10Y"]);
    expect(model.since).toBe(Date.UTC(2026, 8, 22) / 1000);
    expect(model.lines[0].last).toBeCloseTo(1.5, 6);
    expect(model.missing).toEqual([]);
  });
  it("keeps one-minute bars when the asset has no daily history, listing daily-only overlays", () => {
    const model = crossAssetModel({
      symbol: "BTC",
      name: "Bitcoin",
      bars: [0, 1, 2].map((i) => ({ time: minute(i), close: 100 + i })),
      daily_bars: [],
      cross_asset: {
        DXY: [
          { time: "2026-09-24", close: 97 },
          { time: "2026-09-25", close: 97.2 },
        ],
        NASDAQ: [],
      },
      cross_asset_interval: { DXY: 86400, NASDAQ: 60 },
    });
    expect(model.daily).toBe(false);
    expect(model.lines).toEqual([]);
    expect(model.dailyOnly.map((m) => m.key)).toEqual(["DXY"]);
    expect(model.missing.map((m) => m.key)).toEqual(["NASDAQ"]);
    expect(model.empty).toBe("no_overlays");
  });
  it("reports an overlay that ends before the common start as not overlapping", () => {
    const model = crossAssetModel({
      symbol: "BTC",
      name: "Bitcoin",
      bars: [5, 6, 7].map((i) => ({ time: minute(i), close: 100 + i })),
      cross_asset: {
        DXY: [0, 1, 2].map((i) => ({ time: minute(i), close: 97 })),
        US10Y: [5, 6, 7].map((i) => ({ time: minute(i), close: 4 + i / 100 })),
      },
    });
    expect(model.lines.map((l) => l.key)).toEqual(["BTC", "US10Y"]);
    expect(model.unaligned.map((m) => m.key)).toEqual(["DXY"]);
    expect(model.missing).toEqual([]);
  });
  it("on daily closes, lists an overlay with only recent one-minute bars instead of cutting the comparison to its days", () => {
    const sessions = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"];
    const daily = (from: number) => sessions.map((time, i) => ({ time, close: from + i }));
    // NASDAQ came as one-minute bars of the last two sessions only (a live feed).
    const nasdaqMinutes = [24, 25].flatMap((day) =>
      [0, 1, 2].map((i) => ({
        time: new Date(Date.UTC(2026, 8, day, 14, i)).toISOString(),
        close: 18000 + day + i,
      })),
    );
    const model = crossAssetModel({
      symbol: "NVDA",
      name: "NVIDIA",
      bars: [],
      daily_bars: daily(200),
      cross_asset: { DXY: daily(97), NASDAQ: nasdaqMinutes },
      cross_asset_interval: { DXY: 86400, NASDAQ: 60 },
    });
    expect(model.daily).toBe(true);
    expect(model.lines.map((l) => l.key)).toEqual(["NVDA", "DXY"]);
    // The comparison still starts at the first session, not at NASDAQ's one day.
    expect(model.since).toBe(Date.UTC(2026, 8, 21) / 1000);
    expect(model.intradayOnly.map((m) => m.key)).toEqual(["NASDAQ"]);
    expect(model.missing).toEqual([]);
    expect(model.unaligned).toEqual([]);
    // One session of one-minute bars: listed the same way, never as "No data".
    const oneSession = crossAssetModel({
      symbol: "NVDA",
      name: "NVIDIA",
      bars: [],
      daily_bars: daily(200),
      cross_asset: { DXY: daily(97), NASDAQ: nasdaqMinutes.slice(3) },
      cross_asset_interval: { DXY: 86400, NASDAQ: 60 },
    });
    expect(oneSession.intradayOnly.map((m) => m.key)).toEqual(["NASDAQ"]);
    expect(oneSession.missing).toEqual([]);
    // One-minute bars that do reach back to the asset's first session are compared.
    const long = sessions.map((day, i) => ({ time: `${day}T15:00:00Z`, close: 18000 + i }));
    const covered = crossAssetModel({
      symbol: "NVDA",
      name: "NVIDIA",
      bars: [],
      daily_bars: daily(200),
      cross_asset: { DXY: daily(97), NASDAQ: long },
      cross_asset_interval: { DXY: 86400, NASDAQ: 60 },
    });
    expect(covered.lines.map((l) => l.key)).toEqual(["NVDA", "DXY", "NASDAQ"]);
    expect(covered.intradayOnly).toEqual([]);
  });
  it("keeps one-minute bars when every overlay has intraday history", () => {
    const bars = (from: number) =>
      [0, 1, 2].map((i) => ({ time: minute(i), close: from + i }));
    const model = crossAssetModel({
      symbol: "BTC",
      name: "Bitcoin",
      bars: bars(100),
      cross_asset: { DXY: bars(97) },
    });
    expect(model.daily).toBe(false);
    expect(model.lines).toHaveLength(2);
    expect(model.missing.map((m) => m.key)).toEqual([]);
  });
  it("updates only the forming bar or appends, and resets on changed history", () => {
    const prev = toPoints([0, 1, 2].map((i) => ({ time: minute(i), close: 10 + i })));
    expect(seriesChange(prev, prev)).toEqual({ kind: "none" });
    const moved = [...prev.slice(0, 2), { ...prev[2], value: 99 }];
    expect(seriesChange(prev, moved)).toEqual({ kind: "update", points: [moved[2]] });
    const appended = [...prev, { time: prev[2].time + 60, value: 13 }];
    expect(seriesChange(prev, appended)).toEqual({ kind: "update", points: [appended[3]] });
    // The window slid (the oldest bar dropped): the data is replaced, the chart is kept.
    expect(seriesChange(prev, appended.slice(1))).toEqual({ kind: "reset" });
    expect(seriesChange([], prev)).toEqual({ kind: "reset" });
    expect(seriesChange([], [])).toEqual({ kind: "none" });
  });
});

describe("asset page helpers", () => {
  it("reads the published `drivers` and marks a driver without measured inputs missing", () => {
    const model = driverModel({
      id: "d",
      direction: "WAIT",
      score: 3,
      evidence_confidence: 0,
      market_confirmation: 0,
      data_quality: 80,
      reasons: [],
      confirmation: "",
      invalidation: "",
      audit_status: "NOT_REQUIRED",
      components: {},
      drivers: {
        rows: [
          { driver: "Rates", value: 0 },
          { driver: "Technical", value: 3 },
          { driver: "Positioning", value: 0 },
          { driver: "Other", value: 0 },
        ],
        net: 3,
        unmeasured: ["event", "macro", "reaction", "cross_asset", "regime"],
      },
    });
    expect(model?.basis).toBe("Signed weighted contribution by driver");
    expect(model?.rows).toEqual([
      { name: "Rates", value: null },
      { name: "Technical", value: 3 },
      // Positioning was measured: its zero is a measured zero.
      { name: "Positioning", value: 0 },
      { name: "Other", value: null },
    ]);
    expect(model?.unmeasured).toContain("Market reaction");
  });
  it("labels levels in words and states their inputs", () => {
    expect(basisLabel("sma_20")).toBe("SMA 20");
    expect(basisLabel("range_20d_high")).toBe("20-day range high");
    expect(basisLabel("previous_day_low")).toBe("previous day low");
    const levels = {
      support: 208.94,
      support_basis: "sma_20",
      sma_20: 208.94,
      sma_50: 190.03,
      daily_sessions: 274,
      reference_session: "2026-09-28",
      daily_basis: "daily bars: Alpaca IEX",
      observations: 0,
      scope: "0 one-minute bars; daily from daily bars: Alpaca IEX",
    };
    const rows = levelRows(levels, 225.05, "equity");
    expect(rows.map((r) => r.label)).toContain("SMA 20");
    expect(rows.map((r) => r.label)).not.toContain("daily sessions");
    expect(rows.find((r) => r.label === "Support")?.note).toBe("SMA 20 · -7.16% from price");
    expect(scopeText(levels)).toBe("Computed from 274 daily sessions (Alpaca IEX).");
    expect(
      scopeText({
        observations: 2062,
        scope: "2062 one-minute bars; daily from aggregated intraday bars (partial first day dropped)",
      }),
    ).toBe(
      "Computed from 2,062 one-minute bars; daily levels from aggregated intraday bars (partial first day dropped).",
    );
    expect(levelRows({ support: 4.1 }, 4.2, "bond")[0].value).toBe("4.1%");
  });
  it("words the historical probability horizon the same way in both forms", () => {
    const basis = "past decisions for this asset and horizon in the same score bucket and regime";
    expect(probabilityLabel({ p: 0.64, n: 418, horizon: "4h", n_basis: basis })).toEqual({
      value: "64%",
      detail: "n=418 · horizon 4 hours",
      title: `n: ${basis}`,
    });
    expect(probabilityLabel({ p: 0.61, n: 212, horizon: "1d" }).detail).toBe("n=212 · horizon 1 day");
    expect(probabilityLabel({ p: 0.5, horizon: "next_session" }).detail).toBe("horizon next session");
    // Not enough data: why, the same horizon words, and what n counts.
    expect(
      probabilityLabel({
        status: "NOT ENOUGH DATA",
        n: 151,
        horizon: "4h",
        minimum: 30,
        reason: "No promoted calibration for this asset and horizon",
        n_basis: basis,
      }),
    ).toEqual({
      value: "Not enough data",
      detail: `no promoted calibration for this asset and horizon · horizon 4 hours · 151 ${basis} (minimum 30)`,
    });
    // An older service without n_basis.
    expect(probabilityLabel({ status: "NOT ENOUGH DATA", n: 1 }).detail).toBe("1 comparable past decision");
    expect(probabilityLabel(null)).toEqual({ value: "Not enough data", detail: undefined });
  });
  it("canonicalises asset route symbols", () => {
    expect(canonicalSymbol("btc")).toBe("BTC");
    expect(canonicalSymbol("BTC%20")).toBe("BTC");
    expect(canonicalSymbol("%E0%A4%A")).toBe("%E0%A4%A");
    expect(isAssetSymbol("BTC")).toBe(true);
    expect(isAssetSymbol("US10Y")).toBe(true);
    expect(isAssetSymbol("")).toBe(false);
    expect(isAssetSymbol("%E0%A4%A")).toBe(false);
  });
  it("titles a route with the asset only when the segment names it exactly, however often it was decoded", () => {
    expect(exactSymbol("BTC")).toBe("BTC");
    expect(exactSymbol("BRK.B")).toBe("BRK.B");
    // /assets/BTC%2520: the page sees "BTC%2520", the metadata "BTC%20"; neither is BTC.
    expect(exactSymbol("BTC%2520")).toBeNull();
    expect(exactSymbol("BTC%20")).toBeNull();
    expect(exactSymbol("BTC ")).toBeNull();
    // Redirected by the page to /assets/BTC: never titled here.
    expect(exactSymbol("btc")).toBeNull();
    expect(exactSymbol("NOPE%2F")).toBeNull();
  });
  it("counts the chart's daily sessions against the ones the levels used, from the same bars only", () => {
    const levels = { daily_sessions: 274, daily_basis: "daily bars: Alpaca IEX" };
    expect(storedSessions(levels, "Daily bars: Alpaca IEX")).toBe(274);
    // Another series (official closes): its count is never put beside the chart.
    expect(storedSessions(levels, "Daily closes: FRED")).toBeNull();
    expect(storedSessions({ daily_sessions: 0, daily_basis: "daily bars: Alpaca IEX" }, "Daily bars: Alpaca IEX")).toBeNull();
    expect(storedSessions(null, "Daily bars: Alpaca IEX")).toBeNull();
    expect(storedSessions(levels, null)).toBeNull();
    expect(sessionCountText(260, 274)).toBe("latest 260 of 274 sessions");
    expect(sessionCountText(260, 260)).toBe("260 sessions");
    expect(sessionCountText(1, null)).toBe("1 session");
    expect(sessionCountText(5)).toBe("5 sessions");
  });
  it("gives every price label of a chart the same decimals", () => {
    // "7,743.40" next to "7,743.42", never "7,743.4".
    expect(priceDigits([7743.41, 7743.4])).toBe(2);
    expect(priceDigits([64250.5, 64251])).toBe(1);
    expect(priceDigits([64250.123, 64251])).toBe(2);
    expect(priceDigits([5.17, 5.2])).toBe(2);
    expect(priceDigits([1.14032, 1.1367])).toBe(4);
    expect(priceDigits([0.6523456, 0.65])).toBe(6);
    expect(priceDigits([97, 98])).toBe(0);
    expect(priceDigits([])).toBe(2);
  });
});

describe("shortSourceName", () => {
  it("drops copyright and permission sentences of the real FRED attributions for compact lines", () => {
    const fred = (attribution: string) => ({ provider: "fred", attribution });
    expect(
      shortSourceName(
        fred(
          "Source: S&P Dow Jones Indices LLC via FRED. Copyright S&P Dow Jones Indices LLC; reproduction requires S&P DJI's prior written permission",
        ),
      ),
    ).toBe("S&P Dow Jones Indices LLC via FRED");
    expect(
      shortSourceName(fred("Source: Nasdaq, Inc. via FRED. Copyright NASDAQ OMX Group, Inc. (FRED: copyrighted, pre-approval required)")),
    ).toBe("Nasdaq, Inc. via FRED");
    expect(shortSourceName(fred("Source: Cboe via FRED (copyright Chicago Board Options Exchange, Inc.)"))).toBe(
      "Cboe via FRED",
    );
    expect(shortSourceName(fred("Source: U.S. Energy Information Administration via FRED"))).toBe(
      "U.S. Energy Information Administration via FRED",
    );
    expect(shortSourceName({ provider: "alpaca", feed: "iex" })).toBe("alpaca (IEX)");
  });
});
