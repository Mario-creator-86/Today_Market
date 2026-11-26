/*
 * SUPPORT / RESISTANCE rows from `technical_levels`. Pure; its runtime
 * imports are relative (vitest does not resolve "@/"), so
 * lib/market-data.test.ts can collect it.
 */
import { isNum, humanize } from "../../lib/format";
import { levelText, signedChange } from "../../lib/market-data";

export type Levels = Record<string, unknown>;
export type LevelRow = { label: string; value: string; note?: string };

export function obj(value: unknown): Levels | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Levels)
    : null;
}

type Px = (value: unknown) => string;

/** A level in the asset's unit ("5.17%" for a yield); "Not established" when absent. */
function pricer(assetClass?: string | null): Px {
  return (value) => (isNum(value) ? levelText(value, assetClass, 5) : "Not established");
}

function highLow(px: Px, label: string, value: unknown): LevelRow | null {
  const v = obj(value);
  if (!v || (!isNum(v.high) && !isNum(v.low))) return null;
  return { label, value: `${px(v.high)} / ${px(v.low)}`, note: "high / low" };
}

/** Level names in words: "sma_20" -> "SMA 20", "range_20d_high" -> "20-day range high", "poc" -> "volume point of control". */
export function basisLabel(basis: string): string {
  const sma = /^sma_(\d+)$/.exec(basis);
  if (sma) return `SMA ${sma[1]}`;
  const range = /^range_(\d+)d_(high|low)$/.exec(basis);
  if (range) return `${range[1]}-day range ${range[2]}`;
  if (basis === "vwap") return "VWAP";
  if (basis === "poc") return "volume point of control";
  return humanize(basis);
}

const HANDLED = new Set([
  "support",
  "support_basis",
  "resistance",
  "resistance_basis",
  "vwap",
  "vwap_anchor",
  "atr",
  "atr_daily",
  "previous_day",
  "previous_week",
  "session",
  "swings",
  "volume_profile",
  "options",
  "observations",
  "scope",
  "price",
  "volume_available",
  // Scope of the daily inputs, shown in the footer (not levels).
  "daily_sessions",
  "reference_session",
  "daily_basis",
]);

