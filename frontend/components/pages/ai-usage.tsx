"use client";
import { useId, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { adminHeaders, api, localTime } from "@/lib/api";
import {
  ariaValueNow,
  budgetErrors,
  budgetHints,
  budgetNotice,
  budgetPayload,
  DAILY_MAX,
  dayLabel,
  draftFrom,
  historyDays,
  nonReserveLimit,
  parseUsage,
  remainingToday,
  ROLE_MAX,
  roleLabel,
  tokensText,
  usageLevel,
  usageShare,
  type AiUsage,
  type AiUsageDay,
  type AiUsageRole,
  type BudgetDraft,
  type UsageLevel,
} from "@/lib/ai-usage";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/shared/empty-state";
import { QueryState } from "@/components/shared/query-state";
import { cn } from "@/lib/utils";
import { Column, DataTable, fmt, isNotFound, isNum, Stat } from "./page-utils";

/** Refreshed every minute while Settings is open; reading it never calls the AI. */
const USAGE_REFRESH_MS = 60_000;

const BAR_TONE: Record<UsageLevel, string> = {
  ok: "bg-primary",
  warning: "bg-warning",
  exhausted: "bg-destructive",
  unknown: "bg-muted-foreground",
};

/** "12 / 400"; "12 / Not reported" when the limit is unknown. */
function ofLimit(used: unknown, limit: unknown): string {
  return `${fmt(used, { digits: 0, missing: "Not reported" })} / ${fmt(limit, { digits: 0, missing: "Not reported" })}`;
}

/** Horizontal usage bar (role=progressbar); `mark` draws a tick at a share of the width (the reserve boundary). */
function UsageBar({
  used,
  limit,
  label,
  mark,
  className,
}: {
  used: unknown;
  limit: unknown;
  label: string;
  mark?: number | null;
  className?: string;
}) {
  const share = usageShare(used, limit);
  const level = usageLevel(used, limit);
  const shown = level === "exhausted" && isNum(limit) && limit <= 0 ? 1 : (share ?? 0);
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={isNum(limit) ? limit : undefined}
      aria-valuenow={ariaValueNow(used, limit)}
      aria-valuetext={ofLimit(used, limit)}
      className={cn("relative h-2 w-full overflow-hidden rounded-full bg-secondary", className)}
    >
      <div className={cn("h-full rounded-full", BAR_TONE[level])} style={{ width: `${shown * 100}%` }} />
      {isNum(mark) && mark > 0 && mark < 1 && (
        <span aria-hidden="true" className="absolute inset-y-0 w-px bg-foreground/70" style={{ left: `${mark * 100}%` }} />
      )}
    </div>
  );
}

function roleUse(r: AiUsageRole): React.ReactNode {
  if (isNum(r.cap) && r.cap <= 0)
    return (
      <span className="text-muted-foreground">
        Off (cap 0){isNum(r.used) && r.used > 0 ? ` · ${fmt(r.used, { digits: 0 })} used` : ""}
      </span>
    );
  return (
    <span className="flex min-w-[120px] flex-col gap-1">
      <span className="tabular-nums">{ofLimit(r.used, r.cap)}</span>
      <UsageBar used={r.used} limit={r.cap} label={`${roleLabel(r)}: requests used today`} />
    </span>
  );
}

function count(value: unknown): string {
  return fmt(value, { digits: 0, missing: "Not reported" });
}

const ROLE_COLUMNS: Column<AiUsageRole>[] = [
  { key: "role", label: "Role", render: (r) => <strong>{roleLabel(r)}</strong> },
  { key: "used", label: "Used / daily cap", render: roleUse },
  { key: "tokens", label: "Tokens", numeric: true, render: (r) => tokensText(r.tokens) },
  {
    key: "refused",
    label: "Refused by budget",
    numeric: true,
    render: (r) =>
      isNum(r.refused) && r.refused > 0 ? <span className="text-warning">{count(r.refused)}</span> : count(r.refused),
  },
  { key: "succeeded", label: "Succeeded", numeric: true, render: (r) => count(r.succeeded) },
  { key: "failed", label: "Failed", numeric: true, render: (r) => count(r.failed) },
];

function historyColumns(daily: unknown): Column<AiUsageDay>[] {
  return [
    { key: "day", label: "Day", render: (d) => dayLabel(d.day) },
    {
      key: "used",
      label: "Requests",
      render: (d) => (
        <span className="flex min-w-[140px] items-center gap-3">
          <span className="w-12 shrink-0 text-right tabular-nums">{count(d.used)}</span>
          {/* Scaled to today's budget: a past day over it shows a full red bar. */}
          <UsageBar used={d.used} limit={daily} label={`${dayLabel(d.day)}: requests`} className="flex-1" />
        </span>
      ),
    },
    { key: "tokens", label: "Tokens", numeric: true, render: (d) => tokensText(d.tokens) },
  ];
}

