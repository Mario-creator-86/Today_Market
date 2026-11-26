# Provider configuration from the UI

Settings → Data providers lets the owner (no password in local mode, see below):
- enter or replace provider credentials and connection settings;
- enable or disable any provider;
- test a provider's connection.

Changes apply immediately, with no restart. This document is the contract between the backend (`backend/provider_settings.py`, `backend/api_admin.py`) and the frontend (`frontend/components/pages/provider-settings.tsx`).

## Principles

- **Secrets stay on the server.** Credentials are written to the project `.env` (spec §43: "use environment variables"). The API never returns a secret value, only whether it is configured and where it comes from. The UI treats secret inputs as write-only.
- **Admin only.** Every endpoint below is under `/api/admin` and goes through `admin()` (`backend/main.py`, see [SECURITY.md](SECURITY.md) §3):
  - `ADMIN_AUTH=local` (default; single user on this computer): no password. The request must carry `X-Local-Admin: 1`, come from a loopback client to a loopback `Host`, and carry an allowed `Origin` when it sends one;
  - `ADMIN_AUTH=token` (set by Docker Compose for `api`): `X-Admin-Token` must match `ADMIN_TOKEN`.

  Every change is audited with field names, never values.
- **Validate before writing** (`write_values`). A process-wide lock serialises edits.
  1. `.env` is copied to a same-directory working file created `0600` before any secret is written into it.
  2. The candidate values are written with python-dotenv (`>=1.2.3`, pinned in `pyproject.toml`; older versions mis-escaped backslashes).
  3. The copy must read back exactly: every written key has its value, every removed key is gone and every other key is unchanged. Otherwise 422 ("would not read back exactly").
  4. `Settings` and `NotificationSettings` must load from the copy.
  5. The copy replaces `.env` atomically (`os.replace`). An existing file keeps its mode; a new one is `0600`. On any failure `.env` is left unchanged and the working file is deleted.
- **A stored secret never follows a new address.** `destination_secrets` in `config/rules.d/provider_settings.json` pairs each destination with the secret sent to it: `ollama_base_url` → `ollama_api_key`, `openai_base_url` → `openai_api_key`, `reuters_base_url` → `reuters_api_key`, `ap_base_url` → `ap_api_key`, `smtp_host` and `smtp_port` → `smtp_password`. A PUT that changes a destination while its secret is stored must carry that secret too (re-entered, or `null` to remove it); otherwise 422. Nothing is required when no secret is stored or the new destination is empty. When the secret is set in the OS environment, the destination can only be changed there.
- **No clear-text SMTP login.** An e-mail edit is refused (422) when it would leave a login (`smtp_username` set) with neither `smtp_ssl` nor `smtp_starttls` to a server other than localhost. The channel also refuses to send in that case (`SMTP login refused: no SSL/TLS or STARTTLS`).
- **Hot apply.** After a successful write, settings are re-read, and only the affected provider instances (plus the LLM and alert channels when their fields change) are rebuilt and swapped. `hub.providers` is rebound to a new dict, never mutated in size in place. Log redaction learns the new secret values.
- **OS environment wins.** pydantic-settings gives process environment variables precedence over `.env`. A field set in `os.environ` is reported with `source: "environment"` and `editable: false`, and a PUT for it is rejected (409). This is always the case under Docker Compose (`env_file`); there, edit `.env` on the host and recreate the containers.
- **Enable/disable** is stored in the `providers` record table (key = provider name, payload `{name, enabled, reason, updated_at}`); a missing row means enabled. A disabled provider:
  - stays in `hub.providers` with `enabled = False` and `configured = False`;
  - reports health `{"status": "DISABLED", "error": null}`;
  - is never called by any runtime loop (no errors or warnings logged);
  - never triggers PROVIDER alerts (`DISABLED` is in `alerts.provider.ignored_statuses`).

  Re-enabling rebuilds the provider from the current settings. Celery per-task hubs read the same table and re-read settings per task.
- **Tests use fresh instances** built from the saved settings, so they never touch the running provider's health, circuit breaker, cursors or caches. They are bounded by an outer timeout (30 s) and rate-limited (at most one per target every 10 s; otherwise 429). Only cheap, read-only calls are used. Notification-channel tests really send one test message; the UI labels them "Send test message".

