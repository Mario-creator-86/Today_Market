# Implementation plan and progress

Goal: implement masterprompt.md with observable, fail-closed market intelligence.
Architecture: modular FastAPI services, deterministic engines, SQLAlchemy persistence, replaceable adapters, Next.js UI.

| Phase | Implemented and checked | Remaining |
|---|---|---|
| 1 | FastAPI, Next.js, configuration, SQLAlchemy, SQLite migration, health; local startup passes | Docker/production extensions cannot run on this host |
| 2 | Asset registry, BTC/ETH WebSocket and REST fallback, freshness, one-second UI snapshots | Full paid cross-asset streaming |
| 3 | Fed/ECB/BLS RSS, raw documents, bounded search, X/FMP/SEC/professional adapters | Paid authenticated probes, native licensed wire integrations |
| 4 | Normalization, exact dedupe, provenance, independent confidence, contradictions | Semantic embeddings and broader clustering |
| 5 | 17 live Ollama models/metadata, all ten role assignments, schema correction, fallback, audit logs; real integration PASS | Broader model-specific evaluation and specialist orchestration |
| 6 | Calendar API/import, macro z-score, relative-policy guards | Full central-bank state and release watch |
| 7 | Versioned causal priors, measured reaction windows, observed levels, anomaly helpers; configurable multi-regime engine (fresh quotes only, confirmed-event support, BTC correlation features, DEFENSIVE/CONSTRUCTIVE/MIXED posture, rule-coverage confidence) | Historical technical windows; regime inputs beyond BTC/ETH need entitled feeds; regime not yet a scoring component |
| 8 | Deterministic weights/gates, snapshots/hashes, read-time invalidation and final audit | Populate currently unavailable macro/positioning/cross-asset components |
| 9 | All core pages, chart, providers, settings, safe empty states; browser/build checks | Complete editable settings, full shadcn component set |
| 10 | Chief/auditor integration; deterministic morning brief (once per Europe/Bucharest day, DST-safe, all spec sections, empty/filler omitted) and intraday updates on new confirmed events, macro releases, regime change, score change > threshold and level breaks | LLM narrative for briefs; push delivery of briefs |
| 11 | In-app anomaly/setup alerts, browser permission control, X discovery flow | Filtered stream and full corroboration automation |
| 12 | Causal 5m/15m/1h/4h outcomes, paper spread/slippage, MFE/MAE | Session-close/next-session outcomes and fee-aware portfolios |
| 13 | Purged calibration training, OOS metrics/risk/sample gates, shadow candidates, promotion/rollback API | Sufficient real observations, effective-sample validation and probability UI |
| 14 | Backend/frontend checks, actual browser screenshots, setup/security/architecture documentation | Production infrastructure and licensed-provider load validation |

Verification per stage: pytest, Ruff, Python type checks; frontend TypeScript/Vitest/build; application startup. Record unsupported integration checks explicitly.

## Initial audit
Only masterprompt.md and an untouched PyCharm sample main.py existed. Python 3.14 and Node 24 are available. Docker executable is absent. No existing application or credentials were found. Original sample is safe to replace with an application launcher.

## Execution checkpoints

- Foundation: health/no-fabrication test passed, app started.
- Deterministic engines: 13 tests passed (sessions, holidays, DST, confidence, scoring, surprise, dedupe).
- Providers and AI: 20 tests passed; missing credentials, redaction, schema retry/failure covered.
- Persistent pipeline: 25 tests passed; quote ordering, future times, official-domain spoofing and WAIT gates checked.
- Outcomes/integration: 36 then 59 tests passed as API and analytical coverage expanded.
- Safety review fixes: manual import provenance isolated from trusted retrieval; publication expiry/evidence invalidation; actual reaction required; final content audited; browser cache expires independently.
- Ollama live diagnostic initially found schema failure; corrected retry context, tested model-specific 404 fallback and public catalog discovery. Retest PASS; credentials remain local only. User-supplied key enabled authenticated inference, extraction, planning and search. Primary/backup selected from real successful probes and recorded in audit_logs.
- Local browser visited all primary routes with live backend. Screenshots in artifacts/. Docker is still unavailable.

See docs/TECHNICAL_DEBT.md for unimplemented portions; these phases are not falsely marked production-complete.

