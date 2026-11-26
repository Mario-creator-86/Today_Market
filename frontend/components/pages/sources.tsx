"use client";
import { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api, localTime, MarketEvent, safeUrl } from "@/lib/api";
import {
  eventCountText,
  eventHeadline,
  evidenceCopyNote,
  evidenceSummary,
  groupEvidence,
  otherPublication,
  otherPublicationText,
  sortByPublication,
  sourceHome,
  type EvidenceGroup,
} from "@/lib/events";
import { humanize, matchesSearch } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";
import { EmptyState } from "@/components/shared/empty-state";
import { PageHeading } from "@/components/shared/page-heading";
import { Panel } from "@/components/shared/panel";
import { QueryState } from "@/components/shared/query-state";
import { SearchToolbar } from "@/components/shared/search-toolbar";
import { StatusBadge } from "@/components/shared/status-badge";
import { factConfidenceLabel, isContradicting } from "@/components/event/event-data";
import { EVENT_LIST_REFRESH_MS, useEventList } from "@/components/event/use-event-list";
import { Column, DataTable, fmt, isNum, tone, yesNo } from "./page-utils";
import type { EvidenceRow, SourceRecord } from "./types";

const PAGE = 25;

/**
 * A registry row as GET /api/sources returns it: config fields, the source's
 * homepage (null when none is known) and the status of the provider that
 * delivers it (null when no provider reports one).
 */
export type RegistrySource = SourceRecord & { homepage?: string | null };

/** "Tier 1" when the registry provides a tier; never guessed from weight. */
export function tierLabel(source: SourceRecord | undefined): string {
  return isNum(source?.tier) ? `Tier ${source!.tier}` : "Tier unavailable";
}

/** Text a registry row shows, for search: the id only where it stands in for a missing name. */
function sourceSearchFields(s: RegistrySource): unknown[] {
  return [
    s.name ?? s.id,
    sourceHome(s)?.label ?? s.domain,
    s.kind ? humanize(s.kind) : null,
    tierLabel(s),
    s.independence_group,
    s.status ? humanize(s.status) : null,
  ];
}

/** Source registry table, shared by /sources and Settings > Sources. */
export function SourceRegistryTable({
  sources,
  empty,
}: {
  sources: RegistrySource[];
  /** Replaces the no-sources message, e.g. when a search filtered every source out. */
  empty?: React.ReactNode;
}) {
  const words = "wrap-break-word";
  const columns: Column<RegistrySource>[] = [
    { key: "name", label: "Source", className: words, render: (s) => <strong>{s.name ?? s.id}</strong> },
    {
      key: "domain",
      label: "Site",
      className: words,
      render: (s) => {
        const home = sourceHome(s);
        const feed = safeUrl(s.rss);
        return (
          <span className="flex flex-col items-start gap-1">
            {home ? (
              <a className="source-link" href={home.href} target="_blank" rel="noopener noreferrer">
                {home.label}
              </a>
            ) : (
              // No known homepage: the domain is only the host ingestion matches, never a link.
              (s.domain ?? "Unavailable")
            )}
            {feed && (
              <a
                className="source-link text-[11px] text-muted-foreground"
                href={feed}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={`${s.name ?? s.id} feed`}
              >
                Feed
              </a>
            )}
          </span>
        );
      },
    },
    {
      key: "status",
      label: "Status",
      className: words,
      render: (s) =>
        s.status ? (
          <span className="flex flex-col items-start gap-1">
            <StatusBadge value={s.status} variant={tone(s.status)} />
            {s.last_success && (
              <small className="text-muted-foreground">
                Last success {localTime(s.last_success)}
              </small>
            )}
          </span>
        ) : (
          // No provider reports on this source: that is not an outage.
          <span className="text-muted-foreground">No provider status</span>
        ),
    },
    { key: "kind", label: "Kind", className: words, render: (s) => (s.kind ? humanize(s.kind) : "Unavailable") },
    { key: "tier", label: "Trust tier", className: words, render: (s) => tierLabel(s) },
    { key: "weight", label: "Trust weight", numeric: true, render: (s) => fmt(s.trust_weight) },
    { key: "official", label: "Official", render: (s) => yesNo(s.official) },
    { key: "group", label: "Independence group", className: words, render: (s) => s.independence_group ?? "Unavailable" },
  ];
  const sorted = [...sources].sort(
    (a, b) =>
      (isNum(a.tier) ? a.tier : 99) - (isNum(b.tier) ? b.tier : 99) ||
      String(a.name ?? a.id).localeCompare(String(b.name ?? b.id)),
  );
  return (
    <DataTable
      rows={sorted}
      columns={columns}
      rowKey={(s, i) => s.id ?? String(i)}
      caption="Source registry"
      empty={empty ?? <EmptyState title="No sources registered" detail="The source registry is loaded from versioned configuration." />}
    />
  );
}

