import { test, expect, type Page, type Request } from "@playwright/test";
import type { ProviderSettings, SettingField, SettingGroup } from "../lib/provider-settings";

/*
 * Settings → Data providers against an intercepted, stateful, test-only backend
 * (docs/PROVIDER_SETTINGS.md). Routing is method-aware: GET, PUT and POST on the
 * same path get different answers. Nothing here reaches a real backend.
 */

const TOKEN = "temporary-test-token";
// A backend bug must still never put a secret on screen.
const LEAKED = "sk-live-should-never-render";
const NEW_SECRET = "new-smtp-password-123";
const REENTER = "Re-enter the key: a stored key is never sent to a new address";
// Destination field -> the secret that must be re-entered with it (docs/PROVIDER_SETTINGS.md).
const PAIRED: Record<string, string> = {
  reuters_base_url: "reuters_api_key",
  smtp_host: "smtp_password",
  smtp_port: "smtp_password",
};

function field(over: Partial<SettingField> & Pick<SettingField, "name" | "env" | "label" | "kind">): SettingField {
  return {
    secret: over.kind === "secret",
    required: false,
    options: null,
    min: null,
    max: null,
    help: null,
    placeholder: null,
    configured: false,
    source: "default",
    value: null,
    editable: true,
    ...over,
  };
}

