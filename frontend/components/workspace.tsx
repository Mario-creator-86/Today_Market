"use client";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Activity,
  ArrowLeftRight,
  AtSign,
  Bell,
  CalendarDays,
  ChartNoAxesCombined,
  Gauge,
  Radio,
  Server,
  Settings2,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
import {
  analysisView,
  API,
  api,
  type AssetQuotesFrame,
  combineAssetQuotes,
  Dashboard,
  isTransientError,
  localClock,
  localTime,
  mergeAssetQuotes,
  parseAssetQuotes,
  retriesAfter,
} from "@/lib/api";
import { timeAgo } from "@/lib/event-time";
import { providerCounts, providerSummaryText } from "@/lib/provider-settings";
import { useEdgeFade } from "@/lib/use-edge-fade";
import { Separator } from "@/components/ui/separator";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  useAlertNotifier,
  useUnreadAlerts,
} from "@/components/dashboard/notifications";
import { parseAlerts } from "@/components/dashboard/selectors";
import { cn } from "@/lib/utils";
const Connection = createContext(false);
export function useLive() {
  return useContext(Connection);
}
/** Dashboard polling while the WebSocket is down, and the slower safety poll while it streams. */
const POLL_MS = 5000;
const LIVE_POLL_MS = 30_000;
/**
 * Stream updates (full snapshots and asset_quotes price frames) are applied at most this often; what arrives within
 * the window is applied when it ends.
 */