## Session 2026-09-26: regime engine and briefs

- Added `backend/regime.py` and `backend/briefs.py`; thresholds live in `config/rules.json` (`regime`, `brief`; rules version 1.1.0).
- API: `/api/regime`, `/api/brief?kind=MORNING|INTRADAY`, `/api/briefs`. Dashboard shows posture and rule coverage (labelled not a probability) and lets the reader choose morning brief or latest update.
- Live check against real Binance data: regime correctly reports INSUFFICIENT_DATA (2 of 31 assets fresh, 8.3% input coverage); morning brief generated for 2026-09-26.
- Found and fixed: event sources are stored as display names, so topic sections now resolve source ids to names.
- Verification: 81 backend tests, Ruff and mypy (22 files) clean; TypeScript, 7 Vitest, 8 Playwright tests and production build pass.

## Session 2026-09-26: one-command local start

- `python main.py` works from any interpreter/folder: re-launches in `.venv`, starts API + UI, reuses running servers, opens the browser; Ctrl+C stops both. `launch.py` is an alias.
- Found while verifying: Deribit option rows were committed one by one on the event loop (~14 s blocked per 15 s cycle) → `Store.put_many` (0.1 s). An httpx/anyio request could absorb task cancellation, hanging shutdown forever → `raise_if_cancelling()` in loops and a bounded `Runtime.stop()`.
- Verified: real Ctrl+C harness (UI + API stopped, exit 0, no listeners), 12/12 immediate shutdowns in 1–2 s, 85 backend tests, Ruff and mypy clean.

## Latest verification

- Backend: full run of 68 tests passed; subsequent authenticated Ollama probe regression also checked. Ruff clean; mypy clean across 20 backend files.
- Frontend: 7 unit tests and 7 browser tests passed; TypeScript and production build passed.
- All nine core browser routes, including event evidence detail, visited against the live API; no browser console errors observed.
- Warnings: Starlette's test client reports an httpx migration deprecation; Vitest reports a future config-loader change. Neither prevented tests/build.

## Session 2026-09-26/27: masterprompt completion (in progress, paused at usage limit)

Done: 164-gap audit; wave 1 modules (causal graph, 7 scoring components, reactions/anomalies/technicals/positioning, sessions, autolearn/backtest, alerts/notifications, telemetry/metrics/streams, refresh cadences, AI prompt registry + 12-check audit + injection screening, dedupe, macro history, provider registry/stubs); integration into service/decision/runtime/api/api_admin; frontend split + shadcn/ui + ESLint (30 Playwright tests); vetted new sources (CFTC COT, Binance/INTX futures, ECB/Eurostat/BLS/Treasury Fiscal/EIA/NY Fed, ECB/BoE/BoJ calendars, GDELT/OFAC/SEC FTS, GOV.UK/EC feeds; keyed FRED/Finnhub); Yahoo Finance rejected (ToS). Fixed: Windows Proactor accept failure (selector loop), browser-disconnect traceback, per-row commits, swallowed cancellation.
Last full suite before final wiring: 378 passed; live smoke of every runtime loop passed.
Next: full test run after last wiring + tests for new runtime loops; live UI check against the new API and screenshots; docs (DATA_SOURCES keys, ARCHITECTURE/DB diagrams, TECHNICAL_DEBT, ERROR_MEMORY WinError 64, .env.example new keys); final §60 report.

## Session 2026-09-27: completion and live verification

- Live walk of every core page against the real verification API (port 8010) with a production UI build (3100). Found and fixed:
  - recalculation churn: level-state break margin shared with alerts, and STALE events no longer trigger;
  - alert noise from sub-margin "breaks";
  - duplicate option-strike levels;
  - FX "other bank" and differentials;
  - the calendar page horizon;
  - event ordering;
  - dashboard latency (SQLite WAL, one source context per request);
  - an entity extractor that treated sentence-initial words ("As", "The") as names;
  - fact confidence shown as "0 %" for STALE events (now "not current");
  - PENDING providers flooding the compact provider panel;
  - opaque feed names (`bls-1` → `bls-empsit`);
  - a causal-graph cache that could pin stale configuration.
