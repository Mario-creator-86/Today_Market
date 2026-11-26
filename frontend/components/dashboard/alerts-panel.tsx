"use client";
import { useMemo } from "react";
import Link from "next/link";
import { Bell } from "lucide-react";
import { Alert, localTime, type Provider } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/shared/empty-state";
import { NotificationControl } from "@/components/shared/notification-control";
import { Panel } from "@/components/shared/panel";
import { useUnreadAlerts } from "./notifications";
import { priorityTone } from "./selectors";
import { alertRows } from "./view-model";

/**
 * §40 in-app alerts: priority, kind, message, links, unread state and browser
 * opt-in. A provider outage that has recovered since (a later recovery alert,
 * or the provider ONLINE in `providers`) is marked RESOLVED and listed after
 * the open alerts.
 */
export function AlertsPanel({
  alerts,
  providers,
}: {
  alerts: Alert[];
  providers: Provider[];
}) {
  const { unread, markRead } = useUnreadAlerts(alerts);
  const rows = useMemo(() => alertRows(alerts, providers), [alerts, providers]);
  return (
    <div id="alerts" className="min-w-0 scroll-mt-6">
      <Panel
        title="Alerts"
        className="[&_[data-slot=card-action]]:flex-wrap [&_[data-slot=card-header]]:flex-wrap"
        action={
          <>
            {unread > 0 && (
              <Button
                variant="link"
                size="sm"
                className="px-1"
                onClick={markRead}
              >
                Mark {unread} read
              </Button>
            )}
            <NotificationControl />
          </>
        }
      >
        {alerts.length ? (
          <ul className="m-0 max-h-[460px] list-none overflow-auto p-0">
            {rows.map(({ alert: a, resolved }) => (
              <li
                key={a.id}
                className="border-b border-border px-[21px] py-3.5 last:border-b-0"
              >
                <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
                  <Badge variant={resolved ? "outline" : priorityTone(a.priority)}>
                    {a.priority === "HIGH" ? "HIGH PRIORITY" : a.priority}
                  </Badge>
                  <Badge variant="outline">{a.kind.replaceAll("_", " ")}</Badge>
                  {resolved && <Badge variant="positive">{resolved}</Badge>}
                  <span>{localTime(a.created_at)}</span>
                </div>
                <h3 className="mt-1.5 text-[13px]">{a.title}</h3>
                {a.message && (
                  <p className="mt-1 whitespace-pre-line text-xs text-muted-foreground">
                    {a.message}
                  </p>
                )}
                {(a.symbol || a.event_id) && (
                  <p className="mt-1.5 flex gap-3 text-[11px]">
                    {a.symbol && (
                      <Link
                        className="text-primary"
                        href={`/assets/${encodeURIComponent(a.symbol)}`}
                      >
                        {a.symbol} detail
                      </Link>
                    )}
                    {a.event_id && (
                      <Link
                        className="text-primary"
                        href={`/events/${encodeURIComponent(a.event_id)}`}
                      >
                        Event evidence
                      </Link>
                    )}
                  </p>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState
            title="No alerts"
            detail="Setup, level-break, divergence, anomaly and macro alerts appear here when their conditions are met."
            icon={Bell}
          />
        )}
      </Panel>
    </div>
  );
}