const SNAPSHOT_MIN_MS = 1000;
/** While the service answers, failed queries (timeout, 5xx) are asked again this often, so pages recover on their own. */
const ERROR_RETRY_MS = 10_000;
const links = [
  ["/", "Dashboard", Gauge],
  ["/events", "Events", Radio],
  ["/social", "X / Social", AtSign],
  ["/calendar", "Calendar", CalendarDays],
  ["/fx", "FX", ArrowLeftRight],
  ["/sources", "Sources", ShieldCheck],
  ["/alerts", "Alerts", Bell],
  ["/performance", "Performance", ChartNoAxesCombined],
  ["/providers", "Providers", Server],
  ["/settings", "Settings", Settings2],
] as const;
function Shell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const cache = useQueryClient();
  const [live, setLive] = useState(false);
  // Set after mount: the server render has no clock.
  const [now, setNow] = useState<number | null>(null);
  const { data, status, errorUpdateCount } = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api<Dashboard>("/dashboard"),
    refetchInterval: live ? LIVE_POLL_MS : POLL_MS,
  });
  // Phone navigation scrolls sideways: the edge with more links behind it fades, the current page's link stays in view.
  const navRef = useEdgeFade<HTMLElement>('[aria-current="page"]', path);
  // Active queries that failed for a reason their own retry rule would ask again are refetched; a 404, a refusal
  // or a query that opts out (e.g. admin settings refusing the token) is not.
  const retryFailed = useCallback(() => {
    void cache.refetchQueries({
      type: "active",
      predicate: (q) =>
        q.state.status === "error" &&
        q.state.fetchStatus === "idle" &&
        q.state.error !== null &&
        retriesAfter(q.options.retry, q.state.error),
    });
  }, [cache]);
  // The timer runs only while the service answers (stream open or dashboard read). During an outage the dashboard's
  // own poll and the stream reconnecting notice the recovery (below) and refresh every failed page at once.
  const answering = live || status === "success";
  useEffect(() => {
    if (!answering) return;
    const id = setInterval(retryFailed, ERROR_RETRY_MS);
    return () => clearInterval(id);
  }, [answering, retryFailed]);
  // The dashboard coming back after a failure means the service is back: refresh every failed page at once.
  const failed = useRef(false);
  useEffect(() => {
    if (status === "error") failed.current = true;
    else if (status === "success" && failed.current) {
      failed.current = false;
      retryFailed();
    }
  }, [status, retryFailed]);
  useEffect(() => {
    const tick = () => setNow(Date.now());
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    let socket: WebSocket;
    let retry: ReturnType<typeof setTimeout>;
    let pending: ReturnType<typeof setTimeout> | undefined;
    // Held until the throttle window ends: the newest full snapshot, and the price frames that arrived after it.
    let snapshot: { data: unknown } | undefined;
    let quotes: AssetQuotesFrame | undefined;
    let disposed = false;
    let last = 0;
    const apply = () => {
      last = Date.now();
      pending = undefined;
      if (snapshot) cache.setQueryData(["dashboard"], snapshot.data);
      const frame = quotes;
      // Prices over the snapshot they arrived after; nothing to patch before the first dashboard arrived.
      if (frame) cache.setQueryData<Dashboard>(["dashboard"], (d) => mergeAssetQuotes(d, frame));
      snapshot = quotes = undefined;
    };
    const connect = () => {
      const url = new URL(API);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      url.pathname = url.pathname.replace(/\/api\/?$/, "") + "/ws";
      socket = new WebSocket(url);
      // A disposed connection's late events must not override the current one.
      socket.onopen = () => {
        if (disposed) return;
        setLive(true);
        retryFailed();
      };
      socket.onmessage = (e) => {
        if (disposed) return;
        let message: { type?: unknown; data?: unknown } | null;
        try {
          message = JSON.parse(e.data);
        } catch {
          return; // Ignore malformed frames; polling remains available.
        }
        if (message?.type === "snapshot") {
          // A full snapshot is newer than every price frame held before it.
          snapshot = { data: message.data };
          quotes = undefined;
        } else if (message?.type === "asset_quotes") {
          // Only the live prices changed: patch the cached dashboard instead of receiving it whole.
          const frame = parseAssetQuotes(message.data);
          if (!frame) return;
          quotes = quotes ? combineAssetQuotes(quotes, frame) : frame;
        } else return; // Other stream types are not used by the pages.
        const wait = SNAPSHOT_MIN_MS - (Date.now() - last);
        if (wait <= 0) {
          clearTimeout(pending);
          return apply();
        }
        // Updates arrive only when something changed: keep the newest and apply them when the window ends.
        pending ??= setTimeout(() => {
          if (!disposed) apply();
        }, wait);
      };
      socket.onerror = () => socket.close();
      socket.onclose = () => {
        if (disposed) return;
        setLive(false);
        retry = setTimeout(connect, 5000);
      };
    };
    connect();
    return () => {
      disposed = true;
      clearTimeout(retry);
      clearTimeout(pending);
      socket?.close();
    };
  }, [cache, retryFailed]);
  // Providers switched off, not configured or not checked yet are not expected to be online.
  const counts = providerCounts(
    (Array.isArray(data?.providers) ? data.providers : []).map((p) => p?.status),
  );
  const analysis = now === null ? null : analysisView(data, now);
  const rawAlerts = data?.alerts;
  const alerts = useMemo(() => parseAlerts(rawAlerts), [rawAlerts]);
  useAlertNotifier(alerts);
  const { unread, more } = useUnreadAlerts(alerts, data?.alerts_total);
  // Until the dashboard first answers nothing is known: say so instead of claiming "not recorded" or "none unread".
  // After a failure it stays "unavailable" while it is asked again (each new attempt is "pending" once more).
  const loading = data === undefined && errorUpdateCount === 0;
  return (
    <Connection.Provider value={live}>
      <div className="app-shell">
        <aside className="sidebar">
          <Link href="/" className="brand">
            <span className="brand-mark">
              <Activity size={21} />
            </span>
            <span>
              Market<span className="brand-sub">Intelligence</span>
            </span>
          </Link>
          <div className="nav-label">Workspace</div>
          <nav aria-label="Main navigation" ref={navRef}>
            {links.map(([href, label, Icon]) => {
              const active = href === "/" ? path === "/" : path.startsWith(href);
              return (
                <Link
                  key={href}
                  href={href}
                  className={active ? "nav-link selected" : "nav-link"}
                  aria-current={active ? "page" : undefined}
                >
                  <Icon size={18} aria-hidden="true" />
                  {label}
                </Link>
              );
            })}
          </nav>
          <div className="sidebar-foot">
            <ShieldCheck size={18} />
            <p>
              Evidence before conviction
              <span>Signals require verified inputs.</span>
            </p>
          </div>
        </aside>
        <div className="main-shell">
          <header className="topbar flex-wrap gap-x-4 gap-y-2 py-3">
            <div className="connection flex-wrap gap-y-1">
              <span
                className="inline-flex items-center gap-2"
                title={
                  live
                    ? "Streaming snapshots over WebSocket"
                    : "WebSocket unavailable; refreshing every 5 seconds"
                }
              >
                <span
                  className={live ? "dot live" : "dot"}
                  aria-hidden="true"
                />
                <strong
                  className={cn(
                    "text-[11px] font-bold tracking-wider",
                    live ? "text-positive" : "text-warning",
                  )}
                >
                  {live ? "LIVE" : "POLLING"}
                </strong>
              </span>
              <Separator
                orientation="vertical"
                className="mx-3 data-[orientation=vertical]:h-[15px] max-[700px]:mx-1"
              />
              <Link
                href="/providers"
                title={data ? providerSummaryText(counts) : undefined}
                className={cn(
                  "hover:text-foreground",
                  data && counts.impaired
                    ? "text-warning"
                    : "text-muted-foreground",
                )}
              >
                {data
                  ? `${counts.online}/${counts.expected} providers online`
                  : loading
                    ? "Checking providers…"
                    : "Provider health unavailable"}
              </Link>
              <Separator
                orientation="vertical"
                className="mx-3 data-[orientation=vertical]:h-[15px] max-[700px]:mx-1"
              />
              <span className="text-muted-foreground">
                Last full analysis{" "}
                {data ? (
                  <span
                    className={cn(
                      "inline-flex items-center gap-1",
                      analysis?.stale ? "text-warning" : "text-foreground",
                    )}
                    title={analysis?.reason ?? undefined}
                  >
                    {analysis?.stale && (
                      <TriangleAlert size={13} aria-hidden="true" />
                    )}
                    {localTime(analysis ? analysis.at : data.last_analysis) +
                      (analysis?.stale && analysis.ageMs !== null
                        ? ` · ${timeAgo(analysis.ageMs)}`
                        : "")}
                    {analysis?.stale && (
                      <span className="sr-only">
                        {" "}
                        Out of date. {analysis.reason}
                      </span>
                    )}
                  </span>
                ) : (
                  // Muted like the label: a placeholder, never a value.
                  <span>{loading ? "Loading…" : "Unavailable"}</span>
                )}
              </span>
            </div>
            <div className="ml-auto flex items-center gap-4">
              <Link
                href="/alerts"
                className="relative inline-flex size-9 items-center justify-center rounded-md border border-border text-muted-foreground hover:text-foreground"
                aria-label={
                  !data
                    ? loading
                      ? "Alerts, unread count loading"
                      : "Alerts, unread count unavailable"
                    : unread
                      ? `Alerts, ${more ? `at least ${unread}` : unread} unread`
                      : "Alerts, none unread"
                }
              >
                <Bell size={16} aria-hidden="true" />
                {data && unread > 0 && (
                  <span
                    aria-hidden="true"
                    className="absolute -top-1.5 -right-1.5 min-w-[18px] rounded-full bg-destructive px-1 text-center text-[10px] font-bold leading-[18px] text-background"
                  >
                    {/* A lower bound (more) reads "30+": the rest of the list could not be counted. */}
                    {unread > 99 ? "99+" : more ? `${unread}+` : unread}
                  </span>
                )}
              </Link>
              <div className="clock">
                <span>{now === null ? "--:--:--" : localClock(new Date(now))}</span>
                <small>Bucharest</small>
              </div>
            </div>
          </header>
          <main id="main-content">{children}</main>
          <footer>
            Market Intelligence <span>All times Europe/Bucharest</span>
          </footer>
        </div>
      </div>
    </Connection.Provider>
  );
}
export function Workspace({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // One quick retry for outages and server errors; a 404 or a refusal answers the same every time.
            retry: (failures, error) => failures < 1 && isTransientError(error),
            staleTime: 10000,
          },
        },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <a className="skip-link" href="#main-content">
          Skip to content
        </a>
        <Shell>{children}</Shell>
      </TooltipProvider>
    </QueryClientProvider>
  );
}
