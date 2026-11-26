"use client";
import { useId, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { adminHeaders, api, durationText, labelText, LOCAL_ADMIN, localTime, RecordData } from "@/lib/api";
import { humanize, text } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EmptyState } from "@/components/shared/empty-state";
import { NotificationControl } from "@/components/shared/notification-control";
import { PageHeading } from "@/components/shared/page-heading";
import { QueryState } from "@/components/shared/query-state";
import { StatusBadge } from "@/components/shared/status-badge";
import { AiUsageSection } from "./ai-usage";
import { AlertsTable } from "./alerts";
import { Column, DataTable, fmt, isNum, ReadOnlyNote, tone, yesNo } from "./page-utils";
import { ProviderHealthSection } from "./provider-health";
import { ProviderSettingsSection } from "./provider-settings";
import { RoleForm } from "./settings-role-form";
import { SourceRegistryTable } from "./sources";
import type { AiModel, SourceRecord } from "./types";
import { useSectionData } from "./use-section-data";

// Tab values are slugs: Radix builds element ids from them, and an id must not contain spaces.
const TABS = [
  { id: "providers", label: "Data providers" },
  { id: "models", label: "AI models" },
  { id: "sources", label: "Sources" },
  { id: "refresh", label: "Refresh" },
  { id: "scoring", label: "Scoring" },
  { id: "alerts", label: "Alerts" },
  { id: "autolearn", label: "Autolearn" },
] as const;
type TabId = (typeof TABS)[number]["id"];

function isRecord(value: unknown): value is RecordData {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

/**
 * How numbers read: plain, a span of seconds ("0 s", "1 h": delays, cooldowns) or a refresh cadence ("every 5 min").
 * A key ending in _seconds makes its numbers (and those nested under it) seconds.
 */
type Units = "plain" | "seconds" | "cadence";

function unitsFor(key: string, parent: Units): Units {
  return parent === "plain" && key.endsWith("_seconds") ? "seconds" : parent;
}

const ENUM = /^[A-Z][A-Z0-9]*(?:[_ ][A-Z0-9]+)*$/;
const SNAKE = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/;
const ISO_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

/** A text value: enum values as badges, snake_case as words, timestamps in Bucharest time. */
function textValue(value: string): React.ReactNode {
  if (ISO_TIME.test(value) && Number.isFinite(Date.parse(value))) return localTime(value);
  if (ENUM.test(value) && value.length <= 40) {
    const t = tone(value);
    return <StatusBadge value={value} variant={t === "neutral" ? "outline" : t} />;
  }
  return SNAKE.test(value) ? humanize(value) : value;
}

function isScalar(value: unknown): boolean {
  return value === null || typeof value !== "object";
}

/** One configuration value. Nested settings and lists of records open on demand instead of printing JSON. */
function ConfigValue({ value, units }: { value: unknown; units: Units }): React.ReactNode {
  if (typeof value === "boolean") return yesNo(value);
  if (isNum(value))
    return units === "cadence"
      ? `every ${durationText(value)}`
      : units === "seconds"
        ? durationText(value)
        : fmt(value, { digits: 4 });
  if (value === null || value === undefined) return <span className="text-muted-foreground">Not set</span>;
  if (typeof value === "string") return value ? textValue(value) : <span className="text-muted-foreground">Empty</span>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="text-muted-foreground">None</span>;
    if (value.every(isScalar))
      return (
        <span className="flex flex-wrap items-center gap-1">
          {value.map((v, i) =>
            typeof v === "string" && ENUM.test(v) ? (
              <span key={i}>{textValue(v)}</span>
            ) : (
              <Badge key={i} variant="outline">
                {typeof v === "string" ? (SNAKE.test(v) ? humanize(v) : v) : <ConfigValue value={v} units={units} />}
              </Badge>
            ),
          )}
        </span>
      );
    if (value.every(isRecord))
      return (
        <details>
          <summary className="cursor-pointer text-primary">
            {value.length} {value.length === 1 ? "entry" : "entries"}
          </summary>
          <RecordsTable rows={value} units={units} />
        </details>
      );
    return value.map((v) => text(v)).join(", ");
  }
  if (isRecord(value)) {
    const n = Object.keys(value).length;
    if (!n) return <span className="text-muted-foreground">None</span>;
    return (
      <details>
        <summary className="cursor-pointer text-primary">
          {n} {n === 1 ? "setting" : "settings"}
        </summary>
        <ConfigTable data={value} caption="Nested settings" units={units} />
      </details>
    );
  }
  return String(value);
}

