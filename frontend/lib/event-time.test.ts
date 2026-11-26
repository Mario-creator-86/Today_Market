import { describe, expect, it } from "vitest";
import {
  ANYTIME_LABEL,
  bucharestDateTime,
  bucharestDay,
  countdownView,
  displayPhase,
  DUE_TEXT,
  eventTimeLabel,
  formatCountdown,
  liveHeadline,
  relativeTime,
  subEventLines,
  timeAgo,
} from "./event-time";

// Saturday 26 Sep 2026 12:00 UTC = 15:00 Bucharest (EEST, UTC+3).
const NOW = Date.parse("2026-09-26T12:00:00Z");
const at = (minutes: number) => new Date(NOW + minutes * 60_000).toISOString();

describe("countdown formatting", () => {
  it("counts down in HH:MM:SS under a day and ticks per second", () => {
    expect(formatCountdown(47 * 60_000)).toBe("00:47:00");
    expect(formatCountdown(47 * 60_000 - 1000)).toBe("00:46:59");
    expect(formatCountdown(43 * 60_000 + 21_000)).toBe("00:43:21");
    expect(formatCountdown(500)).toBe("00:00:01");
    expect(formatCountdown(0)).toBe("00:00:00");
    expect(formatCountdown(-5000)).toBe("00:00:00");
  });
  it("prefixes days beyond 24 h", () => {
    expect(formatCountdown(2 * 86_400_000 + 4 * 3_600_000 + 10 * 60_000 + 5000)).toBe("2d 04:10:05");
  });
  it("gives a relative phrase", () => {
    expect(relativeTime(47 * 60_000)).toBe("in 47 min");
    expect(relativeTime(30 * 60_000)).toBe("in 30 min");
    expect(relativeTime(270 * 60_000)).toBe("in 4 h 30 min");
    expect(relativeTime(120 * 60_000)).toBe("in 2 h");
    expect(relativeTime(28 * 3_600_000)).toBe("in 1 day 4 h");
    expect(relativeTime(3 * 86_400_000 + 3_600_000)).toBe("in 3 days");
    expect(relativeTime(0)).toBe("now");
  });
  it("gives the age of a past time", () => {
    expect(timeAgo(20_000)).toBe("just now");
    expect(timeAgo(12 * 60_000)).toBe("12 min ago");
    expect(timeAgo(316 * 60_000)).toBe("5 h 16 min ago");
    expect(timeAgo(5 * 3_600_000)).toBe("5 h ago");
    expect(timeAgo(9 * 3_600_000 + 20 * 60_000)).toBe("9 h ago");
    expect(timeAgo(86_400_000)).toBe("1 day ago");
    expect(timeAgo(3 * 86_400_000)).toBe("3 days ago");
    expect(timeAgo(NaN)).toBe("just now");
  });
});

describe("Bucharest time labels", () => {
  it("shows HH:MM with the zone label today and the weekday on another day", () => {
    expect(eventTimeLabel({ time_utc: at(47) }, NOW)).toBe("15:47 Bucharest");
    expect(eventTimeLabel({ time_utc: "2026-09-29T18:00:00Z" }, NOW)).toBe("Tue 29 Sep 21:00 Bucharest");
  });
  it("writes the day without a leading zero", () => {
    expect(eventTimeLabel({ time_utc: "2026-10-01T12:30:00Z" }, NOW)).toBe("Thu 1 Oct 15:30 Bucharest");
    expect(bucharestDay(Date.parse("2026-10-02T09:00:00Z"))).toBe("Fri 2 Oct");
  });
  it("gives date and time in one format, also across the DST change and many calls", () => {
    expect(bucharestDateTime(Date.parse("2026-10-01T12:30:00Z"))).toBe("Thu 1 Oct 15:30");
    expect(bucharestDateTime(Date.parse("2026-10-28T18:00:00Z"))).toBe("Wed 28 Oct 20:00");
    // The shared formatter is reused: repeated calls give the same, correct text.
    for (let i = 0; i < 3; i++) expect(bucharestDateTime(Date.parse("2026-09-27T23:10:49Z"))).toBe("Mon 28 Sep 02:10");
  });
  it("follows the EU DST change on 25 Oct 2026 (UTC+3 → UTC+2)", () => {
    expect(eventTimeLabel({ time_utc: "2026-10-24T18:00:00Z" }, NOW)).toBe("Sat 24 Oct 21:00 Bucharest");
    expect(eventTimeLabel({ time_utc: "2026-10-28T18:00:00Z" }, NOW)).toBe("Wed 28 Oct 20:00 Bucharest");
  });
  it("date-only rows say time TBA and never count down", () => {
    const row = { time_utc: "2026-10-28T00:00:00Z", time_known: false, date_bucharest: "2026-10-28" };
    expect(eventTimeLabel(row, NOW)).toBe("Wed 28 Oct · time TBA");
    expect(countdownView(row, NOW)).toEqual({ kind: "tba", clock: null, text: "Time TBA" });
  });
  it("missing or invalid times are unavailable, never a guess", () => {
    expect(eventTimeLabel({ time_utc: "garbage" }, NOW)).toBe("Time unavailable");
    expect(eventTimeLabel(null, NOW)).toBe("Time unavailable");
    expect(countdownView({ time_utc: null }, NOW).text).toBe("Time unavailable");
  });
});

