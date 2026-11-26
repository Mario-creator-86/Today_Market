"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  api,
  Asset,
  Dashboard,
  Decision,
  DriverContributions,
  isNotFound,
  number,
} from "@/lib/api";
import { currentAsset, withLiveQuotes, type DecisionTimingFields } from "@/lib/freshness";
import { analysisClock, decisionAge as analysisDecisionAge } from "@/lib/trade-watch";
import { mergeNotices, noticesFor, parseNotices } from "@/lib/market-data";
import { useClock } from "@/lib/use-clock";
import { EmptyState } from "@/components/shared/empty-state";
import { PageHeading } from "@/components/shared/page-heading";
import { Panel } from "@/components/shared/panel";
import { QueryState } from "@/components/shared/query-state";
import { StatusBadge } from "@/components/shared/status-badge";
import { EventList } from "@/components/event/event-list";
import { AssetEventRisk } from "./asset-event-risk";
import { AssetSummary } from "./asset-summary";
import { ComparableSignals } from "./comparables";
import { CrossAssetChart } from "./cross-asset-chart";
import type { SeriesSource } from "./cross-asset";
import { DataProvenance, type ProviderHealth } from "./data-provenance";
import { DecisionCard } from "./decision-card";
import { DecisionTime, PriceSinceDecision } from "./decision-time";
import { DriverBreakdown } from "./driver-breakdown";
import { KeyLevels } from "./key-levels";
import { hasPositioning, Positioning, type PositioningSnapshot } from "./positioning";
import { storedSessions } from "./levels";
import { PriceChart } from "./price-chart";
import { isNum } from "./signal";
import { isAssetSymbol } from "./symbol";

/** GET /api/assets/{symbol}; the fields beyond `Asset` are absent on older services. */
type AssetDetail = Asset &
  SeriesSource & {
    decision: (Decision & DecisionTimingFields & { drivers?: DriverContributions | null }) | null;
    positioning?: PositioningSnapshot | null;
    /** Where the daily bars come from, e.g. "Daily bars: Alpaca IEX" or "Daily closes: FRED". */
    daily_bars_basis?: string | null;
    /**
     * Notices for every provider shown on this page, including the daily
     * closes of the chart and the overlays (e.g. the FRED API notice).
     */
    notices?: unknown;
  };

const TITLE_SUFFIX = "Market Intelligence";
/**
 * The full asset record (minute and daily bars, levels, positioning, events) is
 * reloaded this often. The live quote and proxy come from the dashboard cache,
 * which the workspace keeps current (stream and polling), so the price never
 * waits for this reload.
 */
const ASSET_REFRESH_MS = 30_000;

function Conditions({ asset }: { asset: Asset }) {
  const d = asset.decision;
  const level = (v: unknown) =>
    isNum(v) ? (
      <span className="mt-1 block font-mono text-xs tabular-nums text-muted-foreground">
        Level {number(v, 5)}
        {asset.asset_class === "bond" ? "%" : ""}
      </span>
    ) : null;
  return (
    <dl className="detail-grid">
      <div>
        <dt>Confirmation</dt>
        <dd>
          {d?.confirmation || "Not established"}
          {level(d?.confirmation_level)}
        </dd>
      </div>
      <div>
        <dt>Invalidation</dt>
        <dd>
          {d?.invalidation || "Not established"}
          {level(d?.invalidation_level)}
        </dd>
      </div>
      <div>
        <dt>Data quality</dt>
        <dd>
          {d && isNum(d.data_quality)
            ? `${number(d.data_quality, 0)}/100`
            : "Unavailable"}
        </dd>
      </div>
      <div>
        <dt>Audit</dt>
        <dd>
          <StatusBadge value={d?.audit_status || "NOT RUN"} />
        </dd>
      </div>
    </dl>
  );
}