function limiterText(usage: AiUsage): string | null {
  const l = usage.limiter;
  if (!l) return null;
  const parts = [
    isNum(l.in_flight) && isNum(l.concurrency_limit)
      ? `${fmt(l.in_flight, { digits: 0 })} of ${fmt(l.concurrency_limit, { digits: 0 })} parallel slots in use`
      : null,
    isNum(l.queued) ? `${fmt(l.queued, { digits: 0 })} waiting` : null,
    l.paused_until && Date.parse(l.paused_until) > Date.now() ? `paused until ${localTime(l.paused_until)}` : null,
  ].filter(Boolean);
  return parts.length ? `Right now: ${parts.join(", ")}.` : null;
}

function UsageSummary({ usage }: { usage: AiUsage }) {
  const remaining = remainingToday(usage);
  const reserveMark = isNum(usage.reserve_share) && usage.reserve_share > 0 ? 1 - usage.reserve_share : null;
  const nonReserve = nonReserveLimit(usage.daily_requests, usage.reserve_share);
  const notice = budgetNotice(usage, usage.resets_at ? localTime(usage.resets_at) : "midnight (Bucharest time)");
  const pace = usage.pace;
  const limiter = limiterText(usage);
  return (
    <>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2 text-sm">
        <span>
          <strong className="text-lg tabular-nums">{ofLimit(usage.used, usage.daily_requests)}</strong>{" "}
          <span className="text-muted-foreground">requests used today{usage.day ? ` (${dayLabel(usage.day)})` : ""}</span>
        </span>
        <span className="text-xs text-muted-foreground">
          Resets {usage.resets_at ? localTime(usage.resets_at) : "at midnight"} (Bucharest time)
        </span>
      </div>
      <UsageBar used={usage.used} limit={usage.daily_requests} label="AI requests used today" mark={reserveMark} className="h-3" />
      {reserveMark !== null && nonReserve !== null && (
        <p className="mt-1 text-xs text-muted-foreground">
          The mark at {fmt(nonReserve, { digits: 0 })} requests: past it only the market interpretation, the chief analyst
          and the auditor may call the AI ({fmt((usage.reserve_share ?? 0) * 100, { digits: 0 })} % kept for them).
        </p>
      )}
      {notice && (
        <p
          role="status"
          className={cn("mt-3 text-xs", notice.tone === "destructive" ? "text-destructive" : "text-warning")}
        >
          {notice.text}
        </p>
      )}
      <div className="mt-4 grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3">
        <Stat label="Remaining today" value={fmt(remaining, { digits: 0, missing: "Not reported" })} />
        <Stat
          label={pace && isNum(pace.window_hours) ? `Pace (last ${fmt(pace.window_hours, { digits: 1 })} h)` : "Pace"}
          value={pace ? ofLimit(pace.used, pace.limit) : "Not reported"}
          detail={
            pace && isNum(pace.shared_limit)
              ? `Requests allowed per window, so a burst cannot use up the day. Research, extraction and specialists: ${ofLimit(pace.shared_used, pace.shared_limit)}`
              : "Requests allowed per window, so a burst cannot use up the day"
          }
        />
        <Stat
          label="Last rate limit"
          value={usage.last_rate_limited_at ? localTime(usage.last_rate_limited_at) : "None recorded"}
          detail="The provider answered 429 (quota or too many at once)"
        />
      </div>
      {limiter && <p className="mt-3 text-xs text-muted-foreground">{limiter}</p>}
    </>
  );
}

