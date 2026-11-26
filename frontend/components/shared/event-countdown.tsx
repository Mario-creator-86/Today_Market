import type { EventObject } from "@/lib/api";
import { countdownView, eventTimeLabel } from "@/lib/event-time";
import { cn } from "@/lib/utils";

type CountdownEvent =
  | (Pick<EventObject, "time_utc" | "time_known"> &
      Partial<Pick<EventObject, "phase" | "status" | "timing" | "date_bucharest" | "time_bucharest">>)
  | null
  | undefined;

/**
 * Inline "when": the Bucharest time label, then the compact countdown or
 * backend phase when it adds something ("21:00 Bucharest · in 47 min",
 * "Timing ANYTIME · UNRESOLVED", "Wed 28 Oct · time TBA"), never the same
 * words twice.
 */
export function EventWhen({ event, now }: { event: CountdownEvent; now: number }) {
  const kind = countdownView(event, now).kind;
  return (
    <>
      {eventTimeLabel(event, now)}
      {kind !== "tba" && kind !== "none" && (
        <>
          {" · "}
          <EventCountdown event={event} now={now} compact />
        </>
      )}
    </>
  );
}

/**
 * Live countdown to an event's `time_utc` (frontend arithmetic only, §35).
 * `now` comes from the caller's useClock, so it ticks every second. The
 * backend phase wins: a released, live, post-event or resolved event shows
 * that phase, never a countdown; after T0 with a pre-release phase it reads
 * "Due now · awaiting release" until the backend moves the phase. Unscheduled
 * events (timing ANYTIME) show their status. role="timer" is not announced
 * on every tick.
 */
export function EventCountdown({
  event,
  now,
  className,
  compact = false,
}: {
  event: CountdownEvent;
  now: number;
  className?: string;
  /** Relative text only ("in 47 min"). */
  compact?: boolean;
}) {
  const view = countdownView(event, now);
  const attention = view.kind === "due" || view.kind === "anytime" || (view.kind === "phase" && view.text !== "Resolved");
  if (view.kind !== "countdown")
    return (
      <span className={cn("countdown", attention && "text-warning", className)} data-kind={view.kind}>
        {view.text}
      </span>
    );
  if (compact) return <span className={cn("countdown", className)}>{view.text}</span>;
  return (
    <span className={cn("countdown", className)}>
      <time role="timer" dateTime={event?.time_utc ?? undefined} aria-label={view.text}>
        {view.clock}
      </time>
      <small>{view.text}</small>
    </span>
  );
}
