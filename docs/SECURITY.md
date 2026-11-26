# Security

Scope: a single-user, local-first research tool. There are no user accounts, no multi-tenant isolation and no TLS in the repository. The design assumes the API and UI are reachable only from the operator's machine. Each section below separates what the code does from what it does not do.

## 1. Network exposure and binding

| Mode | API | UI | Other services |
|---|---|---|---|
| `python main.py` (or `launch.py`) | uvicorn on `127.0.0.1:8000` | `next dev --hostname 127.0.0.1` on `:3000` | none |
| `docker compose up` | container listens on `0.0.0.0:8000`, published as `127.0.0.1:8000` only | published as `127.0.0.1:3000` only | `db` (Postgres) and `redis` have **no published ports** (compose network only) |

- Images run as non-root (`appuser` for the backend and `node` for the frontend).
- Redis runs **without a password**. It is safe only because it stays on the internal compose network.
- Postgres needs `POSTGRES_PASSWORD`. Compose refuses to start when it is unset (`${POSTGRES_PASSWORD:?...}`).
- The backend sets no TLS and no security headers (CSP, HSTS, X-Frame-Options). The Next config only disables `X-Powered-By`.

## 2. Secrets handling

- **Source.** Secrets come from environment variables or the root `.env`, loaded by pydantic-settings (`Settings`, `NotificationSettings`, `TelemetrySettings`, `WorkerSettings`). `.env` appears in both `.gitignore` and `.dockerignore`. The backend `Dockerfile` copies only `pyproject.toml`, `backend/`, `config/`, `alembic.ini` and `migrations/`, and compose injects `.env` at runtime through `env_file`. As a result, no secret is baked into an image.
- **Template.** Every secret in `.env.example` is empty (non-secret defaults such as `ADMIN_AUTH=local` are preset). `python -m scripts.init_local` fills in `ADMIN_TOKEN` and `POSTGRES_PASSWORD` with `secrets.token_urlsafe(32)` when they are missing, and never prints them.
- **Typed as `SecretStr`:** `ADMIN_TOKEN`, `OLLAMA_API_KEY`, `OPENAI_API_KEY`, `MASSIVE_API_KEY`, `X_BEARER_TOKEN`, `FMP_API_KEY`, `REUTERS_API_KEY`, `AP_API_KEY`, `CONGRESS_API_KEY`, `FINNHUB_API_KEY`, `FRED_API_KEY`, `BLS_API_KEY`, `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` (both halves: the pair can trade), `TELEGRAM_BOT_TOKEN`, `SMTP_PASSWORD`, `WEBPUSH_VAPID_PRIVATE_KEY`.
- **Not `SecretStr` (plain strings):** `DATABASE_URL`, `REDIS_URL` and `CELERY_BROKER_URL`, which can embed passwords. The same applies to `SMTP_USERNAME`, `TELEGRAM_CHAT_ID` and `WEBPUSH_VAPID_PUBLIC_KEY`, which is public by design.
- **Log redaction** (`backend/main.py`). Logs are JSON by default, or plain text; both go through the same redaction:
  - Regex masking covers the query parameters `api_key`, `apikey`, `key`, `token`, `access_token`, `secret`, `password`, `signature` and `sig`. It also masks `key=value` and `key: value` pairs, `Bearer <token>`, and Telegram `/bot<id>:<token>` paths.
  - The literal value of every configured `SecretStr` of 6 or more characters in `Settings` and `NotificationSettings` is replaced with `[REDACTED]`.
  - The `httpx`, `httpcore`, `hpack` and `websockets.client` loggers are raised to WARNING, because some providers put keys in URLs. Uvicorn loggers are routed through the same handler.
  - Gap: the connection URLs (`DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`) are not in the known-secret list, so do not log them.
- **Writes from the UI** (Settings → Data providers, [PROVIDER_SETTINGS.md](PROVIDER_SETTINGS.md)). Values go to `.env` through a `0600` working copy that must read back exactly (python-dotenv `>=1.2.3`) and load in both settings classes before an atomic replace; a new `.env` is created `0600`. A stored secret is never sent to a new address: changing a base URL or the SMTP host/port requires re-entering the paired secret in the same request (422 otherwise). Newly saved secrets join the log-redaction list immediately.
- **Provider health and errors.** `HTTPProvider` stores only `ExceptionType: HTTP <status>`, never request URLs or response bodies (`backend/providers/base.py`). Telegram transport errors record only the exception type. `/api/settings` reports channel status as `CONFIGURED` or `NOT CONFIGURED` and never includes credentials.

