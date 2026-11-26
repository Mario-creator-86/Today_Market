import type { Asset, Bar, CrossAssetSeries } from "@/lib/api";

/*
 * Chart series for the asset page. Pure, with type-only imports, so the
 * vitest suite (lib/market-data.test.ts) can collect it.
 */

export type Point = { time: number; value: number };

/** Interval of a daily series in `cross_asset_interval` (seconds). */
export const DAY_SECONDS = 86400;

/** Fixed colour per entity (never per rank). Validated against the card surface. */
export const OVERLAY_COLORS: Record<string, string> = {
  DXY: "#3987e5",
  US10Y: "#d95926",
  NASDAQ: "#199e70",
};
/** The subject asset is drawn in ink, so it never competes with an overlay hue. */
export const SUBJECT_COLOR = "#e5edf7";
const SPARE_COLORS = Object.values(OVERLAY_COLORS);
export const DEFAULT_OVERLAYS = ["DXY", "US10Y", "NASDAQ"];
const MAX_OVERLAYS = 3;

export const OVERLAY_NAMES: Record<string, string> = {
  DXY: "US dollar index",
  US10Y: "US Treasury 10Y",
  NASDAQ: "Nasdaq",
};

/** Stored daily bar (GET /api/assets/{symbol} `daily_bars`): `time` is the New York session date. */
export type DailyBar = {
  time: string;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close: number;
  volume?: number | null;
};

type TimedClose = { time: string | number; close: number };

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/** Seconds since epoch (UTC); a calendar date is its UTC midnight. */
function barSeconds(time: string | number): number {
  if (typeof time === "number") return time;
  return Math.floor(
    Date.parse(DATE_ONLY.test(time) ? `${time}T00:00:00Z` : time) / 1000,
  );
}

/** Sorted, de-duplicated, finite points (seconds since epoch, UTC). */
function points(rows: TimedClose[], time: (t: string | number) => number): Point[] {
  const sorted = rows
    .map((b) => ({ time: time(b.time), value: b.close }))
    .filter((p) => Number.isFinite(p.value) && Number.isFinite(p.time))
    .sort((a, b) => a.time - b.time);
  // Keep the last value of a repeated time (the latest close of a day).
  return sorted.filter(
    (p, i) => i === sorted.length - 1 || p.time !== sorted[i + 1].time,
  );
}

/** Bars to sorted, de-duplicated, finite points (seconds since epoch, UTC). */
export function toPoints(bars: TimedClose[] | null | undefined): Point[] {
  return points(bars ?? [], barSeconds);
}

let newYork: Intl.DateTimeFormat | undefined;

/**
 * Daily key of a bar time, as the UTC midnight of its New York session date
 * (the service keys daily bars that way): a calendar date or a UTC-midnight
 * stamp is that date; any other instant is its New York calendar date (a
 * 16:00 ET close belongs to that day).
 */
export function sessionDay(time: string | number): number {
  const seconds = barSeconds(time);
  if (!Number.isFinite(seconds) || seconds % DAY_SECONDS === 0) return seconds;
  newYork ??= new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const p = Object.fromEntries(
    newYork.formatToParts(new Date(seconds * 1000)).map((x) => [x.type, x.value]),
  );
  return Date.UTC(Number(p.year), Number(p.month) - 1, Number(p.day)) / 1000;
}

/** Last close per New York session date, keyed at that date's UTC midnight. */
export function dailyPoints(bars: TimedClose[] | null | undefined): Point[] {
  return points(bars ?? [], sessionDay);
}

/** Decimals a value is written with, up to `cap` ("7743.41" has 2). */
function decimals(value: number, cap: number): number {
  for (let d = 0; d < cap; d++) {
    const scaled = value * 10 ** d;
    if (Math.abs(scaled - Math.round(scaled)) <= 1e-9 * Math.max(1, Math.abs(scaled))) return d;
  }
  return cap;
}

/**
 * Decimals of every price label of a chart, the same on each ("7,743.40" next
 * to "7,743.42", never "7,743.4"): as many as the values carry, at most 2 from
 * 1,000, 4 from 1 and 6 below.
 */
export function priceDigits(values: number[]): number {
  const finite = values.filter(Number.isFinite);
  if (!finite.length) return 2;
  const largest = Math.max(...finite.map(Math.abs));
  const cap = largest >= 1000 ? 2 : largest >= 1 ? 4 : 6;
  return Math.max(...finite.map((v) => decimals(v, cap)));
}

