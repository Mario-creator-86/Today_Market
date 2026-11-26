"use client";
import { useEffect, useMemo, useRef } from "react";
import {
  ColorType,
  createChart,
  LineSeries,
  LineStyle,
  TickMarkType,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from "lightweight-charts";
import type { Bar } from "@/lib/api";
import { asOfTime, calendarDay } from "@/lib/market-data";
import { EmptyState } from "@/components/shared/empty-state";
import {
  dailyPoints,
  priceDigits,
  seriesChange,
  sessionCountText,
  toPoints,
  type ChartLine,
  type DailyBar,
  type Point,
} from "./cross-asset";

const ZONE = "Europe/Bucharest";
// Fixed abbreviations: ICU versions disagree on "Sep" / "Sept".
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function seconds(time: Time): number {
  return typeof time === "number"
    ? time
    : typeof time === "string"
      ? Date.parse(time) / 1000
      : Date.UTC(time.year, time.month - 1, time.day) / 1000;
}

const formats = new Map<string, Intl.DateTimeFormat>();

/** Date parts in `zone`; one formatter per zone (tick marks are formatted often). */
function parts(time: Time, zone: string): Record<string, string> {
  let format = formats.get(zone);
  if (!format) {
    format = new Intl.DateTimeFormat("en-GB", {
      timeZone: zone,
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    });
    formats.set(zone, format);
  }
  return Object.fromEntries(
    format.formatToParts(new Date(seconds(time) * 1000)).map((p) => [p.type, p.value]),
  );
}

/**
 * Axis ticks and crosshair labels. One-minute bars are shown in
 * Europe/Bucharest (the chart defaults to UTC); daily closes are keyed by
 * session date at UTC midnight, so they are shown as that date, without a time.
 */
function timeOptions(daily: boolean) {
  const zone = daily ? "UTC" : ZONE;
  const day = (t: Time) => {
    const p = parts(t, zone);
    // en-GB pads a numeric day in some ICU versions ("01"); the number drops the zero.
    return `${Number(p.day)} ${MONTHS[Number(p.month) - 1]}`;
  };
  return {
    localization: {
      timeFormatter: (t: Time) => {
        if (daily)
          return (
            calendarDay(new Date(seconds(t) * 1000).toISOString().slice(0, 10)) ?? day(t)
          );
        return asOfTime(new Date(seconds(t) * 1000).toISOString()) ?? day(t);
      },
    },
    timeScale: {
      borderColor: "#2b3b52",
      timeVisible: !daily,
      tickMarkFormatter: (t: Time, type: TickMarkType) => {
        const p = parts(t, zone);
        return type === TickMarkType.Year
          ? p.year
          : type === TickMarkType.Month
            ? MONTHS[Number(p.month) - 1]
            : type === TickMarkType.DayOfMonth || daily
              ? day(t)
              : `${p.hour}:${p.minute}`;
      },
    },
  };
}

/**
 * Price labels with thousands separators ("83,478.01"), in the asset's unit
 * ("4.21%"), each with the same number of decimals (priceDigits).
 */
function priceFormatter(unit: string, digits: number) {
  return (price: number) =>
    `${price.toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    })}${unit}`;
}

/** Axis steps are whole multiples of the last shown decimal, so no two labels read the same. */
function pricePrecision(digits: number) {
  return { type: "price" as const, precision: digits, minMove: 10 ** -digits };
}

type Entry = { api: ISeriesApi<"Line">; points: Point[] };

function toData(p: Point) {
  return { time: p.time as Time, value: p.value };
}

/**
 * One chart per display mode, kept across polls: new data replaces or extends
 * each series (seriesChange), so a poll never resets the user's pan, zoom or
 * crosshair. The content is fitted once, and again only when a series appears.
 */
function useLineChart(
  lines: ChartLine[],
  options: { percent?: boolean; height?: number; daily?: boolean; unit?: string; digits?: number },
) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef(new Map<string, Entry>());
  const fitted = useRef(false);
  const { percent = false, height = 280, daily = false, unit = "", digits = 2 } = options;
  useEffect(() => {
    if (!ref.current) return;
    const created = createChart(ref.current, {
      autoSize: true,
      height,
      layout: {
        background: { type: ColorType.Solid, color: "#111e30" },
        textColor: "#98adc8",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "#1c2b40" },
        horzLines: { color: "#1c2b40" },
      },
      rightPriceScale: { borderColor: "#2b3b52" },
      ...timeOptions(daily),
    });
    chart.current = created;
    const known = series.current;
    fitted.current = false;
    return () => {
      known.clear();
      chart.current = null;
      created.remove();
    };
  }, [percent, height, daily, unit]);
  useEffect(() => {
    const current = chart.current;
    if (!current) return;
    const known = series.current;
    const keys = new Set(lines.map((l) => l.key));
    for (const [key, entry] of known)
      if (!keys.has(key)) {
        current.removeSeries(entry.api);
        known.delete(key);
      }
    let added = false;
    for (const line of lines) {
      let entry = known.get(line.key);
      if (!entry) {
        entry = {
          api: current.addSeries(LineSeries, {
            color: line.color,
            lineWidth: line.subject && percent ? 3 : 2,
            lineStyle: LineStyle.Solid,
            title: percent ? line.key : "",
            priceLineVisible: false,
            priceFormat: percent
              ? {
                  type: "custom" as const,
                  minMove: 0.01,
                  formatter: (v: number) => `${v > 0 ? "+" : ""}${v.toFixed(2)}%`,
                }
              : pricePrecision(digits),
          }),
          points: [],
        };
        known.set(line.key, entry);
        added = true;
      }
      const change = seriesChange(entry.points, line.points);
      if (change.kind === "reset") entry.api.setData(line.points.map(toData));
      else if (change.kind === "update")
        for (const p of change.points) entry.api.update(toData(p));
      entry.points = line.points;
    }
    if (lines.length && (!fitted.current || added)) {
      current.timeScale().fitContent();
      fitted.current = true;
    }
  }, [lines, percent, height, daily, unit, digits]);
  // Price labels (axis and crosshair) of an absolute chart; percent charts format through each series' own format.
  useEffect(() => {
    const current = chart.current;
    if (!current || percent) return;
    current.applyOptions({ localization: { priceFormatter: priceFormatter(unit, digits) } });
    for (const entry of series.current.values())
      entry.api.applyOptions({ priceFormat: pricePrecision(digits) });
  }, [percent, height, daily, unit, digits]);
  return ref;
}