/** A list of records as a small table: one column per key. */
function RecordsTable({ rows, units }: { rows: RecordData[]; units: Units }) {
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const columns: Column<RecordData>[] = keys.map((key) => ({
    key,
    label: labelText(key),
    render: (row) => (key in row ? <ConfigValue value={row[key]} units={unitsFor(key, units)} /> : ""),
  }));
  return <DataTable rows={rows} columns={columns} rowKey={(_, i) => String(i)} caption="Configuration entries" />;
}

type Entry = { key: string; value: unknown };

/** Key/value configuration table. */
function ConfigTable({
  data,
  caption,
  labelFor = labelText,
  units = "plain",
}: {
  data: RecordData;
  caption: string;
  labelFor?: (key: string) => string;
  units?: Units;
}) {
  const columns: Column<Entry>[] = [
    { key: "k", label: "Setting", render: (e) => <strong>{labelFor(e.key)}</strong> },
    { key: "v", label: "Value", render: (e) => <ConfigValue value={e.value} units={unitsFor(e.key, units)} /> },
  ];
  return (
    <DataTable
      rows={Object.entries(data).map(([key, value]) => ({ key, value }))}
      columns={columns}
      rowKey={(e) => e.key}
      caption={caption}
      empty={<EmptyState title="No settings" />}
    />
  );
}

function Unavailable({ what }: { what: string }) {
  return (
    <EmptyState
      title="Configuration unavailable"
      detail={`The backend does not provide ${what} yet. Connect the backend to review the active configuration.`}
    />
  );
}

/** Where a read-only configuration can be changed: the audited admin endpoint when the backend lists it as editable. */
function editNote(editable: boolean, what: string): string {
  return editable
    ? `${what} are versioned configuration. They can be changed through the audited admin configuration endpoint; this page does not edit them yet.`
    : `${what} are versioned configuration; there is no admin endpoint to edit them yet.`;
}

function AdminTokenField({ token, setToken }: { token: string; setToken: (v: string) => void }) {
  const id = useId();
  return (
    <Card className="mb-5">
      <CardContent className="flex flex-wrap items-end gap-4">
        <div className="field min-w-[240px] flex-1">
          <label htmlFor={id}>Admin token</label>
          <Input
            id={id}
            type="password"
            autoComplete="off"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Enter to manage providers, models and calibration"
          />
        </div>
        <p className="muted flex-1 text-xs">
          Needed to configure providers and run admin actions. It is <code>ADMIN_TOKEN</code> in the project&apos;s{" "}
          <code>.env</code> file (or run <code>python -m scripts.admin_token</code>). The token stays in memory for this
          page session and is only sent with admin actions.
        </p>
      </CardContent>
    </Card>
  );
}

const MODEL_COLUMNS: Column<AiModel>[] = [
  { key: "name", label: "Model", render: (m) => <strong>{m.display_name ?? m.name}</strong> },
  {
    key: "status",
    label: "Availability",
    render: (m) => {
      const status = m.status ?? (m.availability === false ? "UNAVAILABLE" : m.availability ? "AVAILABLE" : null);
      return status ? <StatusBadge value={status} variant={tone(status)} /> : "Unavailable";
    },
  },
  {
    key: "caps",
    label: "Capabilities",
    render: (m) =>
      Array.isArray(m.capabilities) && m.capabilities.length ? (
        <span className="flex flex-wrap gap-1">
          {m.capabilities.map((c) => (
            <StatusBadge key={c} value={c} variant="outline" />
          ))}
        </span>
      ) : (
        "Not reported"
      ),
  },
  { key: "tools", label: "Tools", render: (m) => yesNo(m.tools_supported) },
  { key: "thinking", label: "Thinking", render: (m) => yesNo(m.thinking_supported) },
  {
    key: "context",
    label: "Context window",
    numeric: true,
    render: (m) => fmt(m.context_window, { digits: 0, suffix: " tokens", missing: "Not reported" }),
  },
  { key: "seen", label: "Last seen", render: (m) => localTime(m.last_seen) },
];