/** "5 sessions", or "latest 260 of 274 sessions" when the service holds more than the chart shows. */
export function sessionCountText(shown: number, stored?: number | null): string {
  const count = (n: number) => n.toLocaleString("en-US");
  if (typeof stored === "number" && Number.isFinite(stored) && stored > shown)
    return `latest ${count(shown)} of ${count(stored)} sessions`;
  return `${count(shown)} session${shown === 1 ? "" : "s"}`;
}

/** At least two points on a positive base: enough to draw and to index. */
function usable(p: Point[]): boolean {
  return p.length >= 2 && p[0].value > 0;
}

/** The asset fields the chart reads. `daily_bars` and `cross_asset_interval` are absent on older services. */
export type SeriesSource = Pick<Asset, "symbol" | "name" | "bars" | "cross_asset"> & {
  daily_bars?: DailyBar[] | null;
  /** Seconds per overlay bar: 60 for one-minute bars, 86400 for daily closes. */
  cross_asset_interval?: Record<string, number> | null;
};

type RawSeries = {
  symbol: string;
  name?: string;
  bars: Bar[] | null;
  /** Daily closes (per `cross_asset_interval`, or calendar-date times). */
  daily: boolean;
};

/** Accepts `{SYMBOL: Bar[] | {bars, name}}` or `[{symbol, bars, name}]`. */
export function overlayInputs(asset: SeriesSource): RawSeries[] | null {
  const raw = asset.cross_asset;
  if (!raw || typeof raw !== "object") return null;
  const entries: [string, Bar[] | CrossAssetSeries | null][] = Array.isArray(
    raw,
  )
    ? raw.map((s, i) => [s.symbol ?? `Series ${i + 1}`, s])
    : Object.entries(raw);
  const interval = asset.cross_asset_interval ?? {};
  return entries.map(([symbol, value]) => {
    const bars = Array.isArray(value) ? value : (value?.bars ?? null);
    const seconds = interval[symbol];
    const daily =
      typeof seconds === "number"
        ? seconds >= DAY_SECONDS
        : !!bars?.length &&
          bars.every((b) => typeof b.time === "string" && DATE_ONLY.test(b.time));
    return Array.isArray(value)
      ? { symbol, bars, daily }
      : { symbol, name: value?.name, bars, daily };
  });
}

export type ChartLine = {
  key: string;
  label: string;
  color: string;
  points: Point[];
  subject?: boolean;
  /** % change from the common start to the last point. */
  last?: number;
};
export type CrossAssetModel = {
  /** False when the API sent no cross-asset field at all. */
  provided: boolean;
  lines: ChartLine[];
  /** Overlays with no usable data: shown as "No data" in the legend, never as flat lines. */
  missing: { key: string; label: string }[];
  /** Overlays with data that does not reach past the common start: "No overlap". */
  unaligned: { key: string; label: string }[];
  /** Overlays with only daily closes while the asset has no daily history to compare them with. */
  dailyOnly: { key: string; label: string }[];
  /**
   * On daily closes: overlays with only recent one-minute bars (a day or two),
   * which start after the asset's daily history and would cut the comparison
   * down to those days.
   */
  intradayOnly: { key: string; label: string }[];
  /** First common timestamp (seconds) that every plotted series covers. */
  since: number | null;
  /** Compared on daily closes (session dates) rather than one-minute bars. */
  daily: boolean;
  /** Points of the subject asset in the compared interval. */
  subjectPoints: number;
  /** Why nothing is plotted: the subject has no history, no overlay has any, or they do not overlap. */
  empty: "no_subject" | "no_overlays" | "no_overlap" | null;
};

/**
 * Index every series to % change from the first timestamp all of them cover,
 * so one axis serves all lines (no dual axis). A series without history at or
 * before that start is reported, not drawn from a different base.
 * Overlays that only have daily closes put the chart on daily closes when the
 * asset has stored daily bars (or no one-minute bars, then its bars per
 * session date), so the time axes overlap; otherwise the one-minute chart
 * lists them as "daily closes only". On daily closes, an overlay sent as
 * one-minute bars that starts after the asset's first session is listed as
 * "one-minute bars only" instead of shortening every line to its few days.
 */