function fixture(): ProviderSettings {
  const groups: SettingGroup[] = [
    {
      id: "fred",
      label: "FRED (St. Louis Fed)",
      category: "macro",
      description: "Macro series and the BAA10Y credit-spread feature.",
      signup_url: "https://fredaccount.stlouisfed.org/apikeys",
      pricing: "Free key",
      status: "NOT CONFIGURED",
      providers: [{ name: "fred", status: "NOT CONFIGURED", enabled: true, stub: false }],
      fields: [field({ name: "fred_api_key", env: "FRED_API_KEY", label: "API key", kind: "secret", required: true })],
      test: { available: true, target: "fred", label: "Test connection", note: null },
    },
    {
      id: "finnhub",
      label: "Finnhub",
      category: "market",
      description: "Equity quotes and the economic calendar.",
      signup_url: "javascript:alert(1)",
      pricing: "Free tier",
      status: "ONLINE",
      providers: [{ name: "finnhub", status: "ONLINE", enabled: true, stub: false }],
      fields: [
        field({
          name: "finnhub_api_key",
          env: "FINNHUB_API_KEY",
          label: "API key",
          kind: "secret",
          required: true,
          configured: true,
          source: "env_file",
          value: LEAKED,
        }),
      ],
      test: { available: true, target: "finnhub", label: "Test connection", note: null },
    },
    {
      id: "x",
      label: "X (Twitter)",
      category: "news",
      description: "Recent posts from configured accounts.",
      signup_url: null,
      pricing: "Paid",
      status: "AUTH_ERROR",
      providers: [{ name: "x", status: "AUTH_ERROR", enabled: true, stub: false }],
      fields: [
        field({
          name: "x_bearer_token",
          env: "X_BEARER_TOKEN",
          label: "Bearer token",
          kind: "secret",
          configured: true,
          source: "environment",
          editable: false,
        }),
      ],
      test: { available: true, target: "x", label: "Test connection", note: "Uses 1 recent search." },
    },
    {
      id: "email",
      label: "E-mail (SMTP)",
      category: "alerts",
      description: "Alert e-mails.",
      signup_url: null,
      pricing: null,
      status: "CONFIGURED",
      providers: [],
      fields: [
        field({ name: "smtp_host", env: "SMTP_HOST", label: "Host", kind: "text", source: "env_file", value: "smtp.desk.io" }),
        field({
          name: "smtp_port",
          env: "SMTP_PORT",
          label: "Port",
          kind: "int",
          min: 1,
          max: 65535,
          source: "env_file",
          value: 587,
        }),
        field({
          name: "smtp_password",
          env: "SMTP_PASSWORD",
          label: "Password",
          kind: "secret",
          configured: true,
          source: "env_file",
        }),
        field({ name: "smtp_to", env: "SMTP_TO", label: "Recipients", kind: "email_list", source: "env_file", value: "ops@desk.io" }),
        field({ name: "smtp_starttls", env: "SMTP_STARTTLS", label: "STARTTLS", kind: "bool", value: true }),
      ],
      test: { available: true, target: "email", label: "Send test message", note: "Sends one real e-mail." },
    },
    {
      id: "reuters",
      label: "Reuters",
      category: "news",
      description: "Licensed news.",
      signup_url: null,
      pricing: "Licensed",
      status: "NOT CONFIGURED",
      providers: [],
      fields: [
        field({
          name: "reuters_base_url",
          env: "REUTERS_BASE_URL",
          label: "Base URL",
          kind: "url",
          source: "env_file",
          value: "https://api.reuters.example/v1",
        }),
        // Not stored as far as this page knows (another session stores one meanwhile, see mockBackend).
        field({ name: "reuters_api_key", env: "REUTERS_API_KEY", label: "API key", kind: "secret" }),
      ],
      test: null,
    },
    {
      id: "sec",
      label: "SEC EDGAR",
      category: "filings",
      description: "Filings and full-text search.",
      signup_url: null,
      pricing: "Free",
      status: "NOT CONFIGURED",
      providers: [],
      fields: [
        // The built-in default is a placeholder the backend rejects: not configured, never pre-filled.
        field({
          name: "sec_user_agent",
          env: "SEC_USER_AGENT",
          label: "User-Agent (name and e-mail)",
          kind: "text",
          required: true,
          value: "MarketIntelligence contact@example.com",
          placeholder: "Jane Doe jane.doe@yourdomain.com",
          help: "example.com addresses are rejected.",
        }),
        // Configured help that already says "Optional.".
        field({
          name: "bls_api_key",
          env: "BLS_API_KEY",
          label: "BLS API key",
          kind: "secret",
          help: "Optional. Without a key BLS allows about 25 queries per day.",
        }),
      ],
      test: { available: true, target: "sec_edgar", label: "Test connection", note: "One filings request." },
    },
    {
      id: "mode",
      label: "Provider mode",
      category: "general",
      description: "Live providers, or development stubs.",
      signup_url: null,
      pricing: null,
      status: "ONLINE",
      providers: [],
      fields: [
        field({ name: "provider_mode", env: "PROVIDER_MODE", label: "Mode", kind: "select", options: ["live", "stub"], value: "live" }),
      ],
      test: null,
    },
  ];
  return {
    env_file: "E:\\Today_Market\\.env",
    runtime: "in_process",
    groups,
    providers: [
      { name: "binance", status: "ONLINE", enabled: true, stub: false, group: null, testable: true },
      { name: "rss_fed", status: "ONLINE", enabled: true, stub: false, group: null, testable: false },
      ...groups.flatMap((g) => g.providers.map((p) => ({ ...p, group: g.id, testable: true }))),
    ],
  };
}

type Call = { method: string; path: string; token: string | null; local: string | null; body: unknown };

