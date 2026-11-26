import type { Asset, AssetCoverage, AssetProxy, DataNotice, Quote } from "./api";
import { isNum } from "./format";

/*
 * Provenance of market values (docs/MARKET_COVERAGE.md): every value is shown
 * with its source, its as-of time and its basis (live, daily close, derived,
 * proxy). Nothing here invents a value; missing inputs stay missing.
 */

const TZ = "Europe/Bucharest";
// Fixed abbreviations: ICU versions disagree on "Sep" / "Sept".
const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

function dateParts(value: unknown): Record<string, string> | null {
  if (typeof value !== "string") return null;
  const t = Date.parse(value);
  if (!Number.isFinite(t)) return null;
  return Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: TZ,
      weekday: "short",
      day: "numeric",
      month: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(new Date(t))
      .map((p) => [p.type, p.value]),
  );
}

/** "24 Sep" (never "01 Oct") in Europe/Bucharest; null when missing or invalid. */
export function shortDate(value: unknown): string | null {
  const p = dateParts(value);
  // en-GB pads a numeric day in some ICU versions ("01"); the number drops the zero.
  return p ? `${Number(p.day)} ${MONTHS[Number(p.month) - 1]}` : null;
}

/** "Fri 25 Sep 23:00" (never "Thu 01 Oct") in Europe/Bucharest; null when missing or invalid. */
export function asOfTime(value: unknown): string | null {
  const p = dateParts(value);
  return p
    ? `${p.weekday} ${Number(p.day)} ${MONTHS[Number(p.month) - 1]} ${p.hour}:${p.minute}`
    : null;
}

/** App-zone name shown after times this app formats (service labels keep their own ET / CET). */
export const APP_ZONE = "Bucharest";

/** "Fri 25 Sep 23:00 Bucharest"; null when missing or invalid. */
export function zonedTime(value: unknown): string | null {
  const time = asOfTime(value);
  return time ? `${time} ${APP_ZONE}` : null;
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/**
 * "Fri 25 Sep" for a calendar date ("2026-09-25", e.g. a New York session
 * date): the date itself, never shifted into another zone. Null when invalid.
 */
export function calendarDay(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (!m) return null;
  const t = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const d = new Date(t);
  if (!Number.isFinite(t) || d.getUTCDate() !== Number(m[3])) return null;
  return `${WEEKDAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

const ISO_INSTANT =
  /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?/g;
const ISO_DATE = /\b\d{4}-\d{2}-\d{2}\b/g;

/**
 * Service text with its raw ISO timestamps made readable: an instant becomes
 * "Fri 25 Sep 23:59 Bucharest", a calendar date "Fri 25 Sep". Everything else
 * is kept as sent ("FRIDAY CLOSE (IEX last trade 2026-09-25T20:59:34+00:00)"
 * -> "FRIDAY CLOSE (IEX last trade Fri 25 Sep 23:59 Bucharest)").
 */
export function readableLabel(value: string): string {
  return value
    .replace(ISO_INSTANT, (iso) => {
      // A timestamp without an offset is read as UTC (the service writes UTC).
      const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`;
      return zonedTime(zoned) ?? iso;
    })
    .replace(ISO_DATE, (day) => calendarDay(day) ?? day);
}

/** Venue names for the session calendar codes (backend market_sessions.venues). */
const VENUE_NAMES: Record<string, string> = {
  CRYPTO: "Crypto (24/7)",
  FX: "Spot FX",
  XNYS: "NYSE",
  XNYS_INDEX: "NYSE (cash index hours)",
  ICE_US: "ICE US",
  CME: "CME Globex",
  US_TREASURY: "US Treasury (CBOT futures hours)",
  CBOE_VIX: "Cboe (VIX)",
};

/** "NYSE" for "XNYS"; an unknown code is shown with spaces instead of underscores. */
export function venueName(code: unknown): string | null {
  if (typeof code !== "string" || !code.trim()) return null;
  return VENUE_NAMES[code] ?? code.replaceAll("_", " ");
}

