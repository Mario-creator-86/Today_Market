# Market coverage: what feeds each tile, and how it is labelled

The hub must show real, correctly labelled information for every asset without fabricating anything. Every value carries its source, its as-of time and its basis:

- **live:** a real-time trade or quote;
- **daily close / fix:** an official end-of-day value (`DAILY_CLOSE`), or one computed by the app from it (`DERIVED_DAILY`);
- **proxy:** a different instrument that tracks the asset, shown under its own name, for direction only.

Decision gates (§24) are unchanged: daily closes and proxies never make a setup actionable. Sources per asset are declared in `config/rules.d/coverage.json` (`coverage.assets` level sources with their keys, `coverage.proxies` in priority order).

## Per-asset sources (in priority order; the first available wins)

| Asset | Direct level (same units as the asset) | Proxy line | Notes |
|---|---|---|---|
| BTC, ETH | Binance WS ticker (live) → Coinbase | — | `change_basis = rolling_24h`, shown as "24h" |
| SP500 | FRED `SP500` (daily close, `FRED_API_KEY`) → Massive (paid) | SPY ETF: Finnhub → Alpaca IEX | FRED quote: "Source: S&P Dow Jones Indices LLC via FRED. Copyright S&P Dow Jones Indices LLC …" (a copyright notice, not a licence). Proxy label: "SPY ETF — proxy for S&P 500 direction" |
| NASDAQ | FRED `NASDAQCOM` (daily, key) → Massive | QQQ ETF (Nasdaq-100): Finnhub → Alpaca | "Source: Nasdaq, Inc. via FRED. Copyright NASDAQ OMX Group, Inc. (FRED: copyrighted, pre-approval required)". Proxy: "QQQ ETF — proxy (Nasdaq-100)" |
| RUSSELL | Massive | IWM ETF: Finnhub → Alpaca | |
| VIX | FRED `VIXCLS` (daily, lags Cboe by days, key) → Massive | — (VIXY is not used: futures-based decay makes it misleading) | "Source: Cboe via FRED (copyright Chicago Board Options Exchange, Inc.)". The Cboe CSV is not used: its terms forbid storing or displaying it without consent |
| DXY | **USD index replica**: the published ICE formula (constant × product of pair^weight, SEK included) applied to the ECB 14:15 CET fix (keyless, provider `ecb-derived`, `DERIVED_DAILY`, `derived: true`) → Massive | UUP ETF (Alpaca) | Label: "USD index replica (ICE DXY formula) — ECB fix, derived by this app"; as-of "ECB 14:15 CET fix, <date> (daily replica, not live; not ICE's index)". Never called "U.S. Dollar Index"/"USDX" (ICE trademarks). The Fed broad index (DTWEXBGS) is never mapped onto DXY |
| US2Y…US30Y | US Treasury daily par yield (keyless) | — | `change_bp` vs the previous business day in the same monthly file (the previous month's file on the first business day) |
| EURUSD… (7 pairs) | ECB reference fix (keyless, daily) → Massive | — | Change vs the previous complete fix (`change_basis: previous_fix`); crosses "derived via EUR" |
| GOLD | Massive (paid) | **PAXG** (Paxos token, 1 PAXG = 1 fine troy oz London Good Delivery gold): Binance WS/REST → Coinbase PAXG-USD → GLD ETF (Alpaca) | Label: "PAXG gold token — proxy for spot gold"; caveat: trades 24/7 and can deviate from spot by a few tenths of a percent. The PAXG timestamp advances only on new trades. Outside CME hours the headline is the 24/7 PAXG price (no in-session snapshot is stored yet; see [TECHNICAL_DEBT.md](TECHNICAL_DEBT.md)) |
| WTI | EIA daily spot table (keyless, published weekly, 1–8 days old) → FRED `DCOILWTICO` (key) → Massive | USO ETF (Alpaca) | Label "WTI spot (Cushing, OK)", as-of "EIA spot, as of <date>"; USO: "USO ETF (IEX) — WTI direction proxy" |
| BRENT | EIA daily spot (keyless) → FRED `DCOILBRENTEU` → Massive | BNO ETF (Alpaca) | Label "Brent spot (Europe)" |
| Equities (10) | Finnhub `/quote` (live) → Alpaca IEX snapshot (labelled "<SYM> (IEX)", `feed: iex`), only for symbols Finnhub could not serve | — | Change vs previous close from the provider |

History for technical levels (previous day/week, SMA20/50) comes from Alpaca daily bars (equities and the ETF proxies) and from stored ticks (crypto). A proxy never defines a level.

## Quote fields (backend `Quote`, API, UI)

`symbol, price, timestamp, provider, latency_ms, bid, ask, volume, change_pct, stub, daily_close`, plus:

| Field | Meaning |
|---|---|
| `previous_close`, `previous_close_at` | reference point for the change |
| `change_bp` | change in basis points, for yields |
| `change_basis` | `previous_close` \| `previous_fix` \| `rolling_24h` \| `provider` |
| `data_class` | `LIVE` \| `DAILY_CLOSE` \| `DERIVED_DAILY` |
| `as_of_label` | human-readable as-of text |
| `label` | display name of the instrument, e.g. "SPY ETF" |
| `attribution` | e.g. "Source: ECB" |
| `derived` | true for values computed by the app (the DXY replica) |
| `feed` | e.g. `iex` |

`service.quote` adds `market_status`, `market_state`, `session`, `freshness_status`, `age_seconds`, `freshness_threshold_seconds`, `delay_label`, and for daily closes `close_status`, `close_date` and `close_expected_date`.

## Freshness: one limit per provider

A LIVE quote is FRESH while its age (the read time minus the tick's timestamp) is within its provider's freshness limit, and the one lookup for that limit is `backend.config.freshness_seconds(provider, default)` (`config/rules.d/market.json` `market_data.freshness`): `HubService.quote`, the decision gates, the pre-publication audit, data quality, reaction baselines, the replay and the stream watchdog all read it, and the quote reports the limit it was judged with as `freshness_threshold_seconds` (the dashboard's top-level `freshness_threshold_seconds` is the stream default; the web client reads the quote's own value first, `lib/freshness.ts`).

| Provider | Limit | Why |
|---|---|---|
| Binance, Coinbase, Massive (one-second streams; any provider not listed) | `QUOTE_STALE_SECONDS` 15 s | A streamed tick arrives within a second; 15 s without one is an outage |
| Finnhub `/quote` (REST, 13 sequential calls per round every 15 s at a fixed rate) | 60 s | The last-trade stamp is already 20.8 s old at the median (p95 31.5 s, p99 36.2 s; 18,117 in-session ticks 28 Sep - 2 Oct 2026) and the next answer comes one poll plus the round later: 31.5 + 15 + 8 = 54.5 s at p95 |
| Alpaca IEX snapshot (one call per round every 15 s) | 30 s | 2.9 s stamp lag + 15 s + 1 s = 19 s typical; 30 s leaves one missed round of margin |

A limit is never applied above the provider's own definition of real-time (`realtime_max_age_seconds`, 120 s for Finnhub and Alpaca): the smaller value wins, so a quote the provider would call delayed is never FRESH here. Measured on the owner's stored ticks, the former single 15 s limit left the equity tiles FRESH 5.0 % of the time the app was polling during the US session (2.1-7.0 % per symbol); the provider limit gives 97.5 % (95.5-98.1 %) on the same ticks, and 881 of the 893 in-session equity decisions of 30 Sep - 2 Oct that carried "Critical data missing or stale" would not.

A tick stamped up to `max_future_seconds` (Settings, 5 s) after the read clock (the exchange's event time, or a tick stored between a cycle taking its time and reading the quote) has `age_seconds` 0 and is FRESH; one stamped further ahead is a clock problem and is never FRESH (its negative age is reported). The web client applies the same 5 s tolerance in its own freshness check (its clock ticks once a second, so a streamed quote is often newer than the last tick it saw).

When the verdict at read time is STALE, a stored as-of or delay label that claims real-time ("real-time (Finnhub /quote last price)") is replaced by `market_data.freshness.stale_label` ("STALE: last price 14:59:25 UTC is older than the 60 s freshness limit") and kept as `as_of_label_at_ingestion`, so no tile says real-time beside a STALE chip. `market_status` is the venue's session state only (LIVE, LAST CLOSE, FRIDAY CLOSE ...) and never says STALE: beside a STALE data chip an open venue still reads MARKET OPEN on the asset page, which is the correct reading (the market is open, the app's last print is old).

Every asset carries:
- `session` (`sessions.session_info`), so a tile without a quote still shows FRIDAY CLOSE and not UNKNOWN;
- `coverage` `{status, providers, missing_keys, disabled}`, so the tile can say which free key would fill it;
- `proxy`: the best proxy, or null.

## Regime (§26)

- **Live basis:** FRESH intraday features only. `live_count` counts only live features: a FRESH quote, a curve of two FRESH quotes, or a correlation whose legs both have FRESH quotes. The daily credit spread and correlations of stored bars without FRESH quotes never make the live basis look sufficient.
- **Last-close basis** (display only: dashboard, `/api/regime`, briefs). Used when the live basis observes fewer than `min_observed_features` (2) and the closes observe more. Inputs: close-to-close changes of `DAILY_CLOSE`/`DERIVED_DAILY` quotes that are `CURRENT` (or at most `max_age_days` 7 old while their market is closed), plus proxy changes allowed by `regime.last_close.proxy_features` (change direction only, never a level threshold such as the VIX level). Live-only features (crypto rolling 24h) are excluded and listed in `excluded`. Correlations are live-only. Decision scoring keeps the live basis.
- `basis_label` is e.g. "last close Fri 25 Sep, markets closed" (dates of the closes used, proxies named).
- When no rule has its required inputs, the result is `INSUFFICIENT_DATA` with `missing_required`, never a false `MIXED`.

## UI (§27)

A tile shows:
- the headline (level or proxy);
- the change with its basis ("−6 bp vs prev close 24 Sep", "+0.7% 24h");
- a market-state badge (LIVE / FRIDAY CLOSE / …);
- a data chip: LIVE, DAILY CLOSE, DERIVED, PROXY, STALE, LAST PRINT, STUB, NOT CONFIGURED (with the key that would fill it), DISABLED, NO DATA YET or NO QUOTE (`dataChip` in `frontend/lib/market-data.ts`);
- an as-of line with the source;
- a proxy line when present.

Official closes that are current are not painted red. STALE stays prominent when a close is genuinely late. The regime banner shows its basis and what is missing. Notices required by a data owner's terms are shown wherever that owner's data appears (`notices`, see below).

## Implementation contract (exact names)

**`Quote` (backend/schemas.py)**

- `provider` Literal: `'binance', 'coinbase', 'massive', 'deribit', 'treasury', 'ecb', 'ecb-derived', 'eia', 'fred', 'finnhub', 'alpaca', 'massive-stub'`.
- Optional fields:
  - `previous_close: float | None (gt 0)`
  - `previous_close_at: AwareDatetime | None`
  - `change_bp: float | None`
  - `change_basis: Literal['previous_close','previous_fix','rolling_24h','provider'] | None`
  - `data_class: Literal['LIVE','DAILY_CLOSE','DERIVED_DAILY'] = 'LIVE'`
  - `as_of_label: str | None`
  - `label: str | None`
  - `attribution: str | None`
  - `derived: bool = False`
  - `feed: str | None` (e.g. `'iex'`)
- Any rejected quote row is logged at warning level (`quote_rejected`) with symbol, provider and reason.
- Ingestion keeps newest-wins per symbol. The same observation (same timestamp, price and provider) re-published with more provenance fills only the fields the stored copy lacks, never values (`Ingestion.ingest_quote` in `backend/ingestion.py`).

**Proxy quotes**

- Stored in the record table `proxy_quotes` (migration `0004`). Key = `<source>:<symbol>`, e.g. `binance:PAXG`, `alpaca:GLD`, `finnhub:SPY`. Only a later observation replaces a stored one.
- Payload (`ProxyQuote` in `backend/providers/models.py`):

  ```
  {symbol, proxy_for, label, caveat, price, change_pct, change_basis, previous_close, timestamp, provider, feed,
   delay_label, attribution, relation: 'direction_only'}
  ```
- Proxies never become ticks of the underlying, a decision input or a gate.
- `config/rules.d/coverage.json` holds `coverage.assets` and `coverage.proxies`:

  ```
  {<ASSET>: [{symbol, source: 'binance'|'coinbase'|'alpaca'|'finnhub', label, caveat, max_age_seconds}]}
  ```

  The list is in priority order. Finnhub's `etf_proxies` must stay consistent with it. `market.json` `proxy_sessions` gives each source's session venue (tokens 24/7 as crypto, ETFs on the NYSE calendar).

**`GET /api/assets` and `/api/dashboard` asset entries** carry:

- `session`: `sessions.session_info(kind, now, symbol)`, with at least `state` (LIVE / FRIDAY CLOSE / LAST CLOSE / …) and `label`.
- `coverage`:

  ```
  {status: 'OK'|'DAILY_ONLY'|'PROXY_ONLY'|'NOT_CONFIGURED'|'NO_DATA_YET'|'DISABLED',
   providers: [ready sources], missing_keys: [env names that would add a source], disabled: [sources switched off]}
  ```

  OK = a live level is shown; DAILY_ONLY = a daily close is shown; PROXY_ONLY = only a proxy; NO_DATA_YET = a source is ready, nothing received; DISABLED = every level source is switched off in Settings and nothing is shown; NOT_CONFIGURED otherwise. A source needing several keys (Alpaca) lists every unset one.
- `proxy`: the first proxy in priority order whose age is within its `max_age_seconds` (`freshness_status: FRESH`), else the latest one marked `STALE`, plus `market_status` from its own session and `relation: 'direction_only'`; or null.

`/api/dashboard` and the asset detail also return `notices: [{id, text, providers}]` for every `source_notices` entry whose providers include the provider of a displayed quote, proxy or regime input (today: the FRED API notice).

**`quote` (as returned by `service.quote`)** additionally carries:

- `close_status`: `'CURRENT'|'STALE'|null` (null for live data or a source without a schedule). Schedules live in `config/rules.d/market.json` `daily_closes.schedules` (`treasury`, `ecb`, `ecb-derived`, `fred`, `eia`, `fred:WTI`, `fred:BRENT`; `finnhub` and `alpaca` as `live_source` session closes). A close is CURRENT when its observation date is on or after the latest business day whose value should already be out (calendar, `publish` time, `lag_days`, `grace_hours`, `extra_holidays`; weekly sources: the latest release).
- `change_pct` / `change_bp` computed vs the previous stored daily close when the provider did not supply them (`change_basis='previous_close'`).
- `freshness_status` keeps its gating meaning: daily closes are never FRESH, and an old close never reports a LIVE market.
- Daily-close ticks are kept `DAILY_CLOSE_RETENTION_DAYS` (400), since each is the previous close of the next one.

**`regime`** (`/api/regime`, `dashboard.regime`) carries:

- `basis`: `'live'|'last_close'`
- `basis_label`: e.g. "last close Fri 25 Sep, markets closed"
- `missing_required`: [feature names]
- `drivers[].input`: e.g. "US2Y −6 bp"
- `drivers[].basis`
- `feature_sources`, and `excluded` on the last-close basis

Posture `INSUFFICIENT_DATA` is used when no rule is evaluable; MIXED is never returned without evaluated rules.

**Daily bars**

- Alpaca daily bars (feed `iex`, split-adjusted) are stored as `Bar(interval_seconds=86400, start=<session date 00:00 America/New_York in UTC>)`, only for sessions that have closed; retention keeps at least 400 days of them.
- `technicals` uses stored daily bars (when present) for previous day, previous week and SMA20/50, keyed by NY session dates, not by UTC midnight.

**Alpaca (`backend/providers/alpaca.py`)**

- Host pinned to `data.alpaca.markets` (the constructor refuses any other base URL).
- Path allowlist: `/v2/stocks/snapshots`, `/v2/stocks/bars`, `/v1beta1/news`.
- GET only; feed `iex` only (a Paper-only account is entitled to IEX only).
- Credentials are sent as headers only.
- Settings: `alpaca_api_key_id`, `alpaca_api_secret_key` (SecretStr), with provider-settings group `alpaca` (category `market`).
- A test fails if a trading host or path (`paper-api.alpaca.markets`, `api.alpaca.markets/v2/orders|account|positions`) appears in the backend.
- News rows become Benzinga documents: source `benzinga` in `sources.json`, tier 4, kind `specialist`, domain `benzinga.com`, headline plus summary only.
- Terms: personal, non-commercial use, no redistribution.
