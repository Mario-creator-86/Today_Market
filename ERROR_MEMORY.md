# Error memory

Regression rules: reject article prices at quote ingestion; reject stale/future ticks; independently recompute freshness; rumors cannot establish confirmed facts; absent audit cannot pass; preserve ingestion time in historical evaluation.

## The research planner re-planned the same events every minute
Error: the Ollama cloud usage quota ran out long before the end of the day. `ai_runs` held 1914 runs in two days: research planner 1466 (77 %, only 411 successful), market interpreter 224 (5 successful), extractor 196; 104 calls in the first startup minute.
Root cause: `Runtime.research_hot_topics` ran every 60 s and called `corroborate()` for every unresolved important event of the latest 50, guarded only by the search budget, whose topic cooldown for priorities 0/1 was 60 s and hourly allowance 60, so the same events were planned again every minute. The interpreter ran every 300 s and every failure (timeouts, 429) ran again at the next tick; a failed extraction was retried on every feed poll. The LLM limiter smoothed the bursts but never lowered the volume, and nothing counted requests per day.
Prevention: a daily AI request budget checked before every chat request (`backend/ai_budget.py`: 400 a day, per-role caps, a 25 % reserve for the interpretation/chief analyst/auditor, a 2-hour pace window, counts read from `ai_runs` so restarts keep them); an event is researched at most twice (6 h apart unless new evidence arrived) and an anomaly once per 6 h, with the attempts stored; hot topics every 900 s; the P0/P1 topic cooldown is at least 6 h; interpretation every 1800 s (3600 s idle), triggers debounced by 600 s from the startup run on; each page extracted once and only when worth an AI read; one 429 retry instead of three. `GET /api/ai/usage` and Settings → AI models show the day's use. Tests: `tests/test_ai_budget.py`, `tests/test_ai_volume.py`.

## The first daily budget would have switched the AI off on the upgrade day, and ran out by mid-afternoon
Error (found in review, before release): the budget counted every older `ai_runs` row (no `requests` field) as one request. The upgrade day already held 1432 such rows (212 in the last 2 hours), so every role, the reserve roles and the Settings test included, would have been refused until Bucharest midnight. A day simulation with the shipped caps also ran the planner, extraction and interpreter caps out between 11:00 and 16:00, with about 150 of the 400 requests unused; the interpreter's regular cadence alone was 48 runs against a cap of 48; a read timeout re-sent the whole prompt uncounted (2 HTTP attempts, plus the limiter retry); the chief analyst and the auditor ran on every analysis cycle of an unchanged setup; a pace refusal kept a page away for the whole day and the next day started on the backlog.
Root cause: counts assumed every stored row came from the budget-aware code; the spread rule limited only the total; `requests` counted admitted attempts, not HTTP sends.
Prevention: only rows with a `requests` field count (older rows appear in the history only); `spread.role_pace` spreads every role's cap over the day; `requests` counts the HTTP requests actually sent (`SendMeter` in `HTTPProvider.request`) and `chat_attempts.default` is 1; the interpreter keeps an unchanged AI basis for 3 h (cap 72); `decision.ai_reuse_seconds` reuses the setup explanation and audit; a pace-refused page waits `burst_hours`, a day-refused page older than `max_page_age_hours` is dropped; the extractor gate is stricter after half its cap. Tests: `tests/test_ai_budget.py`, `tests/test_ai_volume.py`, and the config-writing tests use a private copy of `config/` (`config_copy`).

## Model-specific failure disabled fallback
Error: a missing primary model returned 404, then the backup was blocked by the provider circuit.
Root cause: all HTTP failures were treated as provider-wide outages.
Prevention: non-authentication 4xx errors do not open the provider circuit; regression verifies the exact primary/backup call sequence.

## Catalog discovery required an unnecessary secret
Error: public Ollama model discovery was blocked without an inference API key.
Root cause: shared authentication gate for public catalog and private inference.
Prevention: independent public catalog transport and private inference health; malformed catalogs never retire models.

