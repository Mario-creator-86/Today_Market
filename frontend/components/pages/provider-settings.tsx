"use client";
import { useEffect, useId, useState } from "react";
import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { adminHeaders, api, ApiError, isTransientError, localTime, safeUrl } from "@/lib/api";
import {
  buildPayload,
  byCategory,
  fieldErrors,
  fieldHelp,
  inputValue,
  missingReentry,
  providerLabel,
  REENTER_HINT,
  secretsToReenter,
  type Draft,
  type DraftValue,
  type EnabledResult,
  type ProviderSettings,
  type SettingField,
  type SettingGroup,
  type TestResult,
} from "@/lib/provider-settings";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { QueryState } from "@/components/shared/query-state";
import { StatusBadge } from "@/components/shared/status-badge";
import { cn } from "@/lib/utils";
import { fmt, isNotFound, tone } from "./page-utils";

// Provider tests are bounded server-side at 30 s; allow for the round trip.
const TEST_TIMEOUT_MS = 45000;

function useDebounced<T>(value: T, ms: number): T {
  const [out, setOut] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setOut(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return out;
}

const admin = adminHeaders;

/**
 * Asked again (once at once, then by the workspace's background retry) only when the service did not answer or failed:
 * a refused token (401), a request from another computer (403) and admin not configured (503) answer the same every
 * time.
 */
export function retryProviderSettings(failures: number, error: Error): boolean {
  return failures < 1 && isTransientError(error) && !(error instanceof ApiError && error.status === 503);
}

/** GET /admin/provider-settings for the typed token (debounced, so typing does not send one request per key). */
export function useProviderSettings(token: string) {
  const key = useDebounced(token, 400);
  return useQuery({
    queryKey: ["provider-settings", key],
    queryFn: () => api<ProviderSettings>("/admin/provider-settings", { headers: admin(key) }),
    // Both settings and health observe this key. A later observer must not resubmit a refused token.
    // An explicit refetch still works; changing the token creates a fresh query.
    enabled: (query) => key !== "" && !refused(query.state.error),
    retry: retryProviderSettings,
    // A refused token is not asked again when the window regains focus or the network returns; a new token is.
    refetchOnWindowFocus: (query) => !refused(query.state.error),
    refetchOnReconnect: (query) => !refused(query.state.error),
  });
}

function refused(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}

/** Settings payload when the backend answered with the contract shape. */
export function settingsData(data: unknown): ProviderSettings | undefined {
  return data && typeof data === "object" && Array.isArray((data as ProviderSettings).groups)
    ? (data as ProviderSettings)
    : undefined;
}

/** Re-read configuration and health; the header's provider count (dashboard) refreshes in the background. */
async function refresh(cache: QueryClient) {
  void cache.invalidateQueries({ queryKey: ["dashboard"] });
  await Promise.all([
    cache.invalidateQueries({ queryKey: ["provider-settings"] }),
    cache.invalidateQueries({ queryKey: ["providers"] }),
  ]);
}

export function testProvider(target: string, token: string) {
  return api<TestResult>(`/admin/providers/${encodeURIComponent(target)}/test`, {
    method: "POST",
    headers: admin(token),
    signal: AbortSignal.timeout(TEST_TIMEOUT_MS),
  });
}

/** Native checkbox exposed as a switch; no extra dependency. */
export function Switch({
  checked,
  onChange,
  className,
  ...props
}: Omit<React.ComponentProps<"input">, "onChange" | "type" | "role"> & {
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <span className={cn("relative inline-flex h-5 w-9 shrink-0", className)}>
      <input
        type="checkbox"
        role="switch"
        aria-checked={checked}
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="peer absolute inset-0 z-10 m-0 size-full cursor-pointer opacity-0 disabled:cursor-not-allowed"
        {...props}
      />
      <span
        aria-hidden="true"
        className="pointer-events-none block h-5 w-9 rounded-full border border-input bg-secondary transition-colors after:absolute after:top-[3px] after:left-[3px] after:size-3.5 after:rounded-full after:bg-muted-foreground after:transition-transform peer-checked:border-primary peer-checked:bg-primary/30 peer-checked:after:translate-x-4 peer-checked:after:bg-primary peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-ring peer-disabled:opacity-50 motion-reduce:transition-none motion-reduce:after:transition-none"
      />
    </span>
  );
}

/**
 * Enable/disable switch for one provider (PUT /admin/providers/{name}/enabled).
 * `label` gives a visible label; otherwise `ariaLabel` names the switch.
 */
export function ProviderToggle({
  name,
  enabled,
  token,
  label,
  ariaLabel,
}: {
  name: string;
  enabled: boolean;
  token: string;
  label?: React.ReactNode;
  ariaLabel?: string;
}) {
  const id = useId();
  const cache = useQueryClient();
  const [pending, setPending] = useState<boolean | null>(null);
  const [error, setError] = useState("");
  async function toggle(next: boolean) {
    setPending(next);
    setError("");
    try {
      await api<EnabledResult>(`/admin/providers/${encodeURIComponent(name)}/enabled`, {
        method: "PUT",
        headers: admin(token),
        body: JSON.stringify({ enabled: next }),
      });
      await refresh(cache);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPending(null);
    }
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Switch
        id={id}
        checked={pending ?? enabled}
        disabled={!token || pending !== null}
        onChange={toggle}
        aria-label={label ? undefined : ariaLabel}
      />
      {label && (
        <label htmlFor={id} className="font-semibold text-foreground">
          {label}
        </label>
      )}
      <span role="status" className="text-destructive">
        {error}
      </span>
    </span>
  );
}

/** Result of POST /admin/providers/{target}/test. */
export function TestOutcome({ result }: { result: TestResult }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
      <StatusBadge value={result.status} variant={result.ok ? "positive" : tone(result.status)} />
      <strong className={result.ok ? "text-positive" : "text-destructive"}>{result.ok ? "Passed" : "Failed"}</strong>
      {result.detail && <span className="wrap-anywhere">{result.detail}</span>}
      {result.latency_ms !== null && result.latency_ms !== undefined && (
        <span className="text-muted-foreground">{fmt(result.latency_ms, { digits: 0, suffix: " ms" })}</span>
      )}
      <span className="text-muted-foreground">Checked {localTime(result.checked_at)}</span>
      {result.note && <span className="text-muted-foreground">{result.note}</span>}
    </span>
  );
}

