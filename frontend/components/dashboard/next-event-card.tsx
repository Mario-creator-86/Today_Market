import type { EventObject, EventRisk, Interpretation } from "@/lib/api";
import {
  DATA_INCOMPLETE,
  dominantBeyondNext,
  expectation,
  exposureList,
  exposureWord,
  impactView,
  incompleteFor,
  levelOf,
  levelTone,
  list,
  NO_CONSENSUS,
  relevanceText,
  UNKNOWN,
  type Expectation,
} from "@/lib/event-risk";
import { displayPhase, eventTimeLabel, LIVE_PHASES, subEventLines } from "@/lib/event-time";
import { isNum } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EventCountdown, EventWhen } from "@/components/shared/event-countdown";
import { ImpactValue } from "./impact-value";
import { eventWhy, expectationText, otherIncompleteReasons, publishedSummary } from "./opening";

/** Reported expectation values with their label and source (under the card's details). */
function ExpectationBlock({ title, value }: { title: string; value: Expectation }) {
  return (
    <div className="event-expectation">
      <h4>{title}</h4>
      {value.label && <p className="expectation-label">{value.label}</p>}
      {value.lines.length > 0 && (
        <dl>
          {value.lines.map((l) => (
            <div key={l.label}>
              <dt>{l.label}</dt>
              <dd>{l.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {value.text && <p>{value.text}</p>}
      {value.caption && <small>{value.caption}</small>}
    </div>
  );
}

/** The event's relevance level as one chip ("VERY HIGH"); its score out of 100 is in the card's details. */
function LevelChip({ event }: { event: EventObject }) {
  const level = levelOf(event.relevance_level, event.relevance_score);
  return level ? (
    <Badge variant={levelTone(level)} title="Relevance level of this event">
      {level}
    </Badge>
  ) : null;
}

/**
 * The dominant event when it is not the next one (§6: "what event is next /
 * how important" must show the day's real risk on the first screen).
 */
function DominantLine({ block, event, now }: { block: EventRisk; event: EventObject; now: number }) {
  const impact = impactView(event, block.mode);
  return (
    <div className="dominant-line" role="note" aria-label="Dominant event">
      <Badge variant="negative">DOMINANT EVENT</Badge>
      <b>{event.name}</b>
      <span>
        <EventWhen event={event} now={now} />
      </span>
      {impact.text && (
        <span>
          IMPACT <ImpactValue view={impact} />
        </span>
      )}
      <LevelChip event={event} />
    </div>
  );
}

/**
 * EVENTS, the next key event: name, Bucharest time and live countdown (from
 * time_utc), impact, relevance level and phase as chips, ONE sentence on why
 * it matters, ONE muted line of expectations ("Consensus: not available ·
 * Market pricing: unknown", or the reported values; EVENT DATA INCOMPLETE is
 * flagged on that line) and the most exposed assets once, as chips. After
 * the release, one line states what was published when the service holds it.
 * Scores,
 * the time basis and the source of the schedule are under "Event timing and
 * relevance details". A dominant event that is not the next one is surfaced
 * above it.
 */
export function NextEventCard({ block, interpretation, now }: { block: EventRisk; interpretation?: Interpretation | null; now: number }) {
  const event: EventObject | null =
    block.next_event && typeof block.next_event.event_id === "string" ? block.next_event : null;
  const dominant = dominantBeyondNext(block);
  return (
    <Card asChild className="next-event">
      <section aria-labelledby="next-event-heading">
        <CardHeader>
          <CardTitle id="next-event-heading">Next key event</CardTitle>
          {event && block.dominant_event?.event_id === event.event_id && (
            <Badge variant="negative">DOMINANT EVENT</Badge>
          )}
        </CardHeader>
        {dominant && (
          <CardContent className="pb-0">
            <DominantLine block={block} event={dominant} now={now} />
          </CardContent>
        )}
        {event ? (
          <NextEventBody block={block} event={event} why={eventWhy(event, interpretation)} now={now} />
        ) : (
          <CardContent>
            <p className="text-sm text-muted-foreground">
              No upcoming market-moving event in this window.
            </p>
          </CardContent>
        )}
      </section>
    </Card>
  );
}

function NextEventBody({ block, event, why, now }: { block: EventRisk; event: EventObject; why: string; now: number }) {
  const phase = displayPhase(event, now);
  const live = LIVE_PHASES.has(phase.phase);
  const impact = impactView(event, block.mode);
  const exposed = exposureList(event);
  const subs = subEventLines(event, now);
  const incomplete = incompleteFor(block, event);
  const consensus = expectation(event.consensus, NO_CONSENSUS);
  const pricing = expectation(event.market_pricing, UNKNOWN);
  const otherReasons = otherIncompleteReasons(incomplete.reasons);
  const relevance = relevanceText(event);
  // Once the event is released: what was published, as the service holds it (its official document or values).
  const published = publishedSummary(block, event);
  const known = list<unknown>(event.known_information).filter(
    (line): line is string => typeof line === "string" && !!line.trim() && !line.startsWith("Time basis:"),
  );
  const reported = [
    { title: "Market expectation", value: consensus },
    { title: "Current market pricing", value: pricing },
  ].filter((x) => !x.value.missing);
  const notes = incomplete.flagged ? [] : incomplete.reasons;
  const hasDetails =
    !!event.time_basis || isNum(event.dominance_score) || !!relevance || known.length > 0 || reported.length > 0 || notes.length > 0;
  return (
    <CardContent className="grid gap-3">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <h3 className="next-event-name">{event.name}</h3>
          <p className="next-event-time">{eventTimeLabel(event, now)}</p>
        </div>
        {!live && <EventCountdown event={event} now={now} className="next-event-countdown" />}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="next-event-impact">
          IMPACT <ImpactValue view={impact} strong />
        </span>
        <LevelChip event={event} />
        <Badge variant={live || phase.due ? "neutral" : "outline"}>{phase.label}</Badge>
      </div>
      {subs.length > 0 && (
        <ul className="next-event-subs" aria-label="Event phases">
          {subs.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      )}
      {published && (
        <p className="next-event-published">
          <b>Published:</b> {published}
        </p>
      )}
      {why && <p className="next-event-explanation">{why}</p>}
      <p className="event-expect">
        {incomplete.flagged && <strong>{DATA_INCOMPLETE}</strong>}
        <span data-missing={consensus.missing ? "true" : undefined}>
          {expectationText("Consensus", consensus, "not available")}
        </span>
        {" · "}
        <span data-missing={pricing.missing ? "true" : undefined}>
          {expectationText("Market pricing", pricing, "unknown")}
        </span>
        {incomplete.flagged && otherReasons.length > 0 && ` · ${otherReasons.join(" · ")}`}
      </p>
      <div>
        <h4 className="event-subhead">Most exposed</h4>
        {exposed.length ? (
          <ul className="exposure-chips" aria-label="Most exposed assets">
            {exposed.map((a) => (
              <li key={a.symbol}>
                <b>{a.symbol}</b>
                {exposureWord(a.level) && <span>{exposureWord(a.level)}</span>}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">Exposure not mapped for this event.</p>
        )}
      </div>
      {hasDetails && (
        <details className="view-details">
          <summary>Event timing and relevance details</summary>
          <div className="mt-2 grid gap-1.5 text-xs text-muted-foreground">
            {(relevance || isNum(event.dominance_score)) && (
              <p>
                {[
                  relevance ? `Relevance ${relevance}` : null,
                  isNum(event.dominance_score) ? `Dominance ${Math.round(event.dominance_score)}/100` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
            )}
            {event.time_basis && <p>Time basis: {event.time_basis}</p>}
            {known.map((line) => (
              <p key={line}>{line}</p>
            ))}
            {notes.length > 0 && <p className="event-data-notes">Data notes: {notes.join(" · ")}</p>}
            {reported.length > 0 && (
              <div className="grid grid-cols-2 gap-3 pt-1">
                {reported.map((x) => (
                  <ExpectationBlock key={x.title} title={x.title} value={x.value} />
                ))}
              </div>
            )}
          </div>
        </details>
      )}
    </CardContent>
  );
}