/** Evidence documents of an event, duplicate copies of one address collapsed (same rule as the event page). */
function evidenceGroups(event: MarketEvent): EvidenceGroup[] {
  return groupEvidence(Array.isArray(event.evidence) ? event.evidence : []);
}

function sourceName(e: EvidenceRow): string {
  return e.source ?? e.source_id ?? "Unknown source";
}

/** Text an event block shows, for search: headline, status and the evidence rows' source names. */
function eventVerificationFields(event: MarketEvent): unknown[] {
  return [
    eventHeadline(event),
    event.verification_status ? humanize(event.verification_status) : null,
    evidenceGroups(event).map((g) => sourceName(g.item as EvidenceRow)),
  ];
}

function EventVerification({
  event,
  registry,
}: {
  event: MarketEvent;
  registry: Map<string, SourceRecord>;
}) {
  const rows = evidenceGroups(event);
  const counts = evidenceSummary(rows);
  const headline = eventHeadline(event);
  const doc = (g: EvidenceGroup) => g.item as EvidenceRow;
  const columns: Column<EvidenceGroup>[] = [
    {
      key: "source",
      label: "Source",
      className: "wrap-break-word",
      render: (g) => {
        const e = doc(g);
        const href = safeUrl(e.url);
        const name = sourceName(e);
        const note = evidenceCopyNote(g);
        return (
          <span className="flex flex-col items-start gap-1">
            {href ? (
              <a className="source-link" href={href} target="_blank" rel="noopener noreferrer">
                {name}
              </a>
            ) : (
              name
            )}
            {note && <small className="text-muted-foreground">{note}</small>}
          </span>
        );
      },
    },
    {
      key: "published",
      label: "Published",
      className: "wrap-break-word",
      render: (g) => {
        const gap = otherPublication(g.item, event);
        return (
          <span className="flex flex-col items-start gap-1">
            {localTime(doc(g).published_at)}
            {gap && <small className="text-warning">{otherPublicationText(gap)}</small>}
          </span>
        );
      },
    },
    { key: "ingested", label: "Ingested", render: (g) => localTime(doc(g).ingested_at) },
    {
      key: "tier",
      label: "Trust tier",
      className: "wrap-break-word",
      render: (g) => {
        const e = doc(g);
        const s = e.source_id ? registry.get(e.source_id) : undefined;
        return `${tierLabel(s)}${isNum(e.weight) ? ` · weight ${fmt(e.weight)}` : ""}${e.official ? " · official" : ""}`;
      },
    },
    {
      key: "result",
      label: "Result",
      render: (g) => {
        const e = doc(g);
        const against = isContradicting(g.item);
        return (
          <span className="flex flex-wrap gap-1">
            <StatusBadge value={against ? "CONTRADICTS" : "SUPPORTS"} variant={against ? "negative" : "positive"} />
            {e.discovery_only && <StatusBadge value="DISCOVERY ONLY" variant="outline" />}
          </span>
        );
      },
    },
  ];
  return (
    <article className="border-t border-border first:border-t-0" aria-label={headline || "Untitled event"}>
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 px-[18px] pt-4 pb-2">
        {/* Full width on phones so the headline never shrinks to one word per line beside the badge. */}
        <h3 className="min-w-0 basis-full text-[15px] sm:basis-0 sm:flex-1">
          <Link href={`/events/${encodeURIComponent(event.id)}`} className="hover:underline">
            {headline || "Untitled event"}
          </Link>
        </h3>
        <StatusBadge value={event.verification_status} />
        <span className="text-xs text-muted-foreground">
          Fact confidence {factConfidenceLabel(event)}
        </span>
      </header>
      <p className="px-[18px] pb-2 text-xs text-muted-foreground">
        {counts.supporting} supporting · {counts.contradicting} contradicting ·{" "}
        {counts.independentGroups} independent source group{counts.independentGroups === 1 ? "" : "s"}
        {counts.duplicates > 0 &&
          ` (${counts.duplicates} duplicate cop${counts.duplicates === 1 ? "y" : "ies"} of the same address not counted)`}{" "}
        · first seen{" "}
        {localTime(event.first_seen_utc)} (Bucharest)
      </p>
      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(g, i) => String(g.item.id ?? i)}
        caption={`Evidence for ${headline || "untitled event"}`}
        empty={
          <p className="px-[18px] pb-4 text-xs text-muted-foreground">
            No evidence recorded; this event cannot be treated as verified.
          </p>
        }
      />
    </article>
  );
}

