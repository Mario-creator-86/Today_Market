# Verification record

Last run: 2026-09-27, Windows 11, Python 3.14.3, Node 24.14.0.

## Automated tests

| Suite | Command | Result |
|---|---|---|
| Backend unit + integration | `.venv\Scripts\python -m pytest -q` | **917 passed** |
| Lint | `.venv\Scripts\python -m ruff check backend scripts tests migrations` | clean |
| Types | `.venv\Scripts\python -m mypy backend` | clean (56 source files) |
| Frontend types | `npm run typecheck` | clean |
| Frontend lint | `npx eslint .` | clean |
| Frontend unit | `npm test` (Vitest) | **275 passed** |
| Browser e2e | `npm run test:e2e` (Playwright; run with `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3200` against the production build) | **154 passed** |
| Production build | `npm run build` | passes |

Offline tests isolate every credential (`tests/conftest.py`), so no paid key is ever used. Browser tests intercept `/api` with clearly test-only payloads. Event-aware suites: `tests/test_event_risk.py`, `test_event_risk_contract.py`, `test_event_aware_cases.py` (the 10 critical cases of `imbunatatire.md`), `test_event_calendars.py`, `test_event_triggers.py`, `test_interpretation.py`, `test_event_fixture.py` (the real-payload fixture `frontend/tests/fixtures/event-risk.real.json` stays current) and `frontend/tests/event-aware.spec.ts`. Earlier suites include `tests/test_local_admin.py` (passwordless local administration), `tests/test_provider_settings.py` (destination/secret pairing, SMTP TLS rule, `.env` read-back and `0600`), `tests/test_sources_coverage.py` (Alpaca fences, EIA spot parser with nested stub tables, Treasury non-XML answers), `tests/test_coverage_service.py` and `tests/test_regime_last_close.py`.

## Live check on real data (Sunday 27 Sep 2026)

The owner's instance with real keys (Finnhub, FRED) and the keyless sources. Every dashboard tile had data:

| Tile | Value | Source and basis |
|---|---|---|
| US2Y | 4.81 (−6 bp) | Treasury daily close, vs previous business day |
| EURUSD | 1.1403 (+0.32 %) | ECB fix, vs previous fix |
| DXY | 100.984 (−0.31 %) | USD index replica from the ECB fix (`ecb-derived`) |
| WTI / BRENT | 96.41 / 114.89 | EIA daily spot, as of 22 Sep |
| GOLD | PAXG proxy | PAXG gold token (Binance, Coinbase fallback), direction only |
| SP500 | 7743.41 (+0.51 %) | FRED `SP500` close |
| NASDAQ | 27068.72 (+0.48 %) | FRED `NASDAQCOM` close |
| VIX | 14.21 | FRED `VIXCLS`, 22 Sep, `STALE` (FRED lags) |
| Equities | last trade | Finnhub, change vs previous close |

Regime: CONSTRUCTIVE · RISK_ON on the last-close basis, with no missing required inputs.

## Event-aware dashboard and latency (Sunday 27 Sep 2026, evening)

Verification API on a copy of the owner's database (port 8010, ingestion on, real keys) with a production UI build (port 3100).

- Event layer on real data: next market-moving event US personal income & outlays (PCE), Wed 30 Sep 15:30 Bucharest, impact 6.9/10 HIGH, dominance 65/100, market expectation NO CONSENSUS, market pricing UNKNOWN, EVENT DATA INCOMPLETE flagged; most exposed US2Y (very high), SP500/NASDAQ/DXY/US10Y (high). Favored NASDAQ, SP500, BTC, ETH; under pressure DXY, GOLD; traditional markets closed (weekend) so their implications wait for the reopen, crypto live.
- AI interpretation: Ollama cloud answered HTTP 429 (quota), so the dashboard showed the labelled deterministic summary ("Deterministic summary — AI unavailable"); no other role was blocked.
- Latency, 20 samples every ~3 s after warm-up:

