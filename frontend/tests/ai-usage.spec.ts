import { test, expect, type Page } from "@playwright/test";

/*
 * Settings → AI models → "AI usage today" (GET /api/ai/usage) and the daily budget form
 * (PUT /api/admin/config/ai_budget), docs/AI_AGENTS.md, against an intercepted, stateful, test-only backend.
 * Nothing here reaches a real backend or an AI provider.
 */

const TOKEN = "temporary-test-token";

type Role = {
  role: string;
  label: string | null;
  used: number;
  cap: number;
  tokens: number | null;
  refused: number;
  succeeded: number;
  failed: number;
};

function usageFixture() {
  const roles: Role[] = [
    { role: "market_interpreter", label: "Market interpretation", used: 20, cap: 48, tokens: 812345, refused: 0, succeeded: 18, failed: 2 },
    { role: "research_planner", label: "Research planner", used: 40, cap: 40, tokens: null, refused: 9, succeeded: 31, failed: 9 },
    { role: "extractor", label: "News extraction", used: 60, cap: 120, tokens: 45000, refused: 0, succeeded: 60, failed: 0 },
    { role: "auditor", label: null, used: 0, cap: 60, tokens: null, refused: 0, succeeded: 0, failed: 0 },
  ];
  return {
    day: "2026-09-28",
    // Midnight in Bucharest (UTC+3 in September).
    resets_at: "2026-09-28T21:00:00Z",
    daily_requests: 400,
    used: 120,
    remaining: 280,
    reserve_share: 0.25,
    pace: { window_hours: 2, limit: 33, used: 12, shared_limit: 24, shared_used: 8 },
    roles,
    last_rate_limited_at: "2026-09-28T07:15:00Z",
    history: [
      { day: "2026-09-27", used: 390, tokens: 1200000 },
      { day: "2026-09-26", used: 1914, tokens: null },
      { day: "2026-09-28", used: 120, tokens: 857345 },
    ],
    limiter: { concurrency_limit: 4, in_flight: 1, queued: 2, paused_until: null },
  };
}

type Usage = ReturnType<typeof usageFixture>;
type Call = { method: string; path: string; token: string | null; local: string | null; body: unknown };

/**
 * Stateful backend: a saved budget changes what the next GET /ai/usage returns. `usage` answers the usage request
 * (a status for an error). Returns the recorded calls to /ai and /admin.
 */
async function mockBackend(
  page: Page,
  {
    mode = "local",
    usage = usageFixture(),
    usageStatus = 200,
    probe = null,
  }: { mode?: "local" | "token"; usage?: Usage; usageStatus?: number; probe?: Record<string, unknown> | null } = {},
) {
  const state = usage;
  const calls: Call[] = [];
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname.replace(/^\/api/, "");
    const token = await request.headerValue("x-admin-token");
    const local = await request.headerValue("x-local-admin");
    const body = request.postDataJSON();
    if (path.startsWith("/admin") || path.startsWith("/ai")) calls.push({ method, path, token, local, body });
    const json = (value: unknown, status = 200) => route.fulfill({ status, json: value });
    if (path === "/dashboard") return json({ providers: [], assets: [], events: [], calendar: [], regimes: [] });
    if (path === "/settings") return json(mode === "local" ? { roles: [], admin_mode: "local" } : { roles: [] });
    if (method === "GET" && path === "/ai/usage")
      return usageStatus === 200 ? json(state) : json({ detail: "Not Found" }, usageStatus);
    if (!path.startsWith("/admin")) return json([]);
    const allowed = mode === "local" ? local === "1" : token === TOKEN;
    if (!allowed) return json({ detail: "Invalid administrative token" }, 401);
    if (method === "POST" && path === "/admin/ollama/test" && probe) return json(probe);
    if (method === "PUT" && path === "/admin/config/ai_budget") {
      const change = body as { daily_requests?: number; role_daily?: Record<string, number> };
      if (change.daily_requests === 9999)
        return json({ detail: "Rejected: daily_requests: the server refused this value" }, 422);
      if (typeof change.daily_requests === "number") {
        state.remaining = Math.max(change.daily_requests - state.used, 0);
        state.daily_requests = change.daily_requests;
      }
      for (const [role, cap] of Object.entries(change.role_daily ?? {})) {
        const row = state.roles.find((r) => r.role === role);
        if (!row) return json({ detail: `Rejected: role_daily.${role}: unknown key` }, 422);
        row.cap = cap;
      }
      return json({ fragment: "ai_budget", version: "1.3.1", content_hash: "abc" });
    }
    return json({ detail: `Unexpected ${method} ${path}` }, 405);
  });
  return calls;
}

