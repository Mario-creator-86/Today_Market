"use client";
import { useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, localTime } from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { NativeSelect } from "@/components/ui/native-select";
import { EmptyState } from "@/components/shared/empty-state";
import { PageHeading } from "@/components/shared/page-heading";
import { Panel } from "@/components/shared/panel";
import { QueryState } from "@/components/shared/query-state";
import { StatusBadge } from "@/components/shared/status-badge";
import {
  counterfactualNotice,
  dimensionLabel,
  groupLabel,
  hitRates,
  horizonLabel,
  outcomeMix,
  outcomeMixText,
  performanceSummary,
  readableBasis,
  shortList,
  splitMeasured,
  type PerformanceSummary,
} from "./page-logic";
import { Column, DataTable, fmt, fmtRatio, isNum, Stat, tone } from "./page-utils";
import type { MetricSummary, PaperMetrics, PaperTrade, PerformanceReport } from "./types";

const POPULATIONS = ["all", "actionable", "counterfactual"] as const;
const NED = "Not enough data";

const pct = (v: unknown) => fmt(v, { suffix: "%", signed: true, missing: NED });
const num = (v: unknown, digits = 3) => fmt(v, { digits, missing: NED });

/** Shown as the signal hit rate and false-positive rate of a row without a published signal: both exist only for published signals. */
const NO_SIGNAL = "No signal";
const WORDS = new Set([NED, NO_SIGNAL]);

/** A metric cell: a missing value may wrap (it is words, not a number) so wide metric tables stay narrow. */
function metric(value: string): React.ReactNode {
  return WORDS.has(value) ? (
    <span
      className="whitespace-normal break-normal text-muted-foreground"
      title={value === NO_SIGNAL ? "No published signal has a measured outcome in this row" : undefined}
    >
      {value}
    </span>
  ) : (
    value
  );
}

/**
 * The hit rate of published signals: "No published signal" on a row whose
 * outcomes are all counterfactual (their rate has its own column), and tagged
 * when an older service's single rate also counts counterfactual outcomes.
 */
function SignalHitRate({ summary, population }: { summary: MetricSummary; population: string }) {
  const rates = hitRates(summary, population);
  if (rates.signal === null)
    return metric(outcomeMix(summary, population).basis === "counterfactual" ? NO_SIGNAL : NED);
  return (
    <>
      {fmtRatio(rates.signal)}
      {rates.signalIncludesCounterfactual && (
        <small className="block text-[10px] font-normal text-muted-foreground">incl. counterfactual</small>
      )}
    </>
  );
}

/**
 * Metric columns shared by the horizon and breakdown tables. Each row says how
 * many of its measured outcomes are published signals and how many are counterfactual
 * (WAIT and lean decisions measured on their score sign), and gives the two
 * hit rates apart; the false-positive rate exists only for published signals.
 */
function metricColumns<T extends { summary: MetricSummary }>(first: Column<T>, population: string): Column<T>[] {
  const m = (row: T) => row.summary;
  return [
    first,
    {
      key: "n",
      label: "Outcomes",
      numeric: true,
      render: (r) => {
        const mix = outcomeMix(m(r), population);
        return (
          <>
            {fmt(m(r).observations, { digits: 0, missing: "0" })}
            {outcomeMixText(mix) !== null && (
              <small className="block text-[10px] font-normal whitespace-nowrap text-muted-foreground" data-testid="row-mix">
                {(mix.published ?? 0).toLocaleString("en-US")} published · {(mix.counterfactual ?? 0).toLocaleString("en-US")} counterfactual
              </small>
            )}
          </>
        );
      },
    },
    { key: "hit", label: "Signal hit rate", numeric: true, render: (r) => <SignalHitRate summary={m(r)} population={population} /> },
    {
      key: "cfhit",
      label: "Counterfactual hit rate",
      numeric: true,
      render: (r) => metric(fmtRatio(hitRates(m(r), population).counterfactual)),
    },
    { key: "mean", label: "Avg return", numeric: true, render: (r) => metric(pct(m(r).mean_return_pct)) },
    { key: "median", label: "Median return", numeric: true, render: (r) => metric(pct(m(r).median_return_pct)) },
    { key: "mfe", label: "Avg MFE", numeric: true, render: (r) => metric(pct(m(r).mean_mfe_pct)) },
    { key: "mae", label: "Avg MAE", numeric: true, render: (r) => metric(pct(m(r).mean_mae_pct)) },
    { key: "dd", label: "Max drawdown (pts)", numeric: true, render: (r) => metric(num(m(r).max_drawdown_return_points, 2)) },
    { key: "brier", label: "Brier score", numeric: true, render: (r) => metric(num(m(r).brier_score)) },
    { key: "cal", label: "Calibration error", numeric: true, render: (r) => metric(num(m(r).calibration_error)) },
    {
      key: "fp",
      label: "False-positive rate",
      numeric: true,
      render: (r) =>
        metric(
          outcomeMix(m(r), population).basis === "counterfactual" ? NO_SIGNAL : fmtRatio(m(r).false_positive_rate),
        ),
    },
  ];
}