/** Session basis without the calendar library's name ("… (exchange_calendars IEPA)"). */
export function sessionBasisText(basis: unknown): string | null {
  if (typeof basis !== "string") return null;
  const text = basis.replace(/\s*\(exchange_calendars\b[^)]*\)/g, "").trim();
  return text || null;
}

/** Yields are quoted in percent: "5.17%" for a bond, "83,442.5" otherwise; "Unavailable" when missing. */
export function levelText(
  value: unknown,
  assetClass: string | null | undefined,
  digits = 5,
): string {
  if (!isNum(value)) return "Unavailable";
  const body = value.toLocaleString("en-US", { maximumFractionDigits: digits });
  return assetClass === "bond" ? `${body}%` : body;
}

/**
 * Publishers of official end-of-day values (Treasury, FRED, ECB, EIA); a daily
 * value from a trading feed (a last trade, a vendor close) is not official.
 */
const OFFICIAL_PUBLISHERS = new Set(["treasury", "fred", "ecb", "eia"]);

export function isOfficialPublisher(provider: unknown): boolean {
  return typeof provider === "string" && OFFICIAL_PUBLISHERS.has(provider.toLowerCase());
}

/** Evaluation horizon keys as words: "5m" -> "5 min", "close" -> "At close". */
const HORIZON_WORDS: Record<string, string> = {
  close: "At close",
  next_session: "Next session",
};

export function horizonLabel(horizon: string): string {
  if (HORIZON_WORDS[horizon]) return HORIZON_WORDS[horizon];
  const m = /^(\d+)\s*(m|min|h|d)$/.exec(horizon);
  if (!m) return horizon.replaceAll("_", " ");
  const n = Number(m[1]);
  const unit = m[2] === "h" ? "hour" : m[2] === "d" ? "day" : "min";
  return unit === "min" ? `${n} min` : `${n} ${unit}${n === 1 ? "" : "s"}`;
}

type ClassFields = Pick<Quote, "data_class" | "daily_close" | "derived">;

/** Official end-of-day value or a daily value derived by this app (never a live observation). */
export function isDailyClass(q: ClassFields | null | undefined): boolean {
  if (!q) return false;
  return (
    q.data_class === "DAILY_CLOSE" ||
    q.data_class === "DERIVED_DAILY" ||
    (!q.data_class && q.daily_close === true)
  );
}

/** A daily close that is the latest publication expected for its venue. */
export function isCurrentClose(
  q: (ClassFields & Pick<Quote, "close_status">) | null | undefined,
): boolean {
  return isDailyClass(q) && q?.close_status === "CURRENT";
}

/** Value computed by this app (e.g. the USD index replica from the ECB fix). */
export function isDerived(q: ClassFields | null | undefined): boolean {
  return !!q && (q.derived === true || q.data_class === "DERIVED_DAILY");
}

export type SignedText = { text: string; sign: number };

/** Signed change: basis points win over percent ("-6 bp", "+0.32%"); null when none was reported. */
export function signedChange(q: {
  change_bp?: number | null;
  change_pct?: number | null;
}): SignedText | null {
  const bp = isNum(q.change_bp) ? q.change_bp : null;
  const pct = isNum(q.change_pct) ? q.change_pct : null;
  const value = bp ?? pct;
  if (value === null) return null;
  const body = Math.abs(value).toLocaleString("en-US", {
    maximumFractionDigits: bp !== null ? 1 : 2,
    minimumFractionDigits: bp !== null ? 0 : 2,
  });
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return {
    text: `${sign}${body}${bp !== null ? " bp" : "%"}`,
    sign: Math.sign(value),
  };
}

type BasisFields = {
  change_basis?: string | null;
  previous_close_at?: string | null;
  provider?: string;
};