/** Stateful backend: PUTs change what the next GET returns. Returns the recorded admin calls. */
async function mockBackend(page: Page, mode: "token" | "local" = "token") {
  const state = fixture();
  const calls: Call[] = [];
  // Secrets stored on the server: the fixture's, plus a Reuters key saved by another session after this page loaded.
  const stored = new Set(
    state.groups.flatMap((g) => g.fields.filter((f) => f.secret && f.configured).map((f) => f.name)),
  );
  stored.add("reuters_api_key");
  const health = () =>
    state.providers.map((p) => ({ name: p.name, status: p.status, latency_ms: 40, last_success: null, error: null }));
  await page.routeWebSocket("**/ws", (socket) => socket.close());
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname.replace(/^\/api/, "");
    const token = await request.headerValue("x-admin-token");
    const local = await request.headerValue("x-local-admin");
    const body = request.postDataJSON();
    if (path.startsWith("/admin")) calls.push({ method, path, token, local, body });
    const json = (value: unknown, status = 200) => route.fulfill({ status, json: value });
    if (path === "/dashboard")
      return json({ providers: health(), assets: [], events: [], calendar: [], regimes: [] });
    if (path === "/settings") return json(mode === "local" ? { roles: [], admin_mode: "local" } : { roles: [] });
    if (path === "/providers") return json(health());
    if (!path.startsWith("/admin")) return json([]);
    const allowed = mode === "local" ? local === "1" : token === TOKEN;
    if (!allowed) return json({ detail: "Invalid administrative token" }, 401);

    if (method === "GET" && path === "/admin/provider-settings") return json(state);
    const group = path.match(/^\/admin\/provider-settings\/([^/]+)$/);
    if (method === "PUT" && group) {
      const g = state.groups.find((x) => x.id === group[1]);
      if (!g) return json({ detail: "Unknown provider group" }, 404);
      const values = (body as { values: Record<string, string | number | boolean | null> }).values;
      // The server rule: a stored key never follows a changed destination unless re-entered.
      const unpaired = Object.entries(PAIRED).filter(
        ([destination, secret]) => destination in values && !(secret in values) && stored.has(secret),
      );
      if (unpaired.length)
        return json(
          {
            detail: `${unpaired.map(([d]) => d).join(", ")} changed: re-enter ${[...new Set(unpaired.map(([, s]) => s))].join(", ")} in the same request (a stored key is never sent to a new address)`,
          },
          422,
        );
      if (values.fred_api_key === "rejected-key")
        return json(
          { detail: [{ loc: ["body", "values", "fred_api_key"], msg: "Value error, rejected by the server", type: "value_error" }] },
          422,
        );
      for (const [name, value] of Object.entries(values)) {
        const f = g.fields.find((x) => x.name === name)!;
        Object.assign(
          f,
          value === null
            ? { configured: false, source: "default", value: null }
            : { configured: true, source: "env_file", value: f.secret ? null : value },
        );
      }
      return json(g);
    }
    const enabled = path.match(/^\/admin\/providers\/([^/]+)\/enabled$/);
    if (method === "PUT" && enabled) {
      const on = (body as { enabled: boolean }).enabled;
      const status = on ? "ONLINE" : "DISABLED";
      for (const p of [...state.providers, ...state.groups.flatMap((g) => g.providers)])
        if (p.name === enabled[1]) Object.assign(p, { enabled: on, status });
      for (const g of state.groups) if (g.providers.some((p) => p.name === enabled[1])) g.status = status;
      return json({ name: enabled[1], enabled: on, status });
    }
    const probe = path.match(/^\/admin\/providers\/([^/]+)\/test$/);
    if (method === "POST" && probe)
      return json({
        target: probe[1],
        ok: true,
        status: probe[1] === "email" ? "SENT" : "ONLINE",
        detail: probe[1] === "email" ? "Test message sent to 1 recipient" : "1 observation returned",
        latency_ms: 123,
        checked_at: "2026-09-26T09:00:00Z",
        // The backend repeats the group's test note in the result.
        note: probe[1] === "sec_edgar" ? "One filings request." : null,
      });
    return json({ detail: `Unexpected ${method} ${path}` }, 405);
  });
  return calls;
}

async function unlock(page: Page) {
  await page.goto("/settings");
  await page.getByLabel("Admin token").fill(TOKEN);
  await expect(page.getByRole("article", { name: "FRED (St. Louis Fed)" })).toBeVisible();
}

const card = (page: Page, name: string) => page.getByRole("article", { name });

