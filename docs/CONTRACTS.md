# Internal data contracts

Shared shapes between engines, services, API and UI. Modules may add fields but must not rename or change the meaning of the ones below. All timestamps are timezone-aware UTC ISO-8601 strings; presentation converts to Europe/Bucharest.

## Configuration

`backend.config.rules()` merges `config/rules.json` with every `config/rules.d/*.json` fragment (a fragment may not redefine a key) and adds `content_hash` (sha256 of the merged content). Each domain owns one fragment, and new thresholds belong in that fragment, never in code. Version every fragment with a `<domain>_version` key.

| Fragment | Owner module(s) |
|---|---|
| `rules.json` (`version`, `weights`, `thresholds`, `assets`, `categories`) | `backend/scoring.py`, `backend/engines.py` |
| `rules.d/causal.json` | `backend/causal.py` |
| `rules.d/sources.json` (`sources`, `source_weights`, `source_tiers`, `x_accounts`) | `backend/providers/*`, `backend/ingestion.py` |
| `rules.d/calendars.json` | `backend/providers/calendars.py` |
| `rules.d/market.json` (anomaly, reaction, baskets, technical) | `backend/analytics.py`, `backend/technicals.py`, `backend/positioning.py` |
| `rules.d/autolearn.json` | `backend/evaluation.py`, `backend/calibration.py`, `backend/backtest.py` |
| `rules.d/alerts.json`, `rules.d/refresh.json` | `backend/notifications.py`, runtime cadence |
| `rules.d/regime.json`, `rules.d/brief.json` | `backend/regime.py`, `backend/briefs.py` |
| `config/prompts.json` (not merged into rules) | `backend/ai.py` |

## Event (`Ingestion.event()` output)

Existing keys: `id, event_id, summary_fact, event_type, verification_status, fact_confidence, first_seen_utc, last_updated_utc, sources, evidence, assets, event_time, interpretation, extraction, provenance`.

Added keys:
- `topic`: stable slug, e.g. `IRAN_HORMUZ` or `FED_POLICY`
- `entities`: list[str]
- `novelty`: 0..1
- `importance`: 0..1, deterministic (source tier × causal strength × extractor importance when available)
- `trigger`: causal trigger id, or null
- `trigger_basis`: `rule` | `ai_proposal_accepted` | `none`
- `affected_assets`: symbols derived deterministically from the causal graph (`assets` stays the union of affected, ingested and accepted AI assets)
- `injection_suspected`: bool
- `reactions`: list of Reaction (below)
- `causal_paths`: list of Path (below)

The AI only proposes `event_type`/`assets`. `backend.ai.accept_extraction()` decides what is applied.

## Causal path (`backend/causal.py`)

```json
{"rule_id": "hormuz-oil", "trigger": "GEOPOLITICAL_ENERGY",
 "steps": [{"variable": "oil_supply_risk", "direction": 1}, {"variable": "oil", "direction": 1}, {"variable": "inflation_expectations", "direction": 1}],
 "effects": {"BRENT": 1, "US10Y": 1, "BTC": -1},
 "strength": 70, "decay": 0.83, "active": true, "suppressed_by": null}
```

Competing paths for the same trigger are all returned; `net_effect(paths, symbol)` sums them but the individual paths stay visible.

## Reaction (`backend/analytics.py`)

`{event_id, symbol, horizon (1m|5m|15m|30m|1h|4h|close|next_session), baseline_price, observed_price, change, unit (pct|bp), expected_direction (-1|0|1), status (CONFIRMED|PARTIAL|DIVERGENT|REVERSED|NO_REACTION|NOT_EXPECTED), baseline_timestamp, observed_timestamp}`

Event-level basket: `{event_id, horizon, confirmation_pct 0..100, confirming, diverging, missing, divergence: bool}`.

## Components and decision payload

Each component is `{"value": -100..100 | null, "status": "measured" | "missing", "basis": str}`. A missing component contributes 0 but is listed in `components_missing` and never displayed as a measured zero.

The decision payload (the `Decision.payload` JSON) keeps its existing keys and adds:
- `category`: one of `STRONG LONG SETUP, LONG SETUP, BULLISH LEAN / WAIT, WAIT, BEARISH LEAN / WAIT, SHORT SETUP, STRONG SHORT SETUP, WAIT / DATA INSUFFICIENT`
- `direction`: legacy short form (`STRONG LONG`, `LONG`, `WAIT`, …), kept for existing clients
- `component_detail`: dict name → component
- `components_missing`: list[str]
- `why`: list[str], verified-fact bullets
- `confirmation_level`, `invalidation_level`: float|null (price)
- `confirmation`, `invalidation`: concrete text with levels and cross-asset conditions
- `evidence_confidence`, `market_confirmation`, `data_quality`: graded 0..100
- `historical_probability`: `{"p": 0..1, "n": int, "horizon": str, "calibration": {...}}` or `{"status": "NOT ENOUGH DATA", "n": int}`
- `regime`: `{regimes, posture, confidence}`
- `versions`: `{rules, content_hash, causal, sources, prompts, models: {role: model}}`
- `ai_run_ids`: dict role → ai_runs id
- `audit`: `{status, checks: [{name, passed, detail}], reasons}`

## Sessions (`backend/sessions.py`)

- `session_close(asset_class, at) -> datetime | None`: the end of the session containing `at`
- `next_session_close(asset_class, at) -> datetime | None`
- `market_state(asset_class, at) -> LIVE | LAST CLOSE | FRIDAY CLOSE | AFTER HOURS | PREMARKET | UNKNOWN`

Equities and indices use XNYS via exchange_calendars. FX closes 17:00 America/New_York. Crypto closes at the configured UTC cut-off. Futures, bonds and commodities use venue metadata from config when present, otherwise UNKNOWN.

## Alert

`{id (stable dedupe key), kind (SETUP|LEVEL_BREAK|DIVERGENCE|ANOMALY|EVENT|MACRO_RELEASE|PROVIDER), priority (HIGH|MEDIUM|LOW), title, message, symbol|null, created_at, decision_id|null, event_id|null, metrics}`

## Provider development stubs

When `PROVIDER_MODE=stub` and a paid credential is absent, adapters run against recorded fixtures. Every record they produce carries `provider="<name>-stub"` and `stub=true`. Stub quotes never reach scoring, publication, outcomes or calibration; the UI labels them `DEVELOPMENT STUB`.