## Imported provenance could contaminate retrieved evidence
Error: an identical manual document could be upgraded under its user-selected event linkage.
Root cause: fingerprints did not separate manual and provider provenance.
Prevention: include provenance/source in fingerprints; trusted retrieval cannot inherit manual event linkage. Manual imports remain discovery-only.

## Cached actionable cards outlived evidence
Error: a fresh quote could revive an old setup; browser caches could retain FRESH during outages.
Root cause: freshness checks were limited to initial publication/server snapshots.
Prevention: decision TTL, current evidence/invalidation checks and independent browser clock expiry.

## Cloud JSON is not guaranteed
Error: a real research planner emitted non-JSON and failed validation.
Root cause: Cloud lacks server-enforced schema mode; a generic correction lacked previous output/field errors.
Prevention: strict local Pydantic validation, one correction with precise validation feedback, then validated backup or failure. Live retest exercised successful correction.

## `python main.py` failed outside the project environment
Error: `ModuleNotFoundError: exchange_calendars` when started with the global Python or an IDE default interpreter.
Root cause: dependencies live only in `.venv`; the entry point assumed it was already active. A second `next dev` for the same folder also exits immediately (Next.js per-folder lock), and a UI on another port is rejected by CORS.
Prevention: `main.py` re-launches itself with `.venv`, reuses a running API/UI, detects a dev server holding `.next/dev/lock`, aligns CORS and `NEXT_PUBLIC_API_URL` with the chosen ports, and shuts everything down if the UI dies.

## Event loop blocked ~14 s per derivatives cycle
Error: API, WebSocket snapshots and Ctrl+C were unresponsive for long periods.
Root cause: 1,838 Deribit option rows were saved with one SQLite commit each (7.9 ms) on the event loop, every 15 s.
Prevention: batch writes with `Store.put_many` (one transaction, 0.1 s); never commit per row inside an async loop.

## Shutdown hung forever after an absorbed cancellation
Error: Ctrl+C shortly after startup left the API at "Waiting for application shutdown" indefinitely (intermittent).
Root cause: an httpx/anyio request returned normally although its task was being cancelled; `repeat()` caught nothing and slept again, so `gather()` waited forever.
Prevention: background loops call `raise_if_cancelling()` each cycle, and `Runtime.stop()` waits at most `SHUTDOWN_TIMEOUT_SECONDS` and logs stragglers. Regression tests cover a swallowing operation and a task that ignores cancellation.

## Windows console traceback on browser disconnect
Error: `Exception in callback _ProactorBasePipeTransport._call_connection_lost()` / `ConnectionResetError: [WinError 10054]` after page reloads or closed tabs.
Root cause: CPython's Proactor loop calls `socket.shutdown()` on a connection the browser already reset; the request had completed, only the log was noisy.
Prevention: `install_disconnect_filter()` (installed in the app lifespan) drops only ConnectionReset/Aborted errors raised from `_call_connection_lost`; every other loop error still reaches the default handler (tests/test_loop_errors.py).

## API stopped accepting connections on Windows (WinError 64)
Error: after a burst of browser reloads the API process stayed alive, but every new connection was refused.
Root cause: the Proactor event loop closes the listening socket when `AcceptEx` fails with WinError 64 ("network name no longer available", caused by a client RST during accept). uvicorn never re-opens it.
Prevention: the launcher runs uvicorn on `asyncio:SelectorEventLoop` on Windows (`SERVER_LOOP` in `main.py`). An RST burst regression test (tests/test_server_loop.py) keeps accepting.

## Dashboard took 7–28 s under ingestion load
Error: `/api/dashboard` and `/api/events` timed out in the browser while ingestion loops were running.
Root cause: SQLite in rollback-journal mode serialised readers behind writers, and every event re-read the source registry and overrides.
Prevention: WAL and `synchronous=NORMAL` pragmas on every file-SQLite connection, and one `source_context()` per request instead of per event (0.22 s warm).