test("provider configuration is locked without an admin token", async ({ page }) => {
  const calls = await mockBackend(page);
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Provider configuration" })).toBeVisible();
  await expect(page.getByText("Locked.")).toBeVisible();
  await expect(page.getByText("python -m scripts.admin_token").first()).toBeVisible();
  await expect(page.getByRole("table", { name: "Provider health" }).locator("tbody tr")).toHaveCount(5);
  // Nothing configurable: no group cards, no secret inputs besides the token, no switches or test buttons.
  await expect(page.getByRole("article")).toHaveCount(0);
  await expect(page.locator('input[type="password"]')).toHaveCount(1);
  await expect(page.getByRole("switch")).toHaveCount(0);
  await expect(page.getByRole("columnheader", { name: "Enabled" })).toHaveCount(0);
  expect(calls).toEqual([]);

  // A wrong token shows the backend's reason in plain words (never retried: it answers the same), and still nothing
  // to edit.
  await page.getByLabel("Admin token").fill("wrong-token");
  await expect(
    page.getByRole("alert").filter({ hasText: "The service returned an error (HTTP 401)." }),
  ).toContainText("Invalid administrative token");
  await expect(page.getByRole("article")).toHaveCount(0);
  expect(calls.map((c) => [c.method, c.path, c.token])).toEqual([["GET", "/admin/provider-settings", "wrong-token"]]);
});

test("with a token the groups render by category and secrets stay write-only", async ({ page }) => {
  const calls = await mockBackend(page);
  await unlock(page);
  for (const heading of ["Market data", "Macro & official data", "News & social", "Alert channels", "General"])
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
  expect(calls[0]).toMatchObject({ method: "GET", path: "/admin/provider-settings", token: TOKEN });

  const fred = card(page, "FRED (St. Louis Fed)");
  await expect(fred).toContainText("NOT CONFIGURED");
  await expect(fred).toContainText("Free key");
  await expect(fred.getByRole("link", { name: /Get an API key/ })).toHaveAttribute(
    "href",
    "https://fredaccount.stlouisfed.org/apikeys",
  );
  const fredKey = fred.getByLabel("API key", { exact: true });
  await expect(fredKey).toHaveAttribute("type", "password");
  await expect(fredKey).toHaveAttribute("autocomplete", "new-password");
  await expect(fredKey).toHaveAttribute("placeholder", "Not set");
  await expect(fredKey).toHaveValue("");

  // Unsafe signup links are dropped; a configured secret is never pre-filled, even if the backend sent it.
  const finnhub = card(page, "Finnhub");
  await expect(finnhub.getByRole("link")).toHaveCount(0);
  await expect(finnhub.getByLabel("API key", { exact: true })).toHaveValue("");
  await expect(finnhub.getByLabel("API key", { exact: true })).toHaveAttribute("placeholder", "Configured (.env) — type to replace");
  expect(await page.content()).not.toContain(LEAKED);

  // Values from the OS environment are read-only, with the reason.
  const x = card(page, "X (Twitter)");
  await expect(x.getByLabel("Bearer token", { exact: true })).toBeDisabled();
  await expect(x.getByLabel("Bearer token", { exact: true })).toHaveAttribute("placeholder", "Set by the OS environment (read-only)");
  await expect(x).toContainText("overrides .env");
  await expect(x.getByRole("button", { name: /^Remove/ })).toHaveCount(0);

  // Non-secret values are pre-filled; typed controls per kind.
  const email = card(page, "E-mail (SMTP)");
  await expect(email.getByLabel("Host", { exact: true })).toHaveValue("smtp.desk.io");
  await expect(email.getByLabel("Port", { exact: true })).toHaveValue("587");
  await expect(email.getByLabel("Port", { exact: true })).toHaveAttribute("type", "number");
  await expect(email.getByRole("switch", { name: "STARTTLS" })).toBeChecked();
  await expect(card(page, "Provider mode").getByLabel("Mode", { exact: true })).toHaveValue("live");
  await expect(email.getByRole("button", { name: /^Save/ })).toBeDisabled();

  // The health table gains Enabled and Test columns.
  const table = page.getByRole("table", { name: "Provider health" });
  await expect(table.getByRole("columnheader", { name: "Enabled" })).toBeVisible();
  await expect(table.getByRole("columnheader", { name: "Test" })).toBeVisible();
  await expect(table.locator("tr", { hasText: "rss_fed" })).toContainText("Not testable");
  await page.screenshot({ path: "test-results/provider-settings-desktop.png", fullPage: true });
});

