"use client";
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { api, Dashboard, type TradeWatchItem } from "@/lib/api";
import { eventRiskError, eventRiskOf, interpretationOf, list, livePostEvents } from "@/lib/event-risk";
import { bestSetups, currentAsset, leanSetups } from "@/lib/freshness";
import { parseNotices } from "@/lib/market-data";
import { analysisClock } from "@/lib/trade-watch";
import { useClock } from "@/lib/use-clock";
import { Button } from "@/components/ui/button";
import { DataNotices } from "@/components/shared/data-notices";
import { PageHeading } from "@/components/shared/page-heading";
import { Panel } from "@/components/shared/panel";
import { QueryState } from "@/components/shared/query-state";
import { ActiveEvents } from "./active-events";
import { AllMarkets } from "./all-markets";
import { AiInterpretation } from "./ai-interpretation";
import { AlertsPanel } from "./alerts-panel";
import { BestSetups } from "./best-setups";
import { EventRiskBanner } from "./event-risk-banner";
import { EventsPanel } from "./events-panel";
import { Positioning } from "./market-view";
import { NextEventCard } from "./next-event-card";
import { PostEventPanel } from "./post-event-panel";
import { TradeWatch } from "./trade-watch";
import { BreakingStream } from "./breaking-stream";
import { MarketBrief } from "./market-brief";
import { MarketStrip } from "./market-strip";
import { NextEvents } from "./next-events";
import { calendarUpcoming, noSetupsReason, releasedToday, upcomingEvents } from "./opening";
import { ProviderList } from "./provider-list";
import { RegimeBanner } from "./regime-banner";
import {
  breakingStream,
  DEFAULT_MIN_IMPACT,
  latestConfirmed,
  nextImportantEvents,
  parseAlerts,
  stripItems,
} from "./selectors";

/** "Coming up" and "Latest confirmed" list at most this many rows each. */
const TIMELINE_ROWS = 4;

/**
 * Dashboard (/), §27 and §58. The opening screen answers, in this order and
 * saying each fact once: what is happening (NOW: the regime, the view in two
 * sentences, the driver arrows, one warning), what is coming (EVENTS: the
 * next key event, then the timeline of the other scheduled events, the
 * events released today and the latest confirmed facts), where
 * the markets are (MARKETS: the compact strip), what it means (POSITIONING:
 * favored / under pressure / not confirming, what can change the view) and
 * what to do (SETUPS: the trade watch or setup cards, or one line when
 * nothing is actionable). Shares the ["dashboard"] query with the workspace
 * shell, which polls it and receives WebSocket snapshots.
 *
 * EVENT RISK MODE and a MARKET DIVERGENCE reaction stay above the fold when
 * active. Everything else (scenarios, the interpretation's summary, active
 * and unverified events, the news stream, alerts, provider health, the brief
 * and every tracked market) is in the collapsed sections below. Without
 * `event_risk` (an older backend) the event card and positioning are absent
 * and "Coming up" reads the calendar; when the backend reports that the event
 * layer failed (`event_risk_error`), a quiet notice says so and the rest
 * renders.
 *
 * Until the first snapshot arrives (loading, or the service failing with
 * nothing cached) only neutral placeholders are shown: no section may claim
 * "no setups", "no alerts" or "no events" before there is data to say so.
 */