## Alert and recalculation noise from ticks at a level
Error: "BTC: break of support" alerts for a 0.006 % move, and a decision recorded almost every minute.
Root cause:
- Level-break alerts and `briefs.level_state` had no margin, so a price hovering at a level flapped between states and fired TECHNICAL_BREAKOUT.
- Backfilled official releases (STALE, importance 1.0 from the source tier) fired BREAKING_EVENT / CENTRAL_BANK_STATEMENT as they were ingested.
Prevention:
- One `notifications.break_margin` (the larger of `min_break_pct` of the level and `min_break_atr` × ATR) is shared by alerts and level states.
- `refresh.detect_triggers` ignores events that arrive STALE or age into STALE.
- Regression tests: test_briefs, test_refresh, test_notifications.

## Causal graph pinned to stale configuration
Error: after a config file changed, `causal.settings()` could keep an old graph indefinitely (found through an order-dependent test failure).
Root cause: the causal cache keyed itself on file mtimes but took its content from `rules()`, which may serve its previous content for up to 1 s. A new stamp was then stored with old content and never refreshed.
Prevention: derived caches key on `rules()['content_hash']`, never on their own file stamps.

## Same option strike listed twice as a level
Error: BTC key levels showed "84,000" twice.
Root cause: `positioning.option_strike_levels` concatenated the per-expiry strike concentrations of the two nearest expiries.
Prevention: one level per strike, with open interest summed across the expiries used and the list of expiries kept.

## App crashed after a restart during an edit
Error: the owner's running app failed with `NameError: name 'features' is not defined` (`backend/service.py`) after it was restarted.
Root cause: it was restarted while an agent was midway through a multi-step edit of `backend/service.py`; the process imported a half-edited file.
Prevention: never restart or serve the app while files are being edited. Finish the change, run the tests, then tell the owner when to restart.

## EIA spot parser saw no rows on the live page
Error: the `eia_spot` parser found no rows on the live page, so WTI/BRENT stayed empty, although the unit test passed.
Root cause: the lazy `<tr>.*?</tr>` row match stopped at the nested `<table><tr>…</tr></table>` inside each stub cell, so the price cells were lost. The test fixture lacked that nesting.
Prevention: rows are segmented by `DataRow` openings and located by the series code in the history link. Fixtures must mirror the live markup (the EIA fixture now nests a table in every stub cell).

## Treasury and ECB changes stayed empty on existing databases
Error: US2Y and EURUSD showed no change although the providers now emit `previous_close` / `change_bp`.
Root cause: the closes were already stored without that provenance; the richer re-publication of the same observation (same time, price and provider) was rejected as a duplicate.
Prevention: an identical re-published observation fills only the fields the stored copy lacks and never changes values (`Ingestion.ingest_quote`; tests/test_pipeline.py).

## Review findings, 2026-09-27
- Error: a stored key could be sent to a new host by editing only the destination field (base URL, SMTP host or port).
  Root cause: the `.env` writer validated each field alone.
  Prevention: `destination_secrets` pairs each destination with its secret; a changed destination must carry that secret in the same request (422 otherwise), and the UI asks to re-enter it.
- Error: python-dotenv below 1.2.3 could corrupt values containing backslashes (and swallow the next line).
  Root cause: the dependency was unpinned and the writer trusted `set_key`.
  Prevention: `python-dotenv>=1.2.3` in `pyproject.toml`, and every edit must read back exactly (all other keys unchanged) before it replaces `.env`.
- Error: the regime last-close basis never activated.
  Root cause: the daily credit spread and correlations of stored bars counted as live features, so the live basis always looked sufficient.
  Prevention: `live_count` counts only live, fresh inputs (tests/test_regime_last_close.py).
- Error: a Treasury answer that was not XML (maintenance or bot page) escaped the loop's handled error tuple.
  Root cause: `ElementTree.ParseError` is a `SyntaxError`, not a `ValueError`.
  Prevention: parser errors are converted to `ValueError` (a refused payload, `DEGRADED`); providers raise only the handled error types.

## Event-aware dashboard, 2026-09-27
- Error: the live dashboard crashed with "decision.reasons is not iterable".
  Root cause: the real payload can carry a decision without a `reasons` list; the UI spread it unconditionally (`lib/freshness.ts`, `setup-card.tsx`).
  Prevention: guard every list read from the API (`Array.isArray`), and test the UI against a real exported payload (`scripts/export_event_fixture.py` → `frontend/tests/fixtures/event-risk.real.json`, kept current by tests/test_event_fixture.py).