test("Save sends only edited fields with the token; removal sends null; test after saving", async ({ page }) => {
  const calls = await mockBackend(page);
  await unlock(page);
  const email = card(page, "E-mail (SMTP)");
  const save = email.getByRole("button", { name: /^Save/ });
  const send = email.getByRole("button", { name: /^Send test message/ });
  await expect(send).toBeEnabled();

  // Inline validation blocks saving.
  await email.getByLabel("Port", { exact: true }).fill("70000");
  await expect(email.getByText("Enter a number from 1 to 65535.")).toBeVisible();
  await expect(email.getByLabel("Port", { exact: true })).toHaveAttribute("aria-invalid", "true");
  await expect(save).toBeDisabled();
  await email.getByLabel("Port", { exact: true }).fill("465");
  await expect(email.getByText("Enter a number from 1 to 65535.")).toHaveCount(0);

  await email.getByLabel("Password", { exact: true }).fill(NEW_SECRET);
  await email.getByRole("switch", { name: "STARTTLS" }).click();
  // Two-step remove, keyboard operable; focus stays on the button as it changes.
  await email.getByRole("button", { name: "Remove Recipients" }).focus();
  await page.keyboard.press("Enter");
  const confirm = email.getByRole("button", { name: "Confirm remove Recipients" });
  await expect(confirm).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(email.getByRole("button", { name: "Undo remove Recipients" })).toBeFocused();
  await expect(email.getByLabel("Recipients", { exact: true })).toBeDisabled();

  // Unsaved edits: tests would use the old settings.
  await expect(send).toBeDisabled();
  await expect(email.getByText("Save first")).toBeVisible();
  await expect(save).toBeEnabled();

  const put = page.waitForRequest((r) => r.method() === "PUT" && r.url().endsWith("/api/admin/provider-settings/email"));
  await save.click();
  const request: Request = await put;
  expect(await request.headerValue("x-admin-token")).toBe(TOKEN);
  expect(await request.headerValue("content-type")).toBe("application/json");
  expect(request.postDataJSON()).toEqual({
    values: { smtp_port: 465, smtp_password: NEW_SECRET, smtp_to: null, smtp_starttls: false },
  });
  await expect(email.getByRole("status").getByText("Saved.")).toBeVisible();
  await expect(email.getByRole("status").getByText("Saved.")).not.toHaveClass(/text-destructive/);
  // The typed secret is gone from the page once saved.
  await expect(email.getByLabel("Password", { exact: true })).toHaveValue("");
  await expect(email.getByLabel("Password", { exact: true })).toHaveAttribute("placeholder", "Configured (.env) — type to replace");
  expect(await page.content()).not.toContain(NEW_SECRET);
  await expect(email.getByLabel("Recipients", { exact: true })).toHaveValue("");
  await expect(email.getByLabel("Port", { exact: true })).toHaveValue("465");
  await expect(save).toBeDisabled();

  await expect(send).toBeEnabled();
  const post = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/api/admin/providers/email/test"));
  await send.click();
  expect(await (await post).headerValue("x-admin-token")).toBe(TOKEN);
  const result = email.getByRole("status");
  await expect(result).toContainText("SENT");
  await expect(result).toContainText("Passed");
  await expect(result).toContainText("Test message sent to 1 recipient");
  await expect(result).toContainText("123 ms");
  expect(calls.filter((c) => c.method !== "GET").map((c) => `${c.method} ${c.path}`)).toEqual([
    "PUT /admin/provider-settings/email",
    "POST /admin/providers/email/test",
  ]);
});

test("a validation detail from the backend is shown", async ({ page }) => {
  await mockBackend(page);
  await unlock(page);
  const fred = card(page, "FRED (St. Louis Fed)");
  await fred.getByLabel("API key", { exact: true }).fill("rejected-key");
  await fred.getByRole("button", { name: /^Save/ }).click();
  const failure = fred.getByText("Request failed (422): fred_api_key: Value error, rejected by the server");
  await expect(failure).toBeVisible();
  // A failure is shown as an error, never in the colour of "Saved.".
  await expect(failure).toHaveClass(/text-destructive/);
  await expect(fred.getByRole("status").locator(".text-primary")).toHaveCount(0);
  // The edit is kept so it can be corrected.
  await expect(fred.getByLabel("API key", { exact: true })).toHaveValue("rejected-key");
});