export function crossAssetModel(asset: SeriesSource): CrossAssetModel {
  const inputs = overlayInputs(asset);
  const intraday = toPoints(asset.bars);
  const wanted: RawSeries[] = inputs
    ? inputs.filter((s) => s.symbol !== asset.symbol)
    : DEFAULT_OVERLAYS.filter((s) => s !== asset.symbol).map((symbol) => ({
        symbol,
        bars: null,
        daily: false,
      }));
  const shown = wanted.slice(0, MAX_OVERLAYS);
  const storedDaily = dailyPoints(asset.daily_bars);
  const dailyOverlays = shown.filter((s) => s.daily && usable(toPoints(s.bars)));
  const daily =
    !usable(intraday) || (dailyOverlays.length > 0 && usable(storedDaily));
  const used = new Set<string>();
  const overlays: ChartLine[] = shown.map((s) => {
    const color =
      OVERLAY_COLORS[s.symbol] ??
      SPARE_COLORS.find(
        (c) =>
          !used.has(c) && !wanted.some((w) => OVERLAY_COLORS[w.symbol] === c),
      ) ??
      SPARE_COLORS[0];
    used.add(color);
    return {
      key: s.symbol,
      label: s.name || OVERLAY_NAMES[s.symbol] || s.symbol,
      color,
      // Daily closes cannot be set against one-minute bars.
      points: daily ? dailyPoints(s.bars) : s.daily ? [] : toPoints(s.bars),
    };
  });
  const dailyOnly = overlays
    .filter((o) => !daily && dailyOverlays.some((s) => s.symbol === o.key))
    .map(({ key, label }) => ({ key, label }));
  const subject: ChartLine = {
    key: asset.symbol,
    label: asset.name || asset.symbol,
    color: SUBJECT_COLOR,
    points: !daily
      ? intraday
      : usable(storedDaily)
        ? storedDaily
        : dailyPoints(asset.bars),
    subject: true,
  };
  const subjectStart = usable(subject.points) ? subject.points[0].time : null;
  const intradayOnly = overlays
    .filter((o) => {
      const source = shown.find((s) => s.symbol === o.key);
      if (!daily || !source || source.daily || !usable(toPoints(source.bars))) return false;
      // Too few sessions to draw, or starting after the asset's first session.
      return !usable(o.points) || subjectStart === null || o.points[0].time > subjectStart;
    })
    .map(({ key, label }) => ({ key, label }));
  const listed = (key: string) =>
    dailyOnly.some((d) => d.key === key) || intradayOnly.some((d) => d.key === key);
  const candidates = [subject, ...overlays];
  const plotted = candidates.filter((c) => usable(c.points) && !listed(c.key));
  const missing = candidates
    .filter((c) => !plotted.includes(c) && !listed(c.key))
    .map(({ key, label }) => ({ key, label }));
  const unaligned: { key: string; label: string }[] = [];
  const base = {
    provided: !!inputs,
    daily,
    subjectPoints: subject.points.length,
    dailyOnly,
    intradayOnly,
    unaligned,
  };
  if (!usable(subject.points) || plotted.length < 2)
    return {
      ...base,
      lines: [],
      missing,
      since: null,
      empty: usable(subject.points) ? "no_overlays" : "no_subject",
    };
  const since = Math.max(...plotted.map((c) => c.points[0].time));
  const lines: ChartLine[] = [];
  for (const c of plotted) {
    const startIndex = c.points.findLastIndex((p) => p.time <= since);
    const start = c.points[startIndex]?.value;
    const indexed = c.points.slice(startIndex).map((p, i) => ({
      time: i === 0 ? since : p.time,
      value: (p.value / start - 1) * 100,
    }));
    if (indexed.length < 2) {
      unaligned.push({ key: c.key, label: c.label });
      continue;
    }
    lines.push({ ...c, points: indexed, last: indexed[indexed.length - 1].value });
  }
  if (lines.length < 2 || !lines.some((l) => l.subject))
    return { ...base, lines: [], missing, since: null, empty: "no_overlap" };
  return { ...base, lines, missing, since, empty: null };
}

/** What a chart must do to show `next` after `prev`. */
export type SeriesChange =
  | { kind: "none" }
  /** Replace the last point and/or append newer ones (series.update). */
  | { kind: "update"; points: Point[] }
  /** History changed (a window that slid, a corrected bar): series.setData. */
  | { kind: "reset" };

/**
 * The smallest change between two sorted series, so a poll that only moved
 * the forming bar never rebuilds the chart (the user's pan and zoom stay).
 */
export function seriesChange(prev: Point[], next: Point[]): SeriesChange {
  if (!prev.length) return next.length ? { kind: "reset" } : { kind: "none" };
  if (next.length < prev.length) return { kind: "reset" };
  const last = prev.length - 1;
  for (let i = 0; i < last; i++)
    if (prev[i].time !== next[i].time || prev[i].value !== next[i].value)
      return { kind: "reset" };
  if (next[last].time !== prev[last].time) return { kind: "reset" };
  const changed = next
    .slice(last)
    .filter((p, i) => i > 0 || p.value !== prev[last].value);
  return changed.length ? { kind: "update", points: changed } : { kind: "none" };
}
