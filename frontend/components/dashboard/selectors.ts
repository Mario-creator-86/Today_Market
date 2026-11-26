import type {
  Alert,
  Asset,
  CausalPath,
  MarketEvent,
  RecordData,
} from "../../lib/api";
import type { BadgeVariant } from "../ui/badge";
import { isNum as finite } from "../../lib/format";
import {
  asOfLine,
  changeLine,
  coverageText,
  dataChip,
  disabledSources,
  isDerived,
  isStub,
  levelText,
  missingKeysText,
  proxyChangeTitle,
  proxyLine,
  proxyStale,
  sessionLabel,
  shortDate,
  signedChange,
  sourceText,
  zonedTime,
  type DataChip,
} from "../../lib/market-data";
import { PRIORITY_RANK, symbolLabel } from "./view-model";

/** §27 market strip, used when the backend does not send `market_strip`. */
export const DEFAULT_STRIP = [
  "BTC",
  "ETH",
  "SP500",
  "NASDAQ",
  "DXY",
  "US2Y",
  "US10Y",
  "EURUSD",
  "GBPUSD",
  "USDJPY",
  "GOLD",
  "WTI",
  "BRENT",
] as const;

export function stripLabel(symbol: string): string {
  return symbolLabel(symbol);
}

export type StripItem = { symbol: string; label: string; asset?: Asset };

/** Configured strip symbols in order; a symbol with no delivered asset keeps its slot. */
export function stripItems(assets: Asset[], configured?: unknown): StripItem[] {
  const list =
    Array.isArray(configured) &&
    configured.length &&
    configured.every((s) => typeof s === "string")
      ? (configured as string[])
      : [...DEFAULT_STRIP];
  const bySymbol = new Map(assets.map((a) => [a.symbol, a]));
  return list.map((symbol) => ({
    symbol,
    label: stripLabel(symbol),
    asset: bySymbol.get(symbol),
  }));
}

// Stub detection lives in lib/market-data (unit-tested there).
export { isStub };

/** Tone for session state (§47) and freshness (§46) labels. */
export function marketTone(label: string): BadgeVariant {
  if (label === "LIVE" || label === "FRESH") return "positive";
  if (label === "STALE") return "negative";
  // An open venue whose tile is not live data (a daily close, no quote): quiet, never green.
  if (label === "MARKET OPEN") return "outline";
  if (
    ["LAST CLOSE", "FRIDAY CLOSE", "AFTER HOURS", "PREMARKET"].includes(label)
  )
    return "info";
  return "outline";
}

/**
 * Signed change with its basis: "+1.24%", "-6 bp vs prev close 24 Sep",
 * "+0.71% 24h"; null when the provider sent none.
 */
export function changeText(
  quote: Asset["quote"],
): { text: string; sign: number } | null {
  return quote ? changeLine(quote) : null;
}

function priceDigits(asset: Asset): number {
  if (asset.asset_class === "fx") return asset.symbol === "USDJPY" ? 3 : 5;
  return 2;
}

/** Everything one strip tile shows (§27): level, change + basis, session, data chip, as-of, proxy. */
export type TileView = {
  /** Session badge (§47); from the asset session when there is no quote; "MARKET OPEN" unless the value is live data. */
  state: string;
  /** The asset's own level ("5.17%" for a yield), or "—" (a proxy price is never shown as the level). */
  headline: string;
  change: { text: string; sign: number } | null;
  /** Shown instead of a change when there is none. */
  noChange: string;
  proxy: { text: string; sign: number; stale: boolean } | null;
  chip: DataChip;
  /** "as of Fri 25 Sep 16:00 ET · treasury". */
  asOf: string | null;
  /** Tooltip: instrument label, change basis, session, as-of, attribution, caveats and missing keys. */
  title: string;
  stub: boolean;
  /** The ONE tag the compact tile shows: LIVE, CLOSE Thu, PROXY via PAXG, STALE, NOT CONFIGURED… */
  tag: TileTag;
  /** The change without its basis ("+0.19%", "-6 bp"): the asset's own, else its proxy's; null when none. */
  move: { text: string; sign: number } | null;
};

export type TileTag = { label: string; tone: BadgeVariant; prominent?: boolean };

const WEEKDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** "Thu": the day a daily close belongs to (its UTC date, the same calendar day as the market's close). */
function closeDay(timestamp: unknown): string | null {
  const t = typeof timestamp === "string" ? Date.parse(timestamp) : NaN;
  return Number.isFinite(t) ? WEEKDAY[new Date(t).getUTCDay()] : null;
}

/**
 * The one tag of a compact strip tile, from the data chip: LIVE; "CLOSE Thu"
 * for a current daily close ("DERIVED Thu" for a value this app computes);
 * "PROXY via PAXG" when only a direction proxy exists; STALE (prominent) for
 * late data, including a late proxy; DEVELOPMENT STUB for a stub record;
 * other states (LAST PRINT, NOT CONFIGURED, DISABLED, NO DATA YET, NO QUOTE)
 * as the chip names them.
 */
export function tileTag(asset: Asset | undefined): TileTag {
  const chip = dataChip(asset);
  const base: TileTag = { label: chip.label, tone: chip.tone, prominent: chip.prominent };
  if (chip.label === "DAILY CLOSE" || chip.label === "DERIVED") {
    const day = closeDay(asset?.quote?.timestamp);
    const word = chip.label === "DERIVED" ? "DERIVED" : "CLOSE";
    return { ...base, label: day ? `${word} ${day}` : word };
  }
  // A development stub is named in full: it must never pass for a quote.
  if (chip.label === "STUB") return { ...base, label: "DEVELOPMENT STUB" };
  if (chip.label === "PROXY" && asset?.proxy) {
    if (proxyStale(asset.proxy)) return { label: `STALE · via ${asset.proxy.symbol}`, tone: "negative", prominent: true };
    return { ...base, label: `PROXY via ${asset.proxy.symbol}` };
  }
  return base;
}

function noChangeText(asset: Asset | undefined): string {
  if (!asset) return "No quote";
  if (asset.quote) return "No change reported";
  if (asset.proxy) return "No level · proxy only";
  if (asset.coverage?.status === "NOT_CONFIGURED") return "No source configured";
  if (asset.coverage?.status === "DISABLED") return "Source switched off";
  if (asset.coverage?.status === "NO_DATA_YET") return "Awaiting first data";
  return "No quote";
}

function tileTitle(item: StripItem): string {
  const a = item.asset;
  const q = a?.quote;
  const p = a?.proxy;
  const chip = dataChip(a);
  const lines: (string | null | undefined)[] = [q?.label || a?.name || item.label];
  // What the compact tile leaves out: the change with its basis, the data class, the session and the as-of line.
  lines.push(
    q ? (changeLine(q)?.text ?? noChangeText(a)) : noChangeText(a),
    [chip.label, chip.detail].filter(Boolean).join(" · "),
    a ? `Session state: ${sessionLabel(a)}` : null,
    q ? asOfLine(q) : null,
  );
  if (q) {
    lines.push(q.attribution, q.delay_label);
    if (finite(q.previous_close))
      lines.push(
        `Previous ${q.change_basis === "previous_fix" ? "fix" : "close"} ${levelText(q.previous_close, a?.asset_class, 6)}${shortDate(q.previous_close_at) ? ` on ${shortDate(q.previous_close_at)}` : ""}`,
      );
    if (isDerived(q)) lines.push("Derived by this app; not an official index value");
  }
  if (p) {
    const when = zonedTime(p.timestamp);
    lines.push(
      `Proxy: ${p.label || p.symbol}`,
      proxyChangeTitle(p),
      p.caveat,
      p.attribution,
      `${sourceText(p)}${when ? `, as of ${when}` : ""}`,
    );
  }
  if (a?.coverage && a.coverage.status !== "OK") {
    const keys = missingKeysText(a.coverage.missing_keys);
    lines.push(
      [coverageText(a.coverage), keys].filter(Boolean).join(": "),
      keys ? "Configure in Settings → Data providers" : null,
    );
  }
  const off = disabledSources(a?.coverage);
  if (off.length) lines.push(`Switched off in Settings → Data providers: ${off.join(", ")}`);
  if (a?.session?.basis) lines.push(`Session: ${a.session.basis}`);
  return lines.filter((l): l is string => !!l && !!l.trim()).join("\n");
}

/** View model of one strip tile. Expects an asset already passed through `currentAsset`. */
export function tileView(item: StripItem): TileView {
  const a = item.asset;
  const q = a?.quote ?? null;
  const p = a?.proxy ?? null;
  return {
    state: sessionLabel(a),
    // Yields read as yields ("5.17%"); every other level as a plain number.
    headline: q && a ? levelText(q.price, a.asset_class, priceDigits(a)) : "—",
    change: changeText(q),
    noChange: noChangeText(a),
    proxy: p ? { ...proxyLine(p), stale: proxyStale(p) } : null,
    chip: dataChip(a),
    asOf: q ? asOfLine(q) : p ? `${p.symbol} ${asOfLine(p)}` : null,
    title: tileTitle(item),
    stub: isStub(q),
    tag: tileTag(a),
    move: q ? signedChange(q) : p ? signedChange(p) : null,
  };
}

/** Percent from a 0..100 grade; missing never renders as 0%. */
export function percent(value: unknown, digits = 0): string {
  return finite(value)
    ? `${value.toLocaleString("en-US", { maximumFractionDigits: digits })}%`
    : "Unavailable";
}

// --- events -------------------------------------------------------------------------------

export type FactLabel =
  "CONFIRMED" | "PARTIAL" | "UNVERIFIED" | "CONTRADICTED" | "STALE";

/** Map backend verification statuses onto the four §27 stream labels (plus STALE). */
export function factLabel(status: unknown): FactLabel {
  const s = String(status ?? "")
    .toUpperCase()
    .replaceAll("_", " ");
  if (s === "CONFIRMED") return "CONFIRMED";
  if (s === "CONTRADICTED") return "CONTRADICTED";
  if (s === "STALE") return "STALE";
  if (["PARTIAL", "PARTIALLY CONFIRMED", "CORROBORATED"].includes(s))
    return "PARTIAL";
  return "UNVERIFIED";
}

export function factTone(label: FactLabel): BadgeVariant {
  return label === "CONFIRMED"
    ? "positive"
    : label === "CONTRADICTED" || label === "STALE"
      ? "negative"
      : label === "PARTIAL"
        ? "info"
        : "neutral";
}

/** Importance on a 0..10 scale: contract `importance` (0..1), else extractor 0..100; null if never measured. */
export function importanceOf(event: MarketEvent): number | null {
  if (finite(event.importance))
    return Math.max(0, Math.min(10, event.importance * 10));
  const extracted = (event.extraction as RecordData | null | undefined)
    ?.potential_importance;
  if (finite(extracted)) return Math.max(0, Math.min(10, extracted / 10));
  return null;
}

/**
 * What the importance number is made of, as the backend states it ("source
 * tier x extractor importance"); "extractor estimate" when only the
 * extractor's value is shown; null when importance was never measured or the
 * backend names no basis.
 */
export function importanceBasis(event: MarketEvent): string | null {
  if (finite(event.importance)) {
    const basis = typeof event.importance_basis === "string" ? event.importance_basis.trim() : "";
    return basis ? basis.replaceAll("_", " ") : null;
  }
  return importanceOf(event) === null ? null : "extractor estimate";
}

function eventTime(event: MarketEvent): number {
  const t = Date.parse(event.event_time || event.first_seen_utc || "");
  return Number.isFinite(t) ? t : 0;
}

// Topic titles live in lib/events (unit-tested there).
export { eventTitle, oneSentence, topicLabel } from "../../lib/events";

/** A confirmed fact whose impact on at least one asset is established (a nonzero net effect, or a plain mapping). */
function hasConfirmedImpact(e: MarketEvent): boolean {
  return (
    factLabel(e.verification_status) === "CONFIRMED" &&
    affectedAssets(e).some(
      ({ symbol, direction }) => typeof symbol === "string" && !!symbol.trim() && direction !== 0,
    )
  );
}

/**
 * §27 What matters now: confirmed facts with established asset impact, one per
 * topic, measured importance first (descending), then unmeasured by recency.
 * Active paths must yield a nonzero net effect; plain asset mappings need no
 * invented direction. At most `limit`.
 */
export function whatMattersNow(
  events: MarketEvent[],
  limit = 5,
): MarketEvent[] {
  const ranked = events
    .filter(hasConfirmedImpact)
    .sort((a, b) => {
      const ia = importanceOf(a);
      const ib = importanceOf(b);
      if (ia !== null && ib !== null && ia !== ib) return ib - ia;
      if (ia === null && ib !== null) return 1;
      if (ib === null && ia !== null) return -1;
      return eventTime(b) - eventTime(a);
    });
  const seen = new Set<string>();
  const result: MarketEvent[] = [];
  for (const e of ranked) {
    const key = e.topic || `id:${e.id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(e);
    if (result.length >= limit) break;
  }
  return result;
}

/**
 * "Latest" on the opening screen: confirmed facts with established asset
 * impact (the `whatMattersNow` rule), one per topic, newest first. At most
 * `limit`.
 */
export function latestConfirmed(events: MarketEvent[], limit = 4): MarketEvent[] {
  const seen = new Set<string>();
  const result: MarketEvent[] = [];
  for (const e of events.filter(hasConfirmedImpact).sort((a, b) => eventTime(b) - eventTime(a))) {
    const key = e.topic || `id:${e.id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(e);
    if (result.length >= limit) break;
  }
  return result;
}

export type AffectedAsset = { symbol: string; direction: number | null };

/** Net expected direction per asset from active causal paths; plain asset list otherwise. */
export function affectedAssets(event: MarketEvent): AffectedAsset[] {
  const paths = (event.causal_paths ?? []).filter(
    (p: CausalPath) => p && p.active !== false && p.effects,
  );
  if (paths.length) {
    const net = new Map<string, number>();
    for (const p of paths)
      for (const [symbol, dir] of Object.entries(p.effects ?? {}))
        if (finite(dir)) net.set(symbol, (net.get(symbol) ?? 0) + dir);
    return Array.from(net, ([symbol, v]) => ({
      symbol,
      direction: Math.sign(v),
    }));
  }
  const list = event.affected_assets?.length
    ? event.affected_assets
    : (event.assets ?? []);
  return list.map((symbol) => ({ symbol, direction: null }));
}

/** Source display names from `sources` (strings or objects). */
export function sourceNames(event: MarketEvent): string[] {
  return (event.sources ?? [])
    .map((s) =>
      typeof s === "string"
        ? s
        : s && typeof s === "object"
          ? String(
              (s as RecordData).name ??
                (s as RecordData).source ??
                (s as RecordData).id ??
                "",
            )
          : "",
    )
    .filter(Boolean);
}

/** §27 Breaking / verified: newest non-stale events of every verification status. */
export function breakingStream(
  events: MarketEvent[],
  limit = 8,
): MarketEvent[] {
  return events
    .filter((e) => factLabel(e.verification_status) !== "STALE")
    .sort((a, b) => eventTime(b) - eventTime(a))
    .slice(0, limit);
}

// --- calendar -----------------------------------------------------------------------------

/**
 * n/10 threshold for "high impact" used only when the backend sends no
 * `calendar_min_impact` (an older backend); it matches the calendar page HIGH
 * filter and is never stated as the configured threshold.
 */
export const DEFAULT_MIN_IMPACT = 7;

export function scheduledAt(item: RecordData): number {
  const t = Date.parse(String(item.scheduled_utc ?? item.scheduled_at ?? ""));
  return Number.isFinite(t) ? t : NaN;
}

export function passesMinImpact(item: RecordData, min: number): boolean {
  const impact = item.impact;
  if (finite(impact)) return impact >= min;
  return String(impact ?? "").toUpperCase() === "HIGH";
}

export function impactText(item: RecordData): string {
  const impact = item.impact;
  if (finite(impact))
    return `Impact ${impact.toLocaleString("en-US", { maximumFractionDigits: 1 })}/10`;
  return `Impact ${String(impact).toUpperCase()}`;
}

/** Upcoming (scheduled at or after `now`), high-impact releases, soonest first. */
export function nextImportantEvents(
  calendar: RecordData[],
  now: number,
  minImpact = DEFAULT_MIN_IMPACT,
  limit = 5,
): RecordData[] {
  return calendar
    .filter((item) => {
      const t = scheduledAt(item);
      return Number.isFinite(t) && t >= now && passesMinImpact(item, minImpact);
    })
    .sort((a, b) => scheduledAt(a) - scheduledAt(b))
    .slice(0, limit);
}

// --- alerts -------------------------------------------------------------------------------

/** Keep well-formed alerts only, newest first within priority (the dashboard panel orders them with `alertRows`). */
export function parseAlerts(raw: unknown): Alert[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter(
      (a): a is Alert =>
        !!a &&
        typeof a === "object" &&
        typeof (a as Alert).id === "string" &&
        typeof (a as Alert).title === "string",
    )
    .map((a) => ({
      ...a,
      priority: String(a.priority ?? "LOW").toUpperCase(),
      kind: String(a.kind ?? "ALERT").toUpperCase(),
      message: typeof a.message === "string" ? a.message : "",
      symbol: typeof a.symbol === "string" ? a.symbol : null,
    }))
    .sort(
      (a, b) =>
        (PRIORITY_RANK[a.priority] ?? 3) - (PRIORITY_RANK[b.priority] ?? 3) ||
        (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0),
    );
}

export function priorityTone(priority: string): BadgeVariant {
  return priority === "HIGH"
    ? "negative"
    : priority === "MEDIUM"
      ? "neutral"
      : "outline";
}
