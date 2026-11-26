"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { api, localTime, Provider } from "@/lib/api";
import {
  byCategory,
  categoryLabel,
  isQuiet,
  providerCounts,
  providerLabel,
  statusRank,
  STUB_STATUS,
  type TestResult,
} from "@/lib/provider-settings";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/native-select";
import { EmptyState } from "@/components/shared/empty-state";
import { PageHeading } from "@/components/shared/page-heading";
import { QueryState } from "@/components/shared/query-state";
import { SearchToolbar } from "@/components/shared/search-toolbar";
import { StatusBadge } from "@/components/shared/status-badge";
import { cn } from "@/lib/utils";
import { Column, DataTable, fmt, Stat, tone } from "./page-utils";
import { ProviderToggle, settingsData, TestOutcome, testProvider, useProviderSettings } from "./provider-settings";

/** The table follows the header: health is re-read while the page is open. */
const PROVIDERS_REFRESH_MS = 15_000;

/** The settings category a provider belongs to ("Market data"), when the service names one. */
function providerCategory(p: Provider): string | null {
  return typeof p.category === "string" && p.category ? categoryLabel(p.category) : null;
}

const COLUMNS: Column<Provider>[] = [
  {
    key: "name",
    label: "Provider",
    render: (p) => {
      const label = providerLabel(p);
      // Second line: the id when a label hides it, and the settings category.
      const detail = [label !== p.name ? p.name : null, providerCategory(p)].filter(Boolean).join(" · ");
      return (
        <span className="flex flex-col">
          <strong>{label}</strong>
          {detail && <small className="text-muted-foreground">{detail}</small>}
        </span>
      );
    },
  },
  {
    key: "status",
    label: "Status",
    render: (p) => (
      <span className="flex flex-wrap gap-1">
        <StatusBadge value={p.status} variant={tone(p.status)} />
        {p.stub && <StatusBadge value="DEVELOPMENT STUB" variant="outline" />}
      </span>
    ),
  },
  {
    key: "latency",
    label: "Latency",
    numeric: true,
    render: (p) => fmt(p.latency_ms, { digits: 0, suffix: " ms" }),
  },
  { key: "success", label: "Last success", render: (p) => localTime(p.last_success) },
  {
    key: "error",
    label: "Last error",
    className: "min-w-[200px]",
    render: (p) => p.error ?? "None",
  },
];