- Error: one slow market-interpreter generation (150–200 s) timed out, opened the Ollama circuit and blocked the backup model and every other AI role.
  Root cause: a read timeout of a long LLM generation was treated like a provider outage, and the prompt was 54K characters.
  Prevention: per-role timeouts/attempts (`config/rules.d/ai_runtime.json`); chat requests pass `transport_opens_circuit=False` (DEGRADED, circuit stays closed); interpreter input compacted to ≤ 24,000 characters.
- Error: a heredoc wrote a backspace character (`\x08`) instead of the regex `\b` into `backend/interpretation.py`.
  Root cause: shell/Python quoting of a backslash sequence in a generated edit.
  Prevention: write regex edits with the Edit tool or raw strings, then scan edited files for control characters.
- Error: the dashboard stayed on "Loading market intelligence…" (requests of 2–9 s against a 15 s client timeout under load; spikes to 8.5 s).
  Root cause: `rules()` deep-copied the whole configuration (≈ 1 ms) on every call and per-row code called it ~420 times a second, holding the GIL against request threads; each provider request built a new TLS context; minute bars decoded every tick's JSON payload; reaction capture recomputed exchange sessions per symbol.
  Prevention: per-row and per-tick code reads `rules_view()` (one shared, deeply read-only config; mutation raises `TypeError`); profile the running process (all threads) before guessing, since single-request profiles missed the background contention.
- Error: the Treasury refunding calendar raised `CalendarParseError` on the live page.
  Root cause: the page switched to lower-case month names and "November 4 , 2026"; the regex required `[A-Z][a-z]+ d, yyyy`.
  Prevention: date patterns on scraped official pages are case-insensitive and whitespace-tolerant; the fixture carries the live spelling.
- Error: BEA's JSON fallback was refused after an ICS outage.
  Root cause: both hosts share one provider circuit, which the ICS failure had opened.
  Prevention: the fallback resets the circuit before trying the second host (tests/test_event_calendars.py).
- Error: Vice Chair speeches/testimony could never get a validated surprise.
  Root cause: `prompts.json` `official_evidence.groups_by_class` lacked two classes that `event_risk.json` grades.
  Prevention: a test keeps the two lists aligned.

## Guidance continuity and opening dashboard, 2026-10-01

- Error: a lean could be persisted and streamed after its shared cycle snapshot expired while earlier assets awaited AI.
  Root cause: final publication checks were only inside the setup/AI path.
  Prevention: every directional candidate revalidates the latest eligible live quote, technical confirmation,
  expiry, evidence, invalidation and required providers before publication; regressions in
  `tests/test_publication_continuity.py` cover both persisted and streamed states.
- Error: changed prices could leave a capped technical score unchanged and make an old AI explanation/audit appear valid.
  Root cause: numeric membership accepted an old price present in another field, then cached the later inputs.
  Prevention: compare the original semantic input hash after both AI waits; preserve the analyzed snapshot and
  block publication/cache on drift. Timestamp/age-only refresh controls remain valid.
- Error: an asset detail retained an older actionable decision when the dashboard blocked it and detail reload failed.
  Root cause: quote merging omitted the decision/overlay, and same-id read-time blocks did not advance creation time.
  Prevention: merge current published guidance immediately, retain detail-only charts, prevent same-id resurrection,
  and bound immediate reload retries; unit and browser regressions include a 503 reload and same-id downgrade.
- Error: a cached interpretation remained visually fresh after updates stopped; an open session was described as live data.
  Root cause: the UI trusted the stored stale boolean, and session state was mistaken for observation availability.
  Prevention: backend exposes its configured `stale_after_seconds`; the client compares it against UTC timestamps
  on its clock, including DST boundaries. Session copy states open/closed and current evidence remains separate.
