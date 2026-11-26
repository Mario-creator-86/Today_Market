"use client";
import Link from "next/link";
import { ArrowUpRight } from "lucide-react";
import type { MarketEvent } from "@/lib/api";
import { eventHeadline, eventRowAssets, eventRowSources, publishedText } from "@/lib/events";
import { factConfidenceLabel } from "./event-data";
import { EmptyState } from "@/components/shared/empty-state";
import { StatusBadge } from "@/components/shared/status-badge";
import {
  AssetSymbols,
  SYMBOL_TAP_AREA,
  SYMBOL_TAP_GAP,
  useTrackedSymbols,
} from "./asset-link";

/**
 * Events with verification status, time, sources and affected assets. The
 * headline links to the event and its link covers the whole row; tracked
 * asset symbols link to their asset pages. `empty` replaces the no-data
 * message, e.g. when a search filtered every event out.
 */
export function EventList({
  events,
  empty,
}: {
  events: MarketEvent[];
  empty?: { title: string; detail: string };
}) {
  const tracked = useTrackedSymbols();
  if (!events.length)
    return (
      <EmptyState
        title={empty?.title ?? "No verified events yet"}
        detail={
          empty?.detail ??
          "Incoming events will show their verification status, sources and affected assets."
        }
      />
    );
  return (
    <div className="event-list">
      {events.map((e) => {
        const sources = eventRowSources(e);
        const assets = eventRowAssets(e);
        return (
          <article className="event-row relative" key={e.id}>
            <div className="min-w-0">
              <StatusBadge value={e.verification_status} />
              <span className="event-time">
                Published {publishedText(e)}
              </span>
              <h3>
                <Link
                  href={`/events/${encodeURIComponent(e.id)}`}
                  className="after:absolute after:inset-0 after:content-['']"
                >
                  {eventHeadline(e)}
                </Link>
              </h3>
              <p>
                {assets.length ? (
                  // Above the row link. Only the symbol links take clicks (each with a hit area of at least
                  // 24 px, kept apart); the gaps between them still open the event.
                  <span className="pointer-events-none relative z-10">
                    <AssetSymbols
                      symbols={assets}
                      tracked={tracked}
                      linkClassName={SYMBOL_TAP_AREA}
                      separatorClassName={SYMBOL_TAP_GAP}
                    />
                  </span>
                ) : (
                  "Asset impact not established"
                )}
                {` · Fact confidence ${factConfidenceLabel(e)}`}
                {sources.shown.length > 0 &&
                  ` · ${sources.shown.join(", ")}${sources.more ? ` +${sources.more}` : ""}`}
              </p>
            </div>
            <ArrowUpRight size={17} aria-hidden="true" />
          </article>
        );
      })}
    </div>
  );
}