test("changing a destination requires the stored key again before saving", async ({ page }) => {
  const calls = await mockBackend(page);
  await unlock(page);
  const email = card(page, "E-mail (SMTP)");
  const save = email.getByRole("button", { name: /^Save/ });
  const password = email.getByLabel("Password", { exact: true });
  await expect(password).not.toHaveAttribute("required", "");
  await expect(email.getByText(REENTER)).toHaveCount(0);
  await expect(email.locator("small", { hasText: "SMTP_PASSWORD" })).toContainText("Optional.");

  // A new host with the stored password left out: the password becomes required and Save waits.
  await email.getByLabel("Host", { exact: true }).fill("mail.other.org");
  await expect(email.getByText(REENTER)).toBeVisible();
  await expect(password).toHaveAttribute("required", "");
  await expect(password).toHaveAttribute("aria-required", "true");
  await expect(password).toHaveAttribute("placeholder", "Required: re-enter the key");
  await expect(email.locator("small", { hasText: "SMTP_PASSWORD" })).not.toContainText("Optional.");
  const hintId = await email.getByText(REENTER).getAttribute("id");
  expect((await password.getAttribute("aria-describedby"))?.split(" ")).toContain(hintId);
  await expect(save).toBeDisabled();

  // Back to the stored host: nothing to re-enter.
  await email.getByLabel("Host", { exact: true }).fill("smtp.desk.io");
  await expect(email.getByText(REENTER)).toHaveCount(0);
  await expect(save).toBeDisabled();

  // The port is a destination too; typing the key releases Save and both go in one request.
  await email.getByLabel("Port", { exact: true }).fill("2525");
  await expect(email.getByText(REENTER)).toBeVisible();
  await expect(save).toBeDisabled();
  await password.fill(NEW_SECRET);
  await expect(save).toBeEnabled();
  const put = page.waitForRequest((r) => r.method() === "PUT" && r.url().endsWith("/api/admin/provider-settings/email"));
  await save.click();
  expect((await put).postDataJSON()).toEqual({ values: { smtp_port: 2525, smtp_password: NEW_SECRET } });
  await expect(email.getByRole("status").getByText("Saved.")).toBeVisible();
  await expect(email.getByText(REENTER)).toHaveCount(0);
  expect(calls.filter((c) => c.method === "PUT").map((c) => c.path)).toEqual(["/admin/provider-settings/email"]);
});

test("the backend's refusal to move a stored key is shown as an error", async ({ page }) => {
  await mockBackend(page);
  await unlock(page);
  // This page does not know a Reuters key was stored meanwhile, so it cannot ask for it; the server refuses.
  const reuters = card(page, "Reuters");
  await reuters.getByLabel("Base URL", { exact: true }).fill("https://mirror.example.org/v1");
  await expect(reuters.getByText(REENTER)).toHaveCount(0);
  await reuters.getByRole("button", { name: /^Save/ }).click();
  const failure = reuters.getByText(
    "Request failed (422): reuters_base_url changed: re-enter reuters_api_key in the same request",
  );
  await expect(failure).toBeVisible();
  await expect(failure).toHaveClass(/text-destructive/);
  // The edit is kept; re-entering the key saves it.
  await expect(reuters.getByLabel("Base URL", { exact: true })).toHaveValue("https://mirror.example.org/v1");
  await reuters.getByLabel("API key", { exact: true }).fill("reuters-key-123");
  await reuters.getByRole("button", { name: /^Save/ }).click();
  await expect(reuters.getByRole("status").getByText("Saved.")).toBeVisible();
  await expect(failure).toHaveCount(0);
});

