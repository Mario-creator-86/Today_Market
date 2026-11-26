# Data sources

Reference for every provider built by `build_providers()` in `backend/providers/registry.py`: 60 adapters, plus
`ollama` (always) and `openai` (only with `LLM_PROVIDER=openai`), added in `backend/service.py`. `GET /api/providers`
returns each provider's health plus metrics (circuit state, consecutive failures). Endpoints, limits, labels and terms
below come from `config/rules.d/*.json`. Loop cadences come from `config/rules.d/refresh.json` (`backend/runtime.py`;
Celery beat uses the same values when `CELERY_ENABLED=true` and `REDIS_URL` is set).

## Rules the code enforces

- **Source timestamps are preserved.** Quotes carry the venue trade or event time. Documents carry the feed's
  `published_at`, and feed items without a title, link or date are dropped. Official, positioning and discovery records
  carry `as_of`, `as_of_basis` and `retrieved_at`.
- **Timestamps are stored in UTC.** `backend/ingestion.py` converts every timestamp to UTC. It rejects timestamps more
  than `MAX_FUTURE_SECONDS` (5 s) in the future, unknown symbols, and documents whose URL host is not the registered
  source domain. It skips ticks that are not newer than the stored tick.
- **Failures show up as provider health** (`backend/providers/base.py`, `HTTPProvider`):

  | Status | Meaning |
  |---|---|
  | `PENDING` | Configured, no request completed yet |
  | `ONLINE` | Last request succeeded (`latency_ms`, `last_success`) |
  | `NOT CONFIGURED` | Credential or contact missing; the adapter is never called |
  | `AUTH_ERROR` | HTTP 401/403 |
  | `RATE_LIMITED` | HTTP 429 (honours `Retry-After`, capped at 3600 s) or Binance 418 |
  | `OFFLINE` | Transport error or 5xx after 3 attempts |
  | `DEGRADED` | Other 4xx, payload or layout change, Binance HTTP 451 (`unavailable - restricted location`), Binance WebSocket down with REST fallback active, BLS `REQUEST_NOT_PROCESSED`, LLM chat transport failure (timeout; no circuit) |
  | `DEVELOPMENT STUB` | `PROVIDER_MODE=stub` replacement (see below) |

  After a 401, 403 or 429, or after the last failed attempt (3 by default), the circuit opens for `max(Retry-After, min(300, 2^failures))` s.
  LLM chat calls are the exception for transport failures and HTTP 429: a transport failure sets `DEGRADED` and a 429
  sets `RATE_LIMITED` without opening the circuit (the shared LLM limiter halves its concurrency, pauses and retries);
  they use per-role timeouts and attempts from `config/rules.d/ai_runtime.json` ([AI_AGENTS.md](AI_AGENTS.md)).
  Health errors never include request URLs or response bodies, because some URLs contain keys. `error` is a short
  plain-English cause (`Rate limited (HTTP 429)`, `Access denied (HTTP 403)`, `Not found (HTTP 404)`, `Server error
  (HTTP 503)`, `Timed out`, `Connection failed`, `Network error`; `base.failure_text`); the exception class is kept
  apart in `error_type` for diagnostics and is never shown as the message.
