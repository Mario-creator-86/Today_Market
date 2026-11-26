import Link from "next/link";
import type { MarketEvent } from "@/lib/api";
import { eventHeadline, publishedText } from "@/lib/events";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/shared/empty-state";
import { factLabel, factTone, sourceNames } from "./selectors";

/**
 * §27 Breaking / verified live stream: newest events of every verification
 * status, each labelled CONFIRMED / PARTIAL / UNVERIFIED / CONTRADICTED with
 * its source. Updates with each WebSocket snapshot or poll.
 */
export function BreakingStream({ events }: { events: MarketEvent[] }) {
  if (!events.length)
    return (
      <EmptyState
        title="Stream quiet"
        detail="New items appear here as soon as they are ingested, with their verification label and source."
      />
    );
  return (
    <ul
      className="m-0 list-none p-0"
      aria-live="polite"
      aria-relevant="additions"
    >
      {events.map((e) => {
        const label = factLabel(e.verification_status);
        const sources = sourceNames(e);
        return (
          <li key={e.id} className="border-b border-border last:border-b-0">
            <Link
              href={`/events/${encodeURIComponent(e.id)}`}
              className="block px-[21px] py-3.5 hover:bg-secondary/60"
            >
              <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
                <Badge variant={factTone(label)}>{label}</Badge>
                <span>{publishedText(e)}</span>
              </div>
              <p className="mt-1.5 text-[13px] leading-snug">
                {eventHeadline(e)}
              </p>
              <p className="mt-1 text-[11px] text-muted-foreground">
                Source:{" "}
                {sources.length
                  ? sources.slice(0, 3).join(", ")
                  : "Not recorded"}
                {sources.length > 3 && ` +${sources.length - 3} more`}
              </p>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