## Value rules (server-side, mirrored in the UI)

| kind | rule |
|---|---|
| all | Leading and trailing whitespace is trimmed. At most 4096 characters. No control characters (CR, LF, NUL…). No `${`, which python-dotenv would interpolate. An empty string is not a value: use `null` to remove the key. |
| `secret` | Any other printable text. |
| `url` | `http(s)://host…`, at most 2048 characters. Must be `https` unless the host is `localhost`, `127.0.0.1` or `::1`. |
| `text` | Printable text. `sec_user_agent` must contain an e-mail address and not `example.com` (SEC fair-access policy). `webpush_subject` must start with `mailto:` or `https://`. |
| `email` / `email_list` | A simple `local@domain.tld` pattern; a comma-separated list for `email_list`. |
| `int` | An integer in the declared range (`smtp_port`: 1–65535). |
| `bool` | JSON `true`/`false`, written as `true`/`false`. |
| `select` | One of the declared `options`. |

## Configurable groups

These 18 groups are declared in `config/rules.d/provider_settings.json` (version 1.1.0).

| group id | category | fields (Settings attribute) | providers | test target |
|---|---|---|---|---|
| `ollama` | ai | `ollama_api_key`*, `ollama_base_url` | ollama | ollama (catalog + one web search) |
| `openai` | ai | `openai_api_key`*, `openai_base_url` | openai | openai (`GET /models`) |
| `llm` | ai | `llm_provider` (select ollama/openai) | — | — |
| `massive` | market | `massive_api_key`* | massive, massive-stub | massive |
| `finnhub` | market | `finnhub_api_key`* | finnhub | finnhub |
| `alpaca` | market | `alpaca_api_key_id`*, `alpaca_api_secret_key`* | alpaca | alpaca (one IEX snapshot for SPY) |
| `fred` | macro | `fred_api_key`* | fred | fred |
| `bls` | macro | `bls_api_key`* (optional) | bls_data | bls_data (uses 1 daily BLS query) |
| `fmp` | calendars | `fmp_api_key`* | calendar, calendar-stub | calendar |
| `congress` | calendars | `congress_api_key`* | congress_api, congress_api-stub | congress_api |
| `sec` | filings | `sec_user_agent` | sec_edgar, sec_fulltext | sec_edgar |
| `x` | news | `x_bearer_token`* | x, x-stub | x (1 recent search) |
| `reuters` | news | `reuters_base_url`, `reuters_api_key`* | reuters, reuters-stub | reuters |
| `ap` | news | `ap_base_url`, `ap_api_key`* | ap, ap-stub | ap |
| `telegram` | alerts | `telegram_bot_token`*, `telegram_chat_id` | channel telegram | telegram (sends a message) |
| `email` | alerts | `smtp_host`, `smtp_port`, `smtp_username`, `smtp_password`*, `smtp_from`, `smtp_to`, `smtp_starttls`, `smtp_ssl` | channel email | email (sends a message) |
| `webpush` | alerts | `webpush_vapid_public_key`, `webpush_vapid_private_key`*, `webpush_subject` | channel webpush | webpush (sends to subscribed browsers) |
| `mode` | general | `provider_mode` (select live/stub) | — | — |

`*` = secret. Both Alpaca halves are secrets: the key pair can also trade, although the adapter only reads `data.alpaca.markets` (use a paper-account pair). `ADMIN_TOKEN`, `ADMIN_AUTH`, `DATABASE_URL`, `REDIS_URL`, `CORS_ORIGINS` and the Celery/telemetry settings are **not** editable from the UI.

## API

### `GET /api/admin/provider-settings`