| Endpoint | Before (median / p90 / max) | After |
|---|---|---|
| `/api/dashboard` | 1.54 / 7.27 / 8.41 s | **0.30 / 0.63 / 1.79 s** |
| `/api/regime` | 0.93 / 1.64 / 2.39 s | **0.18 / 0.43 / 0.56 s** |
| `/api/event-risk` | 0.83 / 2.35 / 4.30 s | **0.19 / 0.35 / 0.56 s** |

  Causes found with a whole-process profile of the running app (90 s window): `rules()` deep-copied the 164 KB configuration about 38,000 times (≈ 31 s of CPU, holding the GIL against requests); a new TLS context (CA bundle parse) for every provider request; minute bars decoded ~180,000 JSON tick payloads per regime; BTC bars read twice; reaction capture recomputed exchange sessions per symbol. Fixes: `rules_view()` (shared, deeply read-only) for per-row readers, one shared TLS context, payload fields extracted in SQL, one read per correlation leg (display reuse ≤ 15 s), memoised window ends.
- Screenshots `artifacts/screenshots/` (13 routes on desktop, 3 on phone width): all HTTP 200, 0 console errors, no horizontal overflow; the dashboard shows the event layer on both widths.
- Also found live: the Treasury refunding page now writes lower-case months and "November 4 , 2026", so the parser found nothing (`CalendarParseError`); fixed and verified on the live page (financing estimates 2 Nov 15:00 ET, refunding statement 4 Nov 08:30 ET).

## Playwright audit of the whole UI (Monday 28 Sep 2026)

Verification instance (API 8010, production UI 3100) on a copy of the owner's database with real keys.

- Audit: five Playwright testers (dashboard; all 31 asset pages; events, sources, social; calendar, FX, performance, alerts; shell, settings, providers, accessibility, error states), each followed by an independent verifier: 117 confirmed findings + 12 more from the verifiers (full list in the session scratchpad, summarised in PROGRESS.md).
- Re-audit after two fix rounds: 119 FIXED, 9 PARTIAL, 1 not verifiable; 35 items confirmed open, fixed in round 3 and covered by tests (154 Playwright specs).
- Live during US market hours (17:15 Bucharest): full analysis running every cycle (`analysis.stale` false, no failed symbols); equities live (NVDA 231.09 +2.69 %, AAPL 342.23 +0.35 %); indices and yields labelled daily closes; 51/52 providers online; `/api/dashboard` 0.31 s median (84 KB gzip), `/api/assets/NVDA` 0.29 s, `/api/assets/BTC` 0.77 s, `/api/events?limit=500` 0.24 s.
- WebSocket over 65 s on the dashboard: 3 full snapshots, 9 price-only frames; about 2 MB decompressed instead of about 7 MB before.
- Screenshots `artifacts/screenshots/` (13 routes on desktop, 3 on phone): all HTTP 200, 0 console errors, no horizontal overflow.

## Live verification against real sources (earlier run, keyless sources only)

The verification instance is the real application, not a mock:

- API: `python main.py --api-only --api-port 8010` on a separate SQLite database.
- UI: `next start` production build on port 3100.
- Data: public sources, running for several hours.

Observed:

- **Providers:** 40 ONLINE, 10 NOT CONFIGURED (keys absent) and 2 PENDING. The two pending ones are Coinbase (fallback only) and Ollama (no AI role assigned on this fresh database).
- **Market data:**
  - Binance BTC/ETH ticks via WebSocket, FRESH and LIVE;
  - Deribit options and futures;
  - Treasury yields (US2Y 4.81, US10Y 5.17, labelled FRIDAY CLOSE / STALE);
  - ECB reference FX (STALE, daily).
- **Rates:** ECB deposit rate 2.00 %. NY Fed target range 3.75–4.00 % (midpoint 3.875) fills the Fed state without FRED.
- **Calendar:**
  - FOMC, ECB, BoE and BoJ meetings;
  - Treasury auctions (with competitive close times when published);
  - EIA WPSR;
  - crypto expiries.
- **Official releases:** Fed, ECB, BoE, BoJ, BLS (CPI, jobs, PPI, JOLTS), BEA, EIA, Treasury, White House, SEC, CFTC, Congress, HM Treasury, OFSI, European Commission.
- **Discovery:** GDELT, OFAC, SEC full-text search.
- **Recalculation:** after the fixes in this session, no spurious triggers fired over 10+ minutes. Before them, a trigger fired every 30–60 s.

## Browser verification of every core page

`frontend/scripts/screenshots.mjs` (Playwright, dark mode; `UI_URL`/`API_URL` select the instance) visited every route on the live instance. `artifacts/screenshots/report.json` records HTTP status, heading, console errors and horizontal overflow for each page.