async function openModels(page: Page) {
  await page.goto("/settings");
  await page.getByRole("tab", { name: "AI models" }).click();
  const panel = page.getByRole("region", { name: "AI usage today" });
  await expect(panel).toBeVisible();
  return panel;
}

test("AI usage today: budget, pace, reset, rate limit, roles and the last 7 days", async ({ page }) => {
  const calls = await mockBackend(page);
  const panel = await openModels(page);

  await expect(panel).toContainText("120 / 400");
  await expect(panel).toContainText("requests used today (Mon 28 Sep)");
  await expect(panel).toContainText("Resets Tue 29 Sep 00:00 (Bucharest time)");
  const bar = panel.getByRole("progressbar", { name: "AI requests used today" });
  await expect(bar).toHaveAttribute("aria-valuenow", "120");
  await expect(bar).toHaveAttribute("aria-valuemax", "400");
  // The reserve boundary: 400 x (1 - 0.25).
  await expect(panel).toContainText("The mark at 300 requests");
  await expect(panel).toContainText("25 % kept for them");

  await expect(panel).toContainText("Remaining today");
  await expect(panel).toContainText("280");
  await expect(panel).toContainText("Pace (last 2 h)");
  await expect(panel).toContainText("12 / 33");
  await expect(panel).toContainText("Research, extraction and specialists: 8 / 24");
  await expect(panel).toContainText("Mon 28 Sep 10:15");
  await expect(panel).toContainText("Right now: 1 of 4 parallel slots in use, 2 waiting.");
  // 30 % used and the pace window is not full: no warning.
  await expect(panel.getByText(/budget is used up|pace is reached|More than 80 %/)).toHaveCount(0);

  const roles = page.getByRole("table", { name: "AI requests by role today" });
  const interpreter = roles.locator("tr", { hasText: "Market interpretation" });
  await expect(interpreter).toContainText("20 / 48");
  await expect(interpreter).toContainText("812,345");
  const planner = roles.locator("tr", { hasText: "Research planner" });
  await expect(planner).toContainText("40 / 40");
  await expect(planner).toContainText("Not reported");
  await expect(planner).toContainText("9");
  await expect(planner.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "40");
  // No label from the backend: the plain name, never the key.
  await expect(roles.locator("tr", { hasText: "Auditor" })).toContainText("0 / 60");
  await expect(roles).not.toContainText("research_planner");

  // History oldest first, in Bucharest day names.
  const history = page.getByRole("table", { name: "AI requests per day" }).locator("tbody tr");
  await expect(history).toHaveCount(3);
  await expect(history.nth(0)).toContainText("Sat 26 Sep");
  await expect(history.nth(0)).toContainText("1,914");
  // A past day over the budget: the bar value stays within its range, the true figure is in the text.
  const over = history.nth(0).getByRole("progressbar");
  await expect(over).toHaveAttribute("aria-valuenow", "400");
  await expect(over).toHaveAttribute("aria-valuetext", "1,914 / 400");
  await expect(history.nth(1)).toContainText("1,200,000");
  await expect(history.nth(2)).toContainText("Mon 28 Sep");

  // Reading usage is a plain GET without admin headers.
  expect(calls.filter((c) => c.path === "/ai/usage").every((c) => c.method === "GET")).toBe(true);
  await page.screenshot({ path: "test-results/ai-usage-desktop.png", fullPage: true });
});