- `playwright.config.ts` honours `PLAYWRIGHT_BASE_URL` to test an already running build.
- Docs rewritten: ARCHITECTURE (system and cycle diagrams), DATABASE (ER diagram), DATA_SOURCES, DECISION_ENGINE, AI_AGENTS, AUTOLEARN, SECURITY, TECHNICAL_DEBT, VERIFICATION, README (credentials table, Docker steps).
- Screenshots of all 13 routes (desktop, plus 3 on phone width): `artifacts/screenshots/`, with `report.json` (HTTP status, heading, console errors, overflow).
- The documentation review (agents reading the code while writing the docs) surfaced further defects, all fixed with tests:
  - the probability audit and its thresholds;
  - evidence-age windows in the audit;
  - macro-only sourcing;
  - CROSS_ASSET_DIVERGENCE wiring;
  - fact-checker dispute gate;
  - search budget without a planner;
  - in-process daily calibration;
  - per-cycle configuration recording;
  - push-subscription host allowlist and cap;
  - audit entries for every admin write;
  - typed, atomic config edits;
  - request-size limit;
  - SQLite foreign keys;
  - removal of unused settings;
  - Finnhub calls limited to tracked symbols.
- Final verification:
  - backend: 434 tests, ruff and mypy clean;
  - frontend: tsc, eslint, 15 Vitest, 30 Playwright, production build;
  - `frontend/scripts/screenshots.mjs`: 16 captures, all HTTP 200, 0 console errors, no overflow.

Remaining work: see docs/TECHNICAL_DEBT.md (Docker run, keyed providers, commodity/DXY quotes, alert channels, AI role assignment on fresh databases).

## Session 2026-09-27: providers from the UI, market coverage, local admin

- Providers from the UI (Settings → Data providers, [docs/PROVIDER_SETTINGS.md](docs/PROVIDER_SETTINGS.md)): 18 groups (new `alpaca`), enter/replace/remove keys, enable/disable, connection tests, hot apply without restart. Hardening: a changed destination (Ollama/OpenAI/Reuters/AP base URL, SMTP host/port) needs its secret re-entered (422 otherwise); no SMTP login without SSL/TLS or STARTTLS except to localhost; `.env` writer pins python-dotenv `>=1.2.3`, reads back every key, `0600` working/new file.
- Market coverage ([docs/MARKET_COVERAGE.md](docs/MARKET_COVERAGE.md)): every tile carries source, as-of and basis.
  - New sources: Alpaca market data (IEX snapshots, ETF proxies, equity fallback, daily bars as `Bar` 86400 at NY session dates, Benzinga news as source `benzinga`, tier 4); EIA daily spot WTI/Brent (keyless); PAXG gold proxy (Binance stream + REST, Coinbase fallback) in `proxy_quotes` (migration 0004); DXY replica from the ECB fix (`ecb-derived`); FRED closes for SP500/NASDAQ/VIX/WTI/BRENT with corrected attributions and the FRED API notice (`dashboard.notices`); Finnhub ETF proxies stored.
  - Treasury previous close/`change_bp` on its own hourly loop; ECB previous fix (+ SEK for the replica).
  - `config/rules.d/coverage.json` (level sources and proxies per asset); coverage status per asset (OK, DAILY_ONLY, PROXY_ONLY, NOT_CONFIGURED, NO_DATA_YET, DISABLED) with `missing_keys`; `close_status` CURRENT/STALE from `market.json` `daily_closes` schedules.
  - Regime last-close basis (`basis`, `basis_label`, `missing_required`, INSUFFICIENT_DATA instead of a false MIXED); `live_count` counts only live, fresh features.
  - Ingestion fills missing provenance on an identical re-published tick; events carry `topic_basis` (`rule` / `entities`).
