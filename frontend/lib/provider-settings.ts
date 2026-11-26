/**
 * Provider configuration contract (docs/PROVIDER_SETTINGS.md): payload types, the
 * server-side value rules mirrored for inline validation, edit payloads and
 * status helpers. Pure, with relative imports only, so vitest can collect it.
 */

export type FieldKind = "secret" | "url" | "text" | "email" | "email_list" | "int" | "bool" | "select";
export type FieldSource = "env_file" | "environment" | "default";
export type FieldValue = string | number | boolean | null;

export type SettingField = {
  name: string;
  env: string;
  label: string;
  kind: FieldKind;
  secret: boolean;
  required: boolean;
  options: string[] | null;
  min: number | null;
  max: number | null;
  help: string | null;
  placeholder: string | null;
  configured: boolean;
  source: FieldSource;
  /** Always null for secrets. */
  value: FieldValue;
  editable: boolean;
};

export type GroupProvider = {
  name: string;
  /** Human name from configuration when the backend sends one; show `label ?? name`. */
  label?: string | null;
  status: string;
  enabled: boolean;
  stub: boolean;
};

export type GroupTest = { available: boolean; target: string | null; label: string; note: string | null };

export type SettingGroup = {
  id: string;
  label: string;
  category: string;
  description: string | null;
  signup_url: string | null;
  pricing: string | null;
  status: string;
  providers: GroupProvider[];
  fields: SettingField[];
  test: GroupTest | null;
};

export type ProviderEntry = GroupProvider & { group: string | null; testable: boolean };

/** GET /admin/provider-settings. */
export type ProviderSettings = {
  env_file: string;
  runtime: "in_process" | "celery" | string;
  groups: SettingGroup[];
  providers: ProviderEntry[];
};

/** PUT /admin/provider-settings/{group_id}: a missing field is unchanged, null removes the key. */
export type SettingsUpdate = { values: Record<string, FieldValue> };

/** PUT /admin/providers/{name}/enabled. */
export type EnabledUpdate = { enabled: boolean; reason?: string };
export type EnabledResult = { name: string; enabled: boolean; status: string };

/** POST /admin/providers/{target}/test. */
export type TestResult = {
  target: string;
  ok: boolean;
  status: string;
  detail: string;
  latency_ms: number | null;
  checked_at: string;
  note?: string | null;
};

/** Edited inputs by field name. Absent = untouched; null = remove the key from .env. */
export type DraftValue = string | boolean | null;
export type Draft = Record<string, DraftValue>;

const MAX_LENGTH = 4096;
const MAX_URL = 2048;
// Same classes as the backend: control characters (CR, LF, NUL…), lone surrogates, line/paragraph separators.
const UNPRINTABLE = /[\p{Cc}\p{Cs}\p{Zl}\p{Zp}]/u;
const EMAIL = /^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z0-9-]{2,}$/;
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);
const INT = /^-?\d{1,10}$/;