describe("countdown view and displayed phase", () => {
  it("counts down from time_utc on the client clock", () => {
    const e = { time_utc: at(47) };
    expect(countdownView(e, NOW)).toEqual({ kind: "countdown", clock: "00:47:00", text: "in 47 min" });
    expect(countdownView(e, NOW + 1000).clock).toBe("00:46:59");
  });
  it("after T0 without a backend phase change it is due, never released", () => {
    const e = { time_utc: at(47), phase: "IMMINENT" };
    const later = NOW + 48 * 60_000;
    expect(countdownView(e, later)).toEqual({ kind: "due", clock: "00:00:00", text: DUE_TEXT });
    const phase = displayPhase(e, later);
    expect(phase).toEqual({ phase: "IMMINENT", label: DUE_TEXT, due: true });
    expect(phase.label).not.toMatch(/released|resolved/i);
  });
  it("the backend phase wins over the countdown once the event is out", () => {
    // Five minutes after the FOMC statement: never "awaiting release".
    const t0 = at(-5);
    expect(countdownView({ time_utc: t0, time_known: true, phase: "IN_PROGRESS" }, NOW)).toEqual({
      kind: "phase",
      clock: null,
      text: "In progress",
    });
    expect(countdownView({ time_utc: t0, phase: "RELEASED" }, NOW).text).toBe("Released");
    expect(countdownView({ time_utc: t0, phase: "POST_EVENT" }, NOW).text).toBe("Post-event");
    expect(countdownView({ time_utc: t0, phase: "RESOLVED" }, NOW).text).toBe("Resolved");
    // status alone (older payloads) counts too.
    expect(countdownView({ time_utc: t0, status: "IN_PROGRESS" }, NOW).text).toBe("In progress");
    // A session-anchored date-only row the backend moved to POST_EVENT shows that phase, not "Time TBA".
    expect(countdownView({ time_utc: t0, time_known: false, phase: "POST_EVENT" }, NOW).kind).toBe("phase");
    // Before release the clock still ticks; a live phase before T0 never shows a countdown.
    expect(countdownView({ time_utc: at(47), phase: "APPROACHING" }, NOW).kind).toBe("countdown");
    expect(countdownView({ time_utc: at(3), phase: "IN_PROGRESS" }, NOW).kind).toBe("phase");
  });
  it("unscheduled events read timing ANYTIME once, with their status", () => {
    const hormuz = { time_utc: null, time_known: false, timing: "ANYTIME", status: "UNRESOLVED", phase: "ACTIVE" };
    expect(eventTimeLabel(hormuz, NOW)).toBe(ANYTIME_LABEL);
    expect(countdownView(hormuz, NOW)).toEqual({ kind: "anytime", clock: null, text: "UNRESOLVED" });
    expect(countdownView({ ...hormuz, status: "DE-ESCALATING" }, NOW).text).toBe("DE-ESCALATING");
    expect(`${eventTimeLabel(hormuz, NOW)} · ${countdownView(hormuz, NOW).text}`).not.toMatch(/TBA/);
  });
  it("shows the backend phase otherwise", () => {
    expect(displayPhase({ time_utc: at(47), phase: "APPROACHING" }, NOW).label).toBe("Approaching");
    expect(displayPhase({ time_utc: at(-5), phase: "IN_PROGRESS" }, NOW).label).toBe("In progress");
    // Date-only rows are never due from the local clock.
    expect(displayPhase({ time_utc: at(-5), phase: "SCHEDULED", time_known: false }, NOW).due).toBe(false);
  });
});

describe("sub-event lines", () => {
  const fed = {
    time_utc: at(-1),
    category: "CENTRAL_BANK",
    kind: "DECISION",
    is_decision: true,
    sub_events: [{ sub_event: "PRESS_CONFERENCE", time_utc: at(30), phase: "SCHEDULED" }],
  };
  it("says the decision is published and when the press conference starts", () => {
    expect(subEventLines({ ...fed, phase: "IN_PROGRESS" }, NOW)).toEqual([
      "Decision published — press conference in 30 min",
    ]);
    expect(subEventLines({ ...fed, phase: "IN_PROGRESS" }, NOW + 31 * 60_000)).toEqual([
      "Decision published — press conference under way",
    ]);
  });
  it("lists sub-event times before the release", () => {
    expect(subEventLines({ ...fed, time_utc: at(10), phase: "IMMINENT" }, NOW)).toEqual([
      "Press conference 15:30 Bucharest",
    ]);
  });
  it("macro releases say data released", () => {
    expect(subEventLines({ time_utc: at(-1), phase: "RELEASED", category: "MACRO" }, NOW)).toEqual([
      "Data released",
    ]);
    expect(subEventLines({ time_utc: at(-1), phase: "RELEASED", kind: "RELEASE", is_decision: false }, NOW)).toEqual([
      "Data released",
    ]);
  });
  it("only decisions say Decision published: testimony, minutes and speeches do not", () => {
    const cb = { time_utc: at(-10), category: "CENTRAL_BANK", phase: "IN_PROGRESS" };
    expect(subEventLines({ ...cb, kind: "TESTIMONY", is_decision: false }, NOW)).toEqual(["Testimony under way"]);
    expect(subEventLines({ ...cb, kind: "MINUTES", is_decision: false }, NOW)).toEqual(["Minutes released"]);
    expect(subEventLines({ ...cb, kind: "SPEECH", is_decision: false }, NOW)).toEqual(["Speech under way"]);
    // A central-bank category alone is not a decision (older payloads without is_decision / kind).
    expect(subEventLines(cb, NOW)).toEqual(["Released"]);
    // is_decision is authoritative over kind.
    expect(liveHeadline({ kind: "DECISION", is_decision: false, category: "CENTRAL_BANK" })).toBe("Released");
    expect(liveHeadline({ kind: "DECISION" })).toBe("Decision published");
    expect(liveHeadline({ is_decision: true })).toBe("Decision published");
  });
});