### Frontend

- The only client-side variable is `NEXT_PUBLIC_API_URL` (`frontend/lib/api.ts`, default `http://127.0.0.1:8000/api`). It is inlined at build time (`ARG` in `frontend/Dockerfile`). The frontend reads no secret.
- Next.js reads `frontend/.env*`, not the root `.env`. The root `.env.example` entry `NEXT_PUBLIC_API_URL` (without `/api`) is not used by the frontend build.
- `frontend/.dockerignore` excludes `.env.local` but not `.env`. Keep secrets out of all frontend env files, because any `NEXT_PUBLIC_*` value ends up in the browser bundle.
- **Admin calls.** Every admin call sends `X-Local-Admin: 1` (`adminHeaders()` in `frontend/lib/api.ts`). The Settings page reads `admin_mode` from `GET /api/settings`: in local mode it shows no token field; in token mode the token is typed into a password field and kept only in React state (page memory), sent only as `X-Admin-Token` on admin calls and never persisted. Playwright tests assert it is absent from `localStorage`. `localStorage` holds only alert-display preferences.

## 3. Administrative authentication

The app is single-user on the owner's PC, so the default needs no password (the owner asked for none). Every `/api/admin/*` route depends on `admin()` (`backend/main.py`). `Settings.admin_auth` (env `ADMIN_AUTH`) selects the mode:

- A matching `X-Admin-Token` (`secrets.compare_digest`, constant-time) is accepted in both modes when `ADMIN_TOKEN` is set.
- **`local` (default).** No password. The request must pass all of:
  - `X-Local-Admin: 1`. A custom header forces a CORS preflight, which the API grants only to `CORS_ORIGINS`, so another website cannot send it (CSRF);
  - a loopback client address (`127.0.0.0/8`, `::1`, `localhost`), so other machines are refused;
  - a loopback `Host` header, so a DNS-rebinding page (which keeps its own host name) is refused;
  - an `Origin` in `CORS_ORIGINS` when the request carries one.

  Otherwise `403` ("Administration is only available from this computer").
- **`token`.** `X-Local-Admin` is ignored. An empty `ADMIN_TOKEN` returns `503` ("Administrative writes disabled"); a wrong token returns `401`. Docker Compose sets `ADMIN_AUTH=token` for `api`, because requests reach the container from the Docker bridge, not loopback.
- Tests: `tests/test_local_admin.py` (no password on this computer; missing header, foreign `Origin`, rebinding `Host` and another machine are refused; the preflight allows `x-local-admin` only for the UI origin; token mode ignores the header).
- **Local mode trusts every process on the machine.** Any local program that can reach the port can send the header without a browser; the Host and Origin checks only stop web pages.
- **Not implemented:**
  - users, roles, sessions, token expiry or rotation endpoints;
  - lockout or rate limiting of failed attempts;
  - a minimum token length;
  - actor identity in audit rows. There is one owner (local mode) or one shared token.

### Admin endpoints and audit coverage (`audit_logs`)