test("the budget form validates, sends only the changes with the local admin header and shows the saved state", async ({
  page,
}) => {
  const calls = await mockBackend(page);
  const panel = await openModels(page);
  const form = panel.getByRole("form", { name: "AI budget" });
  const daily = form.getByLabel("Daily requests (all roles)");
  const planner = form.getByLabel("Research planner", { exact: true });
  const save = form.getByRole("button", { name: "Save budget" });
  await expect(daily).toHaveValue("400");
  await expect(planner).toHaveValue("40");
  await expect(form.getByLabel("Auditor", { exact: true })).toHaveValue("60");
  await expect(save).toBeDisabled();

  // Out of range: an inline message, the field marked invalid, nothing to save.
  await daily.fill("10001");
  await expect(form.getByText("Enter a whole number from 0 to 10,000.")).toBeVisible();
  await expect(daily).toHaveAttribute("aria-invalid", "true");
  await expect(save).toBeDisabled();
  await daily.fill("300");
  await expect(form.getByText("Enter a whole number from 0 to 10,000.")).toHaveCount(0);

  await planner.fill("6000");
  await expect(form.getByText("Enter a whole number from 0 to 5,000.")).toBeVisible();
  await expect(save).toBeDisabled();
  await planner.fill("0");
  await expect(form).toContainText("Never calls the AI (cap 0): Research planner.");
  await expect(save).toBeEnabled();

  const put = page.waitForRequest((r) => r.method() === "PUT" && r.url().endsWith("/api/admin/config/ai_budget"));
  await save.click();
  const request = await put;
  expect(request.postDataJSON()).toEqual({ daily_requests: 300, role_daily: { research_planner: 0 } });
  expect(await request.headerValue("x-local-admin")).toBe("1");
  expect(await request.headerValue("x-admin-token")).toBeNull();
  await expect(form.getByRole("status")).toContainText("Budget saved. It applies to the next AI request");
  await expect(save).toBeDisabled();

  // The usage is read again: the new budget and the switched-off role show at once.
  await expect(panel).toContainText("120 / 300");
  await expect(
    page.getByRole("table", { name: "AI requests by role today" }).locator("tr", { hasText: "Research planner" }),
  ).toContainText("Off (cap 0)");
  await expect(daily).toHaveValue("300");
  await expect(planner).toHaveValue("0");
  expect(calls.filter((c) => c.method === "PUT")).toHaveLength(1);
});

test("a budget the server rejects keeps the form and shows the reason; Undo restores the saved values", async ({ page }) => {
  await mockBackend(page);
  const panel = await openModels(page);
  const form = panel.getByRole("form", { name: "AI budget" });
  const daily = form.getByLabel("Daily requests (all roles)");
  await daily.fill("9999");
  await form.getByRole("button", { name: "Save budget" }).click();
  await expect(form.getByRole("status")).toContainText("the server refused this value");
  await expect(daily).toHaveValue("9999");
  await expect(panel).toContainText("120 / 400");

  await form.getByRole("button", { name: "Undo changes" }).click();
  await expect(daily).toHaveValue("400");
  await expect(form.getByRole("button", { name: "Save budget" })).toBeDisabled();
});

test("a used-up budget says what happens until the reset", async ({ page }) => {
  const usage = usageFixture();
  Object.assign(usage, { used: 400, remaining: 0, pace: { window_hours: 2, limit: 33, used: 33 } });
  await mockBackend(page, { usage });
  const panel = await openModels(page);
  await expect(panel.getByRole("status").filter({ hasText: "Today's AI budget is used up." })).toContainText(
    "AI requests resume at Tue 29 Sep 00:00",
  );
  await expect(panel.getByRole("progressbar", { name: "AI requests used today" })).toHaveAttribute("aria-valuenow", "400");
});

test("a full pace window explains the wait before the daily budget is used up", async ({ page }) => {
  const usage = usageFixture();
  Object.assign(usage, { pace: { window_hours: 2, limit: 33, used: 33 } });
  await mockBackend(page, { usage });
  const panel = await openModels(page);
  await expect(panel.getByRole("status").filter({ hasText: "The hourly pace is reached" })).toContainText(
    "33 / 33 requests in the last 2 h",
  );
});

