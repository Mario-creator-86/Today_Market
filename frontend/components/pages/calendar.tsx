"use client";
import { useEffect, useMemo, useState } from "react";
import { localDay, localHourMinute } from "@/lib/api";
import { levelOf, levelTone } from "@/lib/event-risk";
import { displayPhase, relativeTime } from "@/lib/event-time";
import { cn } from "@/lib/utils";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { NativeSelect } from "@/components/ui/native-select";
import { EmptyState } from "@/components/shared/empty-state";
import { PageHeading } from "@/components/shared/page-heading";
import { QueryState } from "@/components/shared/query-state";
import { SearchToolbar } from "@/components/shared/search-toolbar";
import { StatusBadge } from "@/components/shared/status-badge";
import {
  calendarSearchText,
  consensusView,
  type ImpactFilter,
  isReleased,
  matchesText,
  passesImpact,
  present,
  type ReleaseTiming,
  releaseTiming,
  releaseWindow,
  relevanceText,
  scheduledOf as scheduled,
  sourceView,
  TIMING_LABEL,
  timeBasisText,
  withUnit,
} from "./page-logic";
import { fmt, isNum, tone } from "./page-utils";
import type {
  CalendarItem,
  CalendarScenario,
  Reaction,
  ScenarioImplication,
} from "./types";
import { useSectionData } from "./use-section-data";

type TimingFilter = "ALL" | "UPCOMING" | "RELEASED";

const TIMING_TONE: Record<ReleaseTiming, BadgeVariant> = {
  RELEASED: "positive",
  UPCOMING: "info",
  DUE: "neutral",
  AWAITING: "outline",
};

/** The calendar row's own impact (0-10), the same number the dashboard shows as IMPACT. */
function impactLabel(impact: unknown): string {
  if (isNum(impact)) return `Impact ${fmt(impact, { digits: 1 })}/10`;
  return present(impact) ? `Impact ${String(impact)}` : "Impact unrated";
}

function impactTone(impact: unknown): BadgeVariant {
  if (isNum(impact)) return impact >= 7 ? "negative" : impact >= 4 ? "neutral" : "outline";
  return tone(impact);
}

const DIRECTION: Record<string, { word: string; cls: string }> = {
  "1": { word: "positive", cls: "text-positive" },
  "-1": { word: "negative", cls: "text-destructive" },
  "0": { word: "neutral", cls: "text-muted-foreground" },
};

/** Normalise array or {ASSET: direction|text} implications into one list. */
export function implications(value: CalendarScenario["implications"]): ScenarioImplication[] {
  if (Array.isArray(value)) return value.filter((i) => i && typeof i.asset === "string");
  if (value && typeof value === "object")
    return Object.entries(value).map(([asset, v]) =>
      isNum(v) ? { asset, direction: Math.sign(v) as -1 | 0 | 1 } : { asset, text: String(v) },
    );
  return [];
}

/** Deterministic band membership; needs a numeric actual and at least one bound. */
export function matchesScenario(s: CalendarScenario, actual: unknown): boolean {
  if (!isNum(actual) || (!isNum(s.lower) && !isNum(s.upper))) return false;
  return (!isNum(s.lower) || actual >= s.lower) && (!isNum(s.upper) || actual <= s.upper);
}

function scenarioCondition(s: CalendarScenario, unit: unknown): string {
  if (s.condition) return s.condition;
  const u = typeof unit === "string" && unit.trim() === "%" ? "%" : "";
  if (isNum(s.lower) && isNum(s.upper)) return `${fmt(s.lower, { digits: 3 })}–${fmt(s.upper, { digits: 3 })}${u}`;
  if (isNum(s.upper)) return `≤ ${fmt(s.upper, { digits: 3 })}${u}`;
  if (isNum(s.lower)) return `≥ ${fmt(s.lower, { digits: 3 })}${u}`;
  return s.label ?? "Scenario";
}