function NotFound({ symbol }: { symbol: string }) {
  return (
    <div className="rounded-lg border border-border bg-card">
      <EmptyState
        title={symbol ? `Unknown asset ${symbol}` : "Unknown asset"}
        detail="This symbol is not tracked by the data service. Tracked assets are listed on the dashboard."
      />
      <p className="pb-6 text-center text-sm">
        <Link href="/" className="text-primary underline-offset-2 hover:underline">
          Go to the dashboard
        </Link>
      </p>
    </div>
  );
}

/**
 * Asset detail page (/assets/[symbol]). Freshness is re-evaluated every
 * second; the full record is reloaded every ASSET_REFRESH_MS, the quote and
 * proxy follow the dashboard cache in between (withLiveQuotes). Nothing is
 * polled once the service says the asset does not exist (404), which shows a
 * not-found state instead.
 */
export function AssetView({ symbol }: { symbol: string }) {
  const now = useClock();
  const valid = isAssetSymbol(symbol);
  const q = useQuery({
    queryKey: ["asset", symbol],
    queryFn: () => api<AssetDetail>(`/assets/${encodeURIComponent(symbol)}`),
    enabled: valid,
    retry: (count, error) => !isNotFound(error) && count < 1,
    // A 404 is final: never poll, refocus or reconnect into it again.
    refetchInterval: (query) => (isNotFound(query.state.error) ? false : ASSET_REFRESH_MS),
    refetchOnWindowFocus: (query) => !isNotFound(query.state.error),
    refetchOnReconnect: (query) => !isNotFound(query.state.error),
  });
  // A refetch of a query without data resets it to loading with no error
  // (TanStack v5): keep the last error meanwhile, so neither the error nor the
  // not-found state flickers.
  const [lastError, setLastError] = useState<Error | null>(null);
  if (!q.data && q.error && q.error !== lastError) setLastError(q.error);
  if (q.data && lastError) setLastError(null);
  const error = q.error ?? (q.data ? null : lastError);
  const missing = !valid || isNotFound(error);
  // Provider notices (the FRED API notice), provider health, the analysis
  // timing, the event-risk block and the live quotes come with the dashboard,
  // which the shell keeps current (WebSocket snapshots and quote updates, polling).
  const dashboard = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api<Dashboard>("/dashboard"),
  });
  const live = Array.isArray(dashboard.data?.assets)
    ? dashboard.data.assets.find((x) => x?.symbol === symbol)
    : undefined;
  const a = q.data ? (currentAsset(withLiveQuotes(q.data, live), now) as AssetDetail) : undefined;
  // Until the full record arrives, the dashboard's copy of the asset (quote, proxy and published decision) shows the
  // price at once; bars, levels and events wait for the record.
  const preview = !q.data && live ? currentAsset(live, now) : undefined;
  // Live guidance takes effect above; refresh the rest of the record once per
  // newer decision. A failed request then follows the normal retry and poll
  // schedule instead of starting an immediate refetch loop.
  const liveAt = Date.parse(String(live?.decision?.created_at ?? ""));
  const pageAt = Date.parse(String(q.data?.decision?.created_at ?? ""));
  const behind = Number.isFinite(liveAt) && (!Number.isFinite(pageAt) || liveAt > pageAt);
  const refreshVersion = behind ? `${symbol}:${live?.decision?.id}:${liveAt}` : null;
  const requestedVersion = useRef<string | null>(null);
  const { refetch, isFetching } = q;
  useEffect(() => {
    if (!refreshVersion) {
      requestedVersion.current = null;
    } else if (!isFetching && requestedVersion.current !== refreshVersion) {
      requestedVersion.current = refreshVersion;
      void refetch();
    }
  }, [refreshVersion, isFetching, refetch]);
  // The dashboard cards' rule (lib/trade-watch): the service's analysis status, or a decision the last successful
  // cycle did not replace. Unknown until the dashboard answers: no STALE DECISION against a guess.
  const clock = dashboard.data ? analysisClock(dashboard.data, now) : null;
  const decisionStale =
    !!clock && analysisDecisionAge(a?.decision?.created_at, now, clock) !== null;
  const providers = (dashboard.data?.providers ?? []) as ProviderHealth[];
  // The asset's own notices cover the daily closes of its charts (e.g. a FRED
  // overlay); the dashboard's cover its quote and proxy on older services.
  const notices = mergeNotices(
    parseNotices(q.data?.notices),
    noticesFor(parseNotices(dashboard.data?.notices), a),
  );
  const name = q.data?.name;
  useEffect(() => {
    if (missing) document.title = `Asset not found · ${TITLE_SUFFIX}`;
    else if (name) document.title = `${name} (${symbol}) · ${TITLE_SUFFIX}`;
  }, [missing, name, symbol]);
  const events = [...(a?.events ?? [])].sort((x, y) =>
    String(y.event_time || y.first_seen_utc).localeCompare(
      String(x.event_time || x.first_seen_utc),
    ),
  );
  const back = (
    <Link className="back-link inline-flex min-h-6 items-center" href="/">
      Back to dashboard
    </Link>
  );
  if (missing)
    return (
      <>
        {back}
        <PageHeading
          title="Asset not found"
          description={symbol ? `No tracked asset has the symbol ${symbol}.` : "No symbol was given."}
        />
        <NotFound symbol={symbol} />
      </>
    );
  return (
    <>
      {back}
      <PageHeading
        title={(a ?? preview)?.name || symbol}
        description={`${(a ?? preview)?.symbol || symbol} / ${(a ?? preview)?.asset_class || "Market evidence"}`}
      />
      {preview && <AssetSummary asset={preview} />}
      <QueryState loading={q.isLoading && !error} error={error} />
      {a && (
        <>
          <AssetSummary asset={a} />
          <AssetEventRisk asset={a} dashboard={dashboard.data} now={now} />
          <div className="mb-6">
            <DataProvenance asset={a} notices={notices} providers={providers} />
          </div>
          <div className="dashboard-columns !mt-0">
            <Panel title="Price history">
              <PriceChart
                symbol={a.symbol}
                bars={a.bars}
                dailyBars={a.daily_bars}
                dailyBasis={a.daily_bars_basis}
                storedSessions={storedSessions(a.technical_levels, a.daily_bars_basis)}
                unit={a.asset_class === "bond" ? "%" : ""}
              />
            </Panel>
            <DecisionCard asset={a} now={now} stale={decisionStale} />
          </div>
          <div className="dashboard-columns">
            <Panel
              title={`Why is ${a.symbol} moving?`}
              action={
                a.decision ? (
                  <DecisionTime
                    decision={a.decision}
                    now={now}
                    stale={decisionStale}
                    className="justify-end text-right"
                  />
                ) : undefined
              }
            >
              <DriverBreakdown decision={a.decision} symbol={a.symbol} />
              <PriceSinceDecision
                decision={a.decision}
                price={a.quote?.price}
                assetClass={a.asset_class}
                className="px-5 pb-4"
              />
            </Panel>
            <Panel title="Support / resistance">
              <KeyLevels asset={a} />
            </Panel>
          </div>
          <div className="dashboard-columns">
            <Panel title="Confirmation & invalidation">
              <Conditions asset={a} />
            </Panel>
            <Panel title="Event timeline">
              <EventList events={events} />
            </Panel>
          </div>
          {hasPositioning(a.positioning) && (
            <div className="mt-6">
              <Positioning positioning={a.positioning} now={now} />
            </div>
          )}
          <div className="mt-6">
            <Panel title="Cross-asset chart">
              <CrossAssetChart asset={a} />
            </Panel>
          </div>
          <div className="mt-6">
            <Panel title="Historical comparable signals">
              <ComparableSignals symbol={a.symbol} />
            </Panel>
          </div>
        </>
      )}
    </>
  );
}
