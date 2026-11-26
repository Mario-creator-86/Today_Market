"use client";
import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Alert, unreadAlertCount } from "@/lib/api";
import { ALERTS_LIMIT } from "@/components/pages/page-logic";
import { sectionQueryKey } from "@/components/pages/use-section-data";
import { parseAlerts } from "./selectors";

/**
 * Per-viewer alert preferences kept in localStorage (never credentials):
 * - browser notifications on/off (opt-in, needs a user gesture to request permission)
 * - ids already notified, so a HIGH priority alert notifies once
 * - when the alert list was last marked read (unread count)
 */
export const NOTIFY_KEY = "mi.alerts.browser-notifications";
const NOTIFIED_KEY = "mi.alerts.notified-ids";
const READ_KEY = "mi.alerts.last-read";
const CHANGE = "mi-local-preference";
/** Alerts older than this are never pushed as browser notifications. */
const NOTIFY_MAX_AGE_MS = 60 * 60 * 1000;
const NOTIFIED_CAP = 300;

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null) {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* Storage blocked (private mode): the preference simply is not kept. */
  }
  window.dispatchEvent(new Event(CHANGE));
}

function subscribe(callback: () => void) {
  window.addEventListener("storage", callback);
  window.addEventListener(CHANGE, callback);
  return () => {
    window.removeEventListener("storage", callback);
    window.removeEventListener(CHANGE, callback);
  };
}

function useLocalValue(key: string): string | null {
  return useSyncExternalStore(
    subscribe,
    () => read(key),
    () => null,
  );
}

export type PermissionState = NotificationPermission | "unsupported";

function permission(): PermissionState {
  return typeof window === "undefined" || !("Notification" in window)
    ? "unsupported"
    : window.Notification.permission;
}

/** Browser-notification opt-in state and actions. */
export function useBrowserNotifications() {
  const pref = useLocalValue(NOTIFY_KEY);
  const state = useSyncExternalStore(
    subscribe,
    permission,
    () => "unsupported" as const,
  );
  const enabled = pref === "on" && state === "granted";
  const enable = useCallback(async () => {
    if (!("Notification" in window)) return;
    // Called from a click handler so the browser treats it as a user gesture.
    const result = await window.Notification.requestPermission();
    write(NOTIFY_KEY, result === "granted" ? "on" : null);
  }, []);
  const disable = useCallback(() => write(NOTIFY_KEY, "off"), []);
  return { permission: state, enabled, enable, disable };
}

function notifiedIds(): string[] {
  try {
    const parsed = JSON.parse(read(NOTIFIED_KEY) || "[]");
    return Array.isArray(parsed)
      ? parsed.filter((x) => typeof x === "string")
      : [];
  } catch {
    return [];
  }
}

/**
 * Push each new HIGH priority alert once as a browser notification while the
 * viewer has opted in. Dedupe is by alert id and survives reloads.
 */
export function useAlertNotifier(alerts: Alert[]) {
  const { enabled } = useBrowserNotifications();
  useEffect(() => {
    if (!enabled) return;
    const seen = notifiedIds();
    const known = new Set(seen);
    const now = Date.now();
    let changed = false;
    for (const alert of alerts) {
      if (alert.priority !== "HIGH" || known.has(alert.id)) continue;
      const created = Date.parse(alert.created_at);
      if (!Number.isFinite(created) || now - created > NOTIFY_MAX_AGE_MS)
        continue;
      known.add(alert.id);
      seen.push(alert.id);
      changed = true;
      try {
        new window.Notification(`HIGH PRIORITY · ${alert.title}`, {
          body: alert.message,
          tag: alert.id,
        });
      } catch {
        /* Some mobile browsers only allow service-worker notifications. */
      }
    }
    if (changed) write(NOTIFIED_KEY, JSON.stringify(seen.slice(-NOTIFIED_CAP)));
  }, [alerts, enabled]);
}

/**
 * The newest ALERTS_LIMIT alerts (GET /alerts accepts 1-500) under the Alerts page's own cache key, so the page and
 * the unread count share one request and one cache entry.
 */
const ALERT_LIST_KEY = sectionQueryKey("alerts", { limit: ALERTS_LIMIT });

/** The most recently created alert (null for an empty list). */
function newestAlert(alerts: Alert[]): Alert | null {
  let newest: Alert | null = null;
  for (const a of alerts) if (!newest || Date.parse(a.created_at) > Date.parse(newest.created_at)) newest = a;
  return newest;
}

/**
 * Unread alerts: created after the viewer last marked the list read. The
 * dashboard carries only the newest alerts; when every one of them is unread,
 * older ones may be too, so the longer /alerts list is counted instead. That
 * list is read once and again only when `alerts` shows a newer alert than it
 * holds (no polling of its own; on /alerts the page's polling keeps it current).
 * `total` is the number of stored alerts when the caller knows it (the
 * dashboard's alerts_total): with it the count is often exact without the list.
 * `more` marks a lower bound ("at least"); see unreadAlertCount (lib/api).
 */
export function useUnreadAlerts(alerts: Alert[], total?: number | null) {
  const lastRead = Date.parse(useLocalValue(READ_KEY) ?? "");
  const { needsLonger } = unreadAlertCount(alerts, lastRead, total, undefined, ALERTS_LIMIT);
  const full = useQuery({
    queryKey: ALERT_LIST_KEY,
    queryFn: () => api<unknown>(`/alerts?limit=${ALERTS_LIMIT}`),
    enabled: needsLonger,
    staleTime: Infinity,
  });
  const longer = needsLonger && full.data !== undefined ? parseAlerts(full.data) : undefined;
  const { unread, more } = unreadAlertCount(alerts, lastRead, total, longer, ALERTS_LIMIT);
  // A newer alert than the list holds (a snapshot brought it): read the list again, once per newest alert.
  const newest = newestAlert(alerts)?.id ?? null;
  const missing = longer !== undefined && newest !== null && !longer.some((a) => a.id === newest);
  const asked = useRef<string | null>(null);
  const { refetch } = full;
  useEffect(() => {
    if (!missing || asked.current === newest) return;
    asked.current = newest;
    void refetch();
  }, [missing, newest, refetch]);
  const markRead = useCallback(() => {
    const latest = alerts.reduce(
      (max, a) => Math.max(max, Date.parse(a.created_at) || 0),
      0,
    );
    write(READ_KEY, new Date(Math.max(latest, Date.now())).toISOString());
  }, [alerts]);
  return { unread, more, markRead };
}