- Error: editorial importance put confirmed but unmapped headlines in the primary “What matters now” list.
  Root cause: ranking ran without requiring an established tracked-asset impact.
  Prevention: require a confirmed fact and an existing nonzero impact; causal effects take precedence over
  generic asset labels, including cancelled/zero effects. Keep all other news in the full stream. Regressions
  cover measured/unmeasured ranking, invalid directions, deduplication and both event-risk layouts.
- Error: a limiter timing assertion intermittently counted client construction as time spent in the queue.
  Prevention: start the timer at the actual limiter entry and retain the original minimum-delay/fast-response bounds.

## Playwright UI audit, 2026-09-28
- Error: no full analysis had run since 02:10; decisions, setups and briefs were hours old while the header looked current.
  Root cause: `analytics.basket_confirmation` stores `expected` as a count, `decision.latest_basket` tested `symbol in basket['expected']` (TypeError once a confirmed event had stored baskets), and one exception aborted the whole cycle. `last_analysis` was also advanced by the morning brief.
  Prevention: baskets carry `expected_symbols`; one asset's failure keeps its previous decision; the cycle records its own status (`analysis` on /api/dashboard, stale warning in the header); regression tests run a cycle with stored legacy baskets.
- Error: parallel fix agents changed shared text (date format, labels) and broke other owners' Playwright specs they could not run.
  Root cause: owners could edit only their own files and Playwright needs a central build.
  Prevention: each round ends with one central build and the full Playwright suite; spec assertions that another owner's text change breaks are handed over explicitly.
- Error: a snapshot "fingerprint" meant to skip unchanged WebSocket pushes changed on every build.
  Root cause: values recomputed on every read (event_risk.generated_at, decaying fact confidence, per-poll provider counters, live prices) were part of it.
  Prevention: time-only keys are excluded, decaying values are compared at displayed precision, live prices travel as small `asset_quotes` frames and the full snapshot is capped at `ui_snapshot_full`.

## Self-inflicted LLM lockout: 8 Ollama 429s became 260 refused calls
Error: in one hour (2026-09-28 13:33-14:44 UTC) 260 of 315 AI runs failed with `ProviderUnavailable`, no HTTP status and 0 s latency; the Ollama provider also showed DEGRADED 'Timed out' although the paid subscription was working.
Root cause: every loop sent its chat calls at once (104 in one startup minute, no concurrency limit anywhere). Ollama cloud queued them (a trivial prompt took 37-119 s to its first byte) and answered 8 of them with HTTP 429; `HTTPProvider.request` opened the provider circuit on each 429 (`max(Retry-After, ...)`), so every other role was refused locally ('circuit open') for minutes. The 60-150 s chat timeouts also expired inside Ollama's own queue.
Prevention: one process-wide priority limiter for chat calls (`backend/llm_limiter.py`, `ai_runtime.limiter`): adaptive concurrency (a 429 halves it and pauses new starts, successes raise it), the interpreter and chief analyst first, the extractor last; a chat 429 never opens the circuit (`rate_limit_opens_circuit=False`) and is retried after the pause; send timeouts of 180-300 s that start only when the request is sent. Tests: `tests/test_llm_limiter.py`.
Review follow-up (same incident class): the lockout had three more ways back in. (1) Ollama `web_search`/`web_fetch` used the same `HTTPProvider` as chat with the default circuit, so one search 429 (runtime.corroborate runs searches right after each planner call) again refused every chat role with 'ollama: circuit open'; they now have their own `ollama_web` client (only a 401/403 is copied to chat, the key is shared). (2) A chat 5xx on the last attempt still opened the circuit, and chat 429s/timeouts had grown `failures`, so one 503 after a series of 429s locked all roles out for 300 s; for chat a 5xx is now DEGRADED for that call only, chat failures other than 401/403 are not counted, and the limiter backs off on 502/503/504 like on 429. (3) Priority without aging or reserved slots could starve the extraction or leave the interpreter behind a full slot of extraction; the queue now has aging, reserved slots and one deadline per call. Tests: `tests/test_llm_limiter.py` (web search 429 then chat, 429s then 503, aging, reserved slot, overall deadline).