```json
{
  "env_file": "E:\\Today_Market\\.env",
  "runtime": "in_process",
  "groups": [
    {
      "id": "fred",
      "label": "FRED (St. Louis Fed)",
      "category": "macro",
      "description": "Macro series and the BAA10Y credit-spread feature.",
      "signup_url": "https://fredaccount.stlouisfed.org/apikeys",
      "pricing": "Free key",
      "status": "NOT CONFIGURED",
      "providers": [{"name": "fred", "status": "NOT CONFIGURED", "enabled": true, "stub": false}],
      "fields": [
        {"name": "fred_api_key", "env": "FRED_API_KEY", "label": "API key", "kind": "secret", "secret": true,
         "required": true, "options": null, "min": null, "max": null, "help": "…", "placeholder": null,
         "configured": false, "source": "default", "value": null, "editable": true}
      ],
      "test": {"available": true, "target": "fred", "label": "Test connection", "note": null}
    }
  ],
  "providers": [
    {"name": "binance", "status": "ONLINE", "enabled": true, "stub": false, "group": null, "testable": true}
  ]
}
```

- `runtime` is `"celery"` when Celery owns the loops (workers pick changes up on their next task).
- `source` is one of `"env_file"`, `"environment"` or `"default"`.
- `value` is `null` for secrets. Otherwise it is the effective value: a string, number or boolean.
- `status` (group) is the worst status among the group's providers or channel. Channel statuses are `CONFIGURED`, `NOT CONFIGURED` or `UNAVAILABLE`.
- `providers` (top level) lists every provider in `hub.providers`, including stubs, with `testable` and the group id it belongs to.

### `PUT /api/admin/provider-settings/{group_id}`

Body: `{"values": {"fred_api_key": "abc…", "other_field": null}}`. A missing field is left unchanged; `null` removes the key from `.env`.

Responses:
- **200:** the updated group, in the same shape as above.
- **404:** unknown group.
- **422:** unknown field for the group, a value that breaks the rules, a destination changed without its stored secret, an SMTP login without SSL/TLS or STARTTLS to a remote server, or a value that would not read back exactly from `.env`. The detail names the field, never the value; the message starts with "Rejected, .env left unchanged" for the writer's policies.
- **409:** field set in the OS environment.

Audit: `provider_settings_change` with `{group, changed: [field…], removed: [field…]}`.

### `PUT /api/admin/providers/{name}/enabled`

Body: `{"enabled": false, "reason": "optional, ≤ 200 chars"}`.

- **200:** `{"name", "enabled", "status"}`.
- **404:** unknown provider.

Audit: `provider_enabled` or `provider_disabled`.

### `POST /api/admin/providers/{target}/test`

`target` is any provider name, or `telegram`, `email`, `webpush`, `ollama` or `openai`.

- **200:** `{"target", "ok": bool, "status": "ONLINE|AUTH_ERROR|RATE_LIMITED|OFFLINE|DEGRADED|NOT CONFIGURED|DISABLED|DEVELOPMENT STUB|TIMEOUT|SENT|UNAVAILABLE", "detail": "safe text, never a secret", "latency_ms": 123, "checked_at": "ISO UTC", "note": "optional cost note"}`.
- **404:** unknown target.
- **429:** tested less than 10 s ago.

Audit: `provider_test` with `{target, ok, status}`.

## UI (Settings → Data providers)

- `GET /api/settings` returns `admin_mode` (`local` or `token`). In local mode there is no token field: the page sends `X-Local-Admin: 1` (`adminHeaders()` in `frontend/lib/api.ts`, token placeholder `LOCAL_ADMIN`) and every admin action is unlocked.
- In token mode the admin token field explains where the token is: `ADMIN_TOKEN` in the project `.env`, or run `python -m scripts.admin_token`. **Provider configuration** is shown only once a token is entered. Groups are listed by category (AI, Market data, Macro & official data, Calendars, Filings, News & social, Alert channels, General). Each group card shows:
  - its status, pricing and a signup link;
  - one input per field (secrets as password inputs showing "Configured (.env)" / "Not set", never the value). When a paired destination is edited, its stored secret shows "Required: re-enter the key" (`PAIRED_SECRET` in `frontend/lib/provider-settings.ts` mirrors `destination_secrets`);
  - a per-field "Remove";
  - Save;
  - Test / Send test message, which is disabled while there are unsaved edits;
  - the test result;
  - enable/disable switches for the group's providers.
- **Provider health** is the existing table, plus Enabled (switch) and Test columns when admin actions are unlocked. `DISABLED` is a neutral, quiet status: grey badge, not counted as impaired, and not shown as needing attention on the dashboard.
- Error messages from the backend (`detail`) are shown to the user.