type HorizonRow = { horizon: string; summary: MetricSummary };
type GroupRow = { group: string; label: string; summary: MetricSummary };

const HORIZON_FIRST: Column<HorizonRow> = {
  key: "horizon",
  label: "Horizon",
  render: (r) => <strong>{horizonLabel(r.horizon)}</strong>,
};
const GROUP_FIRST: Column<GroupRow> = {
  key: "group",
  label: "Group",
  render: (r) => <strong>{r.label}</strong>,
};

function side(t: PaperTrade): string {
  if (t.direction) return t.direction;
  return t.side === 1 ? "LONG" : t.side === -1 ? "SHORT" : "Unavailable";
}

const price = (v: unknown) => fmt(v, { digits: 5 });

const TRADE_COLUMNS: Column<PaperTrade>[] = [
  { key: "symbol", label: "Asset", render: (t) => <strong>{t.symbol}</strong> },
  { key: "side", label: "Side", render: (t) => <StatusBadge value={side(t)} variant={tone(side(t))} /> },
  { key: "status", label: "Status", render: (t) => <StatusBadge value={t.status} variant={tone(t.status)} /> },
  { key: "opened", label: "Opened", render: (t) => localTime(t.opened_at) },
  { key: "entry", label: "Entry", numeric: true, render: (t) => price(t.entry_price) },
  { key: "stop", label: "Stop", numeric: true, render: (t) => fmt(t.stop, { digits: 5, missing: "None set" }) },
  { key: "target", label: "Target", numeric: true, render: (t) => fmt(t.target, { digits: 5, missing: "None set" }) },
  {
    key: "exit",
    label: "Exit",
    numeric: true,
    render: (t) => fmt(t.exit_price, { digits: 5, missing: t.status === "OPEN" ? "Open" : "Unavailable" }),
  },
  {
    key: "reason",
    label: "Exit reason",
    render: (t) =>
      t.exit_reason ? <StatusBadge value={t.exit_reason} variant={tone(t.exit_reason)} /> : t.status === "OPEN" ? "Open" : "Unavailable",
  },
  { key: "closed", label: "Closed", render: (t) => (t.closed_at ? localTime(t.closed_at) : "Open") },
  {
    key: "return",
    label: "Return (net)",
    numeric: true,
    render: (t) => fmt(t.return_pct, { suffix: "%", signed: true, missing: t.status === "OPEN" ? "Open" : "Unavailable" }),
  },
  { key: "mfe", label: "MFE", numeric: true, render: (t) => fmt(t.mfe_pct, { suffix: "%", signed: true }) },
  { key: "mae", label: "MAE", numeric: true, render: (t) => fmt(t.mae_pct, { suffix: "%", signed: true }) },
];

function PaperSummary({ paper }: { paper: PaperMetrics | undefined }) {
  if (!paper) return <EmptyState title="Paper metrics unavailable" />;
  return (
    <div className="p-[18px]">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat
          label="Closed trades"
          value={fmt(paper.closed_trades, { digits: 0, missing: "0" })}
          detail={`${fmt(paper.open_trades, { digits: 0, missing: "0" })} open`}
        />
        <Stat label="Hit rate" value={fmtRatio(paper.hit_rate)} />
        <Stat label="Profit factor" value={num(paper.profit_factor, 2)} />
        <Stat
          label="Max drawdown"
          value={
            isNum(paper.max_drawdown_equity_pct)
              ? `${fmt(paper.max_drawdown_equity_pct)}% equity`
              : num(paper.max_drawdown_return_points, 2)
          }
          detail={isNum(paper.total_pnl) ? `Virtual P&L ${fmt(paper.total_pnl, { signed: true })}` : undefined}
        />
        <Stat label="Avg return" value={pct(paper.mean_return_pct)} />
        <Stat label="Median return" value={pct(paper.median_return_pct)} />
        <Stat label="Avg MFE" value={pct(paper.mean_mfe_pct)} />
        <Stat label="Avg MAE" value={pct(paper.mean_mae_pct)} />
      </div>
      {paper.basis && (
        <p className="mt-3 text-xs text-muted-foreground">
          {readableBasis(paper.basis)}. Paper evaluation only, never real trading.
        </p>
      )}
    </div>
  );
}

