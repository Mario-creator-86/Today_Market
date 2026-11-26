import { describe, expect, it } from "vitest";
import {
  ariaValueNow,
  budgetErrors,
  budgetHints,
  budgetNotice,
  budgetPayload,
  capTotal,
  dayLabel,
  draftFrom,
  historyDays,
  nonReserveLimit,
  parseUsage,
  remainingToday,
  roleLabel,
  tokensText,
  usageLevel,
  usageShare,
  type BudgetDraft,
} from "./ai-usage";

const USAGE = {
  day: "2026-09-28",
  resets_at: "2026-09-28T21:00:00Z",
  daily_requests: 400,
  used: 120,
  remaining: 280,
  reserve_share: 0.25,
  pace: { window_hours: 2, limit: 33, used: 12 },
  roles: [
    { role: "market_interpreter", label: "Market interpretation", used: 10, cap: 48, tokens: 51234, refused: 0, succeeded: 9, failed: 1 },
    { role: "research_planner", label: null, used: 40, cap: 40, tokens: null, refused: 7, succeeded: 30, failed: 10 },
    { role: "extractor", used: 70, cap: 120 },
  ],
  last_rate_limited_at: null,
  history: [
    { day: "2026-09-27", used: 390, tokens: 900000 },
    { day: "2026-09-26", used: 1100, tokens: null },
  ],
  limiter: { concurrency_limit: 4, in_flight: 1, queued: 0, paused_until: null },
};

describe("parseUsage", () => {
  it("keeps the contract fields and drops malformed rows", () => {
    const usage = parseUsage({
      ...USAGE,
      roles: [...USAGE.roles, { label: "no key" }, "text", null],
      history: [...USAGE.history, { used: 3 }],
    })!;
    expect(usage.daily_requests).toBe(400);
    expect(usage.roles.map((r) => r.role)).toEqual(["market_interpreter", "research_planner", "extractor"]);
    expect(usage.roles[2]).toMatchObject({ tokens: null, refused: null });
    expect(usage.history).toHaveLength(2);
    expect(usage.pace).toEqual({ window_hours: 2, limit: 33, used: 12, shared_limit: null, shared_used: null });
    expect(parseUsage({ ...USAGE, pace: { ...USAGE.pace, shared_limit: 24, shared_used: 9 } })!.pace).toMatchObject({
      shared_limit: 24,
      shared_used: 9,
    });
    expect(usage.limiter).toMatchObject({ concurrency_limit: 4, paused_until: null });
  });

  it("is null for anything that is not a usage record", () => {
    expect(parseUsage(null)).toBeNull();
    expect(parseUsage([])).toBeNull();
    expect(parseUsage({ detail: "nope" })).toBeNull();
  });

  it("tolerates a partial answer: missing numbers stay null, never zero", () => {
    const usage = parseUsage({ used: 5 })!;
    expect(usage.daily_requests).toBeNull();
    expect(usage.pace).toBeNull();
    expect(usage.limiter).toBeNull();
    expect(usage.roles).toEqual([]);
    expect(remainingToday(usage)).toBeNull();
  });
});

describe("labels and figures", () => {
  it("uses the backend label, then the known name, then the key in words", () => {
    expect(roleLabel({ role: "market_interpreter", label: "Market interpretation" })).toBe("Market interpretation");
    expect(roleLabel({ role: "research_planner", label: null })).toBe("Research planner");
    expect(roleLabel({ role: "extractor" })).toBe("News extraction");
    expect(roleLabel({ role: "new_role" })).toBe("New role");
  });

  it("measures usage against a limit", () => {
    expect(usageShare(100, 400)).toBe(0.25);
    expect(usageShare(500, 400)).toBe(1);
    expect(usageShare(1, 0)).toBeNull();
    expect(usageShare(null, 400)).toBeNull();
    expect(usageLevel(100, 400)).toBe("ok");
    expect(usageLevel(320, 400)).toBe("warning");
    expect(usageLevel(400, 400)).toBe("exhausted");
    expect(usageLevel(0, 0)).toBe("exhausted");
    expect(usageLevel(undefined, 400)).toBe("unknown");
  });

  it("computes what is left and the reserve boundary", () => {
    expect(remainingToday({ remaining: 280, daily_requests: 400, used: 120 })).toBe(280);
    expect(remainingToday({ remaining: -3 })).toBe(0);
    expect(remainingToday({ daily_requests: 400, used: 450 })).toBe(0);
    expect(nonReserveLimit(400, 0.25)).toBe(300);
    expect(nonReserveLimit(401, 0.25)).toBe(300);
    expect(nonReserveLimit(400, null)).toBe(400);
    expect(nonReserveLimit(null, 0.25)).toBeNull();
  });

  it("formats days, history and tokens", () => {
    expect(dayLabel("2026-09-28")).toBe("Mon 28 Sep");
    expect(dayLabel("yesterday")).toBe("yesterday");
    const days = Array.from({ length: 9 }, (_, i) => ({ day: `2026-09-${String(20 + i).padStart(2, "0")}` }));
    expect(historyDays([...days].reverse()).map((d) => d.day)).toEqual(days.slice(2).map((d) => d.day));
    expect(tokensText(1234567.4)).toBe("1,234,567");
    expect(tokensText(null)).toBe("Not reported");
  });
});