function urlError(v: string): string | null {
  if (v.length > MAX_URL) return `At most ${MAX_URL} characters.`;
  let url: URL;
  try {
    url = new URL(v);
  } catch {
    return "Enter a full URL starting with https://.";
  }
  if (!/^https?:\/\//i.test(v) || !url.hostname || /\s/.test(v)) return "Enter a full URL starting with https://.";
  if (url.username || url.password) return "Do not put credentials in the URL; use the key field.";
  if (url.protocol === "http:" && !LOCAL_HOSTS.has(url.hostname))
    return "Use https (plain http is allowed only for localhost).";
  return null;
}

function textError(name: string, v: string): string | null {
  if (name === "sec_user_agent") {
    if (!/[^\s@]+@[^\s@]+\.[^\s@]+/.test(v)) return "Include a contact e-mail address (SEC fair-access policy).";
    if (/example\.com/i.test(v)) return "Use your own contact e-mail, not example.com.";
  }
  if (name === "webpush_subject" && !/^(mailto:|https:\/\/)/.test(v)) return "Start with mailto: or https://.";
  return null;
}

function intError(field: SettingField, v: string): string | null {
  if (!INT.test(v)) return "Enter a whole number.";
  const n = Number(v);
  const { min, max } = field;
  if ((min !== null && n < min) || (max !== null && n > max))
    return min !== null && max !== null
      ? `Enter a number from ${min} to ${max}.`
      : min !== null
        ? `Enter a number of at least ${min}.`
        : `Enter a number of at most ${max}.`;
  return null;
}

/**
 * Inline check of one edited value against the contract's value rules. Returns
 * the message to show, or null. Untouched, removed and empty values pass: the
 * payload decides what they mean.
 */
export function validateField(field: SettingField, value: DraftValue | undefined): string | null {
  if (value === undefined || value === null) return null;
  if (field.kind === "bool") return typeof value === "boolean" ? null : "Choose on or off.";
  if (typeof value !== "string") return "Enter a text value.";
  const v = value.trim();
  if (!v) return null;
  if (v.length > MAX_LENGTH) return `At most ${MAX_LENGTH} characters.`;
  if (UNPRINTABLE.test(v)) return "Remove line breaks and control characters.";
  if (v.includes("${")) return 'Must not contain "${".';
  switch (field.kind) {
    case "url":
      return urlError(v);
    case "text":
      return textError(field.name, v);
    case "email":
      return EMAIL.test(v) ? null : "Enter an e-mail address such as name@domain.org.";
    case "email_list":
      return v.split(",").every((part) => EMAIL.test(part.trim()))
        ? null
        : "Enter e-mail addresses separated by commas.";
    case "int":
      return intError(field, v);
    case "select":
      return field.options?.includes(v) ? null : `Choose one of: ${(field.options ?? []).join(", ")}.`;
    default:
      return null;
  }
}

/** Draft entry -> value to send, or undefined when it changes nothing. */
function outgoing(field: SettingField, raw: DraftValue): FieldValue | undefined {
  if (raw === null) return null;
  if (typeof raw === "boolean") return raw === field.value ? undefined : raw;
  const v = raw.trim();
  // Secrets are write-only: an empty input keeps the stored value.
  if (field.secret || field.kind === "secret") return v || undefined;
  // Clearing a value stored in .env removes it; otherwise the default already applies.
  if (!v) return field.source === "env_file" ? null : undefined;
  const value = field.kind === "int" && INT.test(v) ? Number(v) : v;
  return value === field.value ? undefined : value;
}

/** PUT body with only the edited, editable fields of a group. */
export function buildPayload(fields: SettingField[], draft: Draft): SettingsUpdate {
  const values: Record<string, FieldValue> = {};
  for (const field of fields) {
    if (!field.editable || !(field.name in draft)) continue;
    const value = outgoing(field, draft[field.name]);
    if (value !== undefined) values[field.name] = value;
  }
  return { values };
}

/*
 * A stored key is never sent to a new address: when a destination field
 * changes, the paired secret must be re-entered in the same save, and the
 * backend answers 422 otherwise (docs/PROVIDER_SETTINGS.md).
 */
export const PAIRED_SECRET: Readonly<Record<string, string>> = {
  ollama_base_url: "ollama_api_key",
  openai_base_url: "openai_api_key",
  reuters_base_url: "reuters_api_key",
  ap_base_url: "ap_api_key",
  smtp_host: "smtp_password",
  smtp_port: "smtp_password",
};

export const REENTER_HINT = "Re-enter the key: a stored key is never sent to a new address";

/**
 * Secrets that must be re-entered before saving: the stored, editable secret
 * paired with a destination field this draft changes. Removing the secret in
 * the same save also satisfies the rule (nothing stored is left to follow).
 */
export function secretsToReenter(fields: SettingField[], draft: Draft): string[] {
  const { values } = buildPayload(fields, draft);
  const names = new Set<string>();
  for (const [destination, secret] of Object.entries(PAIRED_SECRET)) {
    if (!(destination in values)) continue;
    const field = fields.find((f) => f.name === secret);
    if (field?.configured && field.editable) names.add(secret);
  }
  return [...names];
}

/** Of `secretsToReenter`, those the draft has not re-entered (or removed) yet; Save waits for them. */
export function missingReentry(fields: SettingField[], draft: Draft): string[] {
  return secretsToReenter(fields, draft).filter((name) => {
    const value = draft[name];
    return value !== null && !(typeof value === "string" && value.trim());
  });
}

/** Validation messages by field name for the edited fields. */
export function fieldErrors(fields: SettingField[], draft: Draft): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of fields) {
    const error = validateField(field, draft[field.name]);
    if (error) errors[field.name] = error;
  }
  return errors;
}

// Free-text kinds whose built-in default can be a placeholder the backend rejects (the SEC example.com contact).
const TYPED_KINDS = new Set<FieldKind>(["text", "url", "email", "email_list", "int"]);

/**
 * Input value for a field: the draft when edited, else the effective value. Secrets are never pre-filled, and
 * neither is a built-in default that does not count as configured (it is invalid), so the placeholder shows.
 */
export function inputValue(field: SettingField, draft: Draft): string | boolean {
  const edited = draft[field.name];
  if (field.kind === "bool") return typeof edited === "boolean" ? edited : field.value === true;
  if (typeof edited === "string") return edited;
  if (edited === null || field.secret || field.value === null) return "";
  if (field.source === "default" && !field.configured && TYPED_KINDS.has(field.kind)) return "";
  return String(field.value);
}

/** Help line under a field: "Optional." once (configured help may already say so), then the help text. */
export function fieldHelp(field: Pick<SettingField, "help">, required: boolean): string {
  const help = (field.help ?? "").replace(/^\s*optional\.\s*/i, "").trim();
  return [required ? "" : "Optional.", help].filter(Boolean).join(" ");
}

