"use client";
import { useQuery } from "@tanstack/react-query";
import { api, RecordData } from "@/lib/api";

/**
 * Background refresh while a section's page is open (ms). Lists that grow during the day poll; configuration and
 * reports load once. A failed load is not polled: the workspace shell asks it again (transient errors only, never a
 * 404) while the service answers, and at once when the service comes back.
 */
export const SECTION_REFRESH_MS: Readonly<Record<string, number>> = {
  events: 30_000,
  alerts: 15_000,
  sources: 60_000,
  calendar: 60_000,
  fx: 60_000,
};

export type SectionQueryOptions = {
  /** Query-string parameters, e.g. {limit: 500}; part of the cache key. */
  params?: Record<string, string | number | boolean | null | undefined>;
  /** Poll interval in ms while the page is open; false loads once. Default: SECTION_REFRESH_MS[section]. */
  refetchInterval?: number | false;
};

function queryString(params: SectionQueryOptions["params"]): string {
  const entries = Object.entries(params ?? {}).filter(
    (entry): entry is [string, string | number | boolean] => entry[1] !== null && entry[1] !== undefined,
  );
  return entries.length ? `?${new URLSearchParams(entries.map(([k, v]) => [k, String(v)]))}` : "";
}

/** The cache key of `/api/<section>` with these params: other readers of the same list share its cache entry. */
export function sectionQueryKey(section: string, params?: SectionQueryOptions["params"]): readonly string[] {
  const search = queryString(params);
  return search ? [section, search] : [section];
}

/** Fetch `/api/<section>` under the query key [section] (plus the params when given). */
export function useSectionData<T = unknown>(section: string, options: SectionQueryOptions = {}) {
  const search = queryString(options.params);
  const interval = options.refetchInterval ?? SECTION_REFRESH_MS[section] ?? false;
  return useQuery({
    queryKey: sectionQueryKey(section, options.params),
    queryFn: () => api<T>(`/${section}${search}`),
    // Polling pauses while the last load failed (the shell owns recovery, so an outage is not asked twice as often)
    // and resumes after the next success; a record the service does not have is never asked again.
    refetchInterval: (query) => (query.state.status === "error" ? false : interval),
  });
}

/** Array payloads as records; anything else becomes an empty list. */
export function asRecords(data: unknown): RecordData[] {
  return Array.isArray(data) ? (data as RecordData[]) : [];
}