- **Every health row names its provider for people.** Besides the id (`name`), each row carries `label` and `category`
  from `config/rules.d/providers.json` `provider_labels` (e.g. `finnhub` → "Finnhub (US equities)", category `market`,
  the `provider_settings` category of the Settings group that lists the provider, so `bls_release_calendar`, set up
  with the SEC contact, is under `filings` in Settings and on /providers alike). A news feed without an entry is named after its source ("Federal Reserve news
  feed"), a development stub after the adapter it replaces; an unknown id is shown unchanged. PROVIDER alerts use the
  label in the title, plain words for the status (`alerts.json` `provider.status_text`: "rate limited", "access
  rejected", ...), the health `error` (or the status change) as the message, and are deduplicated per provider and
  status, so one provider failing never hides another's alert in the same cooldown window.
- **Delayed and daily-close data are labelled.** A quote is `FRESH` only when it is at most `QUOTE_STALE_SECONDS`
  (15 s) old and not a daily close. Daily closes (`data_class` `DAILY_CLOSE` or `DERIVED_DAILY`) are Treasury yields,
  ECB reference FX, the USD index replica (`ecb-derived`), EIA spot and FRED closes. They are never `FRESH`, carry an
  `as_of_label`, a `change_basis` and a session state such as `LAST CLOSE` or `FRIDAY CLOSE` (`backend/service.py`
  `quote()`, `backend/sessions.py`). `close_status` is `CURRENT` when the observation is the latest one the source's
  publication schedule should have out (`config/rules.d/market.json` `daily_closes.schedules`: `treasury`, `ecb`,
  `ecb-derived`, `fred`, `eia`, `fred:WTI`, `fred:BRENT`, plus `finnhub` and `alpaca` as `live_source` session
  closes), otherwise `STALE`. When a provider gives no change, it is computed vs the previous stored daily close.
  Proxies (ETFs, the PAXG token) are stored in `proxy_quotes`, never as ticks of the asset they stand in for
  ([MARKET_COVERAGE.md](MARKET_COVERAGE.md)). Official records carry `live: false` plus a `delay` and a
  `label` (`LAST CLOSE`, `POLICY RATE`, `OFFICIAL RELEASE`, `T+1` or `WEEKLY`). Positioning records are
  `label: "descriptive"`, `directional_score: 0`. Dates and times inside labels and delays are written for people in
  the source's own zone (`EIA spot, as of Tue 22 Sep`, `weekly, as of Fri 18 Sep`, `last trade Fri 25 Sep 16:00 ET`;
  `base.label_day` / `label_time`), never as ISO timestamps; the machine-readable times stay in `timestamp` / `as_of`. When a provider re-publishes an observation
  already stored (same time, price and provider), ingestion refreshes its presentation (`as_of_label`, `label`,
  `attribution`) and the provider's close classification (`daily_close`, `data_class`), so a stored tick never keeps
  an outdated label such as `real-time` once its session has closed; the values themselves are never changed.
- **Search and discovery results never count as prices or as confirmation.** Ollama `web_search` and `web_fetch` results
  are stored as `research_jobs` with `discovery_only: true`. Only a document fetched from a registered source can
  confirm an event (`Runtime.corroborate`). GDELT candidates are `discovery_only` and `UNCONFIRMED`. Posts from
  unregistered X accounts are discovery only.
- **`PROVIDER_MODE=stub`** replaces `massive`, `x`, `calendar`, `congress_api`, `reuters` and `ap`, and only when their key
  is missing. Replacements are named `<name>-stub`, report status `DEVELOPMENT STUB` and `stub: true`, and read
  `backend/providers/fixtures/stub_data.json`. Stub documents use the `dev-stub` source (Tier 5, discovery only). Stub
  data is never used for scoring, publication, outcomes or calibration. Configured keys always use the real adapter.
- **Vendor rate limits are enforced on the client side** by per-provider guards configured in the JSON files cited below.
  Vendor rows are validated with strict models; invalid rows are dropped and counted in health.

## Market data and quotes

| Provider | Supplies | Endpoint / host | Credential | Cadence | Delay / latency label | Terms / attribution (config) |
|---|---|---|---|---|---|---|
| `binance` | BTC, ETH spot: last, bid/ask, 24 h volume, change % (`rolling_24h`); PAXGUSDT as the GOLD proxy (`proxy_quotes` `binance:PAXG`) | `wss://data-stream.binance.vision` (ticker stream, BTC/ETH + PAXG); REST `https://data-api.binance.vision/api/v3/ticker/24hr` (+ `/api/v3/trades` for the PAXG last trade) | none | WebSocket push (`crypto_stream`); BTC/ETH REST fallback in `quotes` loop (5 s) when the stream is silent > 15 s; PAXG stored at most every 5 s (`proxy_stream`), REST fallback every 30 s (`proxy_rest_fallback`) while the stream shows no PAXG trade | Real-time, event time `E`; status `DEGRADED` while on REST fallback. A PAXG row is written only when the message shows a new trade (last trade id or price changed), so its timestamp advances only on new trades | "Source: Binance" |
| `coinbase` | BTC, ETH USD ticker; PAXG-USD as the second GOLD proxy | `https://api.exchange.coinbase.com/products/{BTC,ETH}-USD/ticker`; `/products/PAXG-USD/ticker` and `/stats` | none | `quotes` loop, only if the Binance REST fallback fails | Real-time (vendor `time`); PAXG change = rolling 24 h from `/stats` | "Source: Coinbase Exchange" |
| `massive` | 10 US equities (COIN MSTR BMNR NVDA MSFT AAPL AMZN META GOOGL TSLA); indices I:SPX, I:NDX, I:RUT, I:VIX → SP500, NASDAQ, RUSSELL, VIX; 7 FX pairs | `https://api.massive.com` (`/v2/snapshot/locale/us/markets/stocks/tickers`, `/v3/snapshot/indices`, `/v1/last_quote/currencies/...`) | `MASSIVE_API_KEY` | `quotes` loop 5 s | Last-trade/update time; no plan-delay label (see `backend/providers/market.py`) | none in config |
| `finnhub` | Same 10 equities (change vs previous close) + SPY/QQQ/IWM ETF proxies (`proxy_quotes` `finnhub:<SYM>`) | `https://finnhub.io/api/v1/quote` | `FINNHUB_API_KEY` | `equity_quotes` 15 s (13 calls/round, guard 60/min) | `real-time` only if last trade ≤ 120 s and NYSE `LIVE`; otherwise `STALE: no trade for N min…` or `LAST CLOSE`/`FRIDAY CLOSE`/`AFTER HOURS`/`PREMARKET` (`daily_close`), with the trade time in New York time (`last trade Fri 25 Sep 16:00 ET`, never an ISO timestamp) | "Source: Finnhub"; free plan personal use only, no redistribution; index levels are premium, so proxies are never mapped onto indices |
| `alpaca` | IEX snapshots: ETF proxies SPY, QQQ, IWM, GLD, UUP, USO, BNO (`proxy_quotes` `alpaca:<SYM>`) and the tracked equities Finnhub could not serve (labelled `<SYM> (IEX)`, `feed: iex`); daily bars (1Day, split-adjusted) for equities and the ETFs; Benzinga news (see News) | `https://data.alpaca.markets` only: `GET /v2/stocks/snapshots`, `/v2/stocks/bars`, `/v1beta1/news`, always `feed=iex` | `ALPACA_API_KEY_ID` + `ALPACA_API_SECRET_KEY` (headers only) | `alpaca_quotes` 15 s (one call); `alpaca_bars` 21600 s (420 days first, then the last 14); guard 150/60 s (Basic plan documents 200/min) | Price = latest IEX trade at its trade time; change vs IEX prior-session close; `real-time IEX (single exchange, not consolidated)` or `STALE`/`LAST CLOSE` with the trade time in ET. An extended-hours (after-hours or premarket) trade is never called real-time or a close, whatever its age: it is replaced by the IEX regular-session close of the latest session that closed before it (the daily bar's close: extended-hours trades never set a daily bar's price), stamped at that session's close and labelled `<state> (IEX regular-session close Fri 25 Sep 16:00 ET)` (while the market is open and IEX has no regular-session trade yet: `IEX regular-session close Fri 25 Sep 16:00 ET (no regular-session IEX trade yet today)`); it is an IEX close, not the listing exchange's official close. Because the close is stamped earlier than the extended-hours trades, those trades are never stored, so none can hide the close. Without a closed daily bar in the snapshot the trade itself is kept as `IEX extended-hours trade Fri 25 Sep 16:59 ET (outside the regular session; not a close)`, never a daily close. Daily bars are stored as `Bar` interval 86400 starting at the New York session date 00:00, only for closed sessions | "Source: Alpaca Market Data (IEX exchange only)"; personal, non-commercial use, no redistribution. IEX only because a Paper-only account is entitled to IEX data only. Never a trading endpoint |
| `treasury_yields` | US2Y, US5Y, US10Y, US30Y par yields, each with the previous business day's close and `change_bp` | `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml` | none | Own `treasury_yields` loop, one attempt per 3600 s (so a slow server never stalls the 5 s `quotes` loop) | Daily close stamped 16:00 America/New_York; `DAILY_CLOSE`, `LAST CLOSE`/`FRIDAY CLOSE`; a non-XML answer is a refused payload (`DEGRADED`) | "Source: U.S. Department of the Treasury"; "Official daily par yield curve … never shown as live" (`providers.json`) |
| `eia_spot` | WTI (RWTC, Cushing) and Brent (RBRTE, Europe) daily spot closes, with the previous published observation | `https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm` (conditional GET, `If-Modified-Since`) | none | `eia_spot` 3600 s (guard 4/h) | Published weekly (Wednesday, through Tuesday), so 1–8 days old; `DAILY_CLOSE` stamped 16:00 ET; `EIA spot, as of <date>`. Rows are located by the series code in the history link and segmented by `DataRow` (stub cells nest tables); an unrecognised layout sets `DEGRADED` | "Source: U.S. Energy Information Administration"; price source Refinitiv (LSEG) per EIA; https://www.eia.gov/about/copyrights_reuse.php |

ECB reference FX fixes and the USD index replica enter as daily-close quotes through `ecb_data`, and FRED closes
(SP500, NASDAQ, VIX, WTI, BRENT) through `fred` (see Rates and official data).

Notes:
- `providers.json` also lists Massive futures (GC, CL, BZ, DX), option underlyings and a WebSocket URL, and
  `cb_calendars.json` lists a Finnhub WebSocket URL. Runtime calls none of these: `MassiveProvider.options()`, `bars()`
  and both stream helpers exist but are not called. Without Massive, GOLD is filled by the PAXG proxy, WTI/BRENT by EIA
  (or FRED) closes and DXY by the ECB-derived replica ([MARKET_COVERAGE.md](MARKET_COVERAGE.md)).
- Proxy sources and priorities come from `config/rules.d/coverage.json` (`coverage.proxies`); Finnhub's `etf_proxies`
  must stay consistent with it.

## Derivatives and positioning

| Provider | Supplies | Endpoint / host | Credential | Cadence | Delay / latency label | Terms / attribution (config) |
|---|---|---|---|---|---|---|
| `deribit` | BTC/ETH option and future book summaries (OI, mark price, mark IV, funding, basis) | `https://www.deribit.com/api/v2/public/get_book_summary_by_currency` | none | `derivatives` 15 s | Snapshot; vendor `creation_timestamp` + `ingested_at` | none in config |
| `binance_futures` | Funding, mark vs index, OI for BTCUSDT/ETHUSDT (USDⓈ-M) and BTCUSD_PERP/ETHUSD_PERP (COIN-M); USDⓈ-M hourly OI history + global long/short account ratio | `https://fapi.binance.com`, `https://dapi.binance.com` (`/premiumIndex`, `/openInterest`, `/futures/data/openInterestHist`, `/futures/data/globalLongShortAccountRatio`) | none | `positioning` 60 s (cache ≥ 30 s; stats only when a new hourly bucket can exist) | `real-time venue perpetual (not spot)`; stats `hourly bucket` | "Binance Futures public market data (… not spot; personal use, not redistributed)"; uses 25 % of 2400 weight/min and 25 % of 1000 stats req/5 min; HTTP 451 → 1 h backoff; COIN-M stats disabled (frozen series seen 2026-09-26) |
| `coinbase_intx` | BTC-PERP, ETH-PERP funding, basis, OI (slow cross-check) | `https://api.international.coinbase.com/api/v1/instruments/{inst}[/funding]` | none | `positioning` 60 s (cache 60 s, cap 20 req/min) | `real-time venue perpetual (not spot); funding settles hourly` | "Source: Coinbase International Exchange public market data"; CDP terms forbid circumventing limits |
| `cftc_cot` | COT Legacy, Disaggregated, TFF (futures-only) for 20 contracts (gold, WTI, index futures, UST, FX, DXY, VIX, BTC, ETH), 156-week history for percentiles | `https://publicreporting.cftc.gov/resource/{6dca-aqww,72hh-3qpy,gpe5-46if}.json` (Socrata) | none | `positioning` 60 s; cached until the next scheduled Friday 15:30 ET release (daily safety refresh, ≥ 1 h between refreshes, cap 30 req/h) | `weekly, as of Tue <date>`, `live: false`, `stale` after 11 days | "Source: CFTC Commitments of Traders (public domain; acknowledgement requested by the CFTC)"; https://www.cftc.gov/WebPolicy/index.htm |

## Rates and official data

All of these run in the `official_data` loop (1800 s). The records are official series, never live prices.

| Provider | Supplies | Endpoint / host | Credential | Cadence | Delay / latency label | Terms / attribution (config) |
|---|---|---|---|---|---|---|
| `ecb_data` | DFR and MRO policy rates; euro-area AAA 2Y/10Y spot (EU2Y, EU10Y); euro-area HICP y/y; ECB reference rates (USD GBP JPY AUD CAD CHF NZD, plus SEK for the replica; last 3 fixes) → EURUSD GBPUSD USDJPY AUDUSD USDCAD USDCHF NZDUSD (crosses via EUR), each with the previous complete fix (`change_basis: previous_fix`); DXY USD index replica (provider `ecb-derived`, `DERIVED_DAILY`): the published ICE formula applied to the same fix, not ICE's index | `https://data-api.ecb.europa.eu/service/data/{FM,YC,HICP,EXR}/…` (`format=csvdata`) | none | 1800 s | `official policy rate, as of effective date` (POLICY RATE); yields `T+1` (LAST CLOSE); HICP `monthly release`; FX `daily fix` (LAST CLOSE, `DAILY_CLOSE`, 14:15 CET fixing) | "Source: ECB"; replica "Computed from ECB reference rates", never called "U.S. Dollar Index"/"USDX" (ICE trademarks); ECB disclaimer page; self-imposed 60/min; ECB asks for polling no more often than every 15 min |
| `eurostat` | Euro-area HICP y/y, flash vs final (`prc_hicp_fpd`) | `https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_fpd` | none | 1800 s | `monthly flash release` / `monthly release` | "Source: Eurostat"; Eurostat copyright notice; self-imposed 30/min |
| `bls_data` | CPI (SA, NSA), core CPI, nonfarm payrolls, unemployment rate, PPI final demand (one batched query) | `POST https://api.bls.gov/publicAPI/v2/timeseries/data/` | optional `BLS_API_KEY` | 1800 s loop, BLS call at most every 2 h (`BLS_MIN_SECONDS`) | `monthly release (08:30 ET)` | "Source: U.S. Bureau of Labor Statistics"; https://www.bls.gov/developers/termsOfService.htm; guard 20/day unregistered, 450/day registered |
| `treasury_fiscal` | TGA opening/closing balance, deposits, withdrawals (Daily Treasury Statement) | `https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance` | none | 1800 s | `T+1` | "Source: U.S. Treasury, Daily Treasury Statement (Fiscal Data)"; self-imposed 60/min |
| `eia_wpsr` | WPSR table 1: commercial crude, SPR, gasoline, distillate stocks | `https://ir.eia.gov/wpsr/table1.csv` (redirects allowed only to `ir.eia.gov`) | none | 1800 s | `weekly, as of <Fri date>` (WEEKLY) | "Source: U.S. Energy Information Administration (EIA)"; https://www.eia.gov/about/copyrights_reuse.php; 12/h |
| `fred` | 17 series: DGS2, DGS10, T10Y2Y, BAA10Y, VIXCLS, SP500, NASDAQCOM, DTWEXBGS, DCOILWTICO, DCOILBRENTEU, CPIAUCSL, PAYEMS, UNRATE, WALCL, RRPONTSYD, DFEDTARU, DFEDTARL; ALFRED vintages and first releases; BAA10Y credit spread; Fed target range. Daily-close quotes (`fred.quote_symbols`): SP500 → SP500, NASDAQCOM → NASDAQ, VIXCLS → VIX, DCOILWTICO → WTI, DCOILBRENTEU → BRENT | `https://api.stlouisfed.org/fred/series/observations` | `FRED_API_KEY` | 1800 s (only when configured) | Per series: `EOD`, `weekly (H.10)`, `EOD, refreshed weekly`, `monthly release`, `weekly, as of Wed`, `official policy rate`; quotes `FRED <series>, close <date> (daily; FRED lags its source)` stamped 16:00 ET (VIXCLS often lags several days → `STALE`); `credit_spreads/BAA10Y` is `FRESH` ≤ 4 days, otherwise `STALE` | "Source: FRED, Federal Reserve Bank of St. Louis". Quote attributions name the owner and end in "via FRED": S&P DJI and Nasdaq copyright notices on SP500/NASDAQCOM (FRED: copyrighted, pre-approval required; no licence is implied), "Source: Cboe via FRED (copyright Chicago Board Options Exchange, Inc.)", "U.S. Energy Information Administration via FRED". FRED API terms: `source_notices` shows "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis." wherever FRED values appear (`dashboard.notices`, asset detail). DTWEXBGS is never mapped onto DXY. BAA10Y "Moody's via FRED"; guard 100/min (120 documented) |
| `nyfed_rates` | EFFR + FOMC target range → Fed policy rate (midpoint) | `https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json` | none | `official_data`, called only when FRED gives no Fed rate | `T+1 (EFFR effective date)` | "Source: Federal Reserve Bank of New York" |

`central_bank_state` for USD and EUR is built from `fred` (with fallbacks to `nyfed_rates` and the Treasury US2Y close) and
`ecb_data`. Missing inputs stay missing, and `expected_next_rate` is never supplied (`backend/providers/official_data.py`).

## Central banks

| Provider | Supplies | Endpoint / host | Credential | Cadence | Delay / latency label | Terms / attribution (config) |
|---|---|---|---|---|---|---|
| `fomc_calendar` | FOMC decision rows at 14:00 America/New_York (`sep: true` on meetings marked `*`, i.e. with projections) and the Chair's press conference at 14:30 ET as a `PRESS_CONFERENCE` sub-event (`parent_event_id` = the decision row); also configured `central_bank_meetings` (empty) as provider `configured`. The authority for decision dates | `https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm` | none | `calendar` loop | Date from the page, time from config | "Source: Federal Reserve Board" |
| `ecb_calendar` | ECB decision (14:15 CET) and press conference (14:45 CET) | `https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html` | none | `calendar` loop; page re-fetched at most hourly | `static schedule (official page, polled daily)`; time configured | "Source: ECB"; free reuse if reproduced accurately with the ECB cited |
| `boe_mpc_calendar` | BoE MPC Bank Rate decision (12:00 UK) | `https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates` | none | same | same | "Source: Bank of England"; personal or internal non-commercial use only; store dates as facts, do not republish page text |
| `boj_mpm_calendar` | BoJ MPM decision; 12:00 JST placeholder, `time_known: false` | `https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm` | none | same | same | "Source: Bank of Japan"; reproduction with credit, not for commercial purposes |

The Fed, ECB, BoE and BoJ press feeds (`fed`, `ecb`, `boe`, `boj`) are polled by the `central_banks` loop: 300 s
normally, 30 s within ±15 min of a decision (see News). A calendar page that parses to zero meetings sets `DEGRADED`
with `alert: true`. The `poll_seconds: 86400` value in `cb_calendars.json` is not read by the code; the `calendar` loop
sets the cadence.

## Calendars

The `calendar` loop runs every 21600 s, or sooner at the start of the next release-watch window
(`backend/refresh.py` `next_interval`). It fetches from yesterday to +45 days. The `release_watch` loop re-fetches only
the FMP `calendar` source.

- **Minimum intervals.** A source listed in `refresh.json` `calendar_sources.min_seconds` is fetched at most that often
  (`Runtime.calendar_due`); every other source is fetched on each calendar run. A failed fetch is retried on the next
  run. The last-fetch times are kept in memory by the `Runtime`, so a restart fetches every source again.
- **Row shape.** Every row carries `event_class`, `time_known`, `time_basis`, `time_source` (`source`, `config`, `rule`
  or `none`), `reference_period`, `attribution` and `source_url`, plus `kind`/`is_decision` and, where they apply,
  `sub_event`, `parent_event_id` and `speaker_role` ([EVENT_AWARENESS.md](EVENT_AWARENESS.md) §1, §8).
- **Hygiene** (`Runtime.refresh_calendar`). A source that answered replaces its stored rows inside the fetch window. The
  same release from two sources is kept once: an exact time beats a rule time beats a date, then
  `calendars.dedupe.provider_priority` (official sources first); vendor values stay on the kept row as
  `metrics`.
- **Layout changes.** A page that answers but parses to nothing raises `CalendarParseError`: `DEGRADED` with
  `alert: true`, and the stored rows are kept.

| Provider | Supplies | Endpoint / host | Credential | Cadence | Delay / latency label | Terms / attribution (config) |
|---|---|---|---|---|---|---|
| `calendar` (FMP) | Economic calendar (consensus, previous, actual, raw surprise); earnings dates for the tracked equities | `https://financialmodelingprep.com/stable/economic-calendar`, `/earnings-calendar` | `FMP_API_KEY` | `calendar` loop + `release_watch` (30 s inside watch windows) | Rows treated as UTC; earnings `date only; session not published` | none in config |
| `fed_events_calendar` | FOMC minutes, Beige Book, Chair and Vice Chair testimony and speeches (`speaker_role` and `speaker` from the title; only the Chair gets `FED_TESTIMONY`/`FED_SPEECH_CHAIR`, anyone else `FED_SPEECH`), G.17 industrial production. FOMC meeting and press-conference rows are skipped: decisions stay from `fomc_calendar` | `https://www.federalreserve.gov/json/calendar.json` | none | `calendar` loop, at most every 21600 s | ET clock time from the calendar; `date only (no time in the Fed calendar)` when it gives none | "Source: Federal Reserve Board"; public domain, cite the Board |
| `bea_release_calendar` | GDP (the Advance estimate has impact 9) and Personal Income & Outlays (PCE); reference period and estimate vintage from the ICS `SUMMARY` | ICS `https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics`; fallback `https://apps.bea.gov/API/signup/release_dates.json` (no reference period) when the ICS request fails or lists no release. Both URLs go through one client; an ICS failure resets its circuit before the fallback request, so the second host is always tried | none | `calendar` loop, at most every 43200 s | Exact UTC times; `BEA release schedule (ICS, UTC)` or `(release_dates.json, UTC)` | "Source: U.S. Bureau of Economic Analysis"; US federal work, cite BEA without implying endorsement |
| `census_calendar` | Retail sales (advance), durable goods, housing starts and permits, new home sales; parsed per `<tr>` from the visible date and time cells. Suspended releases are counted in health, never stored | `https://www.census.gov/economic-indicators/calendar-listview.html` | none | `calendar` loop, at most every 43200 s | Times read as ET (the page prints no zone) | "Source: U.S. Census Bureau" |
| `bls_release_calendar` | CPI, Employment Situation (NFP), PPI, JOLTS, ECI | `https://www.bls.gov/schedule/news_release/bls.ics` | `SEC_USER_AGENT` with a real contact e-mail (not `example.com`), sent as `MarketIntelligenceHub/1.0 (release schedule; contact: …)`. Without it `NOT CONFIGURED`: BLS blocks anonymous robots | `calendar` loop, at most every 43200 s | `BLS release schedule (ICS)` | "Source: U.S. Bureau of Labor Statistics"; public domain, robots must identify a contact |
| `fred` (release dates) | Fallback for `bls_release_calendar`: runs only while that source is switched off, not configured or its last fetch failed (`FredProvider.fallback_for`, `Runtime.primary_usable`). Release ids from `official_data.json` `fred.release_calendar`: 10 CPI, 50 Employment Situation, 46 PPI, 192 JOLTS, 11 ECI (one request each) | `https://api.stlouisfed.org/fred/release/dates` | `FRED_API_KEY` | `calendar` loop, at most every 43200 s | Date from FRED plus the configured BLS standard time (08:30 ET; JOLTS 10:00 ET), `time_source: config` | "Source: FRED release calendar, Federal Reserve Bank of St. Louis (schedule of the U.S. Bureau of Labor Statistics)" |
| `dol_claims_rule` | US initial jobless claims by rule: Thursday 08:30 ET, Wednesday when Thursday is a US federal holiday (holidays computed in code, 5 U.S.C. 6103); reference period `week ending <Saturday>`. No request | none (DOL publishes no machine-readable schedule) | none | `calendar` loop, at most every 3600 s | `DOL weekly rule: …`, `time_source: rule` | "Source: U.S. Department of Labor (weekly release rule)" |
| `survey_schedule` | Schedule-only rows (`schedule_only: true`): UMich consumer sentiment on configured dates (preliminary and final, 10:00 ET; configured through 2026-12-18) and Conference Board consumer confidence (last Tuesday of the month, 10:00 ET; `exceptions` for moved dates, currently empty). Values, consensus and actuals are never fetched or stored. No request | none (`providers.json` `econ_calendars.surveys`) | none | `calendar` loop, at most every 3600 s | UMich `time_source: config`, Conference Board `rule`; health notes when the UMich dates end inside the window | "Source: University of Michigan Surveys of Consumers (schedule only)", "Source: The Conference Board (schedule only)" |
| `treasury_refunding` | Next financing estimates and quarterly refunding statement, dated from "The next release is scheduled for …" on the page, at the clock time of the previous release (`assume_previous_release_time`). The page's spelling varies (lower-case months, "November 4 , 2026"); the parser accepts both. Plus the tentative coupon auction schedule XML linked there: Note and Bond auctions (TIPS and FRNs excluded) as `UST_AUCTION_<tenor>Y`, `tentative: true`, dates only; TreasuryDirect's announced auctions with a closing time win the dedupe | `https://home.treasury.gov/policy-issues/financing-the-government/quarterly-refunding/most-recent-quarterly-refunding-documents` (+ the linked `TentativeAuctionSchedule…xml` on the same host) | none | `calendar` loop, at most every 43200 s | Refunding `time_source: rule`; auctions `time_known: false`. An unreadable XML sets `DEGRADED`, and the refunding rows are still returned | "Source: U.S. Department of the Treasury" |
| `finnhub` (earnings) | EARNINGS rows for the 10 tracked equities, one `/calendar/earnings` query per symbol (an all-symbol query is cut at 1500 rows). EPS and revenue estimates are kept under `estimates`, labelled **"Finnhub estimate"** (Finnhub's own estimates included; never "Consensus"), and `consensus` stays `None`. Symbols not queried in a round keep their stored rows | `https://finnhub.io/api/v1/calendar/earnings` | `FINNHUB_API_KEY` | `calendar` loop, at most every 21600 s; shares the 60/min guard with quotes (waits up to 20 s per slot) and uses its own client, so an earnings failure never blocks quotes | Date only (`time_known: false`); `hour` bmo/amc → session `BMO`/`AMC`, anchored at 09:30/16:00 ET | "Source: Finnhub" |
| `treasury_auctions` | Upcoming UST auctions | `https://www.treasurydirect.gov/TA_WS/securities/upcoming?format=json` | none | `calendar` loop | Competitive close time when published, otherwise `date only` | none in config (rows carry "Source: TreasuryDirect (U.S. Department of the Treasury)" from code) |
| `crypto_expiries` | BTC/ETH option expiries (instrument counts). Each row carries its `tenor` (quarterly = the last Friday of March/June/September/December, monthly = the last Friday of another month, weekly = any other Friday, daily = any other day) and that tenor's `importance_score` (`providers.json` `option_expiry_importance`: 20 / 35 / 50 / 60), so a routine daily Deribit expiry stays below the 40 a "next key event" needs (EVR-8) | `https://www.deribit.com/api/v2/public/get_instruments` | none | `calendar` loop | Exact expiry timestamp | "Source: Deribit" |
| `eia_wpsr_calendar` | WPSR release times (Wed 10:30 ET + holiday exceptions) | `https://www.eia.gov/petroleum/supply/weekly/schedule.php` | none | `calendar` loop | `standard Wednesday 10:30 ET` or `holiday alternate (EIA schedule)`; `DEGRADED` if no holiday rows parse | "Source: U.S. Energy Information Administration (EIA)" |
| `congress_api` | Recently actioned bills (LEGISLATION) | `https://api.congress.gov/v3/bill` | `CONGRESS_API_KEY` | `congress` loop (300 s while active, 1800 s otherwise) + `calendar` loop | `action date only` | none in config |

The central-bank calendars are listed under Central banks.

**No free consensus or market-pricing source exists.** Macro consensus comes only from FMP (`FMP_API_KEY`); without it
an event shows `NO CONSENSUS`. Finnhub earnings estimates are shown under their own label, never as a consensus. No
adapter supplies `market_pricing`, so central-bank market pricing is always `UNKNOWN` (with or without FMP), and
`expected_next_rate` is never supplied (`backend/event_risk.py` `consensus_of`, `pricing_of`).

## News, official releases and regulators

The RSS/Atom feeds come from `sources.json` and all belong to Tier-1 official sources. Each item becomes an
`OFFICIAL_RELEASE` document with the feed's own date. At most 30 items and 5 MB are read per poll.
None of the feeds needs a credential. An RSS description is HTML escaped inside the XML: titles and summaries are
stored as plain text (`providers.base.plain_text`: tags removed, entities such as `&amp;` and `&#160;` decoded,
whitespace collapsed). Benzinga headlines and the Fed calendar's titles and locations are decoded the same way.

One page is one document (`backend/ingestion.py`). The same text reached over http and https, or with and without
`www.`, is not stored again (`engines.url_variants`), and copies stored before that rule are listed once when an
event is read: the earliest copy, with the others' ids in `duplicate_document_ids` (`engines.page_key`), so no
document count shows them twice.

**Classification principle** (`config/rules.d/causal.json` `causal_triggers`, `causal.derive_trigger`, logic audit of
2 Oct 2026 DEC-1 / ER-1). A trigger gives an event a direction and causal reach over many assets, so the text has to
state it; an incidental word never does. Central-bank and macro triggers (FED_HAWKISH, INFLATION_HOT, JOBS_WEAK, …) fire
only for the authority itself (`authorities`: the Fed, BLS, BEA, with their official X accounts; the whole text is read
and the authority stands for the rule's actor group) or for a tier-1/2 source whose **headline** states every group
(`max_tier`, `headline_only`); a tier-4 stock note mentioning "Fed rate hike odds" in its body is NEWS about that stock,
linked to the stock's own asset and nothing else. A publisher's own name (`publisher_names`: "SEC" in an SEC press
release, "Fed" in a Fed item) never counts as a keyword hit in its own items. A directional regulatory trigger needs a
direction word (`direction_words`: enforcement action, charges, sues, ban, … / approved, relief, dismissed, …);
without one the item is classified REGULATION with the crypto assets linked (`assets`) and **no trigger**: no
expected effects, no bearish reading of a custody proposal. Items no keyword rule classifies take the type and topic of
their source (`source_event_types`: Fed → CENTRAL_BANK, BLS/BEA → MACRO, SEC → REGULATION) when the provider's type
only says how the document arrived (`generic_event_types`: NEWS, OFFICIAL_RELEASE), at creation and when a stored
event is read, so the verification windows documented per type (`verification.json` `max_age_hours_by_event_type`:
CENTRAL_BANK and REGULATION 72 h, MACRO 48 h, others 24 h) apply to stored events too (ER-2). Stored events are
re-read with the current rules (`Ingestion._classified`, read-only): a deterministic trigger the rules no longer
justify is cleared (`stored_trigger`, `classified_on_read: true`), a legacy event a rule now matches gets its trigger;
an AI-accepted trigger is kept. On a copy of the owner's database (3 Oct 2026, 689 events) six stored triggers were
cleared (three SEC/CFTC crypto items read as "tighter regulation" from the publisher's name, the Intel stock note read
as FED_HAWKISH with reach over 28 assets, a Cuomo quote read as regulatory relief, a market wrap read as energy
de-escalation) and 223 official items took their typed window (150 CENTRAL_BANK, 41 MACRO, 32 REGULATION).

Documents stored before that cleanup are read as plain text too (`ingestion.readable`: event summaries, evidence
titles and excerpts), and the earlier wording of the OFAC and EDGAR adapters (`SANCTIONS_RUSSIA`, ISO dates and times,
`1 amendments`, `add:`) is restated as the adapters now write it (`discovery.restate_legacy`). The stored rows are
never rewritten. When such a page is fetched again it is recognised as the stored document (same source, page and
readable text), so the cleanup never adds a second copy.

One publication is one event (`config/rules.d/dedupe.json`). Besides the similarity and time rules, headlines whose
long numbers differ (`identifier_pattern`: OFSI licence `INT/2025/7895596` vs `INT/2025/8031092`) never merge, and a
document joins an event that already holds another page of its own official source only under an identical headline
(`official_pages_distinct`): the Bank of Japan's Monetary Base and its Transactions with the Government, published
together, are two events. Posts of an official X account are exempt (`official_pages_shared_domains`): one story often
spans several posts.

**Start-up repair** (`Ingestion.repair_stored_events`, called by `HubService.run_event_repair` at every start, once
per `dedupe_version`; `dedupe.json` `repair_note`). Nothing is changed before the SQLite database is copied to
`<database file>.pre-repair-<dedupe_version>` with the sqlite3 backup API; without that copy (or on a database that is
not SQLite) the repair is skipped, logged and tried again at the next start. Then (1) events stored before events
carried a provenance get `legacy_provenance` (they could never be joined, so each of their feed items was stored a
second time as a new event, ER-4); (2) events that are one publication stored twice (same source and provenance,
published within `headline_time_window_hours`, the same page or the same readable text) are merged into the **older**
id: evidence, reaction and basket rows and `research_jobs` references move to it, a row the kept event already has is
kept under `merged:<old key>`, and an alias row (`merged-event:<old id>`) lets the old id still open the kept event;
(3) events merged under earlier rules (a monthly release joined to last month's, nine OFSI licences in one event,
unrelated headlines linked to one stock's quote page) are re-split by `Ingestion.split_merged_events`: a document that
joined by similarity, or by a generic URL, and no longer matches moves, with the other copies of its page, to an event
of its own (`split_from` names the original), and the AI-derived fields of an event that lost documents are reset.
Old and new ids are written to `audit_logs` (`events_repair`, `events_resplit`); a second run changes nothing. On a
copy of the owner's database (3 Oct 2026): 689 events → 722 (37 duplicate pairs merged, 70 split off), 815 evidence and
5,711 reaction rows kept, a second and a forced run found nothing. `scripts/split_merged_events.py --dry-run` lists the
pending splits without changing anything.

**Date-only publications** (`discovery.json` `date_only_documents`, ER-5). OFAC Recent Actions and EDGAR full-text hits
publish a date and no clock time; their adapters stamp such a document at the start of that day in the source's time
zone, and the document and its event are stored and served with `time_known: false` and the date (`published_date`,
`event_date`, `event_time_known` for the web client). The stamp orders and ages the publication; it is never shown as
a time.

| Provider | Source | Feed URL | Cadence |
|---|---|---|---|
| `fed` | Federal Reserve | `https://www.federalreserve.gov/feeds/press_all.xml` | `central_banks` |
| `ecb` | ECB | `https://www.ecb.europa.eu/rss/press.html` | `central_banks` |
| `boe` | Bank of England | `https://www.bankofengland.co.uk/rss/news` | `central_banks` |
| `boj` | Bank of Japan | `https://www.boj.or.jp/en/rss/whatsnew.xml` | `central_banks` |
| `bls`, `bls-empsit`, `bls-ppi`, `bls-jolts` | BLS (CPI, jobs, PPI, JOLTS) | `https://www.bls.gov/feed/{cpi,empsit,ppi,jolts}.rss` | `global_sweep` 300 s + `release_watch` |
| `bea` | BEA | `https://apps.bea.gov/rss/rss.xml` | `global_sweep` + `release_watch` |
| `eia` | EIA press | `https://www.eia.gov/rss/press_rss.xml` | `global_sweep` + `release_watch` |
| `treasury` | US Treasury | `https://home.treasury.gov/rss.xml` | `global_sweep` |
| `whitehouse` | White House | `https://www.whitehouse.gov/news/feed/` | `global_sweep` |
| `sec` | SEC press releases | `https://www.sec.gov/news/pressreleases.rss` | `global_sweep` |
| `cftc` | CFTC | `https://www.cftc.gov/RSS/RSSGP/rssgp.xml` | `global_sweep` |
| `congress` | Congress.gov (presented to President) | `https://www.congress.gov/rss/presented-to-president.xml` | `global_sweep` + `congress` |
| `hm-treasury` | HM Treasury (GOV.UK) | `https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=hm-treasury` | `global_sweep` |
| `ofsi` | OFSI (GOV.UK) | `https://www.gov.uk/government/organisations/office-of-financial-sanctions-implementation.atom` | `global_sweep` |
| `ec-presscorner` | European Commission Press Corner | `https://ec.europa.eu/commission/presscorner/api/rss?language=en&documenttype=1,7&pagesize=30` | `global_sweep` |

- `hm-treasury` and `ofsi` must be attributed as "Contains public sector information licensed under the Open Government
  Licence v3.0". `ec-presscorner` must be attributed as "Source: European Commission".
- The first feed of a source carries the source id. Extra feeds are named `<id>-<feed file>` (e.g. `bls-empsit`), or `<id>-<n>` when the file name is generic (`rss`, `feed`, `index`, `atom`, `news`) (`feed_providers` in `registry.py`).
- OPEC is a registered source with no feed: its public feed returns HTTP 403 to automated clients. OPEC documents arrive
  only through a licensed gateway or an admin import.

| Provider | Supplies | Endpoint / host | Credential | Cadence | Delay / latency label | Terms / attribution (config) |
|---|---|---|---|---|---|---|
| `sec_edgar` | Filings (8-K, 10-Q, 10-K, 6-K, S-1, S-3, 424B5, SC 13D, SC 13G) for 10 tracked CIKs, at most 10 per company | `https://data.sec.gov/submissions/CIK##########.json` | `SEC_USER_AGENT` (`NOT CONFIGURED` while it is empty, the default, or has no real e-mail, e.g. `example.com`) | `sec_filings` 60 s in US market hours, 900 s otherwise | EDGAR `acceptanceDateTime` | Tier-1 `sec-filings` source |
| `alpaca` (news) | Benzinga headlines plus the HTML-stripped summary (never the article body) for the tracked equities and BTC/ETH, with their benzinga.com URL; rows without a URL are dropped | `https://data.alpaca.markets/v1beta1/news` | `ALPACA_API_KEY_ID` + `ALPACA_API_SECRET_KEY` | `alpaca_news` 60 s (first run looks back 6 h, then a cursor with 5 min overlap; 50/page, 3 pages); after a 403/404 (news not entitled) retried every 6 h, market data unaffected | Article `created_at` (cursor on `updated_at`) | Source `benzinga` (`sources.json`: tier 4, kind `specialist`, domain `benzinga.com`); "Source: Benzinga via Alpaca News API" |
| `reuters`, `ap` | Licensed wire news via a customer-operated gateway (`GET {base}/news?since=<cursor>` → `{documents, next_cursor}`), at most 100 docs/poll | `REUTERS_BASE_URL` / `AP_BASE_URL` | `REUTERS_API_KEY` / `AP_API_KEY` (URL and key both required) | `breaking_news` 60 s (60 s timeout, long-poll allowed) | Gateway `published_at` | Tier 2; the adapter never scrapes paid publications |

## Social (X)

| Provider | Supplies | Endpoint / host | Credential | Cadence | Delay / latency label | Terms / attribution (config) |
|---|---|---|---|---|---|---|
| `x` | Recent posts that match the watch query | `https://api.x.com/2/tweets/search/recent` | `X_BEARER_TOKEN` | `x_search` 45 s (`since_id` cursor, `max_results` 50) | Post `created_at` | none in config |

- The query is built from `sources.json`: the 8 `x_accounts` (federalreserve, ecb, USTreasury, WhiteHouse, SECGov,
  BLS_gov, Reuters, AP), `x_monitoring` keywords, and cashtags sent as plain tickers. It adds `-is:retweet` and is
  capped at 512 characters.
- Posts from official accounts become Tier-1 statements, and Reuters/AP posts are Tier 2. Any other account maps to
  `x-unknown` (Tier 5, discovery only) and triggers corroboration.
- Filtered-stream helpers (`set_stream_rules`, `parse_stream_line`) exist but runtime does not start them.

## Discovery

The `discovery` loop runs every 900 s. The `poll_seconds` values in `discovery.json` are not read by the code.

| Provider | Supplies | Endpoint / host | Credential | Cadence | Delay / latency label | Terms / attribution (config) |
|---|---|---|---|---|---|---|
| `gdelt_events` | Candidate events for CAMEO roots 13 and 15–20 in 22 energy/geopolitics countries (≥ 3 mentions, ≤ 3 days old, ≤ 50 candidates) + per-country conflict intensity | `https://data.gdeltproject.org/gdeltv2/lastupdate.txt` → `{batch}.export.CSV.zip` (MD5 checked for the listed batch) | none | 900 s | `15-minute batch (GDELT machine-coded news events)`; `discovery_only`, `UNCONFIRMED` | "Source: The GDELT Project (gdeltproject.org), cited as required for redistribution"; ≤ 12 calls/15 min |
| `sec_fulltext` | Keyword hits in filings: tracked 8-K Item 2.02, going concern, Item 1.03 bankruptcy, "bitcoin treasury" 8-Ks | `https://efts.sec.gov/LATEST/search-index` | `SEC_USER_AGENT` with a real e-mail (not `example.com`) | 900 s | `file_date is day-granular; EDGAR full-text index latency is not documented` | "U.S. SEC, EDGAR full-text search (public domain, 17 U.S.C. 105)"; SEC fair access max 10 req/s; local guard 60/min |
| `ofac_actions` | OFAC recent actions (designations, removals, general licences) | `https://ofac.treasury.gov/recent-actions` | none | 900 s | `same-day publication; the page gives the date only, not the time` | "U.S. Treasury, OFAC Recent Actions (public domain, 17 U.S.C. 105)"; ≤ 6 calls/15 min |
| `ofac_sls` | SDN additions and removals from Sanctions List Service delta files; programme → asset map (Iran/Russia → WTI, BRENT, GOLD, …) | `https://sanctionslistservice.ofac.treas.gov/changes/history/{year}`, `/changes/{id}` (S3 redirect host allow-listed) | none | 900 s | `same-day publication (SLS delta file, time from the SLS publication history)` | "U.S. Treasury, OFAC Sanctions List Service (public domain, 17 U.S.C. 105)"; ≤ 12 calls/15 min |

GDELT output is stored separately as `discovery_items`. SEC full-text and OFAC output becomes Tier-1 documents (official
filings and notices, keyword-matched, not confirmations of market impact).

## AI and search

| Provider | Supplies | Endpoint / host | Credential | Cadence | Delay / latency label | Terms / attribution (config) |
|---|---|---|---|---|---|---|
| `ollama` | Chat for AI roles (when `LLM_PROVIDER=ollama`); `web_search` and `web_fetch` for corroboration; model catalogue | `OLLAMA_BASE_URL` (default `https://ollama.com/api`): `/chat`, `/web_search`, `/web_fetch`; keyless `/tags`, `/show` | `OLLAMA_API_KEY` | `model_discovery` 3600 s; interpretation 300 s (900 s inactive); `market_interpreter` 1800 s while a traditional market is open, else 3600 s, plus event triggers at most every 600 s (`refresh.interpretation`); searches from `hot_topics` (900 s; each event at most twice), X and anomalies, capped by `SEARCH_HOURLY_BUDGET` (60/h) and `SEARCH_TOPIC_COOLDOWN` (at least 6 h for P0/P1 topics), 5 results each; every chat request within the daily AI budget (`ai_runtime.json` `budget`, 400 a day by default) | Search results are `discovery_only`; never used as prices or as confirmation | none in config |
| `openai` | Chat and model list when `LLM_PROVIDER=openai` | `OPENAI_BASE_URL` (default `https://api.openai.com/v1`): `/models`, `/chat/completions` | `OPENAI_API_KEY` | Same AI cadences | n/a | none in config |

Web search always goes through Ollama, so corroboration needs `OLLAMA_API_KEY` even when `LLM_PROVIDER=openai`.
Chat timeouts and attempts per role come from `config/rules.d/ai_runtime.json` (default 60 s and 2 attempts,
`market_interpreter` 150 s and 1, `chief_analyst` 120 s); see [AI_AGENTS.md](AI_AGENTS.md).

## Requires credentials

Enter them in Settings → Data providers (no password in local mode; applied immediately, no restart;
[PROVIDER_SETTINGS.md](PROVIDER_SETTINGS.md)) or in `.env` (template: `.env.example`) and restart. An adapter without
its credential reports `NOT CONFIGURED`. Treasury, ECB (FX fix and the DXY replica) and EIA are keyless.

| Env var(s) | Turns on | Where to obtain | Free? |
|---|---|---|---|
| `MASSIVE_API_KEY` | `massive` (equities, indices, FX quotes) | https://massive.com/dashboard/signup (plans: https://massive.com/pricing) | A $0 Stocks plan exists; the Snapshot endpoints this adapter uses are listed on paid plans |
| `X_BEARER_TOKEN` | `x` | https://console.x.com | No: pay-per-use credits |
| `FMP_API_KEY` | `calendar` (economic + earnings) | https://site.financialmodelingprep.com/register | A free plan exists; check which plans include these endpoints at https://site.financialmodelingprep.com/pricing-plans |
| `CONGRESS_API_KEY` | `congress_api` | https://api.congress.gov/sign-up/ (api.data.gov key) | Yes |
| `FINNHUB_API_KEY` | `finnhub` (equities, SPY/QQQ/IWM proxies, earnings calendar with "Finnhub estimate") | https://finnhub.io/register | Yes (free plan: personal use, 60 calls/min) |
| `ALPACA_API_KEY_ID` + `ALPACA_API_SECRET_KEY` | `alpaca` (ETF proxies, equity fallback, daily bars, Benzinga news) | https://alpaca.markets (generate the pair on a paper account) | Yes (Basic plan: IEX real time, 200 calls/min, personal use) |
| `FRED_API_KEY` | `fred` (SP500, NASDAQ, VIX closes; WTI/BRENT fallback; BAA10Y credit, Fed target range, vintages; BLS release dates when the BLS ICS is unusable) | https://fred.stlouisfed.org/docs/api/api_key.html | Yes |
| `BLS_API_KEY` (optional) | Raises the `bls_data` budget (guard 20 → 450 queries/day) | https://www.bls.gov/developers/home.htm (v2 registration) | Yes |
| `SEC_USER_AGENT` | `sec_edgar`, `sec_fulltext`, `bls_release_calendar` (`"Name contact@yourdomain"`, no key) | https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data | Yes |
| `REUTERS_BASE_URL` + `REUTERS_API_KEY` | `reuters` | Reuters licence (https://www.reutersagency.com/) plus your own gateway implementing the contract in `backend/providers/licensed.py` | No: licensed |
| `AP_BASE_URL` + `AP_API_KEY` | `ap` | AP licence (https://developer.ap.org/) plus your own gateway, same contract | No: licensed |
| `OLLAMA_API_KEY` (+ `OLLAMA_BASE_URL`) | `ollama` chat, web search, web fetch | https://ollama.com/settings/keys | Free tier for web search; paid cloud plans raise limits |
| `OPENAI_API_KEY` + `LLM_PROVIDER=openai` (+ `OPENAI_BASE_URL`) | `openai` for AI roles | https://platform.openai.com/api-keys | No: usage-based |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Telegram alert channel | https://t.me/BotFather | Yes |
| `SMTP_HOST`, `SMTP_FROM`, `SMTP_TO` (+ `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS`, `SMTP_SSL`) | E-mail alert channel | Your mail provider | Depends on provider |
| `WEBPUSH_VAPID_PUBLIC_KEY`, `WEBPUSH_VAPID_PRIVATE_KEY`, `WEBPUSH_SUBJECT` | Web Push alert channel (also needs `pip install .[notifications]`) | Generate locally with `vapid --gen` (py-vapid) | Yes |

- The alert channels (`backend/notifications.py`) are not data providers. Their status (`CONFIGURED`,
  `NOT CONFIGURED` or `UNAVAILABLE`) appears under `alerts` in `GET /api/settings`, not in `/api/providers`.
- Polling cadences, the X watch query and the SEC CIK watchlist are configured in `config/rules.d/` (`refresh.json`,
  `sources.json`, `providers.json`), not through environment variables.

## Deliberately not integrated

| Source | Reason |
|---|---|
| Yahoo Finance | Terms of service prohibit automated access (`PROGRESS.md`: "Yahoo Finance rejected (ToS)") |
| OKX, Kraken | Excluded: terms prohibit automated access or the site sits behind anti-bot protection |
| Google News (scraping) | Same |
| Reddit | Same |
| Nasdaq website | Same |
| Farside (ETF flows) | Same |
| Cboe VIX CSV | Its terms forbid storing or displaying it without consent; VIX comes from FRED `VIXCLS` (or Massive) |
| VIXY as a VIX proxy | Futures-based decay makes it misleading (no VIX proxy is configured) |
| Fed broad dollar index (`DTWEXBGS`) as DXY | A different index; DXY is the ECB-derived replica of the ICE formula |
| ICE BofA OAS on FRED (`BAMLH0A0HYM2`, `BAMLC0A0CM`) | Internal use only under ICE terms. The credit feature uses Moody's `BAA10Y` instead (`regime.json`: `credit_features: ["BAA10Y"]`, and `credit_note` says BAA10Y daily moves are about a third of HY OAS moves, so thresholds were scaled; `official_data.json` `restricted_series_note`) |

Other gaps recorded in config:
- ICE Brent COT: ICE Futures Europe is not in CFTC data (`positioning_sources.json` `gaps`).
- OPEC feed: returns HTTP 403 to automated clients (`sources.json`).
- Binance COIN-M hourly statistics: disabled (`stats_disabled_note`).
- Finnhub index levels: premium-only, so ETF proxies are used instead.