export function DashboardView() {
  const now = useClock();
  const q = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api<Dashboard>("/dashboard"),
  });
  const d = q.data;
  // Only a click spins the button: the shell's background polls of the same query leave it alone.
  const [refreshing, setRefreshing] = useState(false);
  const refresh = () => {
    setRefreshing(true);
    q.refetch().finally(() => setRefreshing(false));
  };
  const assets = (d?.assets ?? []).map((asset) =>
    currentAsset(asset, now, d?.freshness_threshold_seconds),
  );
  // The configured threshold when the backend sends one; the fallback filters but is never stated as configured.
  const configuredMinImpact =
    typeof d?.calendar_min_impact === "number" &&
    Number.isFinite(d.calendar_min_impact)
      ? d.calendar_min_impact
      : null;
  const events = d?.events;
  const latest = useMemo(() => latestConfirmed(events ?? [], TIMELINE_ROWS), [events]);
  const stream = useMemo(() => breakingStream(events ?? []), [events]);
  const rawAlerts = d?.alerts;
  const alerts = useMemo(() => parseAlerts(rawAlerts), [rawAlerts]);
  const rawProviders = d?.providers;
  const providers = useMemo(() => rawProviders ?? [], [rawProviders]);
  const notices = parseNotices(d?.notices);
  const risk = eventRiskOf(d);
  const riskError = risk ? null : eventRiskError(d);
  const interpretation = interpretationOf(d);
  const divergentReaction = livePostEvents(risk).some((entry) => entry.divergence);
  const watch = list<TradeWatchItem>(risk?.trade_watch).filter(
    (t) => t && typeof t.asset === "string" && !!t.asset,
  );
  const setups = bestSetups(assets);
  const upcoming = risk
    ? upcomingEvents(risk, now, TIMELINE_ROWS)
    : calendarUpcoming(
        nextImportantEvents(d?.calendar ?? [], now, configuredMinImpact ?? DEFAULT_MIN_IMPACT, TIMELINE_ROWS),
        now,
      );
  // The header's analysis verdict, so the cards flag old decisions exactly when the header does.
  const clock = analysisClock(d, now);
  const heading = (
    <PageHeading
      title="Today's market"
      description="The facts, the reaction, and what needs to happen next."
      action={
        <Button onClick={refresh} disabled={refreshing} aria-busy={refreshing || undefined}>
          <RefreshCw
            size={15}
            className={refreshing ? "spin" : ""}
            aria-hidden="true"
          />
          Refresh
        </Button>
      }
    />
  );
  if (!d)
    return (
      <>
        {heading}
        <QueryState loading={q.isLoading} error={q.error} />
        <DashboardPlaceholder failed={!q.isLoading && !!q.error} />
      </>
    );
  return (
    <>
      {heading}
      <QueryState loading={q.isLoading} error={q.error} />
      <RegimeBanner dashboard={d} interpretation={interpretation} risk={risk} now={now} />
      {riskError !== null && (
        <p className="event-risk-unavailable" role="status" aria-label="Event risk unavailable">
          Event risk is unavailable right now, so event statuses and countdowns are not shown. Published signals
          and the rest of the dashboard are unaffected.
          {riskError && <span className="block">Reason: {riskError}</span>}
        </p>
      )}
      {risk && <EventRiskBanner block={risk} now={now} />}
      <div className={risk ? "event-grid" : "dashboard-primary-news"}>
        {risk && <NextEventCard block={risk} interpretation={interpretation} now={now} />}
        <EventsPanel
          upcoming={upcoming}
          latest={latest}
          released={releasedToday(risk)}
          keyEventShown={!!risk?.next_event}
        />
      </div>
      {risk && divergentReaction && <PostEventPanel block={risk} now={now} />}
      <MarketStrip items={stripItems(assets, d.market_strip)} />
      {risk && <Positioning block={risk} now={now} />}
      {risk && watch.length > 0 && (
        <TradeWatch
          block={risk}
          items={watch}
          assets={assets}
          setups={setups}
          interpretation={interpretation}
          clock={clock}
          now={now}
        />
      )}
      {!(risk && watch.length > 0) && (
        <BestSetups
          candidates={setups}
          leans={leanSetups(assets)}
          now={now}
          clock={clock}
          waitReason={noSetupsReason(risk, now)}
        />
      )}
      <details className="dashboard-details" id="market-context">
        <summary>{risk || interpretation ? "More context and scenarios" : "Next important events"}</summary>
        <div className="dashboard-details-body">
          {(risk || interpretation) && (
            <AiInterpretation block={risk} interpretation={interpretation} now={now} />
          )}
          <Panel title="Next important events" link="/calendar">
            <NextEvents
              items={nextImportantEvents(d.calendar ?? [], now, configuredMinImpact ?? DEFAULT_MIN_IMPACT)}
              minImpact={configuredMinImpact}
            />
          </Panel>
        </div>
      </details>
      {risk && (
        <details className="dashboard-details">
          <summary>Active events and measured reactions</summary>
          <div className="dashboard-details-body">
            {!divergentReaction && <PostEventPanel block={risk} now={now} />}
            <ActiveEvents block={risk} />
          </div>
        </details>
      )}
      <details className="dashboard-details">
        <summary>Verified news and alerts</summary>
        <div className="dashboard-columns dashboard-details-body">
          <Panel title="Breaking / verified" link="/sources" linkLabel="Verification">
            <BreakingStream events={stream} />
          </Panel>
          <AlertsPanel alerts={alerts} providers={providers} />
        </div>
      </details>
      <details className="dashboard-details">
        <summary>Provider health and market brief</summary>
        <div className="dashboard-columns dashboard-details-body">
          <Panel title="Provider health" link="/providers">
            <ProviderList items={providers} compact />
          </Panel>
          <MarketBrief />
        </div>
      </details>
      <details className="dashboard-details">
        <summary>All tracked markets</summary>
        <div className="dashboard-details-body">
          <AllMarkets assets={assets} />
        </div>
      </details>
      {notices.length > 0 && (
        <div className="dashboard-notices mt-6 border-t border-border pt-3">
          <DataNotices notices={notices} />
        </div>
      )}
    </>
  );
}

/**
 * Shown instead of the sections while there is no snapshot: grey blocks while
 * loading, a plain note after a failure. Never an empty state that would read
 * as a finding ("No actionable setups", "No alerts").
 */
function DashboardPlaceholder({ failed }: { failed: boolean }) {
  if (failed)
    return (
      <p className="mt-5 rounded-lg border border-border px-[21px] py-4 text-sm text-muted-foreground">
        Market sections appear once the data service answers. The page keeps retrying in the background.
      </p>
    );
  const block = "rounded-lg border border-border bg-card motion-safe:animate-pulse";
  return (
    <div className="grid gap-5" aria-hidden="true" data-testid="dashboard-placeholder">
      <div className={`${block} h-[150px]`} />
      <div className={`${block} h-[110px]`} />
      <div className="dashboard-columns mt-0!">
        <div className={`${block} h-[260px]`} />
        <div className={`${block} h-[260px]`} />
      </div>
    </div>
  );
}