/** Sources & verification (/sources, §32): per-event evidence and the source registry. */
export function SourcesPage() {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("ALL");
  const [limit, setLimit] = useState(PAGE);
  const sourcesQ = useQuery({
    queryKey: ["sources"],
    queryFn: () => api<RegistrySource[]>("/sources"),
    refetchInterval: EVENT_LIST_REFRESH_MS,
  });
  const eventsQ = useEventList();
  const sources = useMemo(() => (Array.isArray(sourcesQ.data) ? sourcesQ.data : []), [sourcesQ.data]);
  const events = useMemo(() => sortByPublication(Array.isArray(eventsQ.data) ? eventsQ.data : []), [eventsQ.data]);
  const registry = useMemo(() => new Map(sources.map((s) => [s.id, s])), [sources]);
  const statuses = useMemo(
    () => Array.from(new Set(events.map((e) => e.verification_status).filter(Boolean))).sort(),
    [events],
  );
  const query = search.trim();
  const filtered = status !== "ALL" || query !== "";
  const visible = events
    .filter((e) => status === "ALL" || e.verification_status === status)
    .filter((e) => matchesSearch(e, search, eventVerificationFields));
  const shown = visible.slice(0, limit);
  const matchingSources = sources.filter((s) => matchesSearch(s, search, sourceSearchFields));
  const eventsLoaded = Array.isArray(eventsQ.data);
  const sourcesLoaded = Array.isArray(sourcesQ.data);
  return (
    <>
      <PageHeading
        title="Sources & verification"
        description="Every event with its sources, timing, trust tier and verification result. Uncertainty is never hidden."
      />
      <QueryState loading={eventsQ.isLoading || sourcesQ.isLoading} error={eventsQ.error ?? sourcesQ.error} />
      {(eventsLoaded || sourcesLoaded) && (
        <SearchToolbar
          search={search}
          onSearch={(value) => {
            setSearch(value);
            setLimit(PAGE);
          }}
          // One count on the page: the Event verification panel states it ("330 events, showing 25").
          count={null}
        >
          <NativeSelect
            aria-label="Verification filter"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setLimit(PAGE);
            }}
          >
            <option value="ALL">All verification results</option>
            {statuses.map((s) => (
              <option key={s} value={s}>
                {humanize(s)}
              </option>
            ))}
          </NativeSelect>
        </SearchToolbar>
      )}
      <div className="flex flex-col gap-5">
        {eventsLoaded && (
          <Panel
            title="Event verification"
            action={
              <span className="text-[11px] text-muted-foreground" role="status" aria-live="polite">
                {eventCountText({ total: events.length, matching: visible.length, shown: shown.length, filtered })}
              </span>
            }
          >
            {shown.length ? (
              <>
                {shown.map((event) => (
                  <EventVerification key={event.id} event={event} registry={registry} />
                ))}
                {visible.length > shown.length && (
                  <div className="border-t border-border px-[18px] py-3">
                    <Button size="sm" onClick={() => setLimit(limit + PAGE)}>
                      Show more events ({visible.length - shown.length} remaining)
                    </Button>
                  </div>
                )}
              </>
            ) : filtered && events.length ? (
              <EmptyState
                title={query ? `No events match “${query}”` : "No events with this verification result"}
                detail="Clear the search or choose another verification result to see the other events."
              />
            ) : (
              <EmptyState title="No events to verify" detail="Events appear once sources deliver documents." />
            )}
          </Panel>
        )}
        {sourcesLoaded && (
          <Panel title="Source registry">
            <SourceRegistryTable
              sources={matchingSources}
              empty={
                query && sources.length ? (
                  <EmptyState
                    title={`No sources match “${query}”`}
                    detail={`The registry has ${sources.length} source${sources.length === 1 ? "" : "s"}; clear the search to see them.`}
                  />
                ) : undefined
              }
            />
          </Panel>
        )}
      </div>
    </>
  );
}