const SOURCE_NOTE = {
  environment:
    "Set in the backend's OS environment, which overrides .env, so it cannot be changed here. Change it where the backend is started and restart it (Docker Compose: edit .env on the host and recreate the containers).",
  env_file: "Saved in .env.",
  default: "",
} as const;

function secretPlaceholder(field: SettingField, removed: boolean, reenter: boolean): string {
  if (!field.editable) return "Set by the OS environment (read-only)";
  if (removed) return "Will be removed when you save";
  if (reenter) return "Required: re-enter the key";
  return field.configured ? "Configured (.env) — type to replace" : "Not set";
}

/** One labelled control with help, inline validation and a two-step "Remove" for values stored in .env. */
function FieldRow({
  field,
  draft,
  error,
  reenter = false,
  onChange,
}: {
  field: SettingField;
  draft: Draft;
  error?: string;
  /** A destination this secret is paired with was edited: the key must be typed again to save. */
  reenter?: boolean;
  /** undefined = back to the stored value. */
  onChange: (value: DraftValue | undefined) => void;
}) {
  const id = useId();
  const [confirm, setConfirm] = useState(false);
  const removed = draft[field.name] === null;
  const value = inputValue(field, draft);
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;
  const reenterId = `${id}-reenter`;
  const common = {
    id,
    disabled: !field.editable || removed,
    "aria-describedby": [reenter && !removed ? reenterId : "", helpId, error ? errorId : ""]
      .filter(Boolean)
      .join(" "),
    "aria-invalid": error ? true : undefined,
  };
  // Inputs share the row with Remove when there is room.
  const grow = "w-auto min-w-0 max-w-[400px] flex-1 basis-60";
  const text = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => onChange(e.target.value);
  let control: React.ReactNode;
  if (field.kind === "bool")
    control = <Switch {...common} checked={value === true} onChange={(checked) => onChange(checked)} />;
  else if (field.kind === "select")
    control = (
      <NativeSelect {...common} value={String(value)} onChange={text} className={grow}>
        {!field.options?.includes(String(value)) && <option value={String(value)}>{String(value) || "Default"}</option>}
        {(field.options ?? []).map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </NativeSelect>
    );
  else if (field.kind === "secret" || field.secret)
    control = (
      <Input
        {...common}
        // Required (re-entry): outlined in the warning colour until a key is typed.
        className={cn(grow, reenter && !removed && !String(value).trim() && "border-warning")}
        type="password"
        autoComplete="new-password"
        spellCheck={false}
        required={reenter && !removed}
        aria-required={reenter && !removed ? true : undefined}
        value={String(value)}
        onChange={text}
        placeholder={secretPlaceholder(field, removed, reenter)}
      />
    );
  else
    control = (
      <Input
        {...common}
        className={grow}
        type={field.kind === "int" ? "number" : field.kind === "url" ? "url" : "text"}
        inputMode={field.kind === "int" ? "numeric" : field.kind.startsWith("email") ? "email" : undefined}
        min={field.min ?? undefined}
        max={field.max ?? undefined}
        step={field.kind === "int" ? 1 : undefined}
        autoComplete="off"
        spellCheck={false}
        value={String(value)}
        onChange={text}
        placeholder={removed ? "Will be removed when you save" : (field.placeholder ?? undefined)}
      />
    );
  const removable = field.editable && field.source === "env_file";
  return (
    <div className="field my-0">
      <label htmlFor={id} className="text-foreground">
        {field.label}
      </label>
      <div className="flex flex-wrap items-center gap-2">
        {control}
        {removed && <span className="text-xs text-warning">Will be removed when you save.</span>}
        {(removable || removed) && (
          // One slot so keyboard focus stays on the button as it changes: Remove → Confirm remove → Undo.
          <Button
            type="button"
            size="sm"
            variant={confirm ? "destructive" : "ghost"}
            aria-label={`${removed ? "Undo remove" : confirm ? "Confirm remove" : "Remove"} ${field.label}`}
            onClick={() => {
              if (removed) onChange(undefined);
              else if (confirm) onChange(null);
              setConfirm(!removed && !confirm);
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") setConfirm(false);
            }}
          >
            {removed ? "Undo" : confirm ? "Confirm remove" : "Remove"}
          </Button>
        )}
        {confirm && !removed && (
          <Button type="button" size="sm" variant="ghost" onClick={() => setConfirm(false)}>
            Cancel
          </Button>
        )}
      </div>
      {reenter && !removed && (
        <small id={reenterId} className="text-warning">
          {REENTER_HINT}
        </small>
      )}
      <small id={helpId} className="text-muted-foreground">
        <code className="mr-1.5">{field.env}</code>
        {[fieldHelp(field, field.required || (reenter && !removed)), SOURCE_NOTE[field.source]]
          .filter(Boolean)
          .join(" ")}
      </small>
      {error && (
        <small id={errorId} className="text-destructive">
          {error}
        </small>
      )}
    </div>
  );
}

/** One configurable group: status, fields, Save, Test and the group's provider switches. */
function GroupCard({ group, token, runtime }: { group: SettingGroup; token: string; runtime: string }) {
  const id = useId();
  const cache = useQueryClient();
  const [draft, setDraft] = useState<Draft>({});
  const [busy, setBusy] = useState<"" | "save" | "test">("");
  // Confirmations and failures are kept apart so a failure never looks like "Saved.".
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState<TestResult | null>(null);
  const fields = Array.isArray(group.fields) ? group.fields : [];
  const providers = Array.isArray(group.providers) ? group.providers : [];
  const errors = fieldErrors(fields, draft);
  const { values } = buildPayload(fields, draft);
  const reenter = secretsToReenter(fields, draft);
  const awaitingKey = missingReentry(fields, draft).length > 0;
  const dirty = Object.keys(values).length > 0;
  const invalid = Object.keys(errors).length > 0 || awaitingKey;
  const test = group.test?.available && group.test.target ? group.test : null;
  const signup = safeUrl(group.signup_url);
  function edit(name: string, value: DraftValue | undefined) {
    setDraft((d) => {
      const next = { ...d };
      if (value === undefined) delete next[name];
      else next[name] = value;
      return next;
    });
    setNotice("");
    setError("");
  }
  async function save() {
    if (!dirty || invalid || busy) return;
    setBusy("save");
    setNotice("");
    setError("");
    setResult(null);
    try {
      await api(`/admin/provider-settings/${encodeURIComponent(group.id)}`, {
        method: "PUT",
        headers: admin(token),
        body: JSON.stringify({ values }),
      });
      // Keep the typed values on screen until the saved state has been re-read.
      await refresh(cache);
      setDraft({});
      setNotice(
        runtime === "celery"
          ? "Saved. Background workers use the new settings from their next task."
          : "Saved. The new settings apply immediately.",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }
  async function runTest() {
    if (!test?.target) return;
    setBusy("test");
    setNotice("");
    setError("");
    setResult(null);
    try {
      setResult(await testProvider(test.target, token));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }
  return (
    <Card asChild>
      <article aria-labelledby={`${id}-title`} className="min-w-0">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0 flex-1 basis-[200px]">
            <h4 id={`${id}-title`} className="text-[15px] font-semibold text-foreground">
              {group.label}
            </h4>
            {group.description && <p className="mt-1 text-xs text-muted-foreground">{group.description}</p>}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {group.status && <StatusBadge value={group.status} variant={tone(group.status)} />}
            {group.pricing && <Badge variant="info">{group.pricing}</Badge>}
            {signup && (
              <a href={signup} target="_blank" rel="noopener noreferrer" className="source-link inline-flex items-center gap-1 text-xs">
                Get an API key
                <ExternalLink size={12} aria-hidden="true" />
                <span className="sr-only">(opens in a new tab)</span>
              </a>
            )}
          </div>
        </div>
        <form
          noValidate
          className="px-5 py-4"
          onSubmit={(e) => {
            e.preventDefault();
            save();
          }}
        >
          <div className="flex flex-col gap-4">
            {fields.map((f) => (
              <FieldRow
                key={f.name}
                field={f}
                draft={draft}
                error={errors[f.name]}
                reenter={reenter.includes(f.name)}
                onChange={(v) => edit(f.name, v)}
              />
            ))}
            {providers.length > 0 && (
              <fieldset className="min-w-0 border-0 p-0">
                <legend className="mb-2 text-xs text-muted-foreground">Providers (switch off to stop using one)</legend>
                <ul className="flex flex-col gap-2 text-xs">
                  {providers.map((p) => (
                    <li key={p.name} className="flex flex-wrap items-center gap-2">
                      <ProviderToggle name={p.name} enabled={p.enabled !== false} token={token} label={providerLabel(p)} />
                      <StatusBadge value={p.status} variant={tone(p.status)} />
                      {p.stub && <StatusBadge value="DEVELOPMENT STUB" variant="outline" />}
                    </li>
                  ))}
                </ul>
              </fieldset>
            )}
            <div className="flex flex-wrap items-center gap-3">
              {fields.length > 0 && (
                <Button
                  type="submit"
                  variant="primary"
                  aria-label={`Save ${group.label}`}
                  disabled={!token || !dirty || invalid || busy !== ""}
                >
                  {busy === "save" ? "Saving…" : "Save"}
                </Button>
              )}
              {test && (
                <Button
                  type="button"
                  variant="outline"
                  aria-label={`${test.label}: ${group.label}`}
                  aria-describedby={dirty || invalid ? `${id}-hint` : undefined}
                  disabled={!token || dirty || invalid || busy !== ""}
                  onClick={runTest}
                >
                  {busy === "test" ? "Testing…" : test.label}
                </Button>
              )}
              {test && (dirty || invalid) && (
                <small id={`${id}-hint`} className="text-muted-foreground">
                  Save first: tests use the saved settings.
                </small>
              )}
            </div>
            {/* A test result repeats its own note; the static one is not shown twice. */}
            {test?.note && !result?.note && <p className="text-xs text-muted-foreground">{test.note}</p>}
          </div>
          {/* Always rendered so screen readers announce what appears in it. */}
          <div role="status" className="flex flex-col gap-2 text-xs [&:not(:empty)]:mt-4">
            {notice && <p className="text-primary wrap-anywhere">{notice}</p>}
            {error && <p className="text-destructive wrap-anywhere">{error}</p>}
            {result && <TestOutcome result={result} />}
          </div>
        </form>
      </article>
    </Card>
  );
}

/**
 * Settings → Data providers: credentials, connection settings, enable/disable and tests (docs/PROVIDER_SETTINGS.md).
 * `mode` is the backend's admin mode from /settings: undefined while it loads (or when it failed), so the token
 * prompt appears only when the backend really asks for a token. `settingsFailed`: /settings has not loaded because
 * it failed (also while it is being asked again), so this section waits for it instead of "loading".
 */
export function ProviderSettingsSection({
  token,
  mode,
  settingsFailed = false,
}: {
  token: string;
  mode?: "local" | "token";
  settingsFailed?: boolean;
}) {
  const id = useId();
  const q = useProviderSettings(token);
  const data = settingsData(q.data);
  if (!mode && !token)
    return settingsFailed ? (
      <p className="mb-5 rounded-md border border-border bg-muted/60 px-4 py-3 text-xs text-muted-foreground">
        Provider configuration appears once the workspace settings load (see the notice above).
      </p>
    ) : (
      <Alert role="status">Loading provider configuration…</Alert>
    );
  if (!token)
    return (
      <p className="mb-5 rounded-md border border-border bg-muted/60 px-4 py-3 text-xs text-muted-foreground">
        <strong className="mr-1 text-foreground">Locked.</strong>
        Enter the admin token above to add or replace API keys, switch providers on or off and test connections. The
        token is <code>ADMIN_TOKEN</code> in the project&apos;s <code>.env</code> file; to see or create it, run{" "}
        <code>python -m scripts.admin_token</code> in the project folder.
      </p>
    );
  return (
    <div className="mb-6">
      <p className="mb-4 text-xs text-muted-foreground">
        Keys and connection settings are saved to the backend&apos;s <code>.env</code> file
        {data?.env_file && (
          <>
            {" "}
            (<code className="wrap-anywhere">{data.env_file}</code>)
          </>
        )}{" "}
        and apply without a restart. Saved keys are never shown again: leave a key field empty to keep it.
      </p>
      {isNotFound(q.error) ? (
        <Alert variant="destructive">
          This backend does not offer provider configuration yet. Update and restart the backend.
        </Alert>
      ) : (
        <QueryState
          loading={q.isLoading}
          error={q.error}
          loadingText="Loading provider configuration…"
          onRetry={() => void q.refetch()}
        />
      )}
      {data &&
        byCategory(data.groups).map((c) => (
          <section key={c.id} aria-labelledby={`${id}-${c.id}`} className="mb-6">
            <h3 id={`${id}-${c.id}`} className="mb-3">
              {c.label}
            </h3>
            <div className="grid gap-4 xl:grid-cols-2">
              {c.groups.map((g) => (
                <GroupCard key={g.id} group={g} token={token} runtime={data.runtime} />
              ))}
            </div>
          </section>
        ))}
    </div>
  );
}