type OllamaTest = {
  success?: boolean;
  schema_valid?: boolean | null;
  publication_blocked?: boolean;
  /** 'role' | 'daily' | 'pace' when the daily AI budget refused the probe (it runs the auditor role; nothing sent). */
  budget_refusal?: string | null;
  error?: string | null;
  provider_status?: string;
  checked_at?: string;
};

function ModelsTab({ roles, token }: { roles: RecordData[]; token: string }) {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState<"" | "discover" | "test">("");
  const [probe, setProbe] = useState<OllamaTest | null>(null);
  const cache = useQueryClient();
  const models = useQuery({
    queryKey: ["models"],
    queryFn: () => api<AiModel[]>("/models"),
  });
  const list = Array.isArray(models.data) ? models.data : [];
  async function discover() {
    setBusy("discover");
    setMessage("");
    try {
      await api("/admin/models/discover", { method: "POST", headers: adminHeaders(token) });
      await cache.invalidateQueries({ queryKey: ["models"] });
      setMessage("Model discovery complete.");
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setBusy("");
    }
  }
  async function testOllama() {
    setBusy("test");
    setMessage("");
    try {
      setProbe(await api<OllamaTest>("/admin/ollama/test", { method: "POST", headers: adminHeaders(token) }));
    } catch (e) {
      setProbe(null);
      setMessage((e as Error).message);
    } finally {
      setBusy("");
      // The probe counts against the auditor's daily cap: the usage panel shows it at once.
      void cache.invalidateQueries({ queryKey: ["ai-usage"] });
    }
  }
  return (
    <>
      <AiUsageSection token={token} />
      <h2>Discovered models</h2>
      <p className="muted mb-3">
        Models are discovered from the connected Ollama service. Unavailable models cannot be assigned.
      </p>
      <div className="mb-3 flex flex-wrap gap-3">
        <Button disabled={!token || busy !== ""} onClick={discover}>
          {busy === "discover" ? "Discovering…" : "Discover models"}
        </Button>
        <Button variant="outline" disabled={!token || busy !== ""} onClick={testOllama}>
          {busy === "test" ? "Testing…" : "Test Ollama"}
        </Button>
      </div>
      <p role="status" className="mb-3 text-xs">
        {message}
      </p>
      {probe && (
        <div className="mb-4 flex flex-wrap items-center gap-2 text-xs" aria-label="Ollama test result">
          {probe.budget_refusal ? (
            <>
              <StatusBadge value="REFUSED" variant="neutral" />
              <span>
                Refused by the AI budget (no request sent)
                {probe.error ? `: ${probe.error}` : ""}. The test runs the auditor role and counts against its daily cap.
              </span>
            </>
          ) : (
            <>
              <StatusBadge value={probe.success ? "PASS" : "FAILED"} variant={probe.success ? "positive" : "negative"} />
              <span>Schema valid: {yesNo(probe.schema_valid)}</span>
              <span>Publication blocked: {yesNo(probe.publication_blocked)}</span>
            </>
          )}
          <span>Provider: {text(probe.provider_status)}</span>
          <span>Checked {localTime(probe.checked_at)}</span>
        </div>
      )}
      <QueryState
        loading={models.isLoading}
        error={models.error}
        loadingText="Loading models…"
        onRetry={() => void models.refetch()}
      />
      <Card className="mb-5">
        <DataTable
          rows={list}
          columns={MODEL_COLUMNS}
          rowKey={(m, i) => m.name ?? String(i)}
          caption="Discovered AI models"
          empty={<EmptyState title="No models discovered" detail="Enter the admin token and run model discovery." />}
        />
      </Card>
      <h2>AI role assignments</h2>
      <p className="muted mb-2">Primary, backup and second opinion per role.</p>
      {roles.length ? (
        roles.map((role, i) => (
          <RoleForm key={text(role.role ?? role.name ?? i)} role={role} models={list} token={token} />
        ))
      ) : (
        <EmptyState title="No AI roles loaded" />
      )}
    </>
  );
}