/** "29 Sep 2025" for a UTC-midnight session date. */
function dayWithYear(time: number): string {
  const p = parts(time as Time, "UTC");
  return `${Number(p.day)} ${MONTHS[Number(p.month) - 1]} ${p.year}`;
}

/** "Fri 25 Sep" for a UTC-midnight session date (the date itself, never shifted into another zone). */
function dayText(time: number): string {
  return calendarDay(new Date(time * 1000).toISOString().slice(0, 10)) ?? dayWithYear(time);
}

/** "1-minute bars · Sat 26 Sep 11:31 – Mon 28 Sep 07:16 Bucharest (44 h)". */
function intradayCaption(points: Point[]): string {
  const first = asOfTime(new Date(points[0].time * 1000).toISOString());
  const last = asOfTime(new Date(points[points.length - 1].time * 1000).toISOString());
  const hours = Math.round((points[points.length - 1].time - points[0].time) / 3600);
  return `1-minute bars · ${first} – ${last} Bucharest${hours >= 1 ? ` (${hours} h)` : ""}`;
}

/**
 * Price history: the one-minute bars when the service has them, else the
 * stored daily closes by session date, labelled as such and with where they
 * come from (`dailyBasis`, e.g. "Daily bars: Alpaca IEX"). A close-only series
 * (open/high/low null) is drawn the same way, as a close line. Never "no price
 * history" while daily closes exist.
 */