/** What the change is measured against: "24h", "vs prev close 24 Sep", "vs prev ECB fix 24 Sep". */
export function basisText(q: BasisFields): string {
  const day = shortDate(q.previous_close_at);
  const on = day ? ` ${day}` : "";
  switch (q.change_basis) {
    case "rolling_24h":
      return "24h";
    case "previous_close":
      return `vs prev close${on}`;
    case "previous_fix":
      return /^ecb/i.test(q.provider ?? "")
        ? `vs prev ECB fix${on}`
        : `vs prev fix${on}`;
    case "provider":
      return q.provider ? `per ${q.provider}` : "as reported";
    default:
      return "";
  }
}

/** Change with its basis, e.g. "-6 bp vs prev close 24 Sep" or "+0.71% 24h". */
export function changeLine(
  q: BasisFields & { change_bp?: number | null; change_pct?: number | null },
): SignedText | null {
  const change = signedChange(q);
  if (!change) return null;
  const basis = basisText(q);
  return { ...change, text: basis ? `${change.text} ${basis}` : change.text };
}

/** "treasury", "alpaca (IEX)". */
export function sourceText(q: { provider?: string; feed?: string | null }): string {
  const provider = q.provider || "unknown source";
  return q.feed ? `${provider} (${q.feed.toUpperCase()})` : provider;
}

/**
 * The source as its attribution names it ("Source: Binance" -> "Binance"), with
 * the feed when the attribution does not already say it ("… (IEX)"); the
 * provider id (sourceText) when there is no attribution.
 */
export function sourceName(q: {
  provider?: string;
  feed?: string | null;
  attribution?: string | null;
}): string {
  const named = q.attribution?.replace(/^\s*source:\s*/i, "").trim();
  if (!named) return sourceText(q);
  const feed = q.feed?.trim().toUpperCase();
  return feed && !named.toUpperCase().includes(feed) ? `${named} (${feed})` : named;
}

/** Session states the service may write into an as-of label (backend sessions.STATES). */
const SESSION_STATES = "LIVE|LAST CLOSE|FRIDAY CLOSE|AFTER HOURS|PREMARKET|UNKNOWN";
const LEADING_STATE = new RegExp(`^(?:${SESSION_STATES})\\s*\\((.+)\\)$`);
const TRAILING_STATE = new RegExp(`,\\s*(?:${SESSION_STATES})$`);

/**
 * The as-of text without the session state the service wrote into it when it
 * polled ("PREMARKET (last trade Fri 25 Sep 16:00 ET)" -> "last trade Fri 25
 * Sep 16:00 ET", "real-time (…), AFTER HOURS" -> "real-time (…)"): the page
 * shows the current session state on its own, and a state from an earlier
 * poll beside it would contradict it.
 */
export function withoutSessionState(label: string): string {
  const text = label.trim();
  const inner = LEADING_STATE.exec(text)?.[1];
  return (inner ?? text).replace(TRAILING_STATE, "").trim() || text;
}

/** The text names a clock time ("16:00 ET", "23:59 Bucharest"), not only a date. */
export function hasClockTime(text: string): boolean {
  return /\b\d{1,2}:\d{2}\b/.test(text);
}

/**
 * The quote's as-of text for display: the service's label made readable
 * (readableLabel) without a session state (withoutSessionState); null without one.
 */
export function asOfLabel(q: Pick<Quote, "as_of_label">): string | null {
  const raw = q.as_of_label?.trim();
  return raw ? withoutSessionState(readableLabel(raw)) : null;
}

/**
 * "as of Fri 25 Sep 16:00 ET · U.S. Department of the Treasury" or "as of Fri
 * 25 Sep 23:00 Bucharest · Binance": the service's as-of text (with its own
 * zone, asOfLabel) wins over the raw timestamp, which is shown in the app zone
 * and says so. "as of" is never repeated ("EIA spot, as of Tue 22 Sep"). The
 * source is named as its attribution says (sourceName), the provider id only
 * when there is none.
 */
export function asOfLine(
  q: Pick<Quote, "timestamp" | "provider" | "feed" | "as_of_label" | "attribution">,
): string {
  const label = asOfLabel(q);
  const time = zonedTime(q.timestamp);
  const when = label
    ? /\bas of\b/i.test(label)
      ? label
      : `as of ${label}`
    : time
      ? `as of ${time}`
      : null;
  return [when, shortSourceName(q)].filter(Boolean).join(" · ");
}

