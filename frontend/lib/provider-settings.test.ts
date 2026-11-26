import { describe, expect, it } from "vitest";
import {
  buildPayload,
  byCategory,
  categoryLabel,
  fieldErrors,
  fieldHelp,
  inputValue,
  isQuiet,
  missingReentry,
  providerCounts,
  providerLabel,
  providerSummaryText,
  REENTER_HINT,
  secretsToReenter,
  STATUS_ORDER,
  statusRank,
  validateField,
  worstStatus,
  type SettingField,
} from "./provider-settings";

function field(over: Partial<SettingField>): SettingField {
  return {
    name: "fred_api_key",
    env: "FRED_API_KEY",
    label: "API key",
    kind: "secret",
    secret: true,
    required: true,
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

const secret = field({ configured: true, source: "env_file" });
const url = field({ name: "ollama_base_url", kind: "url", secret: false, value: "http://localhost:11434" });
const port = field({ name: "smtp_port", kind: "int", secret: false, min: 1, max: 65535, value: 587, source: "env_file" });
const tls = field({ name: "smtp_starttls", kind: "bool", secret: false, value: true });
const to = field({ name: "smtp_to", kind: "email_list", secret: false, value: null });
const mode = field({ name: "provider_mode", kind: "select", secret: false, options: ["live", "stub"], value: "live" });
const agent = field({ name: "sec_user_agent", kind: "text", secret: false, value: null });
const subject = field({ name: "webpush_subject", kind: "text", secret: false, value: null });
const fromEnv = field({ name: "x_bearer_token", source: "environment", configured: true, editable: false });

describe("provider value rules", () => {
  it("lets untouched, removed and empty values through", () => {
    expect(validateField(secret, undefined)).toBeNull();
    expect(validateField(secret, null)).toBeNull();
    expect(validateField(secret, "   ")).toBeNull();
  });
  it("rejects control characters, interpolation and oversized values for every kind", () => {
    expect(validateField(secret, "abc\ndef")).toMatch(/control characters/);
    expect(validateField(secret, "a\u0000b")).toMatch(/control characters/);
    expect(validateField(secret, "a b")).toMatch(/control characters/);
    expect(validateField(secret, "a\u0085b")).toMatch(/control characters/);
    expect(validateField(secret, "key-\u{1F511}")).toBeNull();
    expect(validateField(secret, "pre${HOME}")).toMatch(/\$\{/);
    expect(validateField(secret, "x".repeat(4097))).toMatch(/4096/);
    // Surrounding whitespace (a pasted trailing newline) is trimmed, not rejected.
    expect(validateField(secret, "  key-123\n")).toBeNull();
  });
  it("requires https URLs except for local hosts", () => {
    expect(validateField(url, "https://api.example.org/v1")).toBeNull();
    expect(validateField(url, "http://localhost:11434")).toBeNull();
    expect(validateField(url, "http://127.0.0.1:11434")).toBeNull();
    expect(validateField(url, "http://[::1]:11434")).toBeNull();
    expect(validateField(url, "http://ollama.lan:11434")).toMatch(/https/);
    expect(validateField(url, "ftp://host")).toMatch(/full URL/);
    expect(validateField(url, "localhost:11434")).toMatch(/full URL/);
    expect(validateField(url, "https://api.example.org/a b")).toMatch(/full URL/);
    expect(validateField(url, "https://user:pw@api.example.org")).toMatch(/credentials/);
    expect(validateField(url, `https://h.org/${"a".repeat(2050)}`)).toMatch(/2048/);
  });
  it("checks integers against the declared range", () => {
    expect(validateField(port, "465")).toBeNull();
    expect(validateField(port, "0")).toMatch(/from 1 to 65535/);
    expect(validateField(port, "70000")).toMatch(/from 1 to 65535/);
    expect(validateField(port, "4.5")).toMatch(/whole number/);
    expect(validateField(port, "+465")).toMatch(/whole number/);
    expect(validateField(field({ kind: "int", secret: false }), "12345678901")).toMatch(/whole number/);
  });
  it("checks e-mail lists, selects, booleans and the special text fields", () => {
    expect(validateField(to, "a@b.org, c@d.io")).toBeNull();
    expect(validateField(to, "a@b.org,")).toMatch(/separated by commas/);
    expect(validateField(to, "not-an-address")).toMatch(/separated by commas/);
    expect(validateField(to, "a@b.c")).toMatch(/separated by commas/);
    expect(validateField(to, "Ops <ops@desk.io>")).toMatch(/separated by commas/);
    expect(validateField(field({ kind: "email", secret: false }), "ops@desk.io")).toBeNull();
    expect(validateField(mode, "stub")).toBeNull();
    expect(validateField(mode, "paper")).toMatch(/live, stub/);
    expect(validateField(tls, false)).toBeNull();
    expect(validateField(tls, "yes")).toMatch(/on or off/);
    expect(validateField(agent, "Desk Research ops@desk.io")).toBeNull();
    expect(validateField(agent, "Desk Research")).toMatch(/e-mail/);
    expect(validateField(agent, "Sample admin@example.com")).toMatch(/example\.com/);
    expect(validateField(subject, "mailto:ops@desk.io")).toBeNull();
    expect(validateField(subject, "https://desk.io")).toBeNull();
    expect(validateField(subject, "ops@desk.io")).toMatch(/mailto:/);
  });
  it("collects messages per edited field", () => {
    expect(fieldErrors([port, to], { smtp_port: "0", smtp_to: "a@b.org" })).toEqual({
      smtp_port: "Enter a number from 1 to 65535.",
    });
  });
});

describe("provider settings payload", () => {
  const fields = [secret, url, port, tls, to, mode, fromEnv];
  it("sends only edited fields, trimmed and typed", () => {
    expect(buildPayload(fields, {})).toEqual({ values: {} });
    expect(
      buildPayload(fields, { fred_api_key: " new-key ", smtp_port: "465", smtp_starttls: false, provider_mode: "stub" }),
    ).toEqual({ values: { fred_api_key: "new-key", smtp_port: 465, smtp_starttls: false, provider_mode: "stub" } });
  });
  it("treats an empty secret as unchanged and explicit removal as null", () => {
    expect(buildPayload(fields, { fred_api_key: "" })).toEqual({ values: {} });
    expect(buildPayload(fields, { fred_api_key: null })).toEqual({ values: { fred_api_key: null } });
  });
  it("drops edits that restore the current value", () => {
    expect(buildPayload(fields, { smtp_port: "587", smtp_starttls: true, provider_mode: "live" })).toEqual({
      values: {},
    });
    expect(buildPayload(fields, { ollama_base_url: " http://localhost:11434 " })).toEqual({ values: {} });
  });
  it("removes a cleared .env value but leaves defaults alone", () => {
    expect(buildPayload(fields, { smtp_port: "" })).toEqual({ values: { smtp_port: null } });
    expect(buildPayload(fields, { ollama_base_url: "" })).toEqual({ values: {} });
  });
  it("never sends fields set by the OS environment", () => {
    expect(buildPayload(fields, { x_bearer_token: "abc" })).toEqual({ values: {} });
  });
  it("never pre-fills secrets", () => {
    expect(inputValue(secret, {})).toBe("");
    expect(inputValue(secret, { fred_api_key: "typed" })).toBe("typed");
    expect(inputValue(port, {})).toBe("587");
    expect(inputValue(port, { smtp_port: null })).toBe("");
    expect(inputValue(tls, {})).toBe(true);
    expect(inputValue(tls, { smtp_starttls: false })).toBe(false);
  });
  it("does not pre-fill a built-in default the backend rejects, so the placeholder shows", () => {
    const example = field({
      name: "sec_user_agent",
      kind: "text",
      secret: false,
      value: "MarketIntelligence contact@example.com",
      source: "default",
      configured: false,
      placeholder: "Jane Doe jane.doe@yourdomain.com",
    });
    expect(inputValue(example, {})).toBe("");
    expect(inputValue(example, { sec_user_agent: "Desk desk@firm.org" })).toBe("Desk desk@firm.org");
    // A valid default (configured) and a value stored in .env are shown; selects keep their default option.
    expect(inputValue({ ...example, configured: true, value: "Desk desk@firm.org" }, {})).toBe("Desk desk@firm.org");
    expect(inputValue({ ...example, source: "env_file" }, {})).toBe("MarketIntelligence contact@example.com");
    expect(inputValue(mode, {})).toBe("live");
  });
  it("says Optional once, even when the configured help already starts with it", () => {
    const bls = { help: "Optional. Without a key BLS allows about 25 queries per day." };
    expect(fieldHelp(bls, false)).toBe("Optional. Without a key BLS allows about 25 queries per day.");
    expect(fieldHelp(bls, true)).toBe("Without a key BLS allows about 25 queries per day.");
    expect(fieldHelp({ help: "Series API key." }, false)).toBe("Optional. Series API key.");
    expect(fieldHelp({ help: null }, true)).toBe("");
  });
});

describe("a stored key never follows a new address", () => {
  const smtp = [
    field({ name: "smtp_host", kind: "text", secret: false, value: "smtp.desk.io", source: "env_file" }),
    port,
    field({ name: "smtp_password", env: "SMTP_PASSWORD", configured: true, source: "env_file" }),
    to,
  ];
  const ollama = [
    field({ name: "ollama_api_key", env: "OLLAMA_API_KEY", configured: true, source: "env_file" }),
    url,
  ];
  it("requires the paired secret once a destination field changes", () => {
    expect(secretsToReenter(smtp, { smtp_host: "mail.other.org" })).toEqual(["smtp_password"]);
    expect(missingReentry(smtp, { smtp_host: "mail.other.org" })).toEqual(["smtp_password"]);
    expect(missingReentry(smtp, { smtp_port: "465", smtp_password: "  " })).toEqual(["smtp_password"]);
    expect(secretsToReenter(ollama, { ollama_base_url: "https://ollama.example.org" })).toEqual([
      "ollama_api_key",
    ]);
    // Clearing a destination stored in .env is a change of address too.
    expect(secretsToReenter(smtp, { smtp_port: "" })).toEqual(["smtp_password"]);
    expect(REENTER_HINT).toBe("Re-enter the key: a stored key is never sent to a new address");
  });
  it("is satisfied by a re-entered or removed key", () => {
    expect(missingReentry(smtp, { smtp_host: "mail.other.org", smtp_password: "new-pass" })).toEqual([]);
    expect(missingReentry(smtp, { smtp_host: "mail.other.org", smtp_password: null })).toEqual([]);
  });
  it("asks nothing when no destination changes, no key is stored, or the key is not editable", () => {
    expect(secretsToReenter(smtp, { smtp_to: "a@b.org" })).toEqual([]);
    // Typing the current value again is not a change.
    expect(secretsToReenter(smtp, { smtp_host: " smtp.desk.io ", smtp_port: "587" })).toEqual([]);
    const noKey = [field({ name: "ollama_api_key", configured: false }), url];
    expect(secretsToReenter(noKey, { ollama_base_url: "https://ollama.example.org" })).toEqual([]);
    const fromOs = [field({ name: "openai_api_key", configured: true, source: "environment", editable: false }),
      field({ name: "openai_base_url", kind: "url", secret: false, value: "https://api.openai.com/v1" })];
    expect(secretsToReenter(fromOs, { openai_base_url: "https://proxy.example.org/v1" })).toEqual([]);
  });
});

describe("provider statuses and categories", () => {
  it("orders statuses worst first with DISABLED and DEVELOPMENT STUB known", () => {
    expect(STATUS_ORDER).toContain("DISABLED");
    expect(STATUS_ORDER).toContain("DEVELOPMENT STUB");
    expect(statusRank("DISABLED")).toBe(statusRank("NOT CONFIGURED") + 1);
    expect(statusRank("not_configured")).toBe(statusRank("NOT CONFIGURED"));
    expect(statusRank("SOMETHING NEW")).toBe(STATUS_ORDER.length);
    expect(worstStatus(["ONLINE", "DISABLED", "AUTH_ERROR"])).toBe("AUTH_ERROR");
    expect(worstStatus(["ONLINE", "DISABLED"])).toBe("DISABLED");
    expect(worstStatus([])).toBeNull();
  });
  it("counts providers into buckets that add up, expecting online only what is configured and checked", () => {
    const statuses = [
      ...Array(50).fill("ONLINE"),
      ...Array(7).fill("NOT CONFIGURED"),
      "PENDING",
      "pending",
      "AUTH_ERROR",
      "DEGRADED",
      "DISABLED",
    ];
    const c = providerCounts(statuses);
    expect(c).toEqual({
      total: 62,
      online: 50,
      impaired: 2,
      pending: 2,
      notConfigured: 7,
      disabled: 1,
      stub: 0,
      expected: 52,
    });
    expect(c.online + c.impaired + c.pending + c.notConfigured + c.disabled + c.stub).toBe(c.total);
    expect(providerSummaryText(c)).toBe(
      "50 online · 2 need attention · 2 not checked yet · 7 not configured · 1 switched off",
    );
    // Only quiet statuses: nothing needs attention.
    expect(providerCounts(["ONLINE", "NOT CONFIGURED", "PENDING"]).impaired).toBe(0);
    expect(providerCounts([])).toEqual({
      total: 0,
      online: 0,
      impaired: 0,
      pending: 0,
      notConfigured: 0,
      disabled: 0,
      stub: 0,
      expected: 0,
    });
  });
  it("counts development stubs apart: not a problem, not expected online (PROVIDER_MODE=stub)", () => {
    const c = providerCounts(["ONLINE", "DEVELOPMENT STUB", "DEVELOPMENT STUB", "development_stub", "AUTH_ERROR"]);
    expect(c).toMatchObject({ total: 5, online: 1, stub: 3, impaired: 1, pending: 0, expected: 2 });
    expect(c.online + c.impaired + c.pending + c.notConfigured + c.disabled + c.stub).toBe(c.total);
    // Only stubs besides online providers: nothing needs attention, so the header stays quiet.
    expect(providerCounts(["ONLINE", "DEVELOPMENT STUB"]).impaired).toBe(0);
    expect(providerSummaryText(c)).toBe(
      "1 online · 1 need attention · 0 not checked yet · 0 not configured · 0 switched off · 3 development stubs",
    );
    expect(providerSummaryText(providerCounts(["DEVELOPMENT STUB"]))).toContain("1 development stub");
  });
  it("shows a provider's configured label, else its id", () => {
    expect(providerLabel({ name: "finnhub", label: "Finnhub (equities)" })).toBe("Finnhub (equities)");
    expect(providerLabel({ name: "nyfed_rates", label: "  " })).toBe("nyfed_rates");
    expect(providerLabel({ name: "x" })).toBe("x");
  });
  it("treats working, off and unconfigured providers as quiet", () => {
    for (const s of ["ONLINE", "NOT CONFIGURED", "PENDING", "DISABLED", "CONFIGURED", "not_configured", "DEVELOPMENT STUB"])
      expect(isQuiet(s)).toBe(true);
    for (const s of ["OFFLINE", "AUTH_ERROR", "RATE_LIMITED", "DEGRADED", "UNAVAILABLE"])
      expect(isQuiet(s)).toBe(false);
  });
  it("labels and orders categories, unknown ones last", () => {
    expect(categoryLabel("alerts")).toBe("Alert channels");
    expect(categoryLabel("crypto_extra")).toBe("crypto extra");
    const groups = [
      { id: "mode", category: "general" },
      { id: "zz", category: "zeta" },
      { id: "fred", category: "macro" },
      { id: "ollama", category: "ai" },
      { id: "openai", category: "ai" },
    ];
    expect(byCategory(groups).map((c) => [c.id, c.groups.map((g) => g.id)])).toEqual([
      ["ai", ["ollama", "openai"]],
      ["macro", ["fred"]],
      ["general", ["mode"]],
      ["zeta", ["zz"]],
    ]);
  });
});