| Endpoint | `audit_logs` entry |
|---|---|
| `PUT /admin/roles/{role}` | `role_assignment` (model must be discovered and available) |
| `PUT /admin/sources/{id}` | `source_override`, with previous, current and a mandatory reason (3–500 characters) |
| `PUT /admin/config/{fragment}` | `config_change` (before and after), plus a `configuration_versions` snapshot |
| `PUT /admin/provider-settings/{group}`, `PUT /admin/providers/{name}/enabled`, `POST /admin/providers/{target}/test` | `provider_settings_change` (field names only), `provider_enabled` / `provider_disabled`, `provider_test` ([PROVIDER_SETTINGS.md](PROVIDER_SETTINGS.md)) |
| `POST /admin/calendar` | `calendar_import` |
| `POST /admin/calibration/promote/{id}` | `calibration_promote`, plus `<kind>_promotion` |
| `POST /admin/calibration/{symbol}/rollback`, `POST /admin/autolearn/weights/rollback` | `calibration_rollback` or `weights_rollback`, plus `<kind>_rollback` |
| `POST /admin/backtest` | `backtest` |
| `POST /admin/models/discover` | `model_discovered` and `model_unavailable` |
| `POST /admin/documents` | `document_import` (source, URL, event). Stored as a `raw_documents` row with `provenance=manual_import`, always `discovery_only` |
| `POST /admin/recalculate`, `/models/discover`, `/ollama/test`, `/research`, `/social/search`, `/calibration/{symbol}/candidate`, `/autolearn/challengers/{id}/evaluate` | `recalculate`, `model_discovery`, `ollama_test`, `research_search`, `social_search`, `calibration_candidate`, `challenger_evaluation` |
| `GET /admin/filings/{cik}`, `/admin/ai-runs`, `/admin/audit-logs` | reads only |

Every admin write leaves an `audit_logs` entry (tests/test_api_extended.py checks a sample).

### Configuration editing (`PUT /api/admin/config/{fragment}`)

- **Editable fragments:**
  - `refresh` (`rules.d/refresh.json`: `cadences`);
  - `alerts` (`rules.d/alerts.json`: `priorities`, `cooldown_seconds`, `enabled_kinds`, `min_priority`, `channels`);
  - `scoring` (`rules.json`: `weights`, `thresholds`).
- **Not editable over HTTP:** all other rules, including `security.json` and `sources.json`. They change only through file edits.
- **Flow:**
  1. A top-level key outside the allowlist returns `422`.
  2. The change is deep-merged, the fragment version is bumped, the file is written and `reload_rules()` runs.
  3. If the reload raises `ValueError` or `KeyError`, or the scoring weights do not sum to 1, the original file text is restored and the request returns `422`.
- **Shape and write safety:**
  - An edit may change values, never the structure. Unknown nested keys, changed JSON types and negative numbers are rejected with `422` before anything is written (`_shape_errors`).
  - Edits are serialised by a lock and written atomically (temporary file plus `os.replace`), so the `rules()` cache never reads a half-written file.
  - Range semantics beyond "non-negative", such as the weights summing to 1, are checked by the reload.

## 4. CORS and unauthenticated surface

- **`CORSMiddleware` settings:**
  - `allow_origins=CORS_ORIGINS` (default `http://localhost:3000` and `http://127.0.0.1:3000`; `main.py` rewrites it for a non-default `--ui-port`);
  - methods `GET`, `POST`, `PUT`;
  - headers `Content-Type`, `X-Admin-Token`, `X-Local-Admin`, `X-Request-ID`;
  - credentials not allowed;
  - `X-Request-ID` exposed.
- **WebSocket `/ws`.** The connection is closed with code `1008` when an `Origin` header is present and not listed. Clients that send no `Origin` (anything other than a browser) are accepted.
- **CORS is not access control.** Any process that can reach the port can call the following without a token (in local mode any process on this computer can also call `/api/admin/*`, see section 3):
  - all `GET /api/*`;
  - `/health`, `/health/ready`, `/metrics`, `/metrics/json`;
  - `/ws`;
  - FastAPI's default `/docs`, `/redoc`, `/openapi.json`;
  - `POST /api/notifications/push-subscriptions` (a write; see section 8).
- **`X-Request-ID`.** It is accepted only if it matches `^[A-Za-z0-9._:-]{8,128}$` or a valid W3C `traceparent`. Otherwise the server generates a new ID; the supplied value is never reflected.

## 5. Input validation

- **Request bodies.** They use `StrictModel` (`extra='forbid'`, `allow_inf_nan=False`):
  - `DocumentInput`: title 5–1000 characters, content up to 40,000, `HttpUrl`, timezone-aware `published_at`, at most 30 assets, `relation` limited to `supports` or `contradicts`.
  - `CalendarInput`: bounded fields, impact 0–10.
  - `SearchRequest`: 3–512 characters, priority 0–3.
  - `SourceOverride`: tier 1–5, trust 0–1.
  - `BacktestRequest`: step of at least 60 s.
  - List endpoints bound `limit`.
