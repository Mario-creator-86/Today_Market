/**
 * AI usage today (GET /api/ai/usage) and the daily budget form (PUT /api/admin/config/ai_budget), docs/AI_AGENTS.md.
 * Every chat request to the AI provider counts against one daily budget (Bucharest day), a per-role daily cap and a
 * pace window, so a morning burst cannot use up the whole day. Pure helpers: the Settings → AI models panel renders
 * them (components/pages/ai-usage.tsx) and vitest covers them. Every field is optional: an older backend or a partial
 * answer must still render honestly ("Not reported"), never as a measured zero.
 */
import { bucharestDay } from "./event-time";
import { isNum, labelCase } from "./format";

export type AiUsageRole = {
  role: string;
  /** Plain name from the backend, e.g. "Market interpretation". */
  label?: string | null;
  /** Requests that reached the provider today. */
  used?: number | null;
  /** role_daily cap; 0 means the role never calls the AI. */
  cap?: number | null;
  /** Prompt + completion tokens reported by the provider, null when not stored. */
  tokens?: number | null;
  /** Calls the budget refused (no request sent). */
  refused?: number | null;
  succeeded?: number | null;
  failed?: number | null;
};

export type AiUsagePace = {
  window_hours?: number | null;
  limit?: number | null;
  used?: number | null;
  /** The part of the pace window the roles outside the reserve may use together, and what they used. */
  shared_limit?: number | null;
  shared_used?: number | null;
};
export type AiUsageDay = { day: string; used?: number | null; tokens?: number | null };
export type AiLimiterState = {
  concurrency_limit?: number | null;
  in_flight?: number | null;
  queued?: number | null;
  paused_until?: string | null;
};

export type AiUsage = {
  day?: string | null;
  resets_at?: string | null;
  daily_requests?: number | null;
  used?: number | null;
  remaining?: number | null;
  /** Share of the daily budget kept for the reserve roles (interpretation, chief analyst, auditor). */
  reserve_share?: number | null;
  pace?: AiUsagePace | null;
  roles: AiUsageRole[];
  last_rate_limited_at?: string | null;
  history: AiUsageDay[];
  limiter?: AiLimiterState | null;
};

/** Plain names for the roles when the backend sends none (config/rules.d/ai_runtime.json budget.role_daily). */
export const ROLE_LABELS: Record<string, string> = {
  market_interpreter: "Market interpretation",
  chief_analyst: "Chief analyst",
  auditor: "Auditor",
  macro: "Macro specialist",
  fx: "FX specialist",
  crypto: "Crypto specialist",
  equity_credit: "Equity and credit specialist",
  geopolitical_energy: "Geopolitics and energy specialist",
  fact_checker: "Fact checker",
  research_planner: "Research planner",
  extractor: "News extraction",
};

