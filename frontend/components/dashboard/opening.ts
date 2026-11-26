import type {
  EventObject,
  EventRisk,
  Interpretation,
  MarketViewItem,
  RecordData,
  Regime,
  RegimeDriver,
} from "../../lib/api";
import {
  events as wellFormedEvents,
  impactView,
  list,
  livePostEvents,
  type Expectation,
} from "../../lib/event-risk";
import { eventTimeLabel, exactTime, isAnytime, LIVE_PHASES, parseTime } from "../../lib/event-time";
import { humanize, isNum, labelCase } from "../../lib/format";
import { APP_ZONE } from "../../lib/market-data";

/*
 * Rules of the opening screen (sections NOW / EVENTS / MARKETS / POSITIONING /
 * SETUPS): what is said, and that each fact is said once. Pure, with relative
 * imports only, so vitest can collect it. Nothing here invents a value: the
 * helpers only select, shorten or drop text the backend sent.
 */

const isObject = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Whole-word test for a symbol ("GOLD" in "GOLD -1.2%", never in "GOLDMAN"). */
function mentions(text: string, symbol: string): boolean {
  return new RegExp(`(^|[^A-Za-z0-9])${escapeRegExp(symbol)}($|[^A-Za-z0-9])`).test(text);
}

function capitalize(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

function withPeriod(text: string): string {
  const t = text.trim().replace(/[;:,\s]+$/, "");
  return !t || /[.!?…]$/.test(t) ? t : `${t}.`;
}

// --- sentences -----------------------------------------------------------------------------

/** Abbreviations whose full stop does not end a sentence. */
const ABBREVIATION = /(?:\b(?:[A-Za-z]\.){2,}|\b(?:vs|approx|inc|corp|ltd|st|mr|ms|dr|e\.g|i\.e)\.)$/i;

/** "A. B? C" → ["A.", "B?", "C"]; "U.S. Bureau" and "3.1%" are not sentence ends. */
export function sentences(text: unknown): string[] {
  if (typeof text !== "string") return [];
  const clean = text.replace(/\s+/g, " ").trim();
  if (!clean) return [];
  const out: string[] = [];
  let start = 0;
  const end = /[.!?]+(?=\s+\S)/g;
  for (let m = end.exec(clean); m; m = end.exec(clean)) {
    const stop = m.index + m[0].length;
    const candidate = clean.slice(start, stop);
    if (m[0] === "." && ABBREVIATION.test(candidate)) continue;
    // A sentence starts with a capital, a digit or an opening quote / bracket.
    if (!/^\s+["'“(\[]?[A-Z0-9]/.test(clean.slice(stop))) continue;
    out.push(candidate.trim());
    start = stop;
  }
  const rest = clean.slice(start).trim();
  if (rest) out.push(rest);
  return out;
}

/** The first `max` sentences of a text, as written. */
export function clampSentences(text: unknown, max: number): string {
  return sentences(text).slice(0, Math.max(0, max)).join(" ");
}

// --- NOW (hero) ----------------------------------------------------------------------------

/** Symbols listed in a clause before it counts as an enumeration of the positioning lists. */
const ENUMERATION_MIN = 3;

export type HeroTextOptions = {
  /** Names of events the EVENTS section already states (the next key event, the dominant one). */
  eventNames?: string[];
  /** Symbols the POSITIONING section already lists. */
  symbols?: string[];
  /** The closed-markets note is on screen (POSITIONING): the view does not say it again. */
  closedNoteShown?: boolean;
  /** Sentences kept (2). */
  max?: number;
};

/**
 * The market view shown in the hero: at most two sentences, without what the
 * sections below already state. A sentence naming an event of the EVENTS
 * section in full is dropped (its card says it), as is the closed-markets
 * sentence when POSITIONING carries that note, and a ";" clause that lists
 * three or more POSITIONING symbols. Text is only removed, never rewritten;
 * when nothing would be left the first sentence stays as written.
 */
export function heroText(text: unknown, options: HeroTextOptions = {}): string {
  const all = sentences(text);
  if (!all.length) return "";
  const names = (options.eventNames ?? []).map((n) => n.trim().toLowerCase()).filter(Boolean);
  const symbols = (options.symbols ?? []).filter(Boolean);
  const kept: string[] = [];
  for (const sentence of all) {
    const lower = sentence.toLowerCase();
    if (names.some((n) => lower.includes(n))) continue;
    if (options.closedNoteShown && /\bclosed\b/i.test(sentence) && /\breopen/i.test(sentence)) continue;
    const clauses = sentence
      .split(/;\s+/)
      .filter((clause) => symbols.filter((s) => mentions(clause, s)).length < ENUMERATION_MIN);
    if (!clauses.length) continue;
    kept.push(withPeriod(capitalize(clauses.join("; "))));
  }
  const shown = kept.length ? kept : [all[0]];
  return shown.slice(0, options.max ?? 2).join(" ");
}

/**
 * The regime in plain words: "USD strength", "Constructive · Risk on",
 * "Risk off / USD strength" (sentence case, acronyms kept). A MIXED posture
 * adds nothing and is left out; null without an active regime.
 */
export function regimeHeadline(regimes: unknown, posture: unknown): string | null {
  const names = list<unknown>(regimes).filter((r): r is string => typeof r === "string" && !!r.trim());
  if (!names.length) return null;
  const lead = typeof posture === "string" && posture && posture !== "MIXED" ? `${labelCase(posture)} · ` : "";
  return `${lead}${names.map(labelCase).join(" / ")}`;
}

const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

/** "Thursday" from "2026-10-01" (a calendar day, never shifted by a time zone); null when invalid. */
export function weekdayName(day: unknown): string | null {
  if (typeof day !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const t = Date.parse(`${day}T12:00:00Z`);
  return Number.isFinite(t) ? WEEKDAYS[new Date(t).getUTCDay()] : null;
}

/** The newest close date among the regime's inputs ("2026-10-01"); null when none carries one. */
function latestCloseDate(regime: Regime): string | null {
  const days: string[] = [];
  for (const driver of list<RegimeDriver>(regime.drivers))
    for (const input of list<unknown>(driver?.inputs))
      if (isObject(input) && typeof input.close_date === "string" && weekdayName(input.close_date))
        days.push(input.close_date);
  for (const source of Object.values(isObject(regime.feature_sources) ? regime.feature_sources : {}))
    if (isObject(source) && typeof source.close_date === "string" && weekdayName(source.close_date))
      days.push(source.close_date);
  return days.length ? days.sort()[days.length - 1] : null;
}

export type BasisTag = {
  /** "live", "from Thursday's close". */
  label: string;
  /** True for a close-based (historical) reading. */
  historical: boolean;
  /** The backend's full basis label, for the tooltip. */
  title: string | null;
};

/**
 * The ONE basis tag of the hero: "live" for fresh observations, "from
 * Thursday's close" for a last-close reading (the newest close among its
 * inputs; "from the last close" when no input carries a date). The backend's
 * full label ("last close 29 Sep–Thu 1 Oct + proxies (IWM, PAXG)") is the
 * tooltip. Null without a regime basis.
 */
export function basisTag(regime: Regime | null | undefined): BasisTag | null {
  const basis = regime?.basis;
  if (!regime || typeof basis !== "string" || !basis) return null;
  const title = typeof regime.basis_label === "string" && regime.basis_label.trim() ? regime.basis_label.trim() : null;
  if (basis === "live") return { label: "live", historical: false, title };
  if (basis === "last_close") {
    const weekday = weekdayName(latestCloseDate(regime));
    return { label: weekday ? `from ${weekday}'s close` : "from the last close", historical: true, title };
  }
  return { label: humanize(basis).toLowerCase(), historical: false, title };
}

/**
 * What a driver chip's observations are: "last close", "live", or — for
 * current observations read from direction proxies — "via proxies" (every
 * input) / "live + proxies" (some). A proxy is never presented as live data.
 */
export function driverBasisWord(item: RegimeDriver | null | undefined): string | null {
  const inputs = list<unknown>(item?.inputs).filter(isObject);
  const proxied = inputs.filter((i) => (typeof i.via === "string" && !!i.via.trim()) || i.basis === "proxy");
  if (inputs.length && proxied.length === inputs.length) return "via proxies";
  if (proxied.length) return "live + proxies";
  const basis = item?.basis;
  if (typeof basis !== "string" || !basis) return null;
  if (basis === "proxy") return "via proxies";
  return basis === "last_close" ? "last close" : humanize(basis).toLowerCase();
}

export type HeroWarning = {
  kind: "conflict" | "unconfirmed";
  text: string;
  /** Further conflicts, listed under "Data coverage and basis". */
  more: number;
};

/** Drivers that carry at least one measured input. */
export function measuredDrivers(drivers: unknown): RegimeDriver[] {
  return list<RegimeDriver>(drivers).filter(
    (item) => isObject(item) && typeof item.driver === "string" && (list(item.inputs).length > 0 || !!item.input),
  );
}

/**
 * The ONE warning line of the hero. A conflict between the close-based view
 * and today's observations comes first (the first conflict as the backend
 * words it; the rest are counted); else a close-based view nothing current
 * confirms says so. Null for a live view without conflicts.
 */
export function heroWarning(regime: Regime | null | undefined): HeroWarning | null {
  if (!regime) return null;
  const conflicts = list<unknown>(regime.divergences)
    .map((d) => (isObject(d) && typeof d.text === "string" ? d.text.trim() : ""))
    .filter(Boolean);
  if (conflicts.length)
    return { kind: "conflict", text: `Today's observations conflict with this view: ${conflicts[0]}`, more: conflicts.length - 1 };
  if (regime.basis === "last_close" && !measuredDrivers(regime.current_drivers).length)
    return { kind: "unconfirmed", text: "Today's direction is not confirmed: no current observations yet.", more: 0 };
  return null;
}

// --- EVENTS --------------------------------------------------------------------------------

/** Clauses that only restate a chip of the event card (level, exposure) or a score kept in its details. */
const RESTATES_CHIPS = /\b(relevance|most exposed|dominance score)\b/i;

/**
 * ONE sentence of "why it matters": the text before any "Known: …" provenance
 * (that belongs to the card's details), its first sentence, without the ";"
 * clauses that restate the level or exposure chips. "" when nothing is left.
 */
export function whyLine(text: unknown): string {
  if (typeof text !== "string") return "";
  const lead = text.split(/\bKnown:/)[0];
  const first = sentences(lead)[0] ?? "";
  const clauses = first
    .replace(/[.!?]+$/, "")
    .split(/;\s+/)
    .map((c) => c.trim())
    .filter((c) => c && !RESTATES_CHIPS.test(c));
  return clauses.length ? withPeriod(capitalize(clauses.join("; "))) : "";
}

/**
 * Why the next key event matters, in one sentence: the interpretation's
 * explanation for that event, else its reason for calling the event dominant
 * (both the backend's text, shortened by `whyLine`). "" when neither says
 * more than the card's chips.
 */
export function eventWhy(event: Pick<EventObject, "event_id"> | null | undefined, interpretation: Interpretation | null | undefined): string {
  if (!event) return "";
  const explanation = list<unknown>(interpretation?.output?.next_events).find(
    (item) => isObject(item) && item.event_id === event.event_id,
  );
  const own = whyLine(isObject(explanation) ? explanation.why_it_matters : null);
  if (own) return own;
  const dominant = interpretation?.output?.dominant_event;
  return isObject(dominant) && dominant.event_id === event.event_id ? whyLine(dominant.reason) : "";
}

/**
 * One part of the expectations line: "Consensus: not available", "Market
 * pricing: unknown", or the values as sent ("Finnhub estimate: EPS 1.42 USD,
 * Revenue 23.1B"; the provider's own label is never renamed), followed by the
 * backend's own note when it sent one.
 */
export function expectationText(title: string, value: Expectation, missingWord: string): string {
  if (value.missing) return `${title}: ${missingWord}`;
  const values = value.lines.map((l) => `${l.label} ${l.value}`).join(", ");
  return `${value.label ?? title}: ${[values, value.text].filter(Boolean).join(" — ")}`.trim();
}

/** Data-quality reasons the expectations line does not already state (missing consensus / pricing). */
export function otherIncompleteReasons(reasons: string[]): string[] {
  return reasons.filter((r) => !/^(no consensus|market pricing unknown|unknown)$/i.test(r.trim()));
}

export type UpcomingRow = {
  key: string;
  name: string;
  /** "17:30" today, "Wed 7 Oct 17:30" on another day, "Wed 28 Oct · time TBA" (the page footer names the zone). */
  when: string;
  /** ISO time for <time dateTime>; null for date-only rows. */
  iso: string | null;
  /** "6/10"; null when the row has no impact. */
  impact: string | null;
};

const SKIP_PHASES = new Set([...LIVE_PHASES, "RESOLVED"]);

/** A date-only row sent without its release window is taken as due for the 24 h after its anchor time. */
const DATE_ONLY_WINDOW_MS = 24 * 60 * 60_000;

/** A timeline row's time: the event time label without the zone name every row would repeat. */
function rowWhen(event: Parameters<typeof eventTimeLabel>[0], now: number): string {
  return eventTimeLabel(event, now).replace(` ${APP_ZONE}`, "");
}

/**
 * "Coming up": the other scheduled events from now on, soonest first, at most
 * `limit`, wherever they fall relative to the key event (the key event is the
 * earliest HIGH-relevance one, so lesser events are often due before it: the
 * jobless claims of this afternoon, with CPI next week as the key event). The
 * key event and a dominant event shown in its card are left out (each event
 * appears once on the opening screen), as are events already released and
 * unscheduled ones. A date-only row stays listed until its release window
 * ends, whatever its anchor time.
 */
export function upcomingEvents(block: EventRisk | null | undefined, now: number, limit = 4): UpcomingRow[] {
  if (!block) return [];
  const shown = new Set(
    [block.next_event, block.dominant_event].flatMap((e) => (isObject(e) && typeof e.event_id === "string" ? [e.event_id] : [])),
  );
  const seen = new Set<string>();
  return wellFormedEvents(block.events)
    .flatMap((event) => {
      if (shown.has(event.event_id) || seen.has(event.event_id) || isAnytime(event)) return [];
      if (SKIP_PHASES.has(String(event.phase ?? "").toUpperCase())) return [];
      const t = parseTime(event.time_utc);
      if (t === null) return [];
      // Date-only: pending to the end of its window (the calendar page's rule: 24 h after the anchor without one).
      const pendingUntil = event.time_known === false ? (parseTime(event.date_window?.end_utc) ?? t + DATE_ONLY_WINDOW_MS) : t;
      if (pendingUntil < now) return [];
      seen.add(event.event_id);
      return [{ event, t }];
    })
    .sort((a, b) => a.t - b.t)
    .slice(0, limit)
    .map(({ event, t }) => ({
      key: event.event_id,
      name: event.name,
      when: rowWhen(event, now),
      iso: event.time_known === false ? null : new Date(t).toISOString(),
      impact: impactView(event).text,
    }));
}

export type ReleasedRow = {
  key: string;
  /** The scheduled event that was released ("US employment situation (NFP, unemployment, AHE)"). */
  name: string;
  /** What was published, as the official document or the calendar row states it; null when the service sent none. */
  summary: string | null;
  /** Release time (ISO); null when the service sent none. */
  iso: string | null;
  /** Tracked assets the event class exposes (no direction: that depends on the surprise). */
  assets: string[];
  /** The stored news event of the official document, for the link; null when the service names none. */
  newsId: string | null;
};

const textOf = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null);

/** Every well-formed entry of the snapshot's `released_today`, newest release first. */
function releasedRows(block: EventRisk | null | undefined): ReleasedRow[] {
  const seen = new Set<string>();
  return list<unknown>(block?.released_today)
    .flatMap((raw) => {
      if (!isObject(raw)) return [];
      const name = textOf(raw.name);
      const key = textOf(raw.event_id) ?? name;
      if (!name || !key || seen.has(key)) return [];
      seen.add(key);
      const document = isObject(raw.document) ? raw.document : {};
      const t = parseTime(textOf(raw.released_utc) ?? textOf(document.published_at) ?? textOf(raw.time_utc));
      const assets = list<unknown>(raw.affected_assets).flatMap((a) =>
        typeof a === "string" && a.trim() ? [a.trim()] : isObject(a) && typeof a.symbol === "string" && a.symbol ? [a.symbol] : [],
      );
      return [
        {
          key,
          name,
          summary: textOf(raw.summary) ?? textOf(document.summary) ?? textOf(raw.values_text) ?? textOf(document.title),
          t,
          assets: [...new Set(assets)],
          newsId: textOf(raw.news_event_id) ?? textOf(document.event_id),
        },
      ];
    })
    .sort((a, b) => (b.t ?? 0) - (a.t ?? 0))
    .map(({ t, ...row }) => ({ ...row, iso: t === null ? null : new Date(t).toISOString() }));
}

/**
 * Scheduled events released today (the snapshot's `released_today`) for the
 * timeline: a released key event stays on the opening screen with what was
 * published after it leaves the upcoming list, newest first, at most `limit`.
 * An event the key-event card still shows (the next or the dominant event,
 * while its reaction is in progress) is left out: the card states what was
 * published (`publishedSummary`), and each event appears once. So is an event
 * of the post-event panel while that panel is on the opening screen (a MARKET
 * DIVERGENCE): its facts already state the release. Read
 * defensively: an entry needs a name; the published text is the entry's
 * summary, else its document's summary, its series values or the document
 * title. Empty on a backend without the list.
 */
export function releasedToday(block: EventRisk | null | undefined, limit = 2): ReleasedRow[] {
  const inCard = new Set(
    [block?.next_event, block?.dominant_event].flatMap((e) => (isObject(e) && typeof e.event_id === "string" ? [e.event_id] : [])),
  );
  // The post-event panel is above the fold only while a measured reaction diverges; it then lists every live reaction.
  const reactions = livePostEvents(block);
  const inPanel = new Set(reactions.some((entry) => entry.divergence) ? reactions.map((entry) => entry.event_id) : []);
  return releasedRows(block)
    .filter((row) => !inCard.has(row.key) && !inPanel.has(row.key))
    .slice(0, Math.max(0, limit));
}

/** What was published for an event the key-event card shows (its `released_today` entry); null when nothing is held. */
export function publishedSummary(block: EventRisk | null | undefined, event: Pick<EventObject, "event_id"> | null | undefined): string | null {
  if (!event) return null;
  return releasedRows(block).find((row) => row.key === event.event_id)?.summary ?? null;
}

/** "Coming up" from plain calendar rows (a backend without the event layer); rows are already filtered and sorted. */
export function calendarUpcoming(rows: RecordData[], now: number): UpcomingRow[] {
  return rows.flatMap((row, i) => {
    const iso = typeof row.scheduled_utc === "string" ? row.scheduled_utc : typeof row.scheduled_at === "string" ? row.scheduled_at : null;
    const name = typeof row.name === "string" ? row.name : typeof row.event === "string" ? row.event : "";
    if (!name || parseTime(iso) === null) return [];
    const timed = { time_utc: iso, time_known: row.time_known !== false };
    const impact = row.impact;
    return [
      {
        key: typeof row.id === "string" ? row.id : `${i}-${name}`,
        name,
        when: rowWhen(timed, now),
        iso: timed.time_known ? new Date(parseTime(iso)!).toISOString() : null,
        impact: isNum(impact)
          ? `${impact.toLocaleString("en-US", { maximumFractionDigits: 1 })}/10`
          : typeof impact === "string" && impact
            ? impact.toUpperCase()
            : null,
      },
    ];
  });
}

// --- POSITIONING ---------------------------------------------------------------------------

/** Group explanations shown under a positioning group (never per asset). */
const GROUP_NOTES = 2;
/** Up to this many exceptions are named ("except GOLD"); beyond it the assets a note applies to are listed. */
const EXCEPTIONS_NAMED = 2;

/**
 * The explanation a positioning group shares, written once: the reasons
 * (reason_facts) that do not name their own asset and hold for at least two
 * of the group's assets, most widely shared first. A reason that misses some
 * assets says which ("… (except GOLD)"), so it is never claimed for an asset
 * it was not sent for. A single asset keeps its own general reasons.
 */
export function groupNotes(items: MarketViewItem[], limit = GROUP_NOTES): string[] {
  const symbols = items.map((i) => i.symbol);
  return sharedFacts(items, limit).map(([text, held]) => {
    const sentence = capitalize(text);
    if (held.length === symbols.length) return sentence;
    const missing = symbols.filter((s) => !held.includes(s));
    return missing.length <= EXCEPTIONS_NAMED
      ? `${sentence} (except ${missing.join(", ")})`
      : `${sentence} (${held.join(", ")})`;
  });
}

/** The facts written under the group, each with the assets it was sent for. */
function sharedFacts(items: MarketViewItem[], limit: number): [string, string[]][] {
  const holders = new Map<string, string[]>();
  for (const item of items)
    for (const fact of list<unknown>(item.reason_facts)) {
      if (typeof fact !== "string" || !fact.trim() || mentions(fact, item.symbol)) continue;
      const text = fact.trim();
      const held = holders.get(text) ?? [];
      if (!held.includes(item.symbol)) holders.set(text, [...held, item.symbol]);
    }
  const needed = Math.min(2, items.length);
  return [...holders.entries()]
    .filter(([, held]) => held.length >= needed)
    .sort((a, b) => b[1].length - a[1].length)
    .slice(0, limit);
}

/**
 * The reasons left for an asset's own chip (its tooltip): every reason sent
 * for it that the group's notes do not already state, so none is lost.
 */
export function ownFacts(item: MarketViewItem, group: MarketViewItem[] = [item], limit = GROUP_NOTES): string[] {
  const shared = new Set(sharedFacts(group, limit).map(([text]) => text));
  return list<unknown>(item.reason_facts)
    .filter((fact): fact is string => typeof fact === "string" && !!fact.trim())
    .map((fact) => fact.trim())
    .filter((fact) => !shared.has(fact));
}

// --- SETUPS --------------------------------------------------------------------------------

/** A key event this close ahead is what setups wait for. */
const PENDING_WINDOW_MS = 24 * 60 * 60_000;

/** The reason shown when nothing is actionable and no event is pending. */
export const NO_SETUPS_REASON = "waiting for verified catalysts, fresh prices and market confirmation";

/**
 * Why nothing is actionable, for the one-line SETUPS state. While EVENT RISK
 * MODE is on, or the next key event is due within a day (or is an unresolved
 * unscheduled event), setups wait for that event; the EVENTS section names
 * it, so the line only refers to it. Otherwise the standing reason.
 */
export function noSetupsReason(block: EventRisk | null | undefined, now: number): string {
  const event = block?.next_event;
  const pendingEvent = (() => {
    if (!isObject(event) || typeof event.event_id !== "string") return false;
    if (SKIP_PHASES.has(String(event.phase ?? "").toUpperCase())) return false;
    if (isAnytime(event)) return true;
    const t = exactTime(event);
    return t !== null && t - now <= PENDING_WINDOW_MS;
  })();
  return block?.mode?.active || pendingEvent ? "waiting for the next key event" : NO_SETUPS_REASON;
}