function SourcesTab({ sources, editable }: { sources: unknown; editable: boolean }) {
  const fallback = useSectionData<SourceRecord[]>("sources");
  const list = Array.isArray(sources) ? (sources as SourceRecord[]) : Array.isArray(fallback.data) ? fallback.data : [];
  return (
    <>
      <h2>Source registry</h2>
      <ReadOnlyNote>
        {editable
          ? "Sources, tiers and trust weights come from versioned configuration. Trust overrides go through the audited admin endpoint; this page does not edit them yet."
          : "Sources, tiers and trust weights come from versioned configuration. There is no admin endpoint to edit them yet."}
      </ReadOnlyNote>
      <Card>
        <SourceRegistryTable sources={list} />
      </Card>
    </>
  );
}

/** One row of GET /settings `refresh` (config/rules.d/refresh.json cadences). */
type Cadence = RecordData & { kind?: string; label?: string; seconds?: number; mode?: string };

/** Push delivery named by a row's `mode`; rows without one just run at their interval (nothing more is known). */
const DELIVERY: Record<string, string> = { websocket: "WebSocket push", stream: "Stream" };

function delivery(c: Cadence): string | null {
  return typeof c.mode === "string" && c.mode ? (DELIVERY[c.mode] ?? labelText(c.mode)) : null;
}

/**
 * What a row's other intervals mean (config/rules.d/refresh.json, backend/refresh.py next_interval): the key without
 * _seconds names when that interval applies. Unknown keys fall back to their words.
 */
const EXTRA_INTERVAL: Record<string, string> = {
  tick: "Schedule check",
  watch: "During the watch window",
  active: "While active",
  inactive: "While markets are closed",
  off_hours: "Outside market hours",
};

function intervalName(key: string): string {
  const base = key.replace(/_seconds$/, "");
  return EXTRA_INTERVAL[base] ?? labelText(base);
}

const CADENCE_COLUMNS: Column<Cadence>[] = [
  {
    key: "label",
    label: "Refresh",
    render: (c) => {
      const push = delivery(c);
      return (
        <span className="flex flex-col items-start gap-1">
          <strong>{typeof c.label === "string" && c.label ? c.label : labelText(String(c.kind ?? ""))}</strong>
          {push && <Badge variant="info">{push}</Badge>}
        </span>
      );
    },
  },
  {
    key: "interval",
    label: "Interval",
    render: (c) =>
      isNum(c.seconds) ? (
        <span className="flex flex-col">
          every {durationText(c.seconds)}
          {/* config/rules.d/refresh.json: a pushed row's seconds are its polling fallback. */}
          {delivery(c) && <small className="text-muted-foreground">when the push is unavailable</small>}
        </span>
      ) : (
        "Unavailable"
      ),
  },
  {
    key: "other",
    label: "Other intervals",
    render: (c) => {
      // e.g. inactive_seconds, watch_seconds, off_hours_seconds, tick_seconds.
      const extra = Object.entries(c).filter(([k, v]) => k !== "seconds" && k.endsWith("_seconds") && isNum(v));
      return extra.length ? (
        <span className="flex flex-col">
          {extra.map(([k, v]) => (
            <span key={k}>
              {intervalName(k)}: every {durationText(v)}
            </span>
          ))}
        </span>
      ) : (
        <span className="text-muted-foreground">None</span>
      );
    },
  },
];

