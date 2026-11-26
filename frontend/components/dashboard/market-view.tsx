import Link from "next/link";
import type { BreakItem, EventRisk, MarketViewItem } from "@/lib/api";
import {
  breakText,
  closedMarkets,
  eventDataStale,
  list,
  liveMarketsNote,
  riskBadge,
  shortName,
  viewItems,
} from "@/lib/event-risk";
import { eventTimeLabel } from "@/lib/event-time";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { groupNotes, ownFacts } from "./opening";

type Group = {
  key: "favored" | "pressured" | "non_confirming";
  title: string;
};

const GROUPS: Group[] = [
  { key: "favored", title: "Favored now" },
  { key: "pressured", title: "Under pressure" },
  { key: "non_confirming", title: "Not confirming" },
];

/** CSS key of an event-risk level ("VERY HIGH" → "very-high"); "unrated" without one. */
function levelKey(level: string | null | undefined): string {
  return level ? level.toLowerCase().replaceAll(" ", "-") : "unrated";
}

/**
 * One asset of a positioning group: the symbol (a link to its page) and a dot
 * in the colour of its event-risk level. The level, the event behind it and
 * the asset's reasons the group note does not state are the chip's tooltip;
 * the level is also read out.
 */
function AssetChip({
  item,
  group,
  block,
  now,
}: {
  item: MarketViewItem;
  /** The asset's whole group: what the group's note states is not repeated in the tooltip. */
  group: MarketViewItem[];
  block: EventRisk;
  now: number;
}) {
  const risk = riskBadge(block, item.symbol, item.event_risk, item.event_id);
  const title = [
    risk ? `Event risk ${risk.level ?? "unrated"}` : null,
    risk?.event ? `${shortName(risk.event)} · ${eventTimeLabel(risk.event, now)}` : null,
    ...ownFacts(item, group),
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <li className="positioning-chip" title={title || undefined}>
      <Link href={`/assets/${encodeURIComponent(item.symbol)}`}>{item.symbol}</Link>
      {risk && (
        <>
          <i className="risk-dot" data-level={levelKey(risk.level)} aria-hidden="true" />
          <span className="sr-only">Event risk {risk.level ?? "unrated"}</span>
        </>
      )}
    </li>
  );
}

/**
 * POSITIONING (§6 favored now / under pressure / not confirming): the backend
 * lists (at most five each, never forced) as rows of asset chips. What a
 * group's assets share is written once under the group, never per asset; an
 * empty group says "none". Below: what can change this view on one line, and
 * the closed-markets note (the only place the opening screen states it).
 */
export function Positioning({ block, now }: { block: EventRisk; now: number }) {
  const groups = GROUPS.map((g) => ({ ...g, items: viewItems<MarketViewItem>(block[g.key]) }));
  const closed = closedMarkets(block.markets_open);
  const live = liveMarketsNote(block.markets_open);
  const breaks = list<BreakItem>(block.what_can_break)
    .map(breakText)
    .filter((t): t is string => !!t);
  const rated = groups.some((g) => g.items.some((item) => riskBadge(block, item.symbol, item.event_risk, item.event_id)));
  return (
    <Card asChild className="positioning">
      <section aria-labelledby="positioning-heading">
        <CardHeader className="flex-wrap">
          <CardTitle id="positioning-heading">Positioning</CardTitle>
          {rated && (
            <span className="positioning-legend" aria-hidden="true">
              Event risk
              <i className="risk-dot" data-level="critical" /> critical
              <i className="risk-dot" data-level="high" /> high
              <i className="risk-dot" data-level="moderate" /> moderate
            </span>
          )}
        </CardHeader>
        <CardContent className="grid gap-3">
          {eventDataStale(block, now) && (
            <p className="text-xs text-warning" role="status">
              This view uses event data that may be outdated. Wait for a fresh update before relying on it.
            </p>
          )}
          <div className="positioning-groups">
            {groups.map((g) => {
              const notes = groupNotes(g.items);
              const awaiting = g.items.filter((item) => item.awaiting_reopen).map((item) => item.symbol);
              return (
                <section key={g.key} aria-label={g.title} className="positioning-group" data-group={g.key}>
                  <h3 className="event-subhead">{g.title}</h3>
                  {g.items.length ? (
                    <div className="min-w-0">
                      <ul className="positioning-chips">
                        {g.items.map((item) => (
                          <AssetChip key={item.symbol} item={item} group={g.items} block={block} now={now} />
                        ))}
                      </ul>
                      {notes.length > 0 && <p className="positioning-note">{notes.join(" · ")}</p>}
                      {awaiting.length > 0 && (
                        <p className="positioning-note">AWAITING REOPEN: {awaiting.join(", ")}</p>
                      )}
                    </div>
                  ) : (
                    <p className="positioning-none">none</p>
                  )}
                </section>
              );
            })}
          </div>
          {breaks.length > 0 && (
            <p className="positioning-break">
              <b>What can change this view:</b> {breaks.join(" · ")}
            </p>
          )}
          {closed.length > 0 && (
            <p className="text-xs text-warning">
              {closed.join(", ")} closed: their event implications cannot be confirmed until they reopen.
              {live && ` ${live}`}
            </p>
          )}
        </CardContent>
      </section>
    </Card>
  );
}
