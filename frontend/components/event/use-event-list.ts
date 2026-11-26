"use client";
import { useQuery } from "@tanstack/react-query";
import { api, MarketEvent } from "@/lib/api";
import { EVENTS_LIMIT } from "@/lib/events";

/** Poll interval of the event lists, matching the event detail page. */
export const EVENT_LIST_REFRESH_MS = 30_000;

/**
 * Every stored event up to the API limit, newest publication first, shared by
 * /events and /sources (one query key, one request) and refreshed while open.
 */
export function useEventList() {
  return useQuery({
    queryKey: ["events", { limit: EVENTS_LIMIT }],
    queryFn: () => api<MarketEvent[]>(`/events?limit=${EVENTS_LIMIT}`),
    refetchInterval: EVENT_LIST_REFRESH_MS,
  });
}
