# Market Intelligence & Decision Hub

Working local application built from `masterprompt.md`: FastAPI, SQLAlchemy, Next.js, live crypto data, official RSS evidence, Ollama Cloud, deterministic gates, decision snapshots and paper evaluation.

**Status:** implemented end to end and tested locally. What still needs credentials, Docker or real-world validation, and what is deliberately simplified, is listed in [technical debt](docs/TECHNICAL_DEBT.md). See [PROGRESS.md](PROGRESS.md). No real trading is implemented.

## Start locally on Windows

Python 3.12+ and Node.js 20.9+ are required. Secrets live only in `.env` (gitignored); start from `.env.example`.

**Every day — one command** (from the project folder, with any `python`):

```powershell
python main.py
```

The launcher continues inside the project's `.venv` (so a global Python or PyCharm's default interpreter works too), starts the API and the UI, and opens http://127.0.0.1:3000. Ctrl+C in the same window stops both. If the API or UI is already running, it is reused instead of started twice.

- `python main.py --api-only` — backend only (e.g. to debug with breakpoints; select `.venv\Scripts\python.exe` as the PyCharm interpreter for that)
- `python main.py --no-browser` — do not open the browser
- `python main.py --api-port 8010 --ui-port 3100` — other ports (CORS and the UI's API address follow automatically)

**First time only** (fresh checkout):

```powershell
git clone https://github.com/Mario-creator-86/Today_Market.git
cd Today_Market
python -m venv .venv
.venv\Scripts\python -m pip install -e .[dev]
copy .env.example .env
.venv\Scripts\python -m scripts.init_local
.venv\Scripts\python -m alembic upgrade head
cd frontend
npm ci
```

- UI: http://127.0.0.1:3000
- API documentation: http://127.0.0.1:8000/docs
- Readiness: http://127.0.0.1:8000/health/ready
- Local database: `market_hub.db` in the project root (gitignored).

Next.js allows only one UI dev server per folder; if one is already running on another port, the launcher says where and how to stop it. `scripts.init_local` generates missing local administrative/database secrets without printing them. Set `ENABLE_INGESTION=false` in `.env` for offline development. No fixtures are inserted into the runtime database.

## Credentials (all optional)

The app runs without any key: about 40 of the 54 providers are public (Binance, Deribit, Treasury, ECB, NY Fed, Fed/BoE/BoJ/ECB calendars, BLS/BEA/EIA/Eurostat, SEC, CFTC, OFAC, GDELT, …). Without keys the dashboard already shows BTC/ETH live, Treasury yields, ECB FX fixes, the DXY replica (from the ECB fix), EIA WTI/Brent spot and the PAXG gold proxy; FRED fills S&P 500/Nasdaq/VIX and Finnhub or Alpaca the equities ([coverage](docs/MARKET_COVERAGE.md)).

Keys are managed in **Settings → Data providers**: enter, replace, remove, test, switch providers on or off. There is no password when the app is opened on this computer (local mode, `ADMIN_AUTH=local`, the default), and changes apply immediately without a restart ([provider settings](docs/PROVIDER_SETTINGS.md)). Keys can also be put in `.env` (then restart). Details, signup links and terms: [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

| Variable | Enables |
|---|---|
| `OLLAMA_API_KEY` | AI roles, web search for corroboration |
| `MASSIVE_API_KEY` | Equities, indices, FX, US options (Massive, formerly Polygon) |
| `FINNHUB_API_KEY` | US equity quotes and SPY/QQQ/IWM ETF proxies (free tier, personal use) |
| `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` | Alpaca market data (IEX, paper-account pair; never trades): ETF proxies, equity fallback, daily bars for technical levels, Benzinga news (free, personal use) |
| `FRED_API_KEY` | S&P 500, Nasdaq and VIX daily closes, WTI/Brent fallback, macro series and the BAA10Y credit-spread feature (free) |
| `BLS_API_KEY` | Higher BLS v2 quota (free; v1 works without it) |
| `FMP_API_KEY` | Economic and earnings calendar |
| `CONGRESS_API_KEY` | Congress.gov bills (free) |
| `X_BEARER_TOKEN` | X official-account monitoring |
| `SEC_USER_AGENT` | Real contact for SEC EDGAR filings (required by SEC fair-access policy) |
| `REUTERS_*`, `AP_*` | Licensed news gateway |
| `TELEGRAM_*`, `SMTP_*`, `WEBPUSH_*` | Alert delivery channels |

Treasury, ECB and EIA need no key. `PROVIDER_MODE=stub` replaces keyless paid adapters with clearly labelled development stubs (never tradable).

## Ollama Cloud

Put your Ollama Cloud key in `.env` as `OLLAMA_API_KEY`. The integration was verified against catalog discovery, model metadata, authenticated chat, strict audit/extractor/planner JSON and web search. Details: [setup](docs/OLLAMA_SETUP.md).

```powershell
.venv\Scripts\python -m scripts.check_ollama --configure-roles
```

This command makes a small number of real inference/search requests. It fills only unassigned model roles using validated models; no names are hardcoded in business logic. Settings → AI models permits changes (no password in local mode; `ADMIN_TOKEN` in token mode).

## Docker

Requires Docker Desktop. The compose stack runs PostgreSQL with TimescaleDB and pgvector, Redis, a one-shot migration job, the API, a Celery worker, Celery beat and the Next.js production build. All ports bind to 127.0.0.1.

```powershell
cd Today_Market
copy .env.example .env        # only if .env does not exist yet
# in .env set at least: POSTGRES_PASSWORD=<choose one>  and  ADMIN_TOKEN=<choose one>
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f api worker beat
```

- UI: http://127.0.0.1:3000 · API: http://127.0.0.1:8000/docs · readiness: http://127.0.0.1:8000/health/ready
- `DATABASE_URL`, `REDIS_URL` and `CELERY_ENABLED` are set by compose. With Celery on, beat owns polling and analysis, and the API keeps the exchange WebSocket, the `/ws` fan-out and the loops not scheduled by Celery (quotes, equities, Alpaca, EIA spot, Treasury yields, official data, positioning, discovery).
- Compose sets `ADMIN_AUTH=token` for `api`: requests reach the container from the Docker bridge, not loopback, so Settings asks for the `ADMIN_TOKEN` from `.env`. Keys set through `env_file` come from the OS environment there, so edit `.env` on the host and recreate the containers.
- Stop: `docker compose down` (add `-v` to delete the database and Redis volumes).
- Run exactly one `api` and one `beat` replica. Several ingestors are not coordinated.

Docker is not installed on the machine this was built on, so the compose stack has **not been run**. Everything else was verified with the local launcher. Production also needs TLS, access control, backups and licensed data entitlements.

## Verification

```powershell
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check backend scripts tests migrations
.venv\Scripts\python -m mypy backend
cd frontend
npm run typecheck
npm test
npm run test:e2e
npm run build
```

Offline tests isolate local credentials. They never use the paid key. `npm run test:e2e` starts (or reuses) the dev server on port 3000; set `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3100` to run it against an already running `next start` build instead. Browser regression tests use clearly test-only intercepted responses. [Verification record](docs/VERIFICATION.md).

## Behaviour with missing data

Missing market/API access is displayed as NOT CONFIGURED or unavailable. Stale prices cannot publish an actionable setup; the browser independently expires cached prices during outages. Official historical releases remain explicitly STALE. Manual document submissions remain unverified discovery evidence, even when their URL names an official site. Probabilities remain unavailable until independently validated calibration exists.

See [architecture](docs/ARCHITECTURE.md), [database](docs/DATABASE.md), [sources](docs/DATA_SOURCES.md), [decision engine](docs/DECISION_ENGINE.md) and [security](docs/SECURITY.md).

## License

MIT, see [LICENSE](LICENSE).