/**
 * Headline counts: decisions recorded apart from the directional outcomes
 * actually measured, and of those how many are published signals and how many
 * counterfactual (WAIT and lean decisions measured on their score sign).
 */
function Overview({ summary, probability }: { summary: PerformanceSummary; probability: unknown }) {
  const mix = outcomeMixText(summary.mix);
  return (
    <div className="performance-overview">
      <div>
        <span>Decisions recorded</span>
        <strong>{fmt(summary.decisions, { digits: 0 })}</strong>
      </div>
      <div>
        <span>Directional outcomes measured</span>
        <strong>{fmt(summary.directional, { digits: 0 })}</strong>
        {summary.directionalAtBestHorizon && (
          <small className="block text-[11px] text-muted-foreground">most at any one horizon</small>
        )}
      </div>
      {mix && summary.mix.basis !== "unknown" && (
        <div data-testid="outcome-mix">
          <span>Published signals / counterfactual</span>
          <strong>{mix}</strong>
          <small className="block text-[11px] text-muted-foreground">
            {summary.mixHorizon
              ? `outcomes at ${horizonLabel(summary.mixHorizon).toLowerCase()}, the horizon with the most`
              : "decisions with a measured outcome"}
          </small>
        </div>
      )}
      <div>
        <span>Historical probability</span>
        <strong>{isNum(probability) ? `${fmt(probability)}%` : "Not calibrated"}</strong>
      </div>
      <StatusBadge value={summary.status} />
    </div>
  );
}

