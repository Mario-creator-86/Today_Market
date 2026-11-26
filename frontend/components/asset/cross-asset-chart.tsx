"use client";
import { useMemo } from "react";
import type { Asset } from "@/lib/api";
import { calendarDay, zonedTime } from "@/lib/market-data";
import { EmptyState } from "@/components/shared/empty-state";
import { cn } from "@/lib/utils";
import { crossAssetModel, type SeriesSource } from "./cross-asset";
import { MultiSeriesChart } from "./price-chart";
import { signedPct, signTone, toneText } from "./signal";

/** "Fri 25 Sep" for a daily start (UTC midnight of the session date), "Fri 25 Sep 14:35 Bucharest" otherwise. */
function startText(since: number | null, daily: boolean): string {
  if (since === null) return "Not recorded";
  const iso = new Date(since * 1000).toISOString();
  return (daily ? calendarDay(iso.slice(0, 10)) : zonedTime(iso)) ?? "Not recorded";
}

function joinNames(names: string[]): string {
  return names.length <= 1
    ? names.join("")
    : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/**
 * Cross-asset overlay: the asset against DXY, US10Y and Nasdaq (or the
 * configured overlays), each indexed to % change from the first time they all
 * cover. Overlays without data are listed as "No data", never drawn flat.
 * When an overlay only has daily closes and the asset has daily history, the
 * comparison is on daily closes (session dates).
 */
export function CrossAssetChart({ asset }: { asset: Asset & SeriesSource }) {
  const { symbol, name, bars, cross_asset, daily_bars, cross_asset_interval } = asset;
  // Only the series inputs matter; the quote refreshes every second.
  const model = useMemo(
    () =>
      crossAssetModel({ symbol, name, bars, cross_asset, daily_bars, cross_asset_interval }),
    [symbol, name, bars, cross_asset, daily_bars, cross_asset_interval],
  );
  const unit = model.daily ? "daily close" : "one-minute bar";
  const legend = (
    <ul
      className="flex flex-wrap gap-x-5 gap-y-2 px-5 py-3 text-xs"
      aria-label="Chart legend"
    >
      {model.lines.map((l) => (
        <li key={l.key} className="flex items-center gap-2">
          <span
            aria-hidden="true"
            className={cn(
              "inline-block rounded-full",
              l.subject ? "h-[3px] w-4" : "h-[2px] w-4",
            )}
            style={{ background: l.color }}
          />
          <span className="font-medium text-foreground">{l.key}</span>
          <span
            className={cn("font-mono tabular-nums", toneText[signTone(l.last)])}
          >
            {signedPct(l.last)}
          </span>
        </li>
      ))}
      {!model.lines.length && model.empty !== "no_subject" && model.subjectPoints > 0 && (
        <li className="flex items-center gap-2 text-muted-foreground">
          <span className="font-medium text-foreground">{symbol}</span>
          <span>
            {model.subjectPoints.toLocaleString("en-US")} {unit}
            {model.subjectPoints === 1 ? "" : "s"}
          </span>
        </li>
      )}
      {[
        ...model.missing.map((m) => ({ ...m, note: "No data" })),
        ...model.dailyOnly.map((m) => ({ ...m, note: "Daily closes only" })),
        ...model.intradayOnly.map((m) => ({ ...m, note: "One-minute bars only" })),
        ...model.unaligned.map((m) => ({ ...m, note: "No overlap" })),
      ].map((m) => (
        <li
          key={m.key}
          className="flex items-center gap-2 text-muted-foreground"
        >
          <span
            aria-hidden="true"
            className="inline-block h-[2px] w-4 border-t border-dashed border-muted-foreground"
          />
          <span className="font-medium">{m.key}</span>
          <span>{m.note}</span>
        </li>
      ))}
    </ul>
  );
  const listed =
    model.missing.length +
      model.dailyOnly.length +
      model.intradayOnly.length +
      model.unaligned.length >
    0;
  if (!model.lines.length) {
    const overlays = model.missing.filter((m) => m.key !== symbol).map((m) => m.key);
    const daily = model.dailyOnly.map((m) => m.key);
    const intraday = model.intradayOnly.map((m) => m.key);
    const detail = !model.provided
      ? "The data service does not provide DXY, US10Y and Nasdaq history for this view yet."
      : model.empty === "no_subject"
        ? `No stored price history for ${symbol} to compare yet.`
        : model.empty === "no_overlap"
          ? `The stored history of the overlays does not overlap with ${symbol}'s.`
          : [
              daily.length
                ? `${joinNames(daily)} ${daily.length === 1 ? "has" : "have"} daily closes only, and ${symbol} has no stored daily history to compare ${daily.length === 1 ? "it" : "them"} with.`
                : null,
              intraday.length
                ? `${joinNames(intraday)} ${intraday.length === 1 ? "has" : "have"} only recent one-minute bars, too short to compare with ${symbol}'s daily closes.`
                : null,
              overlays.length
                ? `No stored history for ${joinNames(overlays)} yet; each needs at least two closes.`
                : null,
            ]
              .filter(Boolean)
              .join(" ") || "No overlay series are configured for this asset.";
    return (
      <>
        <EmptyState title="Cross-asset history unavailable" detail={detail} />
        {(listed || model.subjectPoints > 0) && legend}
      </>
    );
  }
  const since = startText(model.since, model.daily);
  return (
    <>
      <MultiSeriesChart
        lines={model.lines}
        daily={model.daily}
        label={`${symbol} compared with ${model.lines
          .filter((l) => !l.subject)
          .map((l) => l.key)
          .join(", ")}, as % change since ${since}`}
      />
      {legend}
      <p className="px-5 pb-3 text-[11px] text-muted-foreground">
        {model.daily
          ? `% change of daily closes (session dates) since ${since}, the first session all plotted series have.`
          : `% change since ${since}, the first time all plotted series have data.`}
      </p>
    </>
  );
}
