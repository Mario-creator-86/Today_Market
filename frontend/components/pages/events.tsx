"use client";
import { useMemo, useState } from "react";
import {
  eventCountText,
  eventSearchFields,
  sortByPublication,
} from "@/lib/events";
import { humanize, matchesSearch } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/native-select";
import { PageHeading } from "@/components/shared/page-heading";
import { QueryState } from "@/components/shared/query-state";
import { SearchToolbar } from "@/components/shared/search-toolbar";
import { EventList } from "@/components/event/event-list";
import { useEventList } from "@/components/event/use-event-list";

/** Events rendered per page of the list ("Show more" adds another page). */
const PAGE = 50;

/** Event intelligence list (/events). */
export function EventsPage() {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("ALL");
  const [limit, setLimit] = useState(PAGE);
  const q = useEventList();
  const loaded = Array.isArray(q.data);
  const list = useMemo(
    () => sortByPublication(Array.isArray(q.data) ? q.data : []),
    [q.data],
  );
  const statuses = useMemo(
    () =>
      Array.from(
        new Set(list.map((e) => e.verification_status).filter(Boolean)),
      ).sort(),
    [list],
  );
  const visible = list
    .filter((e) => status === "ALL" || e.verification_status === status)
    .filter((e) => matchesSearch(e, search, eventSearchFields));
  const shown = visible.slice(0, limit);
  const filtered = status !== "ALL" || search.trim() !== "";
  const query = search.trim();
  return (
    <>
      <PageHeading
        title="Event intelligence"
        description="Facts first. Interpretation and market reaction remain separate."
      />
      <QueryState loading={q.isLoading} error={q.error} />
      {loaded && (
        <>
          <SearchToolbar
            search={search}
            onSearch={(value) => {
              setSearch(value);
              setLimit(PAGE);
            }}
            // One count on the page: the Events panel header states it ("330 events, showing 50").
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
          <Card asChild className="panel">
            <section aria-labelledby="events-list-heading">
              <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-border px-[21px] py-3">
                <h2 id="events-list-heading" className="text-sm font-semibold">
                  Events
                </h2>
                <p
                  className="text-xs text-muted-foreground"
                  role="status"
                  aria-live="polite"
                >
                  {eventCountText({
                    total: list.length,
                    matching: visible.length,
                    shown: shown.length,
                    filtered,
                  })}
                </p>
              </div>
              <EventList
                events={shown}
                empty={
                  filtered && list.length
                    ? {
                        title: query
                          ? `No events match “${query}”`
                          : "No events with this verification result",
                        detail:
                          "Clear the search or choose another verification result to see the other events.",
                      }
                    : undefined
                }
              />
              {visible.length > shown.length && (
                <div className="border-t border-border px-[21px] py-3">
                  <Button size="sm" onClick={() => setLimit(limit + PAGE)}>
                    Show more events ({visible.length - shown.length} remaining)
                  </Button>
                </div>
              )}
            </section>
          </Card>
        </>
      )}
    </>
  );
}