- Review fixes: a stored key could follow a new destination; python-dotenv < 1.2.3 corrupted backslashes; the last-close basis never activated (daily credit/stale correlations counted as live); Treasury XML `ParseError` escaped the error tuple; EIA parser vs nested stub tables. See ERROR_MEMORY.md.
- Passwordless local administration: `ADMIN_AUTH=local` (default) needs no password on this computer (loopback client and Host, allowed Origin, `X-Local-Admin` header); `ADMIN_AUTH=token` for Docker (`api` service). `/api/settings` returns `admin_mode`; the Settings page hides the token field in local mode (tests/test_local_admin.py).
- Results: backend 635 passed, ruff and mypy clean (53 source files); frontend tsc and eslint clean, 69 Vitest, 44 Playwright, production build ok. Live check on real data (Sunday 27 Sep 2026): every tile has data (US2Y 4.81 −6 bp, EURUSD 1.1403 +0.32 %, DXY replica 100.984 −0.31 %, WTI 96.41 / Brent 114.89 EIA 22 Sep, GOLD via PAXG, S&P 500 7743.41 +0.51 %, Nasdaq 27068.72 +0.48 %, VIX 14.21 STALE, equities from Finnhub); regime CONSTRUCTIVE · RISK_ON on the last-close basis with no missing inputs ([docs/VERIFICATION.md](docs/VERIFICATION.md)).

## Session 2026-09-27: event-aware dashboard (`imbunatatire.md`)

- Contract: [docs/EVENT_AWARENESS.md](docs/EVENT_AWARENESS.md) (binding shapes in §8). Engine `backend/event_risk.py` (config `event_exposure.json`, `event_risk.json`): event objects with phases (press-conference sub-events, session-anchored date-only rows), relevance / dominance / per-asset event risk, EVENT RISK MODE with the pre-event confidence penalty, favored / pressured / not-confirming lists, scenarios, post-event reactions and MARKET DIVERGENCE, event-aware trade watch (WAIT FOR <EVENT>, CONDITIONAL LONG/SHORT), countdown and weekend logic, unverified X events isolated. Decisions never read the overlay.
- New calendars: Fed `calendar.json`, BEA ICS/JSON, Census, BLS ICS (FRED release dates as fallback), jobless-claims rule, UMich/Conference Board, Treasury refunding + tentative auctions, Finnhub earnings ("Finnhub estimate"). Consensus shows NO CONSENSUS and market pricing UNKNOWN; nothing is invented.
- AI role `market_interpreter` (`backend/interpretation.py`, 11 roles): strict schema, validators (numbers, ids, trade claims, unverified qualifiers, plain language), compacted input, labelled deterministic fallback, per-role timeouts (`ai_runtime.json`) and no circuit trip on a slow generation.
- UI: next market-moving event with countdown, market view, what can break this view, AI interpretation with scenarios, trade watch, risk-mode banner, asset event-risk panel.
- Performance (live profile of the running app): dashboard median 1.5 s → 0.3 s, worst 8.5 s → 1.8 s (`rules_view()`, shared TLS context, SQL-side tick fields, correlation-bar reuse, memoised reaction windows); backend suite 227 s → 74 s.
- Live fixes: Treasury refunding page spelling, BEA fallback circuit, Vice Chair official-evidence classes, deterministic comments no longer repeat the market-view facts.
- Results: backend 764 passed, ruff and mypy clean; frontend tsc/eslint clean, 120 Vitest, 73 Playwright, production build; 16 screenshots, all 200, 0 console errors, no overflow ([docs/VERIFICATION.md](docs/VERIFICATION.md)).

Open points: docs/TECHNICAL_DEBT.md (market pricing has no free source; Ollama cloud quota; UMich dates after 2026-12-18; interpretation not in Celery).

## Session 2026-09-28: full Playwright audit of the UI and fixes