export function PriceChart({
  symbol,
  bars,
  dailyBars,
  dailyBasis,
  storedSessions,
  unit = "",
}: {
  symbol: string;
  bars: Bar[] | null | undefined;
  dailyBars?: DailyBar[] | null;
  /** Source of the daily bars (API `daily_bars_basis`), e.g. "Daily closes: FRED". */
  dailyBasis?: string | null;
  /** Daily sessions the service holds for these bars (levels/storedSessions); the chart may show only the latest. */
  storedSessions?: number | null;
  /** Level unit, e.g. "%" for yields. */
  unit?: string;
}) {
  const intraday = useMemo(() => toPoints(bars), [bars]);
  const daily = useMemo(() => dailyPoints(dailyBars), [dailyBars]);
  const showDaily = intraday.length < 2 && daily.length > 0;
  const points = showDaily ? daily : intraday;
  const digits = useMemo(() => priceDigits(points.map((p) => p.value)), [points]);
  const lines = useMemo<ChartLine[]>(
    () => [
      {
        key: "price",
        label: "Price",
        color: "#73b2f7",
        points,
        subject: true,
      },
    ],
    [points],
  );
  if (!points.length)
    return (
      <EmptyState
        title="No price history stored"
        detail={`The service has no one-minute bars and no daily closes for ${symbol} yet.`}
      />
    );
  const basis = typeof dailyBasis === "string" ? dailyBasis.trim() : "";
  // One value is not a history: no flat line on a zoomed-in axis, the value and its date instead.
  if (points.length < 2) {
    const only = points[0];
    const value = priceFormatter(unit, digits)(only.value);
    return showDaily ? (
      <>
        <EmptyState
          title="Only one daily close stored"
          detail={`${symbol} closed at ${value} on ${dayText(only.time)}. A price history chart appears once more daily closes are stored.`}
        />
        {basis && (
          <p className="price-chart-caption px-5 pb-3 text-center text-[11px] text-muted-foreground">
            {basis}
          </p>
        )}
      </>
    ) : (
      <EmptyState
        title="Only one one-minute bar stored"
        detail={`${symbol} was at ${value} at ${asOfTime(new Date(only.time * 1000).toISOString())} Bucharest. A price history chart appears once more bars are stored.`}
      />
    );
  }
  const caption = showDaily
    ? [
        "Daily closes (session dates)",
        `${sessionCountText(daily.length, storedSessions)}, ${dayWithYear(daily[0].time)} – ${dayWithYear(daily[daily.length - 1].time)}`,
        basis || null,
      ]
        .filter(Boolean)
        .join(" · ")
    : intradayCaption(intraday);
  return (
    <>
      <MultiSeriesChart
        lines={lines}
        percent={false}
        daily={showDaily}
        unit={unit}
        digits={digits}
        label={`${symbol} price history: ${showDaily ? "daily closes by session date" : "one-minute bars, times in Bucharest"}`}
      />
      <p className="price-chart-caption px-5 py-3 text-[11px] text-muted-foreground">
        {caption}
      </p>
    </>
  );
}

/**
 * Line chart (it mounts only with data, so the chart is created with its
 * container). With `percent`, every line is already indexed to % change from
 * a common start, so a single axis serves all series. `daily` shows session
 * dates without times.
 */
export function MultiSeriesChart({
  lines,
  label,
  percent = true,
  height,
  daily = false,
  unit,
  digits,
}: {
  lines: ChartLine[];
  label: string;
  percent?: boolean;
  height?: number;
  daily?: boolean;
  /** Price unit of an absolute chart, e.g. "%" for yields. */
  unit?: string;
  /** Decimals of every price label of an absolute chart (priceDigits). */
  digits?: number;
}) {
  const ref = useLineChart(lines, { percent, height, daily, unit, digits });
  return <div ref={ref} role="img" aria-label={label} />;
}