/**
 * sourceName without a trailing copyright or permission sentence, for compact
 * lines (the as-of line, market-strip tiles): "S&P Dow Jones Indices LLC via
 * FRED. Copyright …" -> "S&P Dow Jones Indices LLC via FRED". The Data source
 * panel keeps the full attribution.
 */
export function shortSourceName(q: Parameters<typeof sourceName>[0]): string {
  return sourceName(q)
    .replace(/(?:[.;]\s+|\s+\()(?:copyright|©|reproduction).*$/i, "")
    .trim();
}

/** "ALPACA" for "ALPACA_API_KEY_ID": keys of one vendor are needed together. */
function keyVendor(key: string): string {
  return key.split("_", 1)[0];
}

/**
 * "needs FRED_API_KEY or MASSIVE_API_KEY"; keys of one vendor that are all
 * needed are joined: "needs FINNHUB_API_KEY or ALPACA_API_KEY_ID + ALPACA_API_SECRET_KEY".
 * Null when no key would help.
 */
export function missingKeysText(keys: unknown): string | null {
  if (!Array.isArray(keys)) return null;
  const names = [
    ...new Set(keys.filter((k): k is string => typeof k === "string" && !!k)),
  ];
  const byVendor = new Map<string, string[]>();
  for (const name of names)
    byVendor.set(keyVendor(name), [...(byVendor.get(keyVendor(name)) ?? []), name]);
  const options = [...byVendor.values()].map((group) => group.join(" + "));
  return options.length ? `needs ${options.join(" or ")}` : null;
}

/**
 * The venue's session state (backend sessions.STATES: LIVE, LAST CLOSE, FRIDAY
 * CLOSE, AFTER HOURS, PREMARKET, UNKNOWN): the quote's status, else the asset
 * session; never UNKNOWN when a session exists. STALE is a freshness verdict,
 * never a session state: a status that says so is skipped.
 */
export function marketState(asset: Asset | undefined): string {
  const q = asset?.quote;
  const venue = (state: unknown) => (typeof state === "string" && state && state !== "STALE" ? state : null);
  return venue(q?.market_status) ?? venue(q?.market_state) ?? venue(asset?.session?.state) ?? "UNKNOWN";
}

/** Development stub marker from the provider contract (stub=true or "<name>-stub"). */
export function isStub(
  item?: { stub?: boolean; provider?: string; name?: string } | null,
): boolean {
  if (!item) return false;
  return (
    item.stub === true ||
    /-stub$/i.test(item.provider ?? "") ||
    /-stub$/i.test(item.name ?? "")
  );
}

/** Session states in which the venue does not trade its regular session (backend sessions.STATES). */
const CLOSED_SESSIONS = new Set([
  "LAST CLOSE",
  "FRIDAY CLOSE",
  "AFTER HOURS",
  "PREMARKET",
]);

/** The venue's regular session is closed, so the last print is not expected to move. */
export function isClosedSession(state: unknown): boolean {
  return typeof state === "string" && CLOSED_SESSIONS.has(state);
}

/** A live-class observation from a real feed (not a daily close, not a stub). */
export function isLiveData(q: Quote | null | undefined): boolean {
  return !!q && (q.data_class == null || q.data_class === "LIVE") && !q.daily_close && !isStub(q);
}

/**
 * Session badge text: the session state, except that an open venue reads
 * "MARKET OPEN" unless the asset's own value is live data. The word LIVE is
 * kept for live data (a daily close shown while the market trades is not live).
 */
export function sessionLabel(asset: Asset | undefined): string {
  const state = marketState(asset);
  return state === "LIVE" && !isLiveData(asset?.quote) ? "MARKET OPEN" : state;
}

/**
 * The session badge shown beside the data chip: the venue state, apart from
 * the chip's freshness verdict. Null when it would add nothing (UNKNOWN, or
 * the same word as the chip: LIVE next to LIVE). An open venue whose quote is
 * not live right now (the chip says STALE) reads "MARKET OPEN", so LIVE never
 * stands beside STALE.
 */
export function sessionBadge(asset: Asset | undefined): string | null {
  const chip = dataChip(asset).label;
  const session = sessionLabel(asset);
  const label = session === "LIVE" && chip !== "LIVE" ? "MARKET OPEN" : session;
  return label === "UNKNOWN" || label === chip ? null : label;
}

export type ChipTone = "positive" | "negative" | "info" | "neutral" | "outline";
export type DataChip = {
  label: string;
  tone: ChipTone;
  /** Short qualifier shown next to the chip, e.g. "needs FRED_API_KEY". */
  detail?: string;
  /** STALE must stand out. */
  prominent?: boolean;
};

/**
 * What kind of value the tile shows: LIVE, DAILY CLOSE, DERIVED, PROXY, STALE,
 * LAST PRINT, STUB, NOT CONFIGURED (with the key that would fill it),
 * DISABLED, NO DATA YET or NO QUOTE. Current official closes and the last
 * print of a closed session are informational, never an error.
 */
export function dataChip(asset: Asset | undefined): DataChip {
  const q = asset?.quote;
  if (!asset) return { label: "NOT DELIVERED", tone: "outline" };
  if (!q) {
    if (asset.proxy) return { label: "PROXY", tone: "neutral" };
    const status = asset.coverage?.status;
    if (status === "NOT_CONFIGURED")
      return {
        label: "NOT CONFIGURED",
        tone: "outline",
        detail: missingKeysText(asset.coverage?.missing_keys) ?? undefined,
      };
    if (status === "DISABLED")
      return { label: "DISABLED", tone: "outline", detail: "switched off in Settings" };
    if (status === "NO_DATA_YET") return { label: "NO DATA YET", tone: "outline" };
    return { label: "NO QUOTE", tone: "outline" };
  }
  // A development stub is never a real quote, whatever its freshness.
  if (isStub(q)) return { label: "STUB", tone: "outline" };
  if (isDailyClass(q)) {
    if (q.close_status === "STALE")
      return { label: "STALE", tone: "negative", prominent: true, detail: "late close" };
    if (q.close_status === "CURRENT")
      return isDerived(q)
        ? { label: "DERIVED", tone: "info" }
        : { label: "DAILY CLOSE", tone: "info" };
    // No close status (older service): fall back to the freshness status below.
  }
  const freshness = q.freshness_status;
  if (freshness === "FRESH") return { label: "LIVE", tone: "positive" };
  if (!freshness || freshness === "STALE") {
    // The last print of a closed session is expected to be old (like proxyStale):
    // STALE stays prominent for a feed that is late while its market trades.
    if (!isDailyClass(q) && isClosedSession(marketState(asset)))
      return { label: "LAST PRINT", tone: "outline" };
    return { label: "STALE", tone: "negative", prominent: true };
  }
  return { label: freshness.replaceAll("_", " "), tone: "outline" };
}

/**
 * Proxy line for a tile, with the basis of the proxy's change: "+0.42% 24h via PAXG",
 * "+0.51% vs prev close via SPY". The proxy's price is never shown as the asset's level.
 */
export function proxyLine(proxy: AssetProxy): SignedText {
  const change = changeLine({
    change_pct: proxy.change_pct,
    change_basis: proxy.change_basis,
    provider: proxy.provider,
  });
  return change
    ? { text: `${change.text} via ${proxy.symbol}`, sign: change.sign }
    : { text: `via ${proxy.symbol} · no change reported`, sign: 0 };
}

/** Tooltip text for a proxy's change, spelling out a rolling window: "+0.42% over a rolling 24 hours (not since a close)". */
export function proxyChangeTitle(proxy: AssetProxy): string {
  const change = changeLine({
    change_pct: proxy.change_pct,
    change_basis: proxy.change_basis,
    provider: proxy.provider,
  });
  if (!change) return `Proxy change: not reported by ${proxy.symbol}`;
  if (proxy.change_basis === "rolling_24h")
    return `Proxy change: ${signedChange({ change_pct: proxy.change_pct })?.text} over a rolling 24 hours (${proxy.symbol}; not since a close)`;
  return `Proxy change: ${change.text} (${proxy.symbol})`;
}

/**
 * A proxy print is late only while its own market trades: the last print of a
 * closed session (FRIDAY CLOSE, LAST CLOSE, …) is not flagged as stale.
 */
export function proxyStale(
  proxy: Pick<AssetProxy, "freshness_status" | "market_status">,
): boolean {
  if (proxy.freshness_status !== "STALE") return false;
  return (
    !proxy.market_status ||
    proxy.market_status === "LIVE" ||
    proxy.market_status === "STALE"
  );
}

/** Human text for a coverage status. */
export function coverageText(coverage: AssetCoverage | null | undefined): string | null {
  switch (coverage?.status) {
    case "OK":
      return "Covered by a configured source";
    case "DAILY_ONLY":
      return "Daily closes only";
    case "PROXY_ONLY":
      return "No level source; proxy only";
    case "NOT_CONFIGURED":
      return "No source configured";
    case "DISABLED":
      return "Sources switched off in Settings";
    case "NO_DATA_YET":
      return "Source configured, no data yet";
    default:
      return null;
  }
}

/** Sources an administrator switched off for this asset, de-duplicated. */
export function disabledSources(coverage: AssetCoverage | null | undefined): string[] {
  const list = coverage?.disabled;
  if (!Array.isArray(list)) return [];
  return [...new Set(list.filter((s): s is string => typeof s === "string" && !!s))];
}

/**
 * Why an input the service could not use is missing, from its asset's coverage:
 * no source configured, sources switched off, no data yet, or data that exists
 * but was stale or not usable (an old close, an old or unsuitable proxy).
 * "unknown" when there is no coverage information.
 */
export type GapKind = "not_configured" | "disabled" | "no_data" | "unusable" | "unknown";

export function gapKind(coverage: AssetCoverage | null | undefined): GapKind {
  switch (coverage?.status) {
    case "NOT_CONFIGURED":
      return "not_configured";
    case "DISABLED":
      return "disabled";
    case "NO_DATA_YET":
      return "no_data";
    case "OK":
    case "DAILY_ONLY":
    case "PROXY_ONLY":
      return "unusable";
    default:
      return "unknown";
  }
}

/** Well-formed notices only (a text is required); ids fall back to their position. */
export function parseNotices(raw: unknown): DataNotice[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((n, i) => {
    if (!n || typeof n !== "object") return [];
    const { id, text, providers } = n as Partial<DataNotice>;
    if (typeof text !== "string" || !text.trim()) return [];
    return [
      {
        id: typeof id === "string" && id ? id : `notice-${i}`,
        text: text.trim(),
        providers: Array.isArray(providers)
          ? providers.filter((p): p is string => typeof p === "string" && !!p)
          : [],
      },
    ];
  });
}

/**
 * One list from several (the asset's own notices first), without repeating a
 * notice: the same id, or the same text, is shown once.
 */
export function mergeNotices(...lists: DataNotice[][]): DataNotice[] {
  const seen = new Set<string>();
  return lists.flat().filter((n) => {
    const keys = [`id:${n.id}`, `text:${n.text}`];
    if (keys.some((k) => seen.has(k))) return false;
    keys.forEach((k) => seen.add(k));
    return true;
  });
}

/** Notices that concern an asset: those naming the provider of its quote or proxy. */
export function noticesFor(notices: DataNotice[], asset: Asset | undefined): DataNotice[] {
  const used = new Set(
    [asset?.quote?.provider, asset?.proxy?.provider]
      .filter((p): p is string => typeof p === "string" && !!p)
      .map((p) => p.toLowerCase()),
  );
  if (!used.size) return [];
  return notices.filter((n) => (n.providers ?? []).some((p) => used.has(p.toLowerCase())));
}