/** Budget limits, as the backend validates them (backend/api_admin.py ai_budget). */
export const DAILY_MAX = 10000;
export const ROLE_MAX = 5000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function num(value: unknown): number | null {
  return isNum(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

/** GET /ai/usage as the typed shape, or null when the answer is not a usage record at all. */
export function parseUsage(data: unknown): AiUsage | null {
  if (!isRecord(data)) return null;
  const roles = Array.isArray(data.roles)
    ? data.roles
        .filter((r): r is Record<string, unknown> => isRecord(r) && !!str(r.role))
        .map((r) => ({
          role: r.role as string,
          label: str(r.label),
          used: num(r.used),
          cap: num(r.cap),
          tokens: num(r.tokens),
          refused: num(r.refused),
          succeeded: num(r.succeeded),
          failed: num(r.failed),
        }))
    : [];
  const history = Array.isArray(data.history)
    ? data.history
        .filter((d): d is Record<string, unknown> => isRecord(d) && !!str(d.day))
        .map((d) => ({ day: d.day as string, used: num(d.used), tokens: num(d.tokens) }))
    : [];
  if (!roles.length && !history.length && num(data.daily_requests) === null && num(data.used) === null) return null;
  const pace = isRecord(data.pace)
    ? {
        window_hours: num(data.pace.window_hours),
        limit: num(data.pace.limit),
        used: num(data.pace.used),
        shared_limit: num(data.pace.shared_limit),
        shared_used: num(data.pace.shared_used),
      }
    : null;
  const limiter = isRecord(data.limiter)
    ? {
        concurrency_limit: num(data.limiter.concurrency_limit),
        in_flight: num(data.limiter.in_flight),
        queued: num(data.limiter.queued),
        paused_until: str(data.limiter.paused_until),
      }
    : null;
  return {
    day: str(data.day),
    resets_at: str(data.resets_at),
    daily_requests: num(data.daily_requests),
    used: num(data.used),
    remaining: num(data.remaining),
    reserve_share: num(data.reserve_share),
    pace,
    roles,
    last_rate_limited_at: str(data.last_rate_limited_at),
    history,
    limiter,
  };
}

/** A role's plain name: the backend's label, the known name, or the key in words. */
export function roleLabel(role: Pick<AiUsageRole, "role" | "label">): string {
  return str(role.label) ?? ROLE_LABELS[role.role] ?? labelCase(role.role);
}

/** used / limit clamped to 0..1; null when either is unknown or the limit is not positive. */
export function usageShare(used: unknown, limit: unknown): number | null {
  if (!isNum(used) || !isNum(limit) || limit <= 0) return null;
  return Math.min(Math.max(used / limit, 0), 1);
}

export type UsageLevel = "ok" | "warning" | "exhausted" | "unknown";

/** How close a count is to its limit: "exhausted" at or over it (or a limit of 0), "warning" from 80 %. */
export function usageLevel(used: unknown, limit: unknown): UsageLevel {
  if (!isNum(used) || !isNum(limit)) return "unknown";
  if (limit <= 0 || used >= limit) return "exhausted";
  return used / limit >= 0.8 ? "warning" : "ok";
}

/**
 * aria-valuenow for a usage bar: the count clamped to 0..limit, since ARIA expects the value within the range (the
 * true figure goes in aria-valuetext). Undefined when the count is unknown.
 */
export function ariaValueNow(used: unknown, limit: unknown): number | undefined {
  if (!isNum(used)) return undefined;
  return isNum(limit) && limit >= 0 ? Math.min(Math.max(used, 0), limit) : used;
}

/** Requests left today: the backend's figure, else the budget minus what was used (never below 0). */
export function remainingToday(usage: Pick<AiUsage, "remaining" | "daily_requests" | "used">): number | null {
  if (isNum(usage.remaining)) return Math.max(usage.remaining, 0);
  if (isNum(usage.daily_requests) && isNum(usage.used)) return Math.max(usage.daily_requests - usage.used, 0);
  return null;
}

/** Requests the non-reserve roles may use today: the budget minus the reserve share (the backend's rule). */
export function nonReserveLimit(daily: unknown, reserveShare: unknown): number | null {
  if (!isNum(daily)) return null;
  const share = isNum(reserveShare) ? Math.min(Math.max(reserveShare, 0), 1) : 0;
  return Math.floor(daily * (1 - share));
}

export type BudgetNotice = { text: string; tone: "warning" | "destructive" };

function count(value: number): string {
  return Math.round(value).toLocaleString("en-US");
}

/**
 * One plain sentence about today's budget state, or null while it is comfortably within budget. `reset` is the reset
 * time as the page shows it. Order: no budget reported, a budget of 0, the day used up, the reserve mark passed (only
 * the reserve roles may still call the AI), the pace window full, the non-reserve share of the pace window full, 80 %.
 */
export function budgetNotice(usage: AiUsage, reset: string): BudgetNotice | null {
  const daily = usage.daily_requests;
  if (!isNum(daily))
    return isNum(usage.used)
      ? {
          text: "No daily AI budget is reported. Without a budget in the configuration, AI requests are not limited.",
          tone: "warning",
        }
      : null;
  if (daily <= 0)
    return {
      text: "The daily AI budget is 0: no AI request is sent. The app uses its rule-based interpretation and extraction.",
      tone: "warning",
    };
  const level = usageLevel(usage.used, daily);
  if (level === "exhausted")
    return {
      text: `Today's AI budget is used up. AI requests resume at ${reset}; until then the app uses its rule-based interpretation and extraction.`,
      tone: "destructive",
    };
  const shared = nonReserveLimit(daily, usage.reserve_share);
  if (isNum(usage.reserve_share) && usage.reserve_share > 0 && shared !== null && isNum(usage.used) && usage.used >= shared)
    return {
      text: `Research, news extraction and the specialists have stopped for today (${count(usage.used)} of ${count(daily)} requests used, past the mark at ${count(shared)}). Until ${reset} only the market interpretation, the chief analyst and the auditor may call the AI.`,
      tone: "warning",
    };
  const pace = usage.pace;
  if (pace && usageLevel(pace.used, pace.limit) === "exhausted")
    return {
      text: `The hourly pace is reached (${count(pace.used ?? 0)} / ${count(pace.limit ?? 0)} requests in the last ${pace.window_hours ?? "few"} h). New AI requests wait until older ones leave the window, so the budget lasts the whole day.`,
      tone: "warning",
    };
  if (pace && isNum(pace.shared_limit) && isNum(pace.shared_used) && pace.shared_used >= pace.shared_limit)
    return {
      text: `Research, news extraction and the specialists have used their share of the hourly pace (${count(pace.shared_used)} / ${count(pace.shared_limit)} in the last ${pace.window_hours ?? "few"} h). They resume as older requests leave the window; the interpretation, the chief analyst and the auditor keep the rest.`,
      tone: "warning",
    };
  if (level === "warning") return { text: `More than 80 % of today's AI budget is used. It resets at ${reset}.`, tone: "warning" };
  return null;
}

/** "Mon 28 Sep" for a "YYYY-MM-DD" Bucharest day; the raw text when it is not a date. */
export function dayLabel(day: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return day;
  // Noon UTC is the same calendar day in Bucharest (UTC+2/+3).
  const t = Date.parse(`${day}T12:00:00Z`);
  return Number.isFinite(t) ? bucharestDay(t) : day;
}

/** History oldest first, at most the last `limit` days. */
export function historyDays(history: AiUsageDay[], limit = 7): AiUsageDay[] {
  return [...history].sort((a, b) => a.day.localeCompare(b.day)).slice(-limit);
}

/** A token count as text ("12,345"), "Not reported" when the provider's counts were not stored. */
export function tokensText(value: unknown): string {
  return isNum(value) ? Math.round(value).toLocaleString("en-US") : "Not reported";
}

// ---------------------------------------------------------------------------------------------------------------
// Budget form (PUT /admin/config/ai_budget)

/** Form values as typed (strings, so an empty or half-typed field stays what the user sees). */
export type BudgetDraft = { daily: string; roles: Record<string, string> };

/** The form's starting values from the usage record: the current daily budget and each role's cap. */
export function draftFrom(usage: Pick<AiUsage, "daily_requests" | "roles">): BudgetDraft {
  const roles: Record<string, string> = {};
  for (const r of usage.roles) if (isNum(r.cap)) roles[r.role] = String(r.cap);
  return { daily: isNum(usage.daily_requests) ? String(usage.daily_requests) : "", roles };
}

/** A whole number from 0 to max, or null. */
function wholeNumber(value: string, max: number): number | null {
  const s = value.trim();
  if (!/^\d+$/.test(s)) return null;
  const n = Number(s);
  return n <= max ? n : null;
}

/** Validation message per field ("daily" or a role key); empty when the draft can be saved. */
export function budgetErrors(draft: BudgetDraft): Record<string, string> {
  const errors: Record<string, string> = {};
  if (wholeNumber(draft.daily, DAILY_MAX) === null)
    errors.daily = `Enter a whole number from 0 to ${DAILY_MAX.toLocaleString("en-US")}.`;
  for (const [role, value] of Object.entries(draft.roles))
    if (wholeNumber(value, ROLE_MAX) === null)
      errors[role] = `Enter a whole number from 0 to ${ROLE_MAX.toLocaleString("en-US")}.`;
  return errors;
}

export type BudgetPayload = { daily_requests?: number; role_daily?: Record<string, number> };

/**
 * The PUT body: only the values that differ from `saved` (the backend merges them into the budget). Null when nothing
 * changed or a value is invalid.
 */
export function budgetPayload(draft: BudgetDraft, saved: BudgetDraft): BudgetPayload | null {
  if (Object.keys(budgetErrors(draft)).length) return null;
  const payload: BudgetPayload = {};
  const daily = Number(draft.daily.trim());
  if (daily !== Number(saved.daily)) payload.daily_requests = daily;
  const roles: Record<string, number> = {};
  for (const [role, value] of Object.entries(draft.roles)) {
    const n = Number(value.trim());
    if (saved.roles[role] === undefined || n !== Number(saved.roles[role])) roles[role] = n;
  }
  if (Object.keys(roles).length) payload.role_daily = roles;
  return Object.keys(payload).length ? payload : null;
}

/** Sum of the per-role caps in the draft (valid values only). */
export function capTotal(draft: BudgetDraft): number {
  return Object.values(draft.roles).reduce((sum, v) => {
    const n = wholeNumber(v, ROLE_MAX);
    return n === null ? sum : sum + n;
  }, 0);
}

/**
 * Plain-English notes on a valid draft: a budget of 0 switches the AI off, caps of 0 switch roles off, and caps that
 * add up to less than the budget leave part of it unused.
 */
export function budgetHints(draft: BudgetDraft, labels: Record<string, string> = {}): string[] {
  if (Object.keys(budgetErrors(draft)).length) return [];
  const hints: string[] = [];
  const daily = Number(draft.daily.trim());
  if (daily === 0) hints.push("A daily budget of 0 switches every AI request off; the app uses its rule-based fallbacks.");
  const off = Object.entries(draft.roles)
    .filter(([, v]) => Number(v.trim()) === 0)
    .map(([role]) => labels[role] ?? ROLE_LABELS[role] ?? labelCase(role));
  if (off.length) hints.push(`Never calls the AI (cap 0): ${off.join(", ")}.`);
  const total = capTotal(draft);
  if (daily > 0 && Object.keys(draft.roles).length && total < daily)
    hints.push(
      `The role caps add up to ${total.toLocaleString("en-US")}, below the daily budget of ${daily.toLocaleString("en-US")}: the rest cannot be used.`,
    );
  return hints;
}