describe("budget form", () => {
  const saved = draftFrom(parseUsage(USAGE)!);

  it("starts from the current budget and caps", () => {
    expect(saved).toEqual({ daily: "400", roles: { market_interpreter: "48", research_planner: "40", extractor: "120" } });
    expect(draftFrom({ daily_requests: null, roles: [{ role: "x", cap: null }] })).toEqual({ daily: "", roles: {} });
  });

  it("validates whole numbers within the backend limits", () => {
    const bad: BudgetDraft = {
      daily: "10001",
      roles: { market_interpreter: "-1", research_planner: "2.5", extractor: "5001" },
    };
    expect(Object.keys(budgetErrors(bad)).sort()).toEqual(["daily", "extractor", "market_interpreter", "research_planner"]);
    expect(budgetErrors({ daily: "", roles: {} }).daily).toBe("Enter a whole number from 0 to 10,000.");
    expect(budgetErrors({ daily: "0", roles: { extractor: "5000" } })).toEqual({});
    expect(budgetPayload(bad, saved)).toBeNull();
  });

  it("sends only what changed", () => {
    expect(budgetPayload(saved, saved)).toBeNull();
    expect(budgetPayload({ ...saved, daily: " 600 " }, saved)).toEqual({ daily_requests: 600 });
    expect(budgetPayload({ ...saved, roles: { ...saved.roles, research_planner: "0" } }, saved)).toEqual({
      role_daily: { research_planner: 0 },
    });
    // "048" is the same number as "48": not a change.
    expect(budgetPayload({ ...saved, roles: { ...saved.roles, market_interpreter: "048" } }, saved)).toBeNull();
  });

  it("explains budgets that switch the AI off or cannot be used", () => {
    expect(capTotal(saved)).toBe(208);
    const hints = budgetHints(saved);
    expect(hints).toEqual([
      "The role caps add up to 208, below the daily budget of 400: the rest cannot be used.",
    ]);
    expect(budgetHints({ daily: "0", roles: { research_planner: "0" } })).toEqual([
      "A daily budget of 0 switches every AI request off; the app uses its rule-based fallbacks.",
      "Never calls the AI (cap 0): Research planner.",
    ]);
    expect(budgetHints({ daily: "x", roles: {} })).toEqual([]);
  });
});

describe("budget notices", () => {
  const RESET = "Tue 29 Sep 00:00";
  const base = parseUsage(USAGE)!;
  const at = (change: Partial<typeof base>) => budgetNotice({ ...base, ...change }, RESET);

  it("is silent while the day is comfortably within budget", () => {
    expect(at({})).toBeNull();
  });

  it("says when no budget is reported (no limit) and when the budget is 0", () => {
    expect(at({ daily_requests: null })?.text).toContain("AI requests are not limited");
    expect(at({ daily_requests: null, used: null })).toBeNull();
    expect(at({ daily_requests: 0, used: 0 })?.text).toContain("The daily AI budget is 0");
  });

  it("warns once the non-reserve roles are stopped for the day, before the whole day is used", () => {
    const notice = at({ used: 300, remaining: 100 });
    expect(notice?.tone).toBe("warning");
    expect(notice?.text).toContain("Research, news extraction and the specialists have stopped for today");
    expect(notice?.text).toContain("300 of 400 requests used, past the mark at 300");
    expect(notice?.text).toContain(`Until ${RESET} only the market interpretation`);
    expect(at({ used: 299 })).toBeNull();
    expect(at({ used: 350, reserve_share: 0 })?.text).toContain("More than 80 %"); // no reserve: no mark
  });

  it("says when the day is used up", () => {
    const notice = at({ used: 400, remaining: 0 });
    expect(notice).toEqual({ tone: "destructive", text: expect.stringContaining(`AI requests resume at ${RESET}`) });
  });

  it("explains a full pace window and a full non-reserve share of it", () => {
    expect(at({ pace: { window_hours: 2, limit: 33, used: 33 } })?.text).toContain(
      "The hourly pace is reached (33 / 33 requests in the last 2 h)",
    );
    const shared = at({ pace: { window_hours: 2, limit: 33, used: 24, shared_limit: 24, shared_used: 24 } });
    expect(shared?.text).toContain("have used their share of the hourly pace (24 / 24 in the last 2 h)");
    expect(at({ pace: { window_hours: 2, limit: 33, used: 20, shared_limit: 24, shared_used: 20 } })).toBeNull();
  });

  it("warns from 80 % of the day", () => {
    expect(at({ used: 330, reserve_share: 0 })?.text).toContain("More than 80 %");
  });
});

describe("usage bar value", () => {
  it("keeps aria-valuenow within 0..limit (the true figure stays in the text)", () => {
    expect(ariaValueNow(1914, 400)).toBe(400);
    expect(ariaValueNow(120, 400)).toBe(120);
    expect(ariaValueNow(-2, 400)).toBe(0);
    expect(ariaValueNow(5, null)).toBe(5);
    expect(ariaValueNow(null, 400)).toBeUndefined();
  });
});