/* --- Statuses --- */

/** Worst first. Unknown statuses rank last. */
export const STATUS_ORDER = [
  "OFFLINE",
  "AUTH_ERROR",
  "RATE_LIMITED",
  "TIMEOUT",
  "UNAVAILABLE",
  "DEGRADED",
  "DEVELOPMENT STUB",
  "PENDING",
  "NOT CONFIGURED",
  "DISABLED",
  "CONFIGURED",
  "SENT",
  "ONLINE",
] as const;

/** "not_configured" / "NOT CONFIGURED" -> "NOT CONFIGURED". */
function canon(status: unknown): string {
  return String(status ?? "").trim().toUpperCase().replaceAll("_", " ");
}

export function statusRank(status: unknown): number {
  const s = canon(status);
  const i = STATUS_ORDER.findIndex((o) => canon(o) === s);
  return i === -1 ? STATUS_ORDER.length : i;
}

/** Worst of several statuses (null when there are none). */
export function worstStatus(statuses: string[]): string | null {
  return statuses.reduce<string | null>(
    (worst, s) => (worst === null || statusRank(s) < statusRank(worst) ? s : worst),
    null,
  );
}

/** A development stub (PROVIDER_MODE=stub, no key) serves sample data: working as designed, never real data. */
export const STUB_STATUS = "DEVELOPMENT STUB";

// Not problems: working, deliberately off or unconfigured, not called yet, or a development stub (the backend counts
// a stub as OK: provider_settings.OK_STATUSES).
const QUIET = new Set(["ONLINE", "NOT CONFIGURED", "PENDING", "DISABLED", "CONFIGURED", STUB_STATUS]);

export function isQuiet(status: unknown): boolean {
  return QUIET.has(canon(status));
}

/** Provider health counts; the buckets always add up to `total`. */
export type ProviderCounts = {
  total: number;
  online: number;
  /** Degraded, offline, authentication or rate-limit problems: every status that is not quiet. */
  impaired: number;
  /** Configured but not checked yet (PENDING, CONFIGURED). */
  pending: number;
  notConfigured: number;
  disabled: number;
  /** Development stubs serving sample data: not a problem, and not real data either. */
  stub: number;
  /**
   * Providers expected to be online now: online plus impaired (not pending, unconfigured, switched off or a
   * development stub).
   */
  expected: number;
};

export function providerCounts(statuses: unknown[]): ProviderCounts {
  const c = { total: statuses.length, online: 0, impaired: 0, pending: 0, notConfigured: 0, disabled: 0, stub: 0 };
  for (const status of statuses) {
    const s = canon(status);
    if (s === "ONLINE") c.online++;
    else if (s === "NOT CONFIGURED") c.notConfigured++;
    else if (s === "DISABLED") c.disabled++;
    else if (s === STUB_STATUS) c.stub++;
    else if (isQuiet(s)) c.pending++;
    else c.impaired++;
  }
  return { ...c, expected: c.online + c.impaired };
}

/**
 * Header summary, e.g. "50 online · 2 need attention · 2 not checked yet · 7 not configured · 0 switched off";
 * development stubs are named only when there are any.
 */
export function providerSummaryText(c: ProviderCounts): string {
  return [
    `${c.online} online`,
    `${c.impaired} need attention`,
    `${c.pending} not checked yet`,
    `${c.notConfigured} not configured`,
    `${c.disabled} switched off`,
    c.stub ? `${c.stub} development ${c.stub === 1 ? "stub" : "stubs"}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

/** A provider's display name: its configured label, else its id. */
export function providerLabel(p: { name: string; label?: string | null }): string {
  return typeof p.label === "string" && p.label.trim() ? p.label.trim() : p.name;
}

/* --- Categories --- */

export const CATEGORIES: readonly (readonly [string, string])[] = [
  ["ai", "AI"],
  ["market", "Market data"],
  ["macro", "Macro & official data"],
  ["calendars", "Calendars"],
  ["filings", "Filings"],
  ["news", "News & social"],
  ["alerts", "Alert channels"],
  ["general", "General"],
];

export function categoryLabel(id: string): string {
  return CATEGORIES.find(([key]) => key === id)?.[1] ?? id.replaceAll("_", " ");
}

/** Groups by category in display order; unknown categories follow, alphabetically. */
export function byCategory<G extends { category: string }>(groups: G[]): { id: string; label: string; groups: G[] }[] {
  const known = CATEGORIES.map(([id]) => id);
  const ids = [...new Set(groups.map((g) => g.category))].sort((a, b) => {
    const ia = known.indexOf(a);
    const ib = known.indexOf(b);
    return (ia === -1 ? known.length : ia) - (ib === -1 ? known.length : ib) || a.localeCompare(b);
  });
  return ids.map((id) => ({ id, label: categoryLabel(id), groups: groups.filter((g) => g.category === id) }));
}