/** Every technical level the API computed; absent inputs mean absent levels. */
export function levelRows(
  levels: Levels,
  price: number | undefined,
  assetClass?: string | null,
): LevelRow[] {
  const px = pricer(assetClass);
  const distance = (v: unknown) =>
    isNum(v) && isNum(price) && price > 0
      ? `${signedChange({ change_pct: (v / price - 1) * 100 })?.text} from price`
      : undefined;
  const basis = (v: unknown) =>
    typeof v === "string" ? basisLabel(v) : undefined;
  const rows: (LevelRow | null)[] = [
    {
      label: "Support",
      value: px(levels.support),
      note: [basis(levels.support_basis), distance(levels.support)]
        .filter(Boolean)
        .join(" · "),
    },
    {
      label: "Resistance",
      value: px(levels.resistance),
      note: [basis(levels.resistance_basis), distance(levels.resistance)]
        .filter(Boolean)
        .join(" · "),
    },
    isNum(levels.vwap)
      ? { label: "VWAP", value: px(levels.vwap), note: distance(levels.vwap) }
      : null,
    isNum(levels.atr)
      ? { label: "ATR (intraday bars)", value: px(levels.atr) }
      : null,
    isNum(levels.atr_daily)
      ? { label: "ATR (daily)", value: px(levels.atr_daily) }
      : null,
    highLow(px, "Previous day", levels.previous_day),
    highLow(px, "Previous week", levels.previous_week),
    highLow(px, "Session", levels.session),
  ];
  for (const [key, value] of Object.entries(levels))
    if (/^range_\d+d$/.test(key))
      rows.push(highLow(px, `${key.slice(6, -1)}-day range`, value));
  for (const [key, value] of Object.entries(levels)) {
    const sma = /^sma_(\d+)$/.exec(key);
    if (sma && isNum(value))
      rows.push({
        label: `SMA ${sma[1]}`,
        value: px(value),
        note: [`${sma[1]}-day simple moving average`, distance(value)]
          .filter(Boolean)
          .join(" · "),
      });
  }
  const profile = obj(levels.volume_profile);
  if (profile && isNum(profile.poc))
    rows.push({
      label: "Volume profile",
      value: `POC ${px(profile.poc)}`,
      note: `Value area ${px(profile.value_area_low)} – ${px(profile.value_area_high)}`,
    });
  const swings = obj(levels.swings);
  const prices = (list: unknown) =>
    Array.isArray(list)
      ? list
          .map((s) => (isNum(s) ? s : obj(s)?.price))
          .filter(isNum)
          .slice(-3)
          .map((v) => px(v))
          .join(", ")
      : "";
  if (swings && prices(swings.highs))
    rows.push({ label: "Recent swing highs", value: prices(swings.highs) });
  if (swings && prices(swings.lows))
    rows.push({ label: "Recent swing lows", value: prices(swings.lows) });
  if (Array.isArray(levels.options) && levels.options.length) {
    const strikes = levels.options
      .map((o) => (isNum(o) ? o : Number(obj(o)?.strike)))
      .filter(isNum)
      .slice(0, 5);
    if (strikes.length)
      rows.push({
        label: "Option strikes",
        value: strikes.map((s) => px(s)).join(", "),
      });
  }
  for (const [key, value] of Object.entries(levels))
    if (
      !HANDLED.has(key) &&
      !/^(range_\d+d|sma_\d+)$/.test(key) &&
      isNum(value)
    )
      rows.push({ label: basisLabel(key), value: px(value) });
  return rows.filter((r): r is LevelRow => r !== null);
}

/**
 * How many daily sessions the service holds for the price chart's daily bars:
 * the levels' session count (`daily_sessions`), when the levels were computed
 * from the same stored bars (their `daily_basis`, "daily bars: Alpaca IEX",
 * names the same source as the chart's "Daily bars: Alpaca IEX"). The chart
 * shows at most the latest configured number of them. Null otherwise: a count
 * from another series is never put beside the chart.
 */
export function storedSessions(levels: unknown, dailyBasis: unknown): number | null {
  const l = obj(levels);
  const count = l?.daily_sessions;
  const basis = l?.daily_basis;
  if (!isNum(count) || count <= 0 || typeof basis !== "string" || typeof dailyBasis !== "string")
    return null;
  const same = basis.trim().toLowerCase() === dailyBasis.trim().toLowerCase();
  return same ? count : null;
}

/**
 * "Computed from 2,062 one-minute bars and 274 daily sessions (Alpaca IEX)."
 * from the counts the service sends; its `scope` text only fills in what the
 * counts do not say. A count of zero is left out.
 */
export function scopeText(levels: Levels): string | null {
  const count = (v: unknown) => (isNum(v) && v > 0 ? v.toLocaleString("en-US") : null);
  const minutes = count(levels.observations);
  const sessions = count(levels.daily_sessions);
  const source =
    typeof levels.daily_basis === "string"
      ? levels.daily_basis.replace(/^daily bars:\s*/i, "").trim()
      : "";
  const scope = typeof levels.scope === "string" ? levels.scope : "";
  const dailyFrom = /;\s*daily from (.+)$/.exec(scope)?.[1];
  const parts = [
    minutes ? `${minutes} one-minute bars` : null,
    sessions
      ? `${sessions} daily sessions${source && source !== "stored daily bars" ? ` (${source})` : ""}`
      : null,
  ].filter(Boolean);
  if (!parts.length) return scope ? `Computed from ${scope}.` : null;
  const daily = !sessions && dailyFrom ? `; daily levels from ${dailyFrom}` : "";
  return `Computed from ${parts.join(" and ")}${daily}.`;
}