/** Historical performance (/performance, §36/§39). */
export function PerformancePage() {
  const [population, setPopulation] = useState<string>("all");
  const [horizon, setHorizon] = useState<string>("");
  const [dimension, setDimension] = useState<string>("");
  // The configured report horizon, remembered from a default request: breakdown_horizon echoes a chosen horizon.
  const [defaultHorizon, setDefaultHorizon] = useState<string | null>(null);
  const params = new URLSearchParams();
  if (population !== "all") params.set("population", population);
  if (horizon) params.set("horizon", horizon);
  const qs = params.toString();
  const q = useQuery({
    queryKey: ["performance", population, horizon],
    queryFn: () => api<PerformanceReport>(`/performance${qs ? `?${qs}` : ""}`),
    placeholderData: keepPreviousData,
  });
  const data = q.data;
  const reportedDefault = data?.default_horizon ?? (!horizon && !q.isPlaceholderData ? data?.breakdown_horizon : undefined);
  if (reportedDefault && reportedDefault !== defaultHorizon) setDefaultHorizon(reportedDefault);
  const metrics = data?.metrics ?? {};
  const horizons = Array.isArray(data?.horizons) ? data.horizons : Object.keys(metrics).filter((k) => k !== "paper");
  const horizonSplit = splitMeasured(
    horizons.map((h): HorizonRow => ({ horizon: h, summary: (metrics[h] as MetricSummary) ?? {} })),
    (r) => horizonLabel(r.horizon),
  );
  const breakdowns = data?.breakdowns ?? {};
  const dimensions = Object.keys(breakdowns);
  const activeDim = dimensions.includes(dimension) ? dimension : dimensions[0] ?? "";
  const groupSplit = splitMeasured(
    Object.entries(breakdowns[activeDim] ?? {}).map(
      ([group, summary]): GroupRow => ({ group, label: groupLabel(activeDim, group), summary }),
    ),
    (r) => r.label,
  );
  const trades = [...(Array.isArray(data?.paper_trades) ? data.paper_trades : [])].sort((a, b) =>
    String(b.opened_at).localeCompare(String(a.opened_at)),
  );
  const breakdownHorizon = data?.breakdown_horizon ? horizonLabel(data.breakdown_horizon) : null;
  // The population the figures reflect (the service echoes it), so each row is labelled for what it is.
  const shown = data?.population ?? population;
  const summary = data ? performanceSummary(data) : null;
  const notice = summary ? counterfactualNotice(summary.mix, shown) : null;
  const horizonColumns = useMemo(() => metricColumns(HORIZON_FIRST, shown), [shown]);
  const groupColumns = useMemo(() => metricColumns(GROUP_FIRST, shown), [shown]);
  const ignored =
    data &&
    !q.isPlaceholderData &&
    ((population !== "all" && data.population && data.population !== population) ||
      (horizon && data.breakdown_horizon && data.breakdown_horizon !== horizon));
  return (
    <>
      <PageHeading
        title="Historical performance"
        description="Observed outcomes and calibrated evidence across recorded decisions."
      />
      <QueryState loading={q.isLoading} error={q.error} onRetry={() => void q.refetch()} />
      {data && summary && (
        <>
          <Overview summary={summary} probability={data.probability} />
          <div className="toolbar flex-wrap">
            <NativeSelect aria-label="Population" value={population} onChange={(e) => setPopulation(e.target.value)}>
              {POPULATIONS.map((p) => (
                <option key={p} value={p}>
                  {p === "all" ? "All decisions" : p === "actionable" ? "Actionable only" : "Counterfactual only"}
                </option>
              ))}
            </NativeSelect>
            <NativeSelect aria-label="Breakdown horizon" value={horizon} onChange={(e) => setHorizon(e.target.value)}>
              <option value="">Default horizon{defaultHorizon ? ` (${horizonLabel(defaultHorizon)})` : ""}</option>
              {horizons.map((h) => (
                <option key={h} value={h}>
                  {horizonLabel(h)}
                </option>
              ))}
            </NativeSelect>
            <NativeSelect
              aria-label="Breakdown dimension"
              value={activeDim}
              onChange={(e) => setDimension(e.target.value)}
              disabled={!dimensions.length}
            >
              {dimensions.length ? (
                dimensions.map((d) => (
                  <option key={d} value={d}>
                    By {dimensionLabel(d)}
                  </option>
                ))
              ) : (
                <option value="">No breakdowns</option>
              )}
            </NativeSelect>
          </div>
          {ignored && (
            <Alert variant="warning">
              The backend returned a different population or horizon than requested; figures below reflect{" "}
              {data.population ?? "all"} decisions at {breakdownHorizon ?? "the default horizon"}.
            </Alert>
          )}
          {notice && (
            <p className="performance-notice" role="note" aria-label="What these figures measure">
              {notice}
            </p>
          )}
          <div className="flex flex-col gap-5">
            <Panel title="Metrics by horizon">
              <DataTable
                rows={horizonSplit.measured}
                columns={horizonColumns}
                rowKey={(r) => r.horizon}
                caption="Outcome metrics by horizon"
                dense
                empty={<EmptyState title={NED} detail="Metrics appear once decisions have measured directional outcomes." />}
              />
              {horizonSplit.measured.length > 0 && horizonSplit.unmeasured.length > 0 && (
                <p className="border-t border-border px-[18px] py-3 text-xs text-muted-foreground">
                  {NED} yet for: {horizonSplit.unmeasured.join(", ")}.
                </p>
              )}
            </Panel>
            <Panel
              title={`Breakdown${activeDim ? ` by ${dimensionLabel(activeDim)}` : ""}${breakdownHorizon ? ` · ${breakdownHorizon}` : ""}`}
            >
              <DataTable
                rows={groupSplit.measured}
                columns={groupColumns}
                rowKey={(r) => r.group}
                caption="Breakdown metrics"
                dense
                empty={
                  <EmptyState
                    title={NED}
                    detail={
                      groupSplit.unmeasured.length
                        ? `No group has a measured directional outcome yet (${groupSplit.unmeasured.length} recorded without one).`
                        : "No outcomes recorded for this breakdown yet."
                    }
                  />
                }
              />
              {groupSplit.measured.length > 0 && groupSplit.unmeasured.length > 0 && (
                <p className="border-t border-border px-[18px] py-3 text-xs text-muted-foreground">
                  {NED} yet for: {shortList(groupSplit.unmeasured)}.
                </p>
              )}
            </Panel>
            <Panel title="Paper trading">
              <PaperSummary paper={metrics.paper as PaperMetrics | undefined} />
            </Panel>
            <Panel title="Paper trades">
              <DataTable
                rows={trades}
                columns={TRADE_COLUMNS}
                rowKey={(t, i) => t.id ?? t.decision_id ?? String(i)}
                caption="Paper trades"
                dense
                empty={<EmptyState title="No paper trades yet" detail="Qualifying audited setups open virtual positions." />}
              />
            </Panel>
          </div>
        </>
      )}
    </>
  );
}