test("disabling a provider sends enabled=false and it becomes a quiet DISABLED", async ({ page }) => {
  const calls = await mockBackend(page);
  await unlock(page);
  const finnhub = card(page, "Finnhub");
  const toggle = finnhub.getByRole("switch", { name: "finnhub" });
  await expect(toggle).toBeChecked();
  const put = page.waitForRequest((r) => r.method() === "PUT" && r.url().endsWith("/api/admin/providers/finnhub/enabled"));
  await toggle.focus();
  await page.keyboard.press("Space");
  const request = await put;
  expect(request.postDataJSON()).toEqual({ enabled: false });
  expect(await request.headerValue("x-admin-token")).toBe(TOKEN);
  await expect(toggle).not.toBeChecked();
  await expect(finnhub).toContainText("DISABLED");

  const table = page.getByRole("table", { name: "Provider health" });
  const row = table.locator("tr", { hasText: "finnhub" });
  await expect(row).toContainText("DISABLED");
  await expect(row.getByRole("switch", { name: "finnhub enabled" })).not.toBeChecked();
  // Quiet: counted as disabled, not impaired, and not expected online in the header.
  const stat = (label: string) =>
    page.locator("div", { has: page.getByText(label, { exact: true }) }).last().locator("strong");
  await expect(stat("Disabled")).toHaveText("1");
  await expect(stat("Impaired")).toHaveText("1"); // x (AUTH_ERROR) only
  // Expected online: binance, rss_fed and x; fred is not configured and finnhub is switched off.
  await expect(page.getByRole("banner")).toContainText("2/3 providers online");

  // Test from the health table, result inline.
  await table.locator("tr", { hasText: "binance" }).getByRole("button", { name: "Test binance" }).click();
  await expect(table.locator("tr", { hasText: "binance" })).toContainText("1 observation returned");
  expect(calls.map((c) => `${c.method} ${c.path}`)).toContain("POST /admin/providers/binance/test");
});

test("provider configuration fits a phone", async ({ page }) => {
  await mockBackend(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await unlock(page);
  await card(page, "E-mail (SMTP)").getByRole("button", { name: "Remove Recipients" }).click();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    "/settings with provider configuration overflows horizontally",
  ).toBe(true);
  await page.screenshot({ path: "test-results/provider-settings-mobile.png", fullPage: true });
});

test("local mode (single user on this computer): no password, everything unlocked", async ({ page }) => {
  const calls = await mockBackend(page, "local");
  await page.goto("/settings");
  await expect(page.getByLabel("Admin token")).toHaveCount(0);
  await expect(page.getByText("Locked.")).toHaveCount(0);
  const fred = card(page, "FRED (St. Louis Fed)");
  await expect(fred).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Enabled" })).toBeVisible();

  await fred.getByLabel("API key", { exact: true }).fill("my-fred-key-123");
  const put = page.waitForRequest((r) => r.method() === "PUT" && r.url().endsWith("/api/admin/provider-settings/fred"));
  await fred.getByRole("button", { name: /^Save/ }).click();
  const request = await put;
  expect(await request.headerValue("x-local-admin")).toBe("1");
  expect(await request.headerValue("x-admin-token")).toBeNull();
  expect(request.postDataJSON()).toEqual({ values: { fred_api_key: "my-fred-key-123" } });
  await expect(fred.getByRole("status").getByText("Saved.")).toBeVisible();
  expect(calls.every((c) => c.local === "1" && c.token === null)).toBe(true);
});