- **Exceptions:**
  - The admin config edit body is a raw `dict`, checked only against the key allowlist.
  - The push-subscription body is a raw `dict`, validated by hand: an `https://` endpoint of at most 2048 characters whose host is a known browser push service (`channels.webpush.allowed_push_hosts`), `p256dh` and `auth` strings of 8–256 characters, and at most `max_subscriptions` (50) subscriptions.
- **Body size.** `BodySizeLimitMiddleware` answers `413` for bodies above `MAX_REQUEST_BYTES` (default 1,000,000), whether declared by Content-Length or streamed. Ingestion truncates document text to `MAX_DOCUMENT_CHARS` (at most 40,000, the schema cap) and marks the document `truncated`.
- **Quotes** (`backend/ingestion.py`):
  - a timestamp more than `MAX_FUTURE_SECONDS` in the future (default 5 s) is rejected;
  - an unknown symbol is rejected;
  - a timestamp at or before the last stored one is dropped;
  - timestamps are stored in UTC.
- **Documents** (`backend/ingestion.py`):
  - a future `published_at` is rejected;
  - `source_id` must exist in the `sources` registry (from `config/rules.d/sources.json`);
  - the URL host must equal the registered `domain` or be a subdomain of it (`Source domain mismatch`);
  - a SHA-256 fingerprint deduplicates identical submissions.
- **Provider payloads** (`backend/providers/models.py`):
  - Each vendor has a Pydantic row model. Extra vendor fields are kept, typed fields must validate, and `inf` or `NaN` values are refused.
  - Invalid rows are dropped and counted in the provider's `rejected_rows`, never coerced.
  - `parse_utc` rejects timestamps without a timezone.
  - The `Quote` model enforces a symbol pattern, `price > 0`, a provider enum and `bid <= ask`.
- **Outbound HTTP.**
  - Providers call fixed base URLs from code, config or env, with `follow_redirects=False`.
  - Two fetches do follow redirects: the OFAC SLS delta and the EIA WPSR table. Both reject a final host outside an allowlist, although the redirected request has already been sent.
  - Alpaca (`backend/providers/alpaca.py`): the constructor refuses any base URL other than `https://data.alpaca.markets`; only `GET` on `/v2/stocks/snapshots`, `/v2/stocks/bars` and `/v1beta1/news` with `feed=iex`, checked before any network I/O; credentials travel only in the `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` headers.
  - Web Push posts to subscriber-supplied URLs, but only on allowlisted push-service hosts (section 8).
- **Celery** accepts only JSON (`accept_content=['json']`). `pickle` is used only in `config.py` to copy trusted, in-process configuration.

## 6. Untrusted external text (prompt injection)

Articles, filings, X posts, search results and LLM output are treated as data. External text is never executed or treated as instructions.

- **Screening at ingestion** (`screen_text`). The title and content are checked against `config/rules.d/security.json`, which holds 14 instruction-like regexes and a set of invisible and bidi characters. Matches set `injection_suspected` and `injection_findings` on the document and event, shown in `/api/verification`. The text itself is kept verbatim as evidence. Screening flags; it does not block.
- **Before any LLM call** (`backend/ai.py`):
  - `screen_inputs` strips invisible and bidi characters and truncates strings over 4,000 characters and lists over 40 items;
  - findings are stored in `ai_runs`;
  - the serialized input is capped at `search_budget.max_llm_input_chars` (160,000);
  - data goes in the user message wrapped as `{"untrusted_input_data": ...}`;
  - the system prompt is versioned and hashed.
- **After the call:**
  - strict schema validation per role (`ROLE_SCHEMAS`, `strict=True`, extra fields forbidden);
  - any evidence ID not supplied by the backend fails closed;
  - for `chief_analyst` and `auditor`, every number in the output must appear in the input or the numeric allowlist;
  - one correction retry is allowed, then the run is recorded as failed.
