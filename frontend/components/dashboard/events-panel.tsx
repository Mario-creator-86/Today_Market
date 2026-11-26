import Link from "next/link";
import { ArrowUpRight } from "lucide-react";
import { localTime, MarketEvent } from "@/lib/api";
import { clampText, publishedText } from "@/lib/events";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import type { ReleasedRow, UpcomingRow } from "./opening";
import { affectedAssets, eventTitle, oneSentence } from "./selectors";

const ARROW: Record<number, string> = { 1: "↑", [-1]: "↓", 0: "→" };
const WORD: Record<number, string> = { 1: "up", [-1]: "down", 0: "no net effect" };

/** Impact chips shown per confirmed event; the rest are counted. */
const IMPACT_CHIPS = 5;
/** "Latest confirmed" lists at most this many lines (releases of today plus news). */
const LATEST_ROWS = 4;
/** Longest headline of a confirmed event (one line; the full fact is its tooltip and on the event page). */
const HEADLINE_MAX = 96;

/** The event's verified fact as a one-line headline (its first sentence); the topic name only without a fact. */
function headline(event: MarketEvent): { text: string; full: string } {
  const fact = typeof event.summary_fact === "string" ? oneSentence(event.summary_fact) : "";
  if (!fact) {
    const title = eventTitle(event);
    return { text: title.text, full: title.full };
  }
  return { text: clampText(fact, HEADLINE_MAX), full: fact };
}

function SubHead({ id, title, href, link }: { id: string; title: string; href: string; link: string }) {
  return (
    <div className="events-subhead">
      <h3 id={id}>{title}</h3>
      <Link href={href} className="flex items-center gap-[5px] text-[11px] text-primary">
        {link} <ArrowUpRight size={13} aria-hidden="true" />
      </Link>
    </div>
  );
}

/** A scheduled event released today: when, the event, what was published (two lines at most), the assets its class exposes. */
function Released({ row }: { row: ReleasedRow }) {
  const body = (
    <>
      {row.iso ? <time dateTime={row.iso}>{localTime(row.iso)}</time> : <span className="events-when">Today</span>}
      <span className="events-name" title={row.name}>
        <b className="events-released">Released</b> {row.name}
      </span>
      {row.summary && (
        <span className="events-published" title={row.summary}>
          {row.summary}
        </span>
      )}
      {row.assets.length > 0 && (
        <span className="events-assets">
          {row.assets.slice(0, IMPACT_CHIPS).map((symbol) => (
            <span key={symbol}>{symbol}</span>
          ))}
          {row.assets.length > IMPACT_CHIPS && <span>+{row.assets.length - IMPACT_CHIPS}</span>}
        </span>
      )}
    </>
  );
  return (
    <li data-released={row.key}>
      {row.newsId ? (
        <Link href={`/events/${encodeURIComponent(row.newsId)}`} className="events-row events-news">
          {body}
        </Link>
      ) : (
        <div className="events-row events-news">{body}</div>
      )}
    </li>
  );
}

/**
 * EVENTS, the timeline next to the key event: "Coming up" lists the other
 * scheduled events from now on, soonest first (time, name, calendar impact)
 * and "Latest confirmed" first the scheduled events released today with what
 * was published, then the newest confirmed facts with an identified asset
 * impact (time, one-line headline, the affected assets with their expected
 * direction). The key event itself is not listed again, and a confirmed fact
 * that is the document of a released event is shown with that event only.
 * Each list says in one line when it has nothing to show.
 */
export function EventsPanel({
  upcoming,
  latest,
  released = [],
  keyEventShown = false,
}: {
  upcoming: UpcomingRow[];
  latest: MarketEvent[];
  released?: ReleasedRow[];
  /** The next key event has its own card beside this panel: an empty list then says "no other" events. */
  keyEventShown?: boolean;
}) {
  const documents = new Set(released.flatMap((row) => (row.newsId ? [row.newsId] : [])));
  // One list of at most LATEST_ROWS lines: the releases first, then the newest news.
  const news = latest.filter((e) => !documents.has(e.id)).slice(0, Math.max(0, LATEST_ROWS - released.length));
  return (
    <Card asChild className="events-panel">
      <section aria-labelledby="events-heading">
        <CardHeader>
          <CardTitle id="events-heading">Events</CardTitle>
        </CardHeader>
        <div className="events-block">
          <SubHead id="events-upcoming" title="Coming up" href="/calendar" link="Calendar" />
          {upcoming.length ? (
            <ol aria-labelledby="events-upcoming">
              {upcoming.map((row) => (
                <li key={row.key} className="events-row">
                  {row.iso ? <time dateTime={row.iso}>{row.when}</time> : <span className="events-when">{row.when}</span>}
                  <span className="events-name" title={row.name}>
                    {row.name}
                  </span>
                  {row.impact && (
                    <span className="events-impact" title="Calendar impact out of 10">
                      {row.impact}
                    </span>
                  )}
                </li>
              ))}
            </ol>
          ) : (
            <p className="events-empty">
              {keyEventShown ? "No other scheduled events in this window." : "No scheduled events in this window."}
            </p>
          )}
        </div>
        <div className="events-block">
          <SubHead id="events-latest" title="Latest confirmed" href="/events" link="All events" />
          {released.length + news.length ? (
            <ol aria-labelledby="events-latest">
              {released.map((row) => (
                <Released key={row.key} row={row} />
              ))}
              {news.map((e) => {
                const title = headline(e);
                const affected = affectedAssets(e).filter((a) => a.direction !== 0);
                return (
                  <li key={e.id}>
                    <Link href={`/events/${encodeURIComponent(e.id)}`} className="events-row events-news">
                      <time dateTime={e.event_time || e.first_seen_utc}>{publishedText(e)}</time>
                      <span className="events-name" title={title.full}>
                        {title.text}
                      </span>
                      <span className="events-assets">
                        {affected.slice(0, IMPACT_CHIPS).map((a) => (
                          <span key={a.symbol}>
                            {a.symbol}
                            {a.direction !== null && (
                              <>
                                <b aria-hidden="true">{ARROW[a.direction]}</b>
                                <span className="sr-only"> {WORD[a.direction]}</span>
                              </>
                            )}
                          </span>
                        ))}
                        {affected.length > IMPACT_CHIPS && <span>+{affected.length - IMPACT_CHIPS}</span>}
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ol>
          ) : (
            <p className="events-empty">No confirmed news with an identified asset impact yet.</p>
          )}
        </div>
      </section>
    </Card>
  );
}