function RefreshTab({ data, editable }: { data: unknown; editable: boolean }) {
  // Current backends send a list of cadence rows; older ones a flat {name_seconds: n} record.
  const rows = Array.isArray(data) ? (data.filter(isRecord) as Cadence[]) : null;
  return (
    <>
      <h2>Refresh cadence</h2>
      <ReadOnlyNote>{editNote(editable, "Refresh cadences")}</ReadOnlyNote>
      {rows ? (
        <Card>
          <DataTable
            rows={rows}
            columns={CADENCE_COLUMNS}
            rowKey={(c, i) => String(c.kind ?? i)}
            caption="Refresh cadence"
            empty={<EmptyState title="No refresh rules" />}
          />
        </Card>
      ) : isRecord(data) ? (
        <Card>
          <ConfigTable
            data={data}
            caption="Refresh cadence"
            labelFor={(k) => labelText(k.replace(/_seconds$/, ""))}
            units="cadence"
          />
        </Card>
      ) : (
        <Unavailable what="refresh settings" />
      )}
    </>
  );
}

function ScoringTab({ data }: { data: unknown }) {
  if (!isRecord(data))
    return (
      <>
        <h2>Scoring</h2>
        <Unavailable what="scoring rules" />
      </>
    );
  const sections: [string, string][] = [
    ["weights", "Component weights"],
    ["thresholds", "Thresholds"],
    ["source_weights", "Source trust weights"],
  ];
  const shown = new Set(["version", "content_hash", ...sections.map(([k]) => k)]);
  const rest = Object.fromEntries(Object.entries(data).filter(([k]) => !shown.has(k)));
  return (
    <>
      <h2>Scoring</h2>
      <ReadOnlyNote>
        Rules are versioned configuration. Changes go through a new rules version, never through the UI or an AI model.
      </ReadOnlyNote>
      <p className="mb-4 text-xs text-muted-foreground">
        Rules version <strong className="text-foreground">{text(data.version)}</strong>
        {typeof data.content_hash === "string" && (
          <>
            {" "}
            · content hash <code className="wrap-anywhere">{data.content_hash.slice(0, 16)}…</code>
          </>
        )}
      </p>
      <div className="flex flex-col gap-4">
        {sections.map(([key, label]) =>
          isRecord(data[key]) ? (
            <Card key={key}>
              <h3 className="px-[18px] pt-4 pb-2">{label}</h3>
              <ConfigTable data={data[key] as RecordData} caption={label} />
            </Card>
          ) : null,
        )}
        {Object.keys(rest).length > 0 && (
          <details className="rounded-lg border border-border bg-card">
            <summary className="cursor-pointer px-[18px] py-3 text-sm font-semibold">Other configuration</summary>
            <ConfigTable data={rest} caption="Other configuration" />
          </details>
        )}
      </div>
    </>
  );
}

function AlertsTab({ data, editable }: { data: unknown; editable: boolean }) {
  return (
    <>
      <h2>Alerts</h2>
      <div className="mb-4">
        <NotificationControl />
      </div>
      {isRecord(data) ? (
        <>
          <ReadOnlyNote>{editNote(editable, "Alert rules")}</ReadOnlyNote>
          <Card className="mb-5">
            <ConfigTable data={data} caption="Alert settings" />
          </Card>
        </>
      ) : (
        <ReadOnlyNote>The backend does not expose alert settings yet.</ReadOnlyNote>
      )}
      <h3 className="mb-2">Recent alerts</h3>
      <AlertsTable compact />
    </>
  );
}

type CalibrationResult = RecordData | null;

