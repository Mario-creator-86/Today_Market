import type { EventObject, SubEvent } from "./api";
import { APP_ZONE } from "./market-data";

/*
 * Event times and countdowns (docs/EVENT_AWARENESS.md §6). All arithmetic is
 * done here from the backend's `time_utc`; nothing is computed by the LLM and
 * a phase is never advanced to RELEASED or RESOLVED from the local clock.
 * Pure, with relative imports only, so vitest can collect it.
 */

const TZ = "Europe/Bucharest";
// Fixed abbreviations: ICU versions disagree on "Sep" / "Sept".
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** Epoch milliseconds of an ISO timestamp; null when missing or invalid. */
export function parseTime(value: unknown): number | null {
  if (typeof value !== "string" || !value) return null;
  const t = Date.parse(value);
  return Number.isFinite(t) ? t : null;
}

// One formatter for every call: building an Intl.DateTimeFormat is far slower than using one (long tables).
const FORMAT = new Intl.DateTimeFormat("en-GB", {
  timeZone: TZ,
  weekday: "short",
  year: "numeric",
  day: "numeric",
  month: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

function parts(t: number): Record<string, string> {
  return Object.fromEntries(FORMAT.formatToParts(new Date(t)).map((p) => [p.type, p.value]));
}

/** "21:00" in Europe/Bucharest. */
export function bucharestHourMinute(t: number): string {
  const p = parts(t);
  return `${p.hour}:${p.minute}`;
}

/** "Wed 28 Oct", "Thu 1 Oct" in Europe/Bucharest (en-GB pads the day to "01": the number drops the zero). */
export function bucharestDay(t: number): string {
  const p = parts(t);
  return `${p.weekday} ${Number(p.day)} ${MONTHS[Number(p.month) - 1]}`;
}

/** "Mon 28 Sep 02:10" in Europe/Bucharest: the app's one date-and-time format (lib/api localTime). */
export function bucharestDateTime(t: number): string {
  const p = parts(t);
  return `${p.weekday} ${Number(p.day)} ${MONTHS[Number(p.month) - 1]} ${p.hour}:${p.minute}`;
}

function dayKey(t: number): string {
  const p = parts(t);
  return `${p.year}-${p.month}-${p.day}`;
}

/** "Wed 28 Oct" from a "2026-10-28" local date string; null when invalid. */
function dayFromDate(value: unknown): string | null {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  // Noon UTC falls on the same calendar day in Bucharest.
  const t = Date.parse(`${value}T12:00:00Z`);
  return Number.isFinite(t) ? bucharestDay(t) : null;
}

/** "00:43:21" under 24 h, "2d 04:10:05" beyond; "00:00:00" once due. */
export function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const days = Math.floor(total / 86_400);
  const pad = (n: number) => String(n).padStart(2, "0");
  const clock = `${pad(Math.floor((total % 86_400) / 3600))}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`;
  return days ? `${days}d ${clock}` : clock;
}

/** "in 47 min", "in 4 h 30 min", "in 1 day 4 h", "in 3 days"; "now" once due. */
export function relativeTime(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "now";
  if (ms >= DAY) {
    const days = Math.floor(ms / DAY);
    const hours = Math.floor((ms % DAY) / HOUR);
    const unit = days === 1 ? "day" : "days";
    return days < 3 && hours ? `in ${days} ${unit} ${hours} h` : `in ${days} ${unit}`;
  }
  const minutes = Math.ceil(ms / MINUTE);
  if (minutes < 60) return `in ${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `in ${h} h ${m} min` : `in ${h} h`;
}

/** Age of a past time: "just now", "12 min ago", "5 h ago", "5 h 20 min ago" under 6 h, "2 days ago". */
export function timeAgo(ms: number): string {
  if (!Number.isFinite(ms) || ms < MINUTE) return "just now";
  if (ms >= DAY) {
    const days = Math.floor(ms / DAY);
    return `${days} ${days === 1 ? "day" : "days"} ago`;
  }
  const minutes = Math.floor(ms / MINUTE);
  if (minutes < 60) return `${minutes} min ago`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h < 6 && m ? `${h} h ${m} min ago` : `${h} h ago`;
}

type Timed = Pick<EventObject, "time_utc" | "time_known" | "date_bucharest" | "time_bucharest"> &
  Partial<Pick<EventObject, "timing">>;

/** Verified unscheduled events (§3) have no clock: timing ANYTIME. */
export function isAnytime(event: Partial<Pick<EventObject, "timing">> | null | undefined): boolean {
  return typeof event?.timing === "string" && event.timing.trim().toUpperCase() === "ANYTIME";
}

export const ANYTIME_LABEL = "Timing ANYTIME";

/** A known exact time, or null for date-only rows (`time_known: false`) and missing times. */
export function exactTime(event: Timed | null | undefined): number | null {
  if (!event || event.time_known === false) return null;
  return parseTime(event.time_utc);
}

/**
 * Bucharest time with the zone label: "21:00 Bucharest" today, "Wed 28 Oct
 * 21:00 Bucharest" on another day, "Wed 28 Oct · time TBA" for date-only rows,
 * "Timing ANYTIME" for unscheduled events.
 */
export function eventTimeLabel(event: Timed | null | undefined, now: number): string {
  if (!event) return "Time unavailable";
  if (isAnytime(event)) return ANYTIME_LABEL;
  const t = parseTime(event.time_utc);
  if (event.time_known === false) {
    const day = dayFromDate(event.date_bucharest) ?? (t !== null ? bucharestDay(t) : null);
    return day ? `${day} · time TBA` : "Time TBA";
  }
  if (t === null) {
    // Backend text only when no timestamp came with it.
    return typeof event.time_bucharest === "string" && event.time_bucharest
      ? `${event.time_bucharest} ${APP_ZONE}`
      : "Time unavailable";
  }
  const clock = `${bucharestHourMinute(t)} ${APP_ZONE}`;
  return dayKey(t) === dayKey(now) ? clock : `${bucharestDay(t)} ${clock}`;
}

export type CountdownView = {
  /**
   * countdown: ticking; due: T0 passed, backend phase not advanced yet; phase: the backend says the event
   * is released, live, being digested or resolved; anytime: unscheduled; tba: date only; none: no time.
   */
  kind: "countdown" | "due" | "phase" | "anytime" | "tba" | "none";
  /** "00:47:00" while counting down. */
  clock: string | null;
  /** "in 47 min", "Due now · awaiting release", "In progress", "UNRESOLVED", "Time TBA", "Time unavailable". */
  text: string;
};

export const DUE_TEXT = "Due now · awaiting release";

type Phased = Timed & Partial<Pick<EventObject, "phase" | "status">>;

/**
 * Live countdown to `time_utc`, recomputed from the client clock every render.
 * The backend phase wins: RELEASED, IN_PROGRESS, POST_EVENT and RESOLVED are
 * shown as such, never as "awaiting release". The clock counts down only
 * before `time_utc` and only when the time is known.
 */
export function countdownView(event: Phased | null | undefined, now: number): CountdownView {
  if (isAnytime(event)) {
    const status = typeof event?.status === "string" && event.status.trim() ? event.status : "ACTIVE";
    return { kind: "anytime", clock: null, text: status.trim().toUpperCase().replaceAll("_", " ") };
  }
  const phase = phaseKey(event?.phase ?? event?.status);
  if (LIVE_PHASES.has(phase) || phase === "RESOLVED")
    return { kind: "phase", clock: null, text: PHASE_LABEL[phase] };
  if (event?.time_known === false) return { kind: "tba", clock: null, text: "Time TBA" };
  const t = exactTime(event);
  if (t === null) return { kind: "none", clock: null, text: "Time unavailable" };
  const ms = t - now;
  if (ms <= 0) return { kind: "due", clock: formatCountdown(0), text: DUE_TEXT };
  return { kind: "countdown", clock: formatCountdown(ms), text: relativeTime(ms) };
}

const PHASE_LABEL: Record<string, string> = {
  SCHEDULED: "Scheduled",
  APPROACHING: "Approaching",
  IMMINENT: "Imminent",
  RELEASED: "Released",
  IN_PROGRESS: "In progress",
  POST_EVENT: "Post-event",
  RESOLVED: "Resolved",
};
const PRE_RELEASE = new Set(["SCHEDULED", "APPROACHING", "IMMINENT"]);
/** Phases in which an event is live or being digested (the post-event panel). */
export const LIVE_PHASES = new Set(["RELEASED", "IN_PROGRESS", "POST_EVENT"]);

function phaseKey(value: unknown): string {
  return typeof value === "string" ? value.trim().toUpperCase().replaceAll(" ", "_") : "";
}

export type PhaseView = { phase: string; label: string; due: boolean };

/**
 * The phase to show: the backend's, except that a pre-release event whose
 * exact time has passed reads "Due now · awaiting release". The local clock
 * never produces RELEASED, IN_PROGRESS or RESOLVED.
 */
export function displayPhase(
  event: (Timed & Pick<EventObject, "phase" | "status">) | null | undefined,
  now: number,
): PhaseView {
  const phase = phaseKey(event?.phase ?? event?.status) || "SCHEDULED";
  const t = exactTime(event);
  if (PRE_RELEASE.has(phase) && t !== null && now >= t)
    return { phase, label: DUE_TEXT, due: true };
  return { phase, label: PHASE_LABEL[phase] ?? phase.replaceAll("_", " "), due: false };
}

/** "PRESS_CONFERENCE" → "press conference". */
export function subEventLabel(sub: Pick<SubEvent, "sub_event">): string {
  return String(sub.sub_event || "sub-event").replaceAll("_", " ").toLowerCase();
}

const capitalize = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

type KindFields = Partial<Pick<EventObject, "kind" | "is_decision" | "category">>;

/** Live headline per event kind (§8). */
const KIND_HEADLINE: Record<string, string> = {
  RELEASE: "Data released",
  MINUTES: "Minutes released",
  PROJECTIONS: "Projections released",
  STATEMENT: "Statement released",
  AUCTION: "Auction results released",
  EARNINGS: "Earnings released",
  EXPIRY: "Expiry reached",
  SPEECH: "Speech under way",
  TESTIMONY: "Testimony under way",
  PRESS_CONFERENCE: "Press conference under way",
};

/**
 * A live event's headline. "Decision published" only for decisions
 * (`is_decision`, or kind DECISION when a payload has no flag): minutes,
 * testimony, speeches and the Beige Book are not decisions.
 */
export function liveHeadline(event: KindFields | null | undefined): string {
  const kind = typeof event?.kind === "string" ? event.kind.trim().toUpperCase() : "";
  const decision = typeof event?.is_decision === "boolean" ? event.is_decision : kind === "DECISION";
  if (decision) return "Decision published";
  if (KIND_HEADLINE[kind]) return KIND_HEADLINE[kind];
  return String(event?.category ?? "").toUpperCase() === "MACRO" ? "Data released" : "Released";
}

/**
 * Phase line with sub-events (§13): "Decision published — press conference in
 * 30 min" while a decision is live, "Press conference 21:30 Bucharest" before
 * it. Empty when there is nothing to add.
 */
export function subEventLines(
  event: (Timed & Pick<EventObject, "phase" | "status" | "category" | "sub_events"> & KindFields) | null | undefined,
  now: number,
): string[] {
  if (!event) return [];
  const subs = (Array.isArray(event.sub_events) ? event.sub_events : []).filter(
    (s): s is SubEvent => !!s && typeof s.sub_event === "string",
  );
  const phase = phaseKey(event.phase ?? event.status);
  if (LIVE_PHASES.has(phase)) {
    const headline = liveHeadline(event);
    const timed = subs
      .map((s) => ({ s, t: exactTime(s) }))
      .filter((x): x is { s: SubEvent; t: number } => x.t !== null)
      .sort((a, b) => a.t - b.t);
    const next = timed.find((x) => x.t > now && phaseKey(x.s.phase) !== "RESOLVED");
    const running = timed.find(
      (x) => x.t <= now && !["RESOLVED", "POST_EVENT"].includes(phaseKey(x.s.phase)),
    );
    if (running && phase !== "POST_EVENT")
      return [`${headline} — ${subEventLabel(running.s)} under way`];
    if (next) return [`${headline} — ${subEventLabel(next.s)} ${relativeTime(next.t - now)}`];
    return [headline];
  }
  return subs.map((s) => `${capitalize(subEventLabel(s))} ${eventTimeLabel(s, now)}`);
}