| Page | Screenshot | Status | Console errors | Overflow |
|---|---|---|---|---|
| Dashboard | `01-dashboard.png`, `01-dashboard-mobile.png` | 200 | 0 | none |
| Asset BTC | `02-asset-BTC.png`, `02-asset-BTC-mobile.png` | 200 | 0 | none |
| Asset EURUSD | `03-asset-EURUSD.png` | 200 | 0 | none |
| Events | `04-events.png` | 200 | 0 | none |
| Event detail | `05-event-detail.png`, `05-event-detail-mobile.png` | 200 | 0 | none |
| Calendar | `06-calendar.png` | 200 | 0 | none |
| FX | `07-fx.png` | 200 | 0 | none |
| Sources & verification | `08-sources.png` | 200 | 0 | none |
| Provider health | `09-providers.png` | 200 | 0 | none |
| Alerts | `10-alerts.png` | 200 | 0 | none |
| X / social | `11-social.png` | 200 | 0 | none |
| Performance | `12-performance.png` | 200 | 0 | none |
| Settings | `13-settings.png` | 200 | 0 | none |

Mobile captures use a 390 × 844 viewport.

## Issues found by the live walk and fixed

Each fix has a regression test.

- Decision churn:
  - level states had no break margin;
  - backfilled STALE releases fired BREAKING/CENTRAL_BANK triggers.
- Alert noise from sub-margin level "breaks".
- Duplicate option-strike levels.
- FX "other bank" and differentials were wrong.
- The calendar page used the dashboard window.
- Event ordering.
- Dashboard latency (WAL, one source context per request).
- Entity extraction treated sentence-initial words as names.
- STALE events showed "fact confidence 0 %".
- The compact provider panel was flooded by PENDING rows.
- Feed provider names were opaque.
- The causal cache could pin stale configuration.

## Issues found by the live check and code review of 2026-09-27 and fixed

Each fix has a regression test.

- A stored key could be sent to a new host by editing only the destination field: a changed base URL or SMTP host/port now requires the paired secret (422 otherwise).
- python-dotenv below 1.2.3 could corrupt values containing backslashes: the dependency is pinned `>=1.2.3` and every edit must read back exactly before `.env` is replaced.
- An SMTP login could travel in clear text: refused without SSL/TLS or STARTTLS unless the server is localhost.
- The regime last-close basis never activated: the daily credit spread and correlations of stored bars counted as live features; `live_count` now counts only live, fresh inputs.
- A Treasury answer that was not XML raised `ParseError` outside the handled error tuple; it is now a refused payload (`DEGRADED`).
- The EIA spot parser found no rows on the live page (nested stub tables); rows are now segmented by `DataRow`.
- Treasury and ECB changes stayed empty on existing databases: an identical re-published observation now fills its missing provenance.

## Issues found by the documentation review and fixed

Each fix has a regression test.

- The probability audit never saw the published value, and its thresholds disagreed.
- The evidence-age audit ignored per-type windows.
- Macro-only setups could never be sourced.
- CROSS_ASSET_DIVERGENCE never fired.
- The AI fact-checker dispute flag was unused.
- The search budget was spent without a planner model.
- Calibration candidates ran only under Celery.
- Hand-edited configuration was recorded only at startup.
- Push subscriptions accepted any HTTPS host (the backend would POST there); they are now limited to browser push services, with a cap.
- Some admin writes were unaudited.
- Config edits were untyped and non-atomic.
- There was no request-size limit.
- SQLite foreign keys were off.
- Settings fields were never read.
- Finnhub spent calls on unused ETF proxies.

## Not verified here

- **Docker Compose stack:** Docker is not installed on this host.
- **Keyed providers:** Massive, X, FMP, Congress.gov, BLS v2, Reuters/AP and OpenAI were never exercised with a real key. The live check recorded no Alpaca-sourced value. Finnhub and FRED were exercised (live check above).
- **Alert delivery:** Telegram, SMTP and WebPush were never exercised with real credentials.
- **Ollama Cloud on this database:** roles were not assigned. It was verified earlier in the main database (`artifacts/ollama-integration.json`: catalog, model metadata, authenticated chat, strict role schemas, web search).