function CalibrationActions({ token }: { token: string }) {
  const id = useId();
  const [symbol, setSymbol] = useState("");
  const [horizon, setHorizon] = useState("4h");
  const [candidate, setCandidate] = useState("");
  const [busy, setBusy] = useState(false);
  // Rolling back retires the current champion: it takes a second, explicit click that names what is rolled back.
  const [confirmRollback, setConfirmRollback] = useState(false);
  const rollbackRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  /** Back to "Roll back"; focus leaves the Cancel button (it disappears) for the Roll back button. */
  function cancelRollback(from: EventTarget | null) {
    setConfirmRollback(false);
    if (from === cancelRef.current) rollbackRef.current?.focus();
  }
  const [status, setStatus] = useState("");
  const [result, setResult] = useState<CalibrationResult>(null);
  async function run(label: string, path: string) {
    setBusy(true);
    setStatus("");
    try {
      const out = await api<RecordData>(path, { method: "POST", headers: adminHeaders(token) });
      setResult(isRecord(out) ? out : { result: out });
      setStatus(`${label} complete.`);
    } catch (e) {
      setResult(null);
      setStatus((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const upper = symbol.trim().toUpperCase();
  const sym = encodeURIComponent(upper);
  const hz = encodeURIComponent(horizon.trim());
  const ready = !!token && !!sym && !!hz && !busy;
  return (
    <Card className="mt-5">
      <CardContent className="flex flex-col gap-4">
        <h3>Calibration (champion / challenger)</h3>
        <p className="muted text-xs">
          Candidates are evaluated in shadow mode. Promotion and rollback are explicit admin actions and are audited.
        </p>
        <div
          role="group"
          aria-label="Candidate and rollback"
          className="flex flex-wrap items-end gap-3"
          // Escape cancels a pending rollback wherever focus is in this row (fields, Cancel or the button itself).
          onKeyDown={(e) => {
            if (e.key === "Escape" && confirmRollback) cancelRollback(e.target);
          }}
        >
          <div className="field my-0">
            <label htmlFor={`${id}-symbol`}>Asset symbol</label>
            <Input
              id={`${id}-symbol`}
              value={symbol}
              onChange={(e) => {
                setSymbol(e.target.value);
                setConfirmRollback(false);
              }}
              placeholder="BTC"
            />
          </div>
          <div className="field my-0">
            <label htmlFor={`${id}-horizon`}>Horizon</label>
            <Input
              id={`${id}-horizon`}
              value={horizon}
              onChange={(e) => {
                setHorizon(e.target.value);
                setConfirmRollback(false);
              }}
            />
          </div>
          <Button
            disabled={!ready}
            onClick={() => run("Candidate creation", `/admin/calibration/${sym}/candidate?horizon=${hz}`)}
          >
            Create candidate
          </Button>
          <Button
            ref={rollbackRef}
            variant={confirmRollback ? "destructive" : "outline"}
            disabled={!ready}
            aria-describedby={confirmRollback ? `${id}-rollback` : undefined}
            onClick={(e) => {
              if (!confirmRollback) return setConfirmRollback(true);
              // The second click of a double-click lands on the armed button: it is not a confirmation.
              if (e.detail > 1) return;
              setConfirmRollback(false);
              void run("Rollback", `/admin/calibration/${sym}/rollback?horizon=${hz}`);
            }}
          >
            {confirmRollback ? `Confirm roll back ${upper} ${horizon.trim()}` : "Roll back"}
          </Button>
          {confirmRollback && (
            <Button ref={cancelRef} variant="ghost" onClick={(e) => cancelRollback(e.currentTarget)}>
              Cancel
            </Button>
          )}
        </div>
        {confirmRollback && (
          <p id={`${id}-rollback`} className="text-xs text-warning">
            Rolling back retires the current {upper} {horizon.trim()} champion and restores the previous one (or none).
            Each confirmed rollback goes one version further back.
          </p>
        )}
        <div className="flex flex-wrap items-end gap-3">
          <div className="field my-0 min-w-[240px]">
            <label htmlFor={`${id}-candidate`}>Candidate ID</label>
            <Input id={`${id}-candidate`} value={candidate} onChange={(e) => setCandidate(e.target.value)} />
          </div>
          <Button
            variant="primary"
            disabled={!token || !candidate.trim() || busy}
            onClick={() =>
              run("Promotion", `/admin/calibration/promote/${encodeURIComponent(candidate.trim())}`)
            }
          >
            Promote candidate
          </Button>
        </div>
        <p role="status" className="text-xs">
          {status}
        </p>
        {result && <ConfigTable data={result} caption="Calibration result" />}
      </CardContent>
    </Card>
  );
}

function AutolearnTab({ data, token }: { data: unknown; token: string }) {
  // Candidate history can be long: it opens on demand under its own heading.
  const record: RecordData = isRecord(data) ? data : {};
  const { versions, ...settings } = record;
  return (
    <>
      <h2>Autolearn</h2>
      <ReadOnlyNote>
        Autolearn is quantitative and controlled: no model rewrites its own rules, and promotion is never automatic unless
        configured.
      </ReadOnlyNote>
      {isRecord(data) ? (
        <Card>
          <ConfigTable data={settings} caption="Autolearn settings" />
        </Card>
      ) : (
        <Unavailable what="autolearn settings" />
      )}
      {isRecord(versions) && (
        <details className="mt-5 rounded-lg border border-border bg-card">
          <summary className="cursor-pointer px-[18px] py-3 text-sm font-semibold">
            Calibration versions (champions, candidates and challengers)
          </summary>
          <ConfigTable data={versions} caption="Calibration versions" />
        </details>
      )}
      <CalibrationActions token={token} />
    </>
  );
}

/** Workspace settings (/settings, §33). */
export function SettingsPage() {
  // Admin token lives in page memory only (survives tab switches, never persisted). In local mode (single user on
  // this computer, the default) there is no password: every admin action is unlocked.
  const [typed, setToken] = useState("");
  const q = useSectionData<RecordData>("settings");
  const data = isRecord(q.data) ? q.data : undefined;
  const mode = data?.admin_mode === "local" ? "local" : data ? "token" : undefined;
  const token = mode === "local" ? LOCAL_ADMIN : typed;
  const roles = Array.isArray(data?.roles) ? (data.roles as RecordData[]) : [];
  const editable = new Set(Array.isArray(data?.editable) ? data.editable.filter((e) => typeof e === "string") : []);
  function content(t: TabId) {
    switch (t) {
      case "providers":
        return (
          <>
            <h2>Provider configuration</h2>
            {/* Failed and not loaded since: stays so while it is asked again in the background. */}
            <ProviderSettingsSection
              token={token}
              mode={mode}
              settingsFailed={q.data === undefined && (q.error !== null || q.errorUpdateCount > 0)}
            />
            <h2>Provider health</h2>
            <ProviderHealthSection token={token} />
          </>
        );
      case "models":
        return <ModelsTab roles={roles} token={token} />;
      case "sources":
        return <SourcesTab sources={data?.sources} editable={editable.has("sources")} />;
      case "refresh":
        return <RefreshTab data={data?.refresh} editable={editable.has("refresh")} />;
      case "scoring":
        return <ScoringTab data={data?.scoring} />;
      case "alerts":
        return <AlertsTab data={data?.alerts} editable={editable.has("alerts")} />;
      case "autolearn":
        return <AutolearnTab data={data?.autolearn} token={token} />;
    }
  }
  return (
    <>
      <PageHeading
        title="Workspace settings"
        description="Provider keys and health, model assignments and analysis configuration."
      />
      <QueryState
        loading={q.isLoading}
        error={q.error}
        loadingText="Loading workspace settings…"
        onRetry={() => void q.refetch()}
      />
      {mode === "token" && <AdminTokenField token={typed} setToken={setToken} />}
      <Tabs defaultValue={TABS[0].id}>
        <TabsList aria-label="Settings sections">
          {TABS.map((t) => (
            <TabsTrigger key={t.id} value={t.id}>
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
        {TABS.map((t) => (
          <TabsContent key={t.id} value={t.id}>
            <Card asChild className="panel settings-panel">
              <section aria-label={t.label}>{content(t.id)}</section>
            </Card>
          </TabsContent>
        ))}
      </Tabs>
    </>
  );
}
