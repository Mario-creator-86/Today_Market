import { localTime, number, Provider } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/shared/empty-state";
import { StatusBadge } from "@/components/shared/status-badge";
import { isQuiet, providerLabel } from "@/lib/provider-settings";
import { isStub } from "./selectors";

/**
 * Quiet non-online statuses (no key, not checked yet, switched off) render grey rather than amber: the same
 * notion of "quiet" the header uses for its online / expected count.
 */
function variant(status: string) {
  return status !== "ONLINE" && isQuiet(status) ? ("outline" as const) : undefined;
}

function Row({ p }: { p: Provider }) {
  return (
    <div className="provider-row">
      <div>
        <strong>{providerLabel(p)}</strong>
        <small>{p.error || `Last success: ${localTime(p.last_success)}`}</small>
      </div>
      <span>
        {p.latency_ms === null || p.latency_ms === undefined
          ? "—"
          : `${number(p.latency_ms, 0)} ms`}
      </span>
      {isStub(p) && <Badge variant="outline">DEVELOPMENT STUB</Badge>}
      <StatusBadge value={p.status} variant={variant(p.status)} />
    </div>
  );
}

/** Provider health rows. `compact` (dashboard) shows status counts and only the providers that need attention;
 * the full list lives on the Providers page. */
export function ProviderList({
  items,
  compact = false,
}: {
  items: Provider[];
  compact?: boolean;
}) {
  if (!items.length) return <EmptyState title="Provider status unavailable" />;
  if (!compact) {
    return (
      <div className="provider-list">
        {items.map((p) => (
          <Row p={p} key={p.name} />
        ))}
      </div>
    );
  }
  const counts = new Map<string, number>();
  for (const p of items) counts.set(p.status, (counts.get(p.status) ?? 0) + 1);
  const attention = items.filter((p) => !isQuiet(p.status) || isStub(p));
  return (
    <div className="provider-list">
      <div className="provider-row" aria-label="Provider status summary">
        <div className="flex flex-wrap gap-2">
          {[...counts.entries()]
            .sort((a, b) => b[1] - a[1])
            .map(([status, count]) => (
              <span key={status} className="inline-flex items-center gap-1.5">
                <StatusBadge value={status} variant={variant(status)} />
                <strong>{count}</strong>
              </span>
            ))}
        </div>
      </div>
      {attention.length ? (
        attention.map((p) => <Row p={p} key={p.name} />)
      ) : (
        <div className="provider-row">
          <small>All configured providers are online.</small>
        </div>
      )}
    </div>
  );
}