/** Per-row provider test (POST /admin/providers/{name}/test) with the result inline. */
function TestCell({ name, label, token }: { name: string; label: string; token: string }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<TestResult | null>(null);
  const [error, setError] = useState("");
  async function run() {
    setBusy(true);
    setResult(null);
    setError("");
    try {
      setResult(await testProvider(name, token));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <span className="flex min-w-[160px] flex-col items-start gap-1.5">
      <Button size="sm" variant="outline" aria-label={`Test ${label}`} disabled={busy} onClick={run}>
        {busy ? "Testing…" : "Test"}
      </Button>
      <span role="status">
        {result && <TestOutcome result={result} />}
        {error && <span className="text-destructive">{error}</span>}
      </span>
    </span>
  );
}

export function useProviders() {
  return useQuery({
    queryKey: ["providers"],
    queryFn: () => api<Provider[]>("/providers"),
    // Paused while the last read failed: the workspace shell asks again (never a 404) and polling resumes after it.
    refetchInterval: (query) => (query.state.status === "error" ? false : PROVIDERS_REFRESH_MS),
  });
}

/** Provider status table (§34), worst status first. With an admin token: Enabled and Test columns. */
export function ProviderHealthTable({ providers, token = "" }: { providers: Provider[]; token?: string }) {
  const settings = settingsData(useProviderSettings(token).data);
  const known = new Map((settings?.providers ?? []).map((p) => [p.name, p]));
  const rows = [...providers].sort(
    (a, b) => statusRank(a.status) - statusRank(b.status) || providerLabel(a).localeCompare(providerLabel(b)),
  );
  const columns: Column<Provider>[] = token
    ? [
        ...COLUMNS.slice(0, 2),
        {
          key: "enabled",
          label: "Enabled",
          render: (p) => (
            <ProviderToggle
              name={p.name}
              enabled={known.get(p.name)?.enabled ?? p.enabled ?? p.status !== "DISABLED"}
              token={token}
              ariaLabel={`${providerLabel(p)} enabled`}
            />
          ),
        },
        ...COLUMNS.slice(2),
        {
          key: "test",
          label: "Test",
          render: (p) =>
            known.get(p.name)?.testable === false ? (
              <span className="text-muted-foreground">Not testable</span>
            ) : (
              <TestCell name={p.name} label={providerLabel(p)} token={token} />
            ),
        },
      ]
    : COLUMNS;
  return (
    <DataTable
      rows={rows}
      columns={columns}
      rowKey={(p) => p.name}
      caption="Provider health"
      empty={<EmptyState title="No matching providers" detail="Clear the search or choose another status." />}
    />
  );
}

/** Status filter: the same buckets as the counts above the table. */
const FILTERS = [
  ["ALL", "All statuses"],
  ["ATTENTION", "Needs attention"],
  ["ONLINE", "Online"],
  ["PENDING", "Not checked yet"],
  ["NOT CONFIGURED", "Not configured"],
  ["DISABLED", "Switched off"],
  [STUB_STATUS, "Development stub"],
] as const;
type Filter = (typeof FILTERS)[number][0];

/** Which count a status belongs to (lib/provider-settings providerCounts uses the same buckets). */
function bucket(status: unknown): Filter {
  const s = String(status ?? "").trim().toUpperCase().replaceAll("_", " ");
  if (s === "ONLINE" || s === "NOT CONFIGURED" || s === "DISABLED" || s === STUB_STATUS) return s;
  return isQuiet(s) ? "PENDING" : "ATTENTION";
}

const ALL_CATEGORIES = "ALL";

function matches(p: Provider, search: string, filter: Filter, category: string): boolean {
  if (filter !== "ALL" && bucket(p.status) !== filter) return false;
  if (category !== ALL_CATEGORIES && (p.category ?? "") !== category) return false;
  const q = search.trim().toLowerCase();
  return !q || [providerLabel(p), p.name, String(p.status ?? "").replaceAll("_", " "), p.error ?? "", providerCategory(p) ?? ""]
    .some((v) => v.toLowerCase().includes(q));
}

/** Counts, refresh control, search and the health table. */
export function ProviderHealthSection({ token = "" }: { token?: string }) {
  const q = useProviders();
  const [manual, setManual] = useState(false);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<Filter>("ALL");
  const [category, setCategory] = useState(ALL_CATEGORIES);
  const list = Array.isArray(q.data) ? q.data : [];
  const counts = providerCounts(list.map((p) => p.status));
  const shown = list.filter((p) => matches(p, search, filter, category));
  // Settings categories present in the rows, in the settings order (older services send none: no filter then).
  const categories = byCategory(
    list.flatMap((p) => (typeof p.category === "string" && p.category ? [{ category: p.category }] : [])),
  );
  // Only a click spins the button; the background refresh stays quiet.
  const refresh = () => {
    setManual(true);
    void q.refetch().finally(() => setManual(false));
  };
  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <p className="flex-1 text-xs text-muted-foreground">
          When a critical market feed is unavailable, live signals that depend on it are downgraded to WAIT.
          Stale or search-derived data is never substituted.
        </p>
        <Button size="sm" onClick={refresh} disabled={manual}>
          <RefreshCw size={14} aria-hidden="true" className={manual ? "spin" : undefined} />
          {manual ? "Refreshing…" : "Refresh status"}
        </Button>
      </div>
      <QueryState
        loading={q.isLoading}
        error={q.error}
        loadingText="Loading provider status…"
        onRetry={refresh}
      />
      {list.length > 0 && (
        <div
          className={cn(
            "mb-4 grid grid-cols-2 gap-3 sm:grid-cols-3",
            counts.stub > 0 ? "lg:grid-cols-7" : "lg:grid-cols-6",
          )}
        >
          <Stat label="Online" value={counts.online} />
          <Stat label="Impaired" value={counts.impaired} detail="Degraded, offline, auth or rate limited" />
          <Stat label="Pending" value={counts.pending} detail="Configured, not checked yet" />
          <Stat label="Not configured" value={counts.notConfigured} />
          <Stat label="Disabled" value={counts.disabled} detail="Switched off by an administrator" />
          {counts.stub > 0 && (
            <Stat label="Development stubs" value={counts.stub} detail="Development mode: sample data, not real data" />
          )}
          <Stat label="Total" value={counts.total} />
        </div>
      )}
      {q.data !== undefined &&
        (list.length ? (
          <>
            <SearchToolbar search={search} onSearch={setSearch} count={shown.length}>
              <NativeSelect
                aria-label="Provider status filter"
                value={filter}
                onChange={(e) => setFilter(e.target.value as Filter)}
              >
                {FILTERS.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </NativeSelect>
              {categories.length > 0 && (
                <NativeSelect
                  aria-label="Provider category filter"
                  value={category}
                  onChange={(e) => setCategory(e.target.value)}
                >
                  <option value={ALL_CATEGORIES}>All categories</option>
                  {categories.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.label}
                    </option>
                  ))}
                </NativeSelect>
              )}
            </SearchToolbar>
            <Card className="panel">
              <ProviderHealthTable providers={shown} token={token} />
            </Card>
          </>
        ) : (
          <Card className="panel">
            <EmptyState title="No providers reported" detail="The service returned an empty provider list." />
          </Card>
        ))}
    </>
  );
}

/** Provider health (/providers). */
export function ProvidersPage() {
  return (
    <>
      <PageHeading
        title="Provider health"
        description="Connection status, latency and last success for every data and AI provider."
      />
      <ProviderHealthSection />
    </>
  );
}