- **No tools or function calling** are passed to the model. Model output never triggers HTTP calls, writes to trust settings or configuration, or admin actions.
- **`accept_extraction`.** An event type or asset proposed by the AI is applied only when the evidence text literally contains a trigger keyword or asset name (word-boundary match). Rejected proposals are recorded, not applied.
- **Verification and confirmation.** The fact checker only proposes relations; `engines.confidence` computes status and confidence. Evidence marked `discovery_only` never counts toward confirmation. That covers manual imports, research results and unknown X accounts. GDELT candidates are stored separately in `discovery_items` and are never evidence.
- **Research queries** (`bound_queries`). URLs are stripped, queries with instruction-like text are dropped, and queries are capped at 256 characters and 3 per plan. Search results are validated (`SearchResult`) and stored in `research_jobs` as `discovery_only`. Results from admin research are also labelled `UNCONFIRMED`.

## 7. No trading

- There are no broker or exchange order endpoints; all market providers are read-only.
- An Alpaca key pair could place orders, so the adapter is fenced to the market-data host (section 5), and `tests/test_sources_coverage.py` fails if a trading host or path (`paper-api.alpaca.markets`, `api.alpaca.markets`, `/v2/orders`, `/v2/account`, `/v2/positions`, `trade_updates`) appears in the backend. Use a paper-account pair.
- `paper_trades` are simulated positions (`mode='PAPER'`) computed from stored ticks.
- Decisions are research output, not orders.

## 8. Notification channels

In-app alerts always work. External channels report `NOT CONFIGURED` and never send without credentials. Delivery records (`notification_deliveries`) store only status, attempt count and error type. Per-channel minimum priority and hourly caps live in `config/rules.d/alerts.json`.

| Channel | Credentials | Handling |
|---|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN` (`SecretStr`), `TELEGRAM_CHAT_ID` | The token is part of the request URL. Errors record only the exception type, `follow_redirects=False`, and log redaction masks `/bot<token>`. |
| SMTP | `SMTP_USERNAME`, `SMTP_PASSWORD` (`SecretStr`) | STARTTLS with the default SSL context (`SMTP_STARTTLS=true`, the default) or implicit TLS (`SMTP_SSL=true`). A login with both false is refused unless the server is localhost: the channel never attempts it (non-retryable `SMTP login refused: no SSL/TLS or STARTTLS`), and a UI save that would create it returns 422. Authentication failures are not retried. |
| Web Push | `WEBPUSH_VAPID_PRIVATE_KEY` (`SecretStr`), public key, `WEBPUSH_SUBJECT` | The public key is served at `GET /api/notifications/push-key` (intended). Subscriptions are registered by an unauthenticated POST (a browser cannot hold the admin token). The endpoint host must be a browser push service (FCM, Mozilla, Apple, WNS), and the number of subscriptions is capped, so the backend cannot be pointed at arbitrary URLs. There is no per-client rate limit. |

## 9. Recommendations for remote deployment (not implemented in the repository)

1. **Proxy and binding.** Keep uvicorn and Next bound to loopback or a private network. Expose them only through a TLS-terminating reverse proxy that authenticates every request (OIDC or SSO, mTLS, or at least basic auth). Put both UI and API behind it, including `/ws`, `/docs`, `/openapi.json` and `/metrics`.
2. **Proxy controls.** At the proxy:
   - restrict `/api/admin/*` and `/api/notifications/push-subscriptions` (IP allowlist or a separate auth realm);
   - add rate limiting (the app has none);
   - add CSP, HSTS and `frame-ancestors` headers.
3. **CORS and API URL.** Set `CORS_ORIGINS` to the exact public UI origin. Rebuild the frontend with `NEXT_PUBLIC_API_URL` set to the public API URL.
4. **Admin mode and token.** Set `ADMIN_AUTH=token` on any instance reached through a proxy or from another machine: a proxy on the same host connects from loopback and may forward a loopback `Host`, which local mode would trust. Use a long random `ADMIN_TOKEN` and rotate it by changing the environment and restarting. Leave it empty on instances that do not need admin writes; admin routes then return 503.
5. **Redis and Postgres.** Give Redis a password or ACL (`redis://:pass@host`) whenever it is not on an isolated network. Never publish Postgres or Redis ports. Use TLS (`sslmode=require`) for managed databases.
6. **Secret storage.** Prefer the orchestrator's secret store over `.env` files, and restrict file permissions on any `.env`.
7. **Backups.** Treat database backups as sensitive: they contain audit logs, raw documents, push subscription endpoints and AI run inputs and outputs.