/** Daily budget and per-role caps (PUT /admin/config/ai_budget); applies without a restart. */
function BudgetForm({ usage, token }: { usage: AiUsage; token: string }) {
  const id = useId();
  const cache = useQueryClient();
  const server = draftFrom(usage);
  const serverKey = JSON.stringify(server);
  const [base, setBase] = useState(serverKey);
  const [saved, setSaved] = useState<BudgetDraft>(server);
  const [draft, setDraft] = useState<BudgetDraft>(server);
  const [status, setStatus] = useState<{ text: string; error: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  // A budget changed elsewhere (another tab, the config file) replaces the form, unless it holds unsaved edits.
  if (serverKey !== base) {
    setBase(serverKey);
    if (!dirty) {
      setSaved(server);
      setDraft(server);
    }
  }
  const errors = budgetErrors(draft);
  const payload = budgetPayload(draft, saved);
  const labels = Object.fromEntries(usage.roles.map((r) => [r.role, roleLabel(r)]));
  const hints = budgetHints(draft, labels);
  const roles = usage.roles.filter((r) => r.role in draft.roles);
  async function save() {
    if (!payload) return;
    setBusy(true);
    setStatus(null);
    try {
      await api("/admin/config/ai_budget", {
        method: "PUT",
        headers: adminHeaders(token),
        body: JSON.stringify(payload),
      });
      setSaved(draft);
      setStatus({ text: "Budget saved. It applies to the next AI request; no restart is needed.", error: false });
      void cache.invalidateQueries({ queryKey: ["ai-usage"] });
      void cache.invalidateQueries({ queryKey: ["settings"] });
    } catch (e) {
      setStatus({ text: (e as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  }
  function field(key: string, label: string, value: string, max: number, onChange: (v: string) => void) {
    const error = errors[key];
    return (
      <div className="field my-0" key={key}>
        <label htmlFor={`${id}-${key}`}>{label}</label>
        <Input
          id={`${id}-${key}`}
          type="number"
          inputMode="numeric"
          min={0}
          max={max}
          step={1}
          value={value}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-${key}-error` : undefined}
          onChange={(e) => {
            onChange(e.target.value);
            setStatus(null);
          }}
        />
        {error && (
          <small id={`${id}-${key}-error`} className="text-destructive">
            {error}
          </small>
        )}
      </div>
    );
  }
  return (
    <form
      aria-label="AI budget"
      className="mt-6 border-t border-border pt-5"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <h3 className="mb-1">Daily budget</h3>
      <p className="mb-4 text-xs text-muted-foreground">
        How many AI requests the app may send per day (Bucharest time), and per role. A cap of 0 means that role never
        calls the AI and uses its rule-based fallback. Set it to what your AI plan allows per day.
      </p>
      <div className="max-w-[260px]">
        {field("daily", "Daily requests (all roles)", draft.daily, DAILY_MAX, (v) => setDraft({ ...draft, daily: v }))}
      </div>
      {roles.length > 0 && (
        <fieldset className="mt-4">
          <legend className="mb-2 text-xs font-semibold text-foreground">Daily cap per role</legend>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-x-4 gap-y-3">
            {roles.map((r) =>
              field(r.role, roleLabel(r), draft.roles[r.role], ROLE_MAX, (v) =>
                setDraft({ ...draft, roles: { ...draft.roles, [r.role]: v } }),
              ),
            )}
          </div>
        </fieldset>
      )}
      {hints.length > 0 && (
        <ul className="mt-3 list-disc pl-5 text-xs text-muted-foreground">
          {hints.map((h) => (
            <li key={h}>{h}</li>
          ))}
        </ul>
      )}
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={!token || !payload || busy}>
          {busy ? "Saving…" : "Save budget"}
        </Button>
        <Button
          type="button"
          variant="outline"
          disabled={!dirty || busy}
          onClick={() => {
            setDraft(saved);
            setStatus(null);
          }}
        >
          Undo changes
        </Button>
        {!token && <span className="text-xs text-muted-foreground">Enter the admin token to change the budget.</span>}
      </div>
      <p role="status" className={cn("mt-2 text-xs", status?.error ? "text-destructive" : "text-positive")}>
        {status?.text ?? ""}
      </p>
    </form>
  );
}

/** Settings → AI models: "AI usage today" from GET /ai/usage, with the budget form. */
export function AiUsageSection({ token }: { token: string }) {
  const headingId = useId();
  const q = useQuery({
    queryKey: ["ai-usage"],
    queryFn: () => api<unknown>("/ai/usage"),
    refetchInterval: USAGE_REFRESH_MS,
    // An older backend without the endpoint answers 404 every time.
    retry: (failures, error) => failures < 2 && !isNotFound(error),
  });
  const usage = q.data === undefined ? null : parseUsage(q.data);
  const history = usage ? historyDays(usage.history) : [];
  return (
    <section aria-labelledby={headingId} className="mb-8">
      <h2 id={headingId}>AI usage today</h2>
      <p className="muted mb-3">
        Every request to the AI provider counts against a daily budget, so the plan&apos;s quota lasts the whole day.
        When a limit is reached the app keeps working with its rule-based fallbacks.
      </p>
      {isNotFound(q.error) && !usage ? (
        <EmptyState
          title="AI usage is not reported"
          detail="This backend does not report AI usage yet. Update and restart the backend to see the daily budget."
        />
      ) : (
        <QueryState
          loading={q.isLoading}
          error={q.error}
          loadingText="Loading AI usage…"
          onRetry={() => void q.refetch()}
        />
      )}
      {q.isSuccess && !usage && (
        <EmptyState title="No AI usage reported" detail="The backend answered without usage figures for today." />
      )}
      {usage && (
        <>
          <Card className="mb-4">
            <CardContent>
              <UsageSummary usage={usage} />
            </CardContent>
          </Card>
          <h3 className="mb-2">By role</h3>
          <Card className="mb-4">
            <DataTable
              rows={usage.roles}
              columns={ROLE_COLUMNS}
              rowKey={(r) => r.role}
              caption="AI requests by role today"
              empty={<EmptyState title="No roles reported" detail="The backend sent no per-role figures." />}
            />
          </Card>
          <h3 className="mb-2">Last 7 days</h3>
          <Card className="mb-4">
            <DataTable
              rows={history}
              columns={historyColumns(usage.daily_requests)}
              rowKey={(d) => d.day}
              caption="AI requests per day"
              empty={<EmptyState title="No history yet" detail="Past days appear once AI requests are recorded." />}
            />
          </Card>
          {isNum(usage.daily_requests) ? (
            <BudgetForm usage={usage} token={token} />
          ) : (
            <EmptyState
              title="Budget settings not reported"
              detail="The backend did not send the daily budget, so it cannot be edited here."
            />
          )}
        </>
      )}
    </section>
  );
}
