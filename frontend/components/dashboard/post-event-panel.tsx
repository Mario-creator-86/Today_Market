import { TriangleAlert } from "lucide-react";
import type { EventRisk, PostEvent } from "@/lib/api";
import { number } from "@/lib/api";
import {
  findEvent,
  list,
  livePostEvents,
  REACTION_HORIZONS,
  reactionCell,
  reactionUnitFor,
} from "@/lib/event-risk";
import { displayPhase, eventTimeLabel, subEventLines } from "@/lib/event-time";
import { isNum } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

function Entry({ block, entry, now }: { block: EventRisk; entry: PostEvent; now: number }) {
  const event = findEvent(block, entry.event_id);
  const phase = displayPhase({ ...(event ?? {}), phase: entry.phase ?? event?.phase }, now);
  const subs = event ? subEventLines({ ...event, phase: entry.phase ?? event.phase }, now) : [];
  const reactions = entry.reactions && typeof entry.reactions === "object" ? entry.reactions : {};
  const symbols = Object.keys(reactions);
  const facts = list<string>(entry.summary_facts).filter((f) => typeof f === "string");
  const name = event?.name ?? entry.name ?? entry.event_id;
  return (
    <article className="post-event" aria-label={`${name} reaction`}>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-[15px]">{name}</h3>
        <Badge variant="neutral">{phase.label.toUpperCase()}</Badge>
        {event && <span className="text-xs text-muted-foreground">{eventTimeLabel(event, now)}</span>}
      </div>
      {subs.length > 0 && <p className="post-event-next">{subs[0]}</p>}
      {entry.divergence && (
        <div className="divergence-alert" role="alert">
          <TriangleAlert size={16} aria-hidden="true" />
          <div>
            <strong>MARKET DIVERGENCE</strong>
            <p>
              Measured reactions contradict the expected direction on most exposed assets. Do not act on the
              expected direction automatically: the event may already be priced, or another catalyst may dominate.
            </p>
          </div>
        </div>
      )}
      <p className="post-event-confirmation">
        Market confirmation{" "}
        <b>{isNum(entry.confirmation) ? `${number(entry.confirmation, 0)}/100` : "Not measurable yet"}</b>
        {!isNum(entry.confirmation) && (
          <span className="block text-[11px] text-muted-foreground">
            Needs a known surprise direction (consensus, or the statement classification).
          </span>
        )}
      </p>
      {symbols.length > 0 ? (
        <Table aria-label="Reaction after the event">
          <TableHeader>
            <TableRow>
              <TableHead>Asset</TableHead>
              {REACTION_HORIZONS.map((h) => (
                <TableHead key={h}>T+{h}</TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {symbols.map((symbol) => (
              <TableRow key={symbol}>
                <TableCell className="min-w-0 font-semibold">{symbol}</TableCell>
                {REACTION_HORIZONS.map((h) => {
                  const cell = reactionCell(reactions[symbol]?.[h], reactionUnitFor(entry, symbol));
                  return (
                    <TableCell
                      key={h}
                      className={cn(
                        "min-w-0 font-mono tabular-nums",
                        cell.sign === null
                          ? "text-muted-foreground"
                          : cell.sign > 0
                            ? "text-positive"
                            : cell.sign < 0
                              ? "text-destructive"
                              : "",
                      )}
                    >
                      {cell.text}
                    </TableCell>
                  );
                })}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ) : (
        <p className="text-xs text-muted-foreground">No reaction measured yet.</p>
      )}
      {facts.length > 0 && (
        <ul className="break-list">
          {facts.map((f) => (
            <li key={f}>{f}</li>
          ))}
        </ul>
      )}
    </article>
  );
}

/**
 * §6 POST-EVENT: shown while an event is RELEASED, IN_PROGRESS or POST_EVENT.
 * Measured reactions T+1m…T+1h, market confirmation, and a MARKET DIVERGENCE
 * alert when the measured signs contradict the expected direction.
 */
export function PostEventPanel({ block, now }: { block: EventRisk; now: number }) {
  const entries = livePostEvents(block);
  if (!entries.length) return null;
  return (
    <Card asChild className="post-event-panel">
      <section aria-labelledby="post-event-heading">
        <CardHeader>
          <CardTitle id="post-event-heading">Post-event reaction</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-6">
          {entries.map((entry) => (
            <Entry key={entry.event_id} block={block} entry={entry} now={now} />
          ))}
        </CardContent>
      </section>
    </Card>
  );
}
