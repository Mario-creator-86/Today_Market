"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, Comparables, number } from "@/lib/api";
import { asOfTime, horizonLabel } from "@/lib/market-data";
import { EmptyState } from "@/components/shared/empty-state";
import { StatusBadge } from "@/components/shared/status-badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import {
  isNum,
  signalTone,
  signed,
  signedPct,
  signTone,
  toneText,
} from "./signal";

function isComparables(value: unknown): value is Comparables {
  return (
    !!value &&
    typeof value === "object" &&
    Array.isArray((value as Comparables).items)
  );
}

const HORIZON_ORDER = [
  "1m",
  "5m",
  "15m",
  "30m",
  "1h",
  "4h",
  "1d",
  "close",
  "next_session",
];

function horizonsOf(data: Comparables): string[] {
  const set = new Set<string>(Object.keys(data.by_horizon ?? {}));
  for (const item of data.items)
    for (const h of Object.keys(item.outcomes ?? {})) set.add(h);
  return [...set].sort(
    (a, b) =>
      (HORIZON_ORDER.indexOf(a) + 1 || 99) -
      (HORIZON_ORDER.indexOf(b) + 1 || 99),
  );
}

type Item = Comparables["items"][number];

/** Narrower cells on a phone, so time, signal and one horizon fit without scrolling. */
const CELL = "min-w-[88px] px-3 sm:min-w-[100px] sm:px-[18px]";

/** A measured outcome: signed in the signal's direction, or only the asset's own move (no direction). */
function hasOutcome(item: Item): boolean {
  return Object.values(item.outcomes ?? {}).some(
    (o) => isNum(o?.return_pct) || isNum(o?.raw_return_pct),
  );
}

function Outcome({ item, horizon }: { item: Item; horizon: string }) {
  const o = item.outcomes?.[horizon];
  if (isNum(o?.return_pct))
    return <span className={toneText[signTone(o.return_pct)]}>{signedPct(o.return_pct)}</span>;
  // A signal without a direction (WAIT) has no return in a direction: the asset's own move is shown, marked as such.
  if (isNum(o?.raw_return_pct))
    return (
      <span
        className="text-muted-foreground"
        title="No direction: the asset's own move over the horizon, not a signal return"
      >
        {signedPct(o.raw_return_pct)}
        <span className="block text-[10px]">no direction</span>
      </span>
    );
  return <span className="text-muted-foreground">Not measured</span>;
}

/**
 * Historical comparable signals for this asset from
 * GET /assets/{symbol}/comparables. Hit rates count directional signals only
 * and samples below the calibration minimum are labelled insufficient; an
 * unavailable endpoint shows "Not enough data". On a phone one horizon is
 * shown at a time (picked above the table).
 */
export function ComparableSignals({ symbol }: { symbol: string }) {
  const q = useQuery({
    queryKey: ["asset-comparables", symbol],
    queryFn: () =>
      api<unknown>(`/assets/${encodeURIComponent(symbol)}/comparables`),
    retry: false,
    refetchInterval: 60000,
  });
  const [picked, setPicked] = useState<string>();
  if (q.isLoading)
    return (
      <p className="p-5 text-xs text-muted-foreground">
        Loading comparable signals…
      </p>
    );
  const data = isComparables(q.data) ? q.data : null;
  const items = data ? data.items.filter(hasOutcome).slice(0, 20) : [];
  if (!data || !items.length)
    return (
      <EmptyState
        title="Not enough data"
        detail={
          data
            ? `No past ${symbol} signals with measured outcomes yet.`
            : "Historical comparable signals are not available from the data service yet."
        }
      />
    );
  const horizons = horizonsOf(data);
  const minimum = data.minimum_observations;
  // Default: the first horizon with directional observations.
  const shown =
    picked && horizons.includes(picked)
      ? picked
      : (horizons.find((h) => (data.by_horizon?.[h]?.observations ?? 0) > 0) ??
        horizons[0]);
  return (
    <div>
      {horizons.length > 0 && (
        <dl className="grid grid-cols-2 gap-3 border-b border-border p-5 sm:grid-cols-4">
          {horizons.map((h) => {
            const s = data.by_horizon?.[h];
            const n = s?.observations ?? 0;
            const insufficient = isNum(minimum) && n < minimum;
            return (
              <div key={h} className="min-w-0">
                <dt className="text-[11px] uppercase tracking-wide text-muted-foreground">
                  {horizonLabel(h)}
                </dt>
                <dd className="m-0">
                  <span className="text-base font-medium">
                    {s && isNum(s.hit_rate)
                      ? `${number(s.hit_rate * 100, 0)}% hit`
                      : "No hit rate"}
                  </span>
                  <span className="block text-[11px] text-muted-foreground">
                    n={n}
                    {s &&
                      isNum(s.mean_return_pct) &&
                      ` · mean ${signedPct(s.mean_return_pct)}`}
                  </span>
                  {insufficient && (
                    <span className="block text-[11px] text-warning">
                      Insufficient (minimum n={minimum})
                    </span>
                  )}
                </dd>
              </div>
            );
          })}
        </dl>
      )}
      {horizons.length > 1 && (
        <label className="flex items-center gap-2 px-5 pt-4 text-xs text-muted-foreground sm:hidden">
          Return horizon
          <select
            className="rounded-sm border border-border bg-card px-2 py-1 text-foreground"
            value={shown}
            onChange={(e) => setPicked(e.target.value)}
          >
            {horizons.map((h) => (
              <option key={h} value={h}>
                {horizonLabel(h)}
              </option>
            ))}
          </select>
        </label>
      )}
      <Table
        aria-label={`Historical comparable ${symbol} signals`}
        label={`Comparable ${symbol} signals`}
      >
        <TableHeader>
          <TableRow>
            <TableHead className={CELL}>Signal time</TableHead>
            <TableHead className={CELL}>Signal</TableHead>
            <TableHead className="hidden text-right sm:table-cell">Score</TableHead>
            {horizons.map((h) => (
              <TableHead
                key={h}
                className={cn(CELL, "text-right", h !== shown && "hidden sm:table-cell")}
              >
                {horizonLabel(h)} return
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((item, i) => (
            <TableRow key={item.decision_id ?? i}>
              <TableCell className={CELL}>
                {asOfTime(item.created_at) ?? "Not recorded"}
              </TableCell>
              <TableCell className={CELL}>
                <StatusBadge
                  value={item.category || item.direction}
                  variant={signalTone(item.category || item.direction)}
                />
              </TableCell>
              <TableCell className="hidden text-right font-mono tabular-nums sm:table-cell">
                {signed(item.score, 0)}
              </TableCell>
              {horizons.map((h) => (
                <TableCell
                  key={h}
                  className={cn(
                    CELL,
                    "text-right font-mono tabular-nums",
                    h !== shown && "hidden sm:table-cell",
                  )}
                >
                  <Outcome item={item} horizon={h} />
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <p className="px-5 py-3 text-[11px] text-muted-foreground">
        Past {symbol} decisions with a measured outcome, times in Bucharest.
        Returns are signed in the signal&apos;s direction; a signal without a
        direction (WAIT) shows the asset&apos;s own move instead, marked
        &ldquo;no direction&rdquo;. Hit rates and n count directional signals
        only. Not a forecast.
      </p>
    </div>
  );
}