test("past the reserve mark: research and extraction have stopped, only the reserve roles may call the AI", async ({
  page,
}) => {
  const usage = usageFixture();
  Object.assign(usage, { used: 310, remaining: 90 });
  await mockBackend(page, { usage });
  const panel = await openModels(page);
  const notice = panel.getByRole("status").filter({ hasText: "have stopped for today" });
  await expect(notice).toContainText("Research, news extraction and the specialists have stopped for today");
  await expect(notice).toContainText("310 of 400 requests used, past the mark at 300");
  await expect(notice).toContainText("only the market interpretation, the chief analyst and the auditor may call the AI");
});

test("a full non-reserve share of the pace window is explained", async ({ page }) => {
  const usage = usageFixture();
  Object.assign(usage, { pace: { window_hours: 2, limit: 33, used: 26, shared_limit: 24, shared_used: 24 } });
  await mockBackend(page, { usage });
  const panel = await openModels(page);
  await expect(panel.getByRole("status").filter({ hasText: "share of the hourly pace" })).toContainText(
    "(24 / 24 in the last 2 h)",
  );
});

test("Test Ollama refused by the budget says so instead of an invalid schema, and the usage is read again", async ({
  page,
}) => {
  const calls = await mockBackend(page, {
    probe: {
      success: false,
      schema_valid: null,
      publication_blocked: false,
      budget_refusal: "role",
      error: "AI daily budget reached",
      provider_status: "ONLINE",
      checked_at: "2026-09-28T09:00:00Z",
    },
  });
  await openModels(page);
  const reads = calls.filter((c) => c.path === "/ai/usage").length;
  await page.getByRole("button", { name: "Test Ollama" }).click();
  const result = page.getByLabel("Ollama test result");
  await expect(result).toContainText("Refused by the AI budget (no request sent): AI daily budget reached");
  await expect(result).toContainText("counts against its daily cap");
  await expect(result).not.toContainText("Schema valid");
  await expect.poll(() => calls.filter((c) => c.path === "/ai/usage").length).toBeGreaterThan(reads);
});

test("an older backend without the usage endpoint gets an honest notice and the tab keeps working", async ({ page }) => {
  await mockBackend(page, { usageStatus: 404 });
  const panel = await openModels(page);
  await expect(panel.getByText("AI usage is not reported")).toBeVisible();
  await expect(panel.getByRole("form", { name: "AI budget" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Discovered models" })).toBeVisible();
});

test("a failing usage endpoint shows the service error with a retry", async ({ page }) => {
  await mockBackend(page, { usageStatus: 500 });
  const panel = await openModels(page);
  await expect(panel.getByRole("alert")).toContainText("The service returned an error (HTTP 500).", { timeout: 15000 });
  await expect(panel.getByRole("button", { name: "Retry now" })).toBeVisible();
});

test("token mode: the budget cannot be saved until the admin token is entered, then it is sent", async ({ page }) => {
  await mockBackend(page, { mode: "token" });
  await page.goto("/settings");
  await page.getByRole("tab", { name: "AI models" }).click();
  const panel = page.getByRole("region", { name: "AI usage today" });
  const form = panel.getByRole("form", { name: "AI budget" });
  await form.getByLabel("Daily requests (all roles)").fill("350");
  await expect(form.getByRole("button", { name: "Save budget" })).toBeDisabled();
  await expect(form).toContainText("Enter the admin token to change the budget.");

  await page.getByLabel("Admin token").fill(TOKEN);
  const put = page.waitForRequest((r) => r.method() === "PUT" && r.url().endsWith("/api/admin/config/ai_budget"));
  await form.getByRole("button", { name: "Save budget" }).click();
  const request = await put;
  expect(await request.headerValue("x-admin-token")).toBe(TOKEN);
  expect(request.postDataJSON()).toEqual({ daily_requests: 350 });
  await expect(form.getByRole("status")).toContainText("Budget saved.");
});

test("AI usage on a phone: no horizontal page scroll", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await mockBackend(page);
  await openModels(page);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await page.screenshot({ path: "test-results/ai-usage-mobile.png", fullPage: true });
});