- Audit (Playwright, every route at desktop and phone width, real data on a copy of the owner's database): 117 confirmed findings plus 12 found by the verifiers; 10 reports were expected behaviour.
- Critical, fixed first: every full analysis cycle crashed with a TypeError (a reaction basket's `expected` count was read as a symbol map), so decisions, setups and briefs had not been recalculated since 02:10. `analytics` baskets now carry `expected_symbols`, `decision.basket_symbols` reads old and new baskets, and one failing asset no longer aborts the cycle.
- Three fix rounds (eight owners on disjoint files, each change reviewed independently), then a live re-audit: 119 of 129 fixed at once, the remaining confirmed items fixed in round 3. Highlights:
  - analysis status in the header (`analysis`: last success/attempt/error, stale), `last_analysis` never advanced by the morning brief;
  - one impact scale (calendar impact) with relevance shown separately; event importance = source tier x extractor importance;
  - price bases per value (live 24h, 24h via PAXG, last close <date>), stale closes marked in WHY NOW;
  - asset pages: daily bars and daily overlays when no minute bars exist (US10Y, DXY, indices, BTC/ETH daily closes from hourly bars), chart kept across polls, positioning, drivers, decision time with the dashboard's stale rule, unknown symbols as a proper not-found;
  - events/sources/alerts: up to 500 rows with paging, publication order, plain labels (no ids, enum tokens, raw ISO times or HTML), source homepages and delivering-provider status, legacy alerts made readable at read time;
  - FX next rate decision per bank; calendar NO CONSENSUS, date windows, earnings estimates;
  - shell: not-found pages, titles, error texts by cause (not found / service error / unreachable), self-recovery after an outage, phone navigation, settings without jargon;
  - traffic: gzip, WebSocket full snapshot at most every 30 s with small price updates in between (about 3.5x less), asset pages reuse minute bars briefly.
- Results: backend 917 passed, ruff and mypy clean; frontend tsc and eslint clean, 275 Vitest, 154 Playwright, production build; live check during US market hours (docs/VERIFICATION.md).

Open points: docs/TECHNICAL_DEBT.md.

## Session 2026-09-30: guidance logic review and corrections

- Review and reproducible fixes: completed-bar anchored breakout levels; same-event reaction scoring; neutral absent basket confirmation; live-only reaction/anomaly/outcome observations; lean expiry and invalidation; AI reuse bound to input semantics and configuration; final revalidation after the auditor; coherent conditional and stale guidance in the UI.
- Fixed duplicate refused-token requests and isolated workspace browser tests from the real backend WebSocket.
- Verification: 1,053 backend tests, 299 frontend unit tests, 168 browser tests; changed-file Ruff, mypy, TypeScript and ESLint checks passed. Browser loading timeouts in earlier runs are documented, not hidden.
- Read-only runtime check: dashboard and BTC detail returned HTTP 200, recent analysis and the completed-minute level reference. No historical database rewrite or historical profitability certification.
- Full findings, changed files, reproduction commands and limits: [docs/LOGIC_AUDIT_2026-09-30.md](docs/LOGIC_AUDIT_2026-09-30.md).

## Session 2026-10-01: simple opening dashboard and guidance continuity

- Read `masterprompt.md` and `imbunatatire.md` completely. Prioritized the opening view: market conclusion and reasons, the next catalyst in Bucharest time, favored/pressured/unconfirmed assets, what can change the view, and up to five watch cards with confirmation/invalidation. Existing secondary context is collapsed; no new market service or signal was added.
- A real-page review found editorially important headlines without established tracked-market impact in “What matters now”. The primary list now requires a confirmed fact with mapped asset impact and excludes explicitly zero/cancelled causal effects; other news stays accessible in the full stream.
- Fixed publication continuity for every directional candidate, including non-AI leans, and bound analyst/auditor explanations to the actual inputs after awaited calls. A later quote cannot retain prose about a different price basis.
- Asset detail immediately adopts published downgrades even if a detail reload fails or the decision ID is unchanged. Stale/blocked guidance cannot be revived by a quote-only update.
- Cached interpretation expires on the browser clock using the backend's configured deadline. A market session being open no longer implies current observations are available. Historical and missing-data warnings remain visible.
- Baseline at continuation was 1,064 backend and 315 frontend unit tests, including saved guidance-continuity work after the prior documented checkpoint. Final verification: **1,090 backend, 348 frontend unit and 180 browser tests passed**; full Ruff, mypy (58 backend files), TypeScript, ESLint and production build passed.
- Tested 41 API reads on a temporary copy of the existing database, including all 31 asset details. Restarted the identified local API to load the corrections and verified the real dashboard in the Codex browser. API/UI remain available at ports 8000/3000; the temporary production test UI on 4178 is stopped after verification.
- The observed dashboard reports historical `MIXED` and insufficient current confirmation, with explicit NO CONSENSUS/UNKNOWN event fields. Massive reported HTTP 403 in existing alerts; provider subscriptions and historical profitability are not certified by these checks.
- Findings, commands, changed files, screenshots and limits: [docs/DASHBOARD_SIMPLIFICATION_2026-10-01.md](docs/DASHBOARD_SIMPLIFICATION_2026-10-01.md).
