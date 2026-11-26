"use client";
import { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api, localTime, type Alert, type Dashboard } from "@/lib/api";
import { humanize } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NotificationControl } from "@/components/shared/notification-control";
import { NativeSelect } from "@/components/ui/native-select";
import { EmptyState } from "@/components/shared/empty-state";
import { PageHeading } from "@/components/shared/page-heading";
import { QueryState } from "@/components/shared/query-state";
import { StatusBadge } from "@/components/shared/status-badge";
import { useUnreadAlerts } from "@/components/dashboard/notifications";
import { ALERTS_LIMIT, ALERTS_PAGE, alertCountText, alertLink, kindLabel, providerLabels } from "./page-logic";
import { Column, DataTable, tone } from "./page-utils";
import type { AlertItem } from "./types";
import { useSectionData } from "./use-section-data";

const PRIORITY = ["HIGH", "MEDIUM", "LOW"];

/**
 * The newest ALERTS_LIMIT alerts (the API default of 100 would silently cut the
 * list), polled while the page is open (SECTION_REFRESH_MS.alerts): the shell's
 * WebSocket refreshes the dashboard, not this list. Query key ["alerts", "?limit=500"].
 */
export function useAlerts() {
  return useSectionData<AlertItem[]>("alerts", { params: { limit: ALERTS_LIMIT } });
}

const NO_LABELS: ReadonlyMap<string, string> = new Map();

function selectProviderLabels(data: Dashboard): ReadonlyMap<string, string> {
  return providerLabels(data.providers);
}

/**
 * Provider display names by id, from the provider list the shell already loads with the dashboard (same query key, so
 * no extra request): an alert stored with only the provider id still names the provider. Empty until it arrives.
 */
function useProviderLabels(): ReadonlyMap<string, string> {
  const { data } = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api<Dashboard>("/dashboard"),
    select: selectProviderLabels,
  });
  return data ?? NO_LABELS;
}

const columns = (labels: ReadonlyMap<string, string>): Column<AlertItem>[] => [
  {
    key: "priority",
    label: "Priority",
    render: (a) => <StatusBadge value={a.priority} variant={tone(a.priority)} />,
  },
  { key: "kind", label: "Kind", render: (a) => kindLabel(a.kind) },
  {
    key: "title",
    label: "Alert",
    className: "min-w-[220px]",
    render: (a) => (
      <span className="flex flex-col">
        <strong>{a.title ?? "Untitled alert"}</strong>
        {a.message && <small className="text-muted-foreground">{a.message}</small>}
      </span>
    ),
  },
  {
    key: "link",
    label: "Related",
    render: (a) => {
      const link = alertLink(a, labels);
      return link ? (
        <Link className="source-link" href={link.href}>
          {link.text}
        </Link>
      ) : (
        "None"
      );
    },
  },
  { key: "created", label: "Created", render: (a) => localTime(a.created_at) },
];

/** Alerts that carry a creation time, in the shape the shared unread tracker reads. */
function timed(list: AlertItem[]): Alert[] {
  return list.filter((a): a is Alert => typeof a.created_at === "string");
}

/** Recent in-app alerts with a priority filter, paging and (on /alerts) mark-as-read. */
export function AlertsTable({ compact = false }: { compact?: boolean }) {
  const [priority, setPriority] = useState("ALL");
  const [limit, setLimit] = useState(ALERTS_PAGE);
  const q = useAlerts();
  const labels = useProviderLabels();
  const tableColumns = useMemo(() => columns(labels), [labels]);
  const loaded = Array.isArray(q.data) ? q.data : [];
  // Unread is shared with the header bell and the dashboard panel (same per-viewer marker).
  const { unread, more, markRead } = useUnreadAlerts(timed(loaded));
  const list = loaded
    .filter((a) => priority === "ALL" || String(a.priority).toUpperCase() === priority)
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  const shown = compact ? list.slice(0, 10) : list.slice(0, limit);
  // Counts and "no alerts" are stated only for data that arrived: never while loading or after a failure.
  const ready = q.data !== undefined;
  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <NativeSelect
          aria-label="Alert priority filter"
          value={priority}
          onChange={(e) => {
            setPriority(e.target.value);
            setLimit(ALERTS_PAGE);
          }}
        >
          <option value="ALL">All priorities</option>
          {PRIORITY.map((p) => (
            <option key={p} value={p}>
              {humanize(p)}
            </option>
          ))}
        </NativeSelect>
        {ready && (
          <span className="text-xs text-muted-foreground" role="status">
            {alertCountText(shown.length, list.length, loaded.length)}
          </span>
        )}
        {ready && !compact && unread > 0 && (
          <Button variant="link" size="sm" className="px-1" onClick={markRead}>
            {`Mark ${unread}${more ? "+" : ""} read`}
          </Button>
        )}
        {compact && ready && list.length > shown.length && (
          <Link className="source-link text-xs" href="/alerts">
            View all alerts
          </Link>
        )}
      </div>
      <QueryState
        loading={q.isLoading}
        error={q.error}
        loadingText="Loading alerts…"
        onRetry={() => void q.refetch()}
      />
      {ready && (
        <Card className="panel">
          <DataTable
            rows={shown}
            columns={tableColumns}
            rowKey={(a, i) => a.id ?? String(i)}
            caption="In-app alerts"
            empty={
              loaded.length ? (
                <EmptyState title="No alerts at this priority" detail="Choose another priority to see the other alerts." />
              ) : (
                <EmptyState title="No alerts yet" detail="Setup, level-break, divergence and anomaly alerts appear here." />
              )
            }
          />
          {!compact && list.length > shown.length && (
            <div className="flex justify-center border-t border-border p-3">
              <Button variant="outline" size="sm" onClick={() => setLimit((n) => n + ALERTS_PAGE)}>
                Show {Math.min(ALERTS_PAGE, list.length - shown.length)} more
              </Button>
            </div>
          )}
        </Card>
      )}
    </>
  );
}

/** Alerts (/alerts). The browser-notification opt-in shares the dashboard's preference. */
export function AlertsPage() {
  return (
    <>
      <PageHeading
        title="Alerts"
        description="In-app alerts from setups, level breaks, divergences, anomalies, macro releases and data providers. Times in Europe/Bucharest."
        action={<NotificationControl />}
      />
      <AlertsTable />
    </>
  );
}
