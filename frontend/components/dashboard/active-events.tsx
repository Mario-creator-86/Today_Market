import type { EventObject, EventRisk } from "@/lib/api";
import { localTime } from "@/lib/api";
import {
  direction,
  events,
  exposureList,
  impactView,
  levelOf,
  levelTone,
  POTENTIAL_LABEL,
} from "@/lib/event-risk";
import { APP_ZONE } from "@/lib/market-data";
import { Badge } from "@/components/ui/badge";
import { Panel } from "@/components/shared/panel";
import { ImpactValue } from "./impact-value";

/** Possible consequences: the event's consequence map, else its exposed assets without a direction. */
function Consequences({ event }: { event: EventObject }) {
  const map = event.consequences && typeof event.consequences === "object" ? event.consequences : null;
  const items = map
    ? Object.entries(map).map(([symbol, v]) => ({ symbol, d: direction(v) }))
    : exposureList(event).map((a) => ({ symbol: a.symbol, d: null }));
  if (!items.length) return null;
  return (
    <p className="text-[11px] text-muted-foreground">
      <span className="mr-1.5">Possible consequences:</span>
      {items.map(({ symbol, d }) => (
        <span key={symbol} className="mr-2.5 inline-block text-foreground">
          {symbol}
          {d && (
            <>
              <b aria-hidden="true" className="ml-0.5">
                {d.arrow}
              </b>
              <span className="sr-only"> {d.word}</span>
            </>
          )}
        </span>
      ))}
    </p>
  );
}

/**
 * When the app first saw the event (first_seen_utc only). The event's own
 * time is not a fallback: for an unscheduled event it is the publication
 * stamp, which can be a date without a clock time (an OFAC notice dated
 * "2 Oct" would read "First seen 07:00").
 */
function seen(event: EventObject): string | null {
  const t = event.first_seen_utc;
  return t && Number.isFinite(Date.parse(t)) ? `First seen ${localTime(t)} ${APP_ZONE}` : null;
}

/**
 * §6 ACTIVE EVENT RISK (verified unscheduled events, timing ANYTIME) and
 * POSSIBLE EVENT — UNDER VERIFICATION (unverified reports, which never change
 * scores or trade statuses).
 */
export function ActiveEvents({ block }: { block: EventRisk }) {
  const active = events(block.active_events);
  const potential = events(block.potential_events);
  if (!active.length && !potential.length) return null;
  return (
    <div className="dashboard-columns">
      {active.length > 0 && (
        <Panel title="Active event risk">
          <ul className="m-0 list-none p-0">
            {active.map((e) => {
              const level = levelOf(e.relevance_level, e.relevance_score);
              const impact = impactView(e, block.mode);
              return (
                <li key={e.event_id} className="event-risk-row">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-[14px]">{e.name}</h3>
                    {level && <Badge variant={levelTone(level)}>{level}</Badge>}
                  </div>
                  <p className="event-risk-meta">
                    <span>
                      Timing <b>{(e.timing || "ANYTIME").toUpperCase()}</b>
                    </span>
                    <span>
                      Status <b>{(e.status || "UNRESOLVED").toUpperCase().replaceAll("_", " ")}</b>
                    </span>
                    {impact.text && (
                      <span>
                        Impact <ImpactValue view={impact} strong />
                      </span>
                    )}
                  </p>
                  <Consequences event={e} />
                  {seen(e) && <small className="text-muted-foreground">{seen(e)}</small>}
                </li>
              );
            })}
          </ul>
        </Panel>
      )}
      {potential.length > 0 && (
        <Panel title="Possible event — under verification">
          <ul className="m-0 list-none p-0">
            {potential.map((e) => (
              <li key={e.event_id} className="event-risk-row">
                <Badge variant="neutral">{POTENTIAL_LABEL}</Badge>
                <h3 className="mt-1.5 text-[14px]">{e.name}</h3>
                <p className="text-[11px] text-muted-foreground">
                  Possible market-moving event under verification. It does not change scores or trade statuses
                  until the fact-verification gates pass.
                </p>
                {seen(e) && <small className="text-muted-foreground">{seen(e)}</small>}
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </div>
  );
}