function Scenarios({ item }: { item: CalendarItem }) {
  const list = Array.isArray(item.scenarios) ? item.scenarios : [];
  const released = isReleased(item);
  if (!list.length)
    return released ? null : (
      <p className="text-xs text-muted-foreground">
        Scenario analysis not available for this release.
      </p>
    );
  const shown = released ? list.filter((s) => matchesScenario(s, item.actual)) : list;
  if (!shown.length) return null;
  return (
    <div>
      <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {released ? "Realised scenario" : "Scenarios"}
      </h4>
      <ul className="grid gap-2 sm:grid-cols-3">
        {shown.map((s, i) => (
          <li
            key={s.id ?? i}
            className="rounded-md border border-border bg-muted/60 px-3 py-2"
          >
            <strong className="block text-sm tabular-nums">
              {scenarioCondition(s, item.unit)}
            </strong>
            {s.label && s.condition && (
              <span className="block text-xs text-muted-foreground">{s.label}</span>
            )}
            {s.interpretation && <p className="text-xs">{s.interpretation}</p>}
            {implications(s.implications).length > 0 && (
              <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs" aria-label="Asset implications">
                {implications(s.implications).map((imp) => {
                  const d = isNum(imp.direction) ? DIRECTION[String(imp.direction)] : undefined;
                  return (
                    <li key={imp.asset}>
                      <span className="font-semibold">{imp.asset}</span>{" "}
                      <span className={d?.cls}>{imp.text ?? d?.word ?? "Unavailable"}</span>
                    </li>
                  );
                })}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function reactionChange(r: Reaction): string {
  if (!isNum(r.change)) return "Pending";
  return fmt(r.change, { signed: true, suffix: r.unit === "bp" ? " bp" : "%" });
}

function MarketReaction({ item }: { item: CalendarItem }) {
  if (!isReleased(item)) return null;
  const list = (Array.isArray(item.reactions) ? item.reactions : Array.isArray(item.reaction) ? item.reaction : []).filter(
    (r) => r && typeof r.symbol === "string",
  );
  return (
    <div>
      <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        Market reaction
      </h4>
      {list.length ? (
        <ul className="flex flex-wrap gap-2 text-xs">
          {list.map((r, i) => (
            <li
              key={`${r.symbol}-${r.horizon}-${i}`}
              className="flex items-center gap-2 rounded-md border border-border px-2 py-1"
            >
              <span className="font-semibold">{r.symbol}</span>
              <span className="text-muted-foreground">{r.horizon}</span>
              <span className="tabular-nums">{reactionChange(r)}</span>
              {r.status && <StatusBadge value={r.status} variant={tone(r.status)} />}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted-foreground">Market reaction not measured yet.</p>
      )}
    </div>
  );
}

/**
 * Event-awareness line of a row: the backend phase and relevance when it sends
 * them, and how far away a timed upcoming release is (client arithmetic from
 * the scheduled time). Date-only rows never count down.
 */
export function calendarMeta(
  item: CalendarItem,
  now: number,
): { phase: string | null; level: string | null; relevance: string | null; relative: string | null } {
  const at = Date.parse(scheduled(item) ?? "");
  const timed = item.time_known !== false && Number.isFinite(at);
  const phase =
    typeof item.phase === "string" && item.phase.trim()
      ? displayPhase({ time_utc: scheduled(item), time_known: item.time_known ?? undefined, phase: item.phase }, now).label
      : null;
  const level = item.relevance_level || isNum(item.relevance_score) ? levelOf(item.relevance_level, item.relevance_score) : null;
  const relative = timed && !isReleased(item) && at > now ? relativeTime(at - now) : null;
  return { phase, level, relevance: relevanceText(item), relative };
}

function Fact({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] text-muted-foreground">{label}</dt>
      <dd className={cn("m-0 font-semibold tabular-nums", className)}>{value}</dd>
    </div>
  );
}

/** "Source: ..." credit, linked to where the row came from when that is an http(s) URL. */
function SourceCredit({ item }: { item: CalendarItem }) {
  const source = sourceView(item);
  if (!source) return null;
  return source.href ? (
    <a className="source-link" href={source.href} target="_blank" rel="noopener noreferrer">
      {source.text}
    </a>
  ) : (
    <span>{source.text}</span>
  );
}

/** One release card (§15/§31). */
export function CalendarCard({ item, now }: { item: CalendarItem; now: number }) {
  const at = scheduled(item);
  const released = isReleased(item);
  const timing = releaseTiming(item, now);
  const surprise = item.surprise ?? item.raw_surprise;
  const z = item.surprise_z_score ?? item.z_score;
  const meta = calendarMeta(item, now);
  const dateOnly = item.time_known === false;
  const consensus = consensusView(item);
  // The window as a Bucharest time range (or the publisher's day); the time basis without what the window already says.
  const windowView = releaseWindow(item);
  const basis = timeBasisText(item);
  const hasMeta = meta.relative || meta.phase || meta.relevance || windowView || basis || sourceView(item);
  return (
    <article className="calendar-card flex flex-col gap-3 border-t border-border px-[18px] py-4 first:border-t-0">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <time dateTime={at} className="font-semibold tabular-nums text-primary">
          {dateOnly ? "Time TBA" : localHourMinute(at)}
        </time>
        <span className="text-xs text-muted-foreground">
          {present(item.country) ? String(item.country) : "Country unavailable"}
        </span>
        {/* On phones the name gets its own full-width line instead of a squeezed column. */}
        <h3 className="min-w-0 text-[15px] max-sm:order-last max-sm:basis-full sm:flex-1">
          {item.name ?? "Unnamed release"}
        </h3>
        <Badge variant={impactTone(item.impact)}>{impactLabel(item.impact)}</Badge>
        <Badge variant={TIMING_TONE[timing]}>{TIMING_LABEL[timing]}</Badge>
      </header>
      {hasMeta && (
        <p className="calendar-meta">
          {meta.relative && <span className="tabular-nums text-foreground">{meta.relative}</span>}
          {meta.phase && <span>Phase {meta.phase}</span>}
          {meta.relevance && <Badge variant={levelTone(meta.level)}>{meta.relevance}</Badge>}
          {windowView && <span>Release window: {windowView.text}</span>}
          {basis && <span>Time basis: {basis}</span>}
          <SourceCredit item={item} />
        </p>
      )}
      <dl className="m-0 grid grid-cols-2 gap-3 sm:grid-cols-5">
        <Fact
          label={consensus.label}
          value={consensus.value}
          className={consensus.missing ? "text-warning" : undefined}
        />
        <Fact label="Previous" value={withUnit(item.previous, item.unit)} />
        <Fact label="Actual" value={withUnit(item.actual, item.unit, released ? "Unavailable" : "Pending")} />
        <Fact
          label="Surprise"
          value={
            !released
              ? "Pending"
              : isNum(surprise)
                ? fmt(surprise, { signed: true, digits: 3 })
                : "Unavailable"
          }
          className={isNum(surprise) ? (surprise > 0 ? "text-positive" : surprise < 0 ? "text-destructive" : "") : ""}
        />
        <Fact
          label="Surprise z-score"
          value={!released ? "Pending" : isNum(z) ? fmt(z, { signed: true }) : "Not enough data"}
        />
      </dl>
      {consensus.note && (
        <p className="text-[11px] text-muted-foreground">
          {consensus.label}: {consensus.note}
        </p>
      )}
      <Scenarios item={item} />
      <MarketReaction item={item} />
    </article>
  );
}

/** Re-render once a minute so "Upcoming" flips after release time. */
function useMinuteClock(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

/** Market calendar (/calendar): cards grouped by Bucharest day. */
export function CalendarPage() {
  const [search, setSearch] = useState("");
  const [impact, setImpact] = useState<ImpactFilter>("HIGH");
  const [country, setCountry] = useState("ALL");
  const [timing, setTiming] = useState<TimingFilter>("ALL");
  const now = useMinuteClock();
  const q = useSectionData<CalendarItem[]>("calendar");
  // Counts and "no releases" are stated only for data that arrived: never while loading or after a failure.
  const ready = q.data !== undefined;
  const list = useMemo(() => (Array.isArray(q.data) ? q.data : []), [q.data]);
  const countries = useMemo(
    () =>
      Array.from(new Set(list.map((i) => i.country).filter((c): c is string => typeof c === "string" && c !== ""))).sort(),
    [list],
  );
  const visible = list
    .filter((i) => passesImpact(i, impact))
    .filter((i) => country === "ALL" || i.country === country)
    .filter((i) => {
      if (timing === "ALL") return true;
      const state = releaseTiming(i, now);
      // A date-only release inside its window is still to come: it stays under "Upcoming".
      return timing === "RELEASED" ? state === "RELEASED" : state === "UPCOMING" || state === "DUE";
    })
    .filter((i) => matchesText(calendarSearchText(i), search))
    .sort((a, b) => String(scheduled(a)).localeCompare(String(scheduled(b))));
  const days = new Map<string, CalendarItem[]>();
  for (const item of visible) {
    const day = localDay(scheduled(item));
    days.set(day, [...(days.get(day) ?? []), item]);
  }
  return (
    <>
      <PageHeading
        title="Market calendar"
        description="Release times in Europe/Bucharest. High-impact events are shown by default."
      />
      <QueryState loading={q.isLoading} error={q.error} onRetry={() => void q.refetch()} />
      {ready && (
        <>
          <SearchToolbar search={search} onSearch={setSearch} count={visible.length}>
            <NativeSelect
              aria-label="Impact filter"
              value={impact}
              onChange={(e) => setImpact(e.target.value as ImpactFilter)}
            >
              <option value="HIGH">High impact</option>
              <option value="ALL">Medium + high impact</option>
            </NativeSelect>
            <NativeSelect
              aria-label="Country filter"
              value={country}
              onChange={(e) => setCountry(e.target.value)}
            >
              <option value="ALL">All countries</option>
              {countries.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </NativeSelect>
            <NativeSelect
              aria-label="Release filter"
              value={timing}
              onChange={(e) => setTiming(e.target.value as TimingFilter)}
            >
              <option value="ALL">All releases</option>
              <option value="UPCOMING">Upcoming</option>
              <option value="RELEASED">Released</option>
            </NativeSelect>
          </SearchToolbar>
          {visible.length ? (
            <div className="flex flex-col gap-5">
              {Array.from(days, ([day, items]) => (
                <section key={day} className="panel overflow-hidden rounded-lg border border-border bg-card" aria-label={day}>
                  <h2 className="calendar-day">{day}</h2>
                  {items.map((item, i) => (
                    <CalendarCard key={item.id ?? `${scheduled(item)}-${item.name}-${i}`} item={item} now={now} />
                  ))}
                </section>
              ))}
            </div>
          ) : (
            <section className="panel rounded-lg border border-border bg-card">
              <EmptyState
                title={list.length ? "No matching releases" : "No releases in the calendar"}
                detail={
                  list.length
                    ? "Try a wider impact filter, another country or a different search."
                    : "Releases appear when the calendar providers deliver them."
                }
              />
            </section>
          )}
        </>
      )}
    </>
  );
}