test("local mode never shows the token prompt while the settings load or when they fail", async ({ page }) => {
  await mockBackend(page, "local");
  // Registered last, so it answers /settings before the mock backend: held until released, then local mode.
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  let fail = false;
  await page.route("**/api/settings", async (route) => {
    if (fail) return route.abort("connectionrefused");
    await held;
    return route.fulfill({ json: { roles: [], admin_mode: "local" } });
  });
  await page.goto("/settings");
  await expect(page.getByText("Loading provider configuration…")).toBeVisible();
  await expect(page.getByText("Locked.")).toHaveCount(0);
  await expect(page.getByText("python -m scripts.admin_token")).toHaveCount(0);
  release();
  await expect(card(page, "FRED (St. Louis Fed)")).toBeVisible();
  await expect(page.getByText("Locked.")).toHaveCount(0);

  // Settings unreachable: a neutral note and the outage notice, still no password prompt.
  fail = true;
  await page.reload();
  await expect(page.getByRole("alert").filter({ hasText: "Unable to reach the data service" })).toBeVisible();
  await expect(page.getByText("Provider configuration appears once the workspace settings load")).toBeVisible();
  await expect(page.getByText("Locked.")).toHaveCount(0);
  await expect(page.getByLabel("Admin token")).toHaveCount(0);
});

test("an invalid built-in default is not pre-filled, help says Optional once, a test note shows once", async ({
  page,
}) => {
  await mockBackend(page);
  await unlock(page);
  const sec = card(page, "SEC EDGAR");
  const agent = sec.getByLabel("User-Agent (name and e-mail)", { exact: true });
  await expect(agent).toHaveValue("");
  await expect(agent).toHaveAttribute("placeholder", "Jane Doe jane.doe@yourdomain.com");
  const blsHelp = await sec.locator("small", { hasText: "BLS_API_KEY" }).textContent();
  expect(blsHelp?.match(/Optional\./g)).toHaveLength(1);
  expect(blsHelp).toContain("Without a key BLS allows about 25 queries per day.");

  await expect(sec.getByText("One filings request.")).toHaveCount(1);
  await sec.getByRole("button", { name: "Test connection: SEC EDGAR" }).click();
  await expect(sec.getByRole("status")).toContainText("Passed");
  // The result carries the note; the static copy is not repeated.
  await expect(sec.getByText("One filings request.")).toHaveCount(1);
});

test("provider configuration recovers by itself after the service did not answer, and offers Retry now", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-09-26T12:00:00Z") });
  await mockBackend(page, "local");
  let fail = true;
  let asked = 0;
  // Registered last, so it answers first: no answer while `fail`, then the mock backend again.
  await page.route("**/api/admin/provider-settings", (route) => {
    asked++;
    return fail ? route.abort("connectionrefused") : route.fallback();
  });
  await page.goto("/settings");
  const notice = page.getByRole("alert").filter({ hasText: "Unable to reach the data service" });
  await expect(notice).toBeVisible();
  await expect(notice.getByRole("button", { name: "Retry now" })).toBeVisible();
  // Plain words, not the raw request error.
  await expect(page.getByText(/Request failed|Provider configuration unavailable/)).toHaveCount(0);
  const before = asked;

  // The service answers again: the workspace asks the failed configuration again without a click or focus change.
  fail = false;
  await page.clock.fastForward(11_000);
  await expect(card(page, "FRED (St. Louis Fed)")).toBeVisible();
  await expect(notice).toHaveCount(0);
  expect(asked).toBeGreaterThan(before);
});

test("a refused admin request is not asked again in the background", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-09-26T12:00:00Z") });
  const calls = await mockBackend(page);
  await page.goto("/settings");
  await page.getByLabel("Admin token").fill("wrong-token");
  await expect(page.getByRole("alert").filter({ hasText: "The service returned an error (HTTP 401)." })).toBeVisible();
  await page.clock.fastForward(25_000);
  await page.waitForTimeout(500);
  expect(calls.filter((c) => c.path === "/admin/provider-settings")).toHaveLength(1);
  await page.getByRole("alert").filter({ hasText: "HTTP 401" }).getByRole("button", { name: "Retry now" }).click();
  await expect.poll(() => calls.filter((c) => c.path === "/admin/provider-settings").length).toBe(2);
  await page.getByLabel("Admin token").fill(TOKEN);
  await expect(card(page, "FRED (St. Louis Fed)")).toBeVisible();
  expect(calls.filter((c) => c.path === "/admin/provider-settings" && c.token === TOKEN)).toHaveLength(1);
});
