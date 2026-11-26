import { TriangleAlert } from "lucide-react";
import type { EventRisk } from "@/lib/api";
import {
  eventDataStale,
  findEvent,
  impactView,
  list,
  modeRelevance,
  modeTarget,
} from "@/lib/event-risk";
import { ANYTIME_LABEL, displayPhase, eventTimeLabel, isAnytime, LIVE_PHASES } from "@/lib/event-time";
import { APP_ZONE, zonedTime } from "@/lib/market-data";
import { cn } from "@/lib/utils";
import { EventCountdown } from "@/components/shared/event-countdown";
import { ImpactValue } from "./impact-value";

const URGENT_MS = 15 * 60_000;

/**
 * §6 EVENT RISK MODE: shown only while the backend says it is active. Event,
 * Bucharest time, a live countdown (or the live phase), the calendar impact
 * out of 10 (the number the calendar shows; relevance / 10 only when there is
 * none, labelled so), the relevance with its level, and the affected groups.
 * Published signals are not removed; statuses carry it.
 */
export function EventRiskBanner({ block, now }: { block: EventRisk; now: number }) {
  const mode = block.mode;
  if (!mode?.active) return null;
  const event = findEvent(block, mode.event_id);
  const target = modeTarget(block);
  const timed = target !== null ? { time_utc: new Date(target).toISOString(), time_known: true } : null;
  // The mode's own phase (§8) first, else the event's; the local clock never advances it.
  const phaseSource = event ?? (mode.phase ? { time_utc: timed?.time_utc, time_known: true } : null);
  const phase = phaseSource ? displayPhase({ ...phaseSource, phase: mode.phase ?? event?.phase }, now) : null;
  const live = !!phase && LIVE_PHASES.has(phase.phase);
  const name = mode.name || event?.name || "Critical event";
  // A verified unscheduled event (§3) has no clock: timing ANYTIME, status until resolved.
  const anytime = isAnytime(event);
  const time = anytime
    ? ANYTIME_LABEL
    : event
      ? eventTimeLabel(event, now)
      : timed
        ? eventTimeLabel(timed, now)
        : mode.time_bucharest
          ? `${mode.time_bucharest} ${APP_ZONE}`
          : "Time unavailable";
  // C10: the calendar impact (the listed event's or the mode's own; relevance / 10 only without one), and the
  // mode's relevance, else the listed event's, so neither fact disappears when the block does not list the event.
  const impact = impactView(event, mode);
  const { text: relevance, level } = modeRelevance(mode, event);
  const groups = list<string>(mode.affected_groups).filter((g) => typeof g === "string");
  const urgent = anytime || live || (target !== null && target - now <= URGENT_MS);
  const stale = eventDataStale(block, now);
  return (
    <section
      aria-label="Event risk mode"
      className={cn("event-risk-banner", urgent && "urgent")}
      data-urgent={urgent ? "true" : undefined}
    >
      <div className="event-risk-kicker">
        <TriangleAlert size={16} aria-hidden="true" />
        EVENT RISK MODE
      </div>
      <div className="event-risk-body">
        <div className="min-w-0">
          <strong className="event-risk-name">{name}</strong>
          <span className="event-risk-time">{time}</span>
        </div>
        <div className="event-risk-clock">
          {anytime ? (
            <span className="countdown text-warning">
              {String(event?.status || "UNRESOLVED").toUpperCase().replaceAll("_", " ")}
            </span>
          ) : live && phase ? (
            <span className="countdown text-warning">{phase.label}</span>
          ) : (
            <EventCountdown event={timed} now={now} />
          )}
        </div>
      </div>
      <dl className="event-risk-facts">
        <div>
          <dt>Impact</dt>
          <dd>
            <ImpactValue view={impact} />
          </dd>
        </div>
        {(relevance || level) && (
          <div>
            <dt>Relevance</dt>
            <dd>{[relevance, level].filter(Boolean).join(" · ")}</dd>
          </div>
        )}
        <div className="min-w-0">
          <dt>Affected</dt>
          <dd>{groups.length ? groups.join(" · ") : "Not reported"}</dd>
        </div>
      </dl>
      <p className="event-risk-note">
        {anytime
          ? "Published signals stay; exposed trade statuses stay conditional while this event is unresolved."
          : "Published signals stay; exposed assets may need to wait for this event."}{" "}
        <a href="#market-context" onClick={() => {
          const context = document.getElementById("market-context");
          if (context instanceof HTMLDetailsElement) context.open = true;
        }} className="text-primary underline-offset-2 hover:underline">
          Scenarios
        </a>
        {stale && block.generated_at && (
          <span className="block text-warning">
            Event data from {zonedTime(block.generated_at)} may be outdated.
          </span>
        )}
      </p>
    </section>
  );
}
