# Event-aware market interpretation (contract)

This implements `imbunatatire.md`. The dashboard must answer two questions: what is happening now, and what can materially change the market next.

The existing deterministic engines stay: regime, events, scoring and decisions, providers, market data and the reaction engine. A deterministic event-risk layer is added on top of them, together with an AI interpretation that only *explains* backend numbers.

Research notes with endpoints, samples, terms and code facts (file:line) are in the session scratchpad: `…/scratchpad/pc/events/{calendars,consensus-earnings-pricing,gap-backend,gap-frontend}.md`.

## 0. Non-negotiable rules

- The AI never invents events, times, consensus, probabilities, prices, scores or levels. The backend injects every number, and the audit's number check rejects any number that is not in the input.
- Missing data is shown as missing:
  - no consensus → `"NO CONSENSUS"`;
  - no market pricing → `"UNKNOWN"`;
  - a CRITICAL or VERY HIGH event with incomplete data → flag `EVENT DATA INCOMPLETE` (levels in `incomplete_flag_levels`).
- The event overlay never removes a signal. The published decision (category, score, blocks) stays unchanged. Event risk adds separate fields: `event_risk`, `adjusted_confidence`, `trade_status`.
- Unverified (X or social) events are shown as `POTENTIAL EVENT · UNCONFIRMED` and never change scores.
- Date-only rows (`time_known: false`) never produce IMMINENT, IN_PROGRESS or EVENT RISK MODE. They show "time TBA".
- Weekend or closed markets: implications for traditional assets are "cannot be confirmed until markets reopen", while live crypto is interpreted live.
- All thresholds, weights and windows live in versioned config: `config/rules.d/event_risk.json` and `event_exposure.json`, each with its own `*_version`.

## 1. Calendar sources (new or fixed)

Rows keep the existing `calendar_row` shape plus these extras:

| field | meaning |
|---|---|
| `event_class` | taxonomy key |
| `time_known` | whether the exact time is known |
| `time_basis` | how the time was obtained |
| `time_source` | `source`, `config`, `rule` or `none` (ranks the row in the dedupe) |
| `reference_period` | the period the release covers |
| `parent_event_id` | links a sub-event to its main event |
| `sub_event` | e.g. `PRESS_CONFERENCE`, `MINUTES`, `PROJECTIONS` |
| `kind`, `is_decision` | §8 kind (`calendars.row_kind`); `is_decision` only for DECISION rows of FOMC, ECB, BoE, BoJ |
| `speaker_role`, `speaker` | speeches and testimony (from the Fed calendar title) |
| `session` | session a date-only row is anchored to: `BMO` / `AMC` (Finnhub earnings), `TOKYO_DAY` (BoJ) |
| `sep` | true on FOMC meetings with projections |
| `source_url` | where the row came from |
| `attribution` | source credit |

| Source | Classes | Notes |
|---|---|---|
| Fed `federalreserve.gov/json/calendar.json` (keyless) and `fomccalendars.htm` | FOMC decision 14:00 ET, press conference 14:30 ET (sub-event), minutes 14:00 ET, SEP/dot plot (`*` meetings), testimony, Chair and Vice Chair speeches (`speaker_role` from the title), Beige Book, G.17 industrial production | fomccalendars.htm stays the authority for decision dates |
| BEA ICS/JSON (keyless) | GDP, Personal Income & Outlays (PCE) | exact times |
| Census indicators HTML (keyless) | Retail Sales, Durable Goods, Housing Starts, New Home Sales | parse per `<tr>` |
| BLS ICS with a contact User-Agent (the configured SEC contact when it contains an e-mail), otherwise FRED `releases/dates` (FRED key) with agency standard times from config | CPI, Employment Situation (NFP, unemployment rate, average hourly earnings), PPI, JOLTS, ECI | `time_basis` says which one |
| Rule generator | US initial jobless claims: Thursday 08:30 ET; Wednesday when Thursday is a federal holiday | `time_basis: 'DOL weekly rule'` |
| UMich, Conference Board | schedule-only rows (UMich 10:00 ET, Conference Board last Tuesday 10:00 ET) | never store their values |
| Treasury refunding page + tentative auction XML (keyless) | refunding announcement, 2/5/7/10/20/30Y auctions | coupon auctions without a closing time stay `time_known: false` |
| Finnhub `/calendar/earnings` (free key, one query per symbol) | EARNINGS for tracked equities | `epsEstimate` / `revenueEstimate` go to `estimates` `{label: 'Finnhub estimate', values, note}`, never "Street consensus" (`consensus` stays empty); `hour` bmo/amc gives `session` BMO/AMC, and bmo/amc/dmh give `time_basis` ("before the US open", "after the US close", "during US market hours"). The row is stamped at the session anchor (09:30, 16:00 or 09:30 ET; 00:00 without an hour) with `time_known: false` |
| Existing ECB / BoE / BoJ / WPSR / crypto expiries | unchanged | ECB press conference 14:45 CET as a sub-event; BoJ keeps `time_known: false` for the statement |

Calendar hygiene (`Runtime.refresh_calendar`, pure helpers in `backend/providers/calendars.py`):
- The fetch window runs from yesterday to today + `fetch_days` (45). A source that answered replaces its stored rows strictly inside that window when it no longer lists them (`stale_ids`; the edge days are skipped because providers filter by local date and the store by UTC date). Rows with an actual are kept as history. A provider's `replace_scope` narrows this: Finnhub symbols not queried in a round keep their rows.
- The same release from two providers is deduplicated at storage (`calendars.dedupe`, over the whole window, stored rows of other sources included). The key is `(event_class, sub_event, symbol, publisher-local date, speaker)`, not `(event_class, local date)`:
  - `speaker` is set only for kinds SPEECH and TESTIMONY (`dedupe.speech_kinds`): the speaker, else the title, else the name, case-folded. Two appearances on one day stay separate.
  - Rows without an `event_class`, stubs and inactive rows are never merged.
  - The best row wins on time rank (`source`/`config` 2, `rule` 1, date-only 0), then on `dedupe.provider_priority` (official first). All rows of the winning provider are kept. Rows of other providers are deleted and named in the kept row's `duplicates`.
- No value is lost in the dedupe:
  - Every valued row of the release is listed in the kept row's `metrics` `[{name, provider, row_id, consensus, actual, previous, unit, ...}]`.
  - `headline_metric` names the metric chosen by `dedupe.headline_metrics` (first matching pattern of the class; a lone metric otherwise; several unmatched metrics are never guessed).
  - When the kept row published no consensus or actual itself, the headline metric's values are lent to it, with `values_source` (the lending provider), `lent_fields` and `metric`. The event layer then reports that consensus with `source` = `values_source`.
  - A row with a structured vendor estimate (Finnhub `estimates`) keeps its estimate. Only `actual`, `previous`, `unit` and `actual_ingested_at` are lent to it (`dedupe.structured_estimate_lendable`).
  - `carry_lent`: a kept row re-fetched without values keeps what was lent to it.
  - Lent values record no macro surprise and raise no second release alert (only rows without `values_source` do).
- The event layer groups rows again when it builds events (`event_risk._group`):
  - Main rows share an event when `(event_class, UTC date, discriminator)` match. The discriminator is the event's own symbol, else the name for class OTHER, else name + clock time for `group_by_name` classes (speeches, testimony).
  - An exact time beats a rule time beats a date, then the higher vendor impact.
  - Sub-event rows attach by `parent_event_id`, else to the closest earlier main row of the same class within `phases.sub_event_max_offset_minutes` (240). A second row of the same sub-event within `sub_event_dedupe_minutes` (30) is a duplicate.
- `watch_state` respects `time_known`.

## 2. Event taxonomy and exposure (`config/rules.d/event_exposure.json`)

`event_classes`: `{class: {label, category, importance (0-100), expected_volatility (0-100, configured prior), has_press_conference, press_conference_offset_minutes, in_progress_minutes, post_event_minutes, scenario_template}}`.

Minimum set of classes:
- Central banks: `FOMC`, `FOMC_MINUTES`, `FED_SPEECH_CHAIR`, `FED_TESTIMONY`, `ECB`, `BOE`, `BOJ`.
- US macro: `US_CPI`, `US_PCE`, `US_NFP`, `US_GDP`, `US_CLAIMS`, `US_RETAIL_SALES`, `US_PPI`, `US_JOLTS`, `US_ISM` (schedule-only), `UMICH`, `CONF_BOARD`.
- Treasury: `UST_AUCTION_{2Y,5Y,7Y,10Y,20Y,30Y}`, `UST_REFUNDING`.
- Other scheduled: `EIA_WPSR`, `CRYPTO_EXPIRY`, `EARNINGS`.
- Unscheduled: `GEO_ENERGY`, `GEO_CONFLICT`, `SANCTIONS`, `CRYPTO_REGULATION`, `REGULATION`, `CB_STATEMENT_UNSCHEDULED`.

`exposure`: `{class: {asset: level}}`, with level ∈ `very_high` (1.0), `high` (0.75), `medium` (0.5), `low` (0.25). The numbers come from `levels` in the same file.
- FOMC: US2Y very_high, US10Y high, DXY very_high, EURUSD very_high, USDJPY very_high, GBPUSD high, NASDAQ very_high, SP500 high, BTC high, ETH high, GOLD high (as in the spec).
- `EARNINGS` is a template: `{"$SELF": very_high, NASDAQ: high, SP500: medium, BTC: low}`, plus an override for COIN, MSTR and BMNR (BTC/ETH medium).
- `GEO_ENERGY` (Hormuz): BRENT very_high, WTI very_high, GOLD high, US10Y high, DXY medium, NASDAQ high, BTC medium.

`regime_effects` (regime cause → favored and pressured assets, for §18, §19, §23 and §24):
- RISK_ON favors NASDAQ, SP500, BTC, ETH and pressures DXY and GOLD.
- RISK_OFF + GROWTH_SCARE favors US10Y and US2Y (Treasuries rally, i.e. lower yields), plus GOLD and USDJPY↓.
- RISK_OFF + INFLATION_SHOCK does **not** favor Treasuries: yields rise, so bonds are pressured; USD and GOLD are favored.

The cause is taken from the active regimes of the regime engine. Tests 9 and 10 depend on this.

## 3. Deterministic engine (`backend/event_risk.py`, config `config/rules.d/event_risk.json`)

`EventObject` (pydantic, strict), as in spec §2:
- identity and timing: `event_id` (display id such as `FED-20261028-FOMC`, plus `row_id` for the stored calendar id), `name`, `event_class`, `category`, `status`/`phase`, `time_utc`, `time_bucharest` (e.g. "21:00", with `date_bucharest`), `time_known`, `time_basis`, `minutes_until_event` (backend value at build time; the UI recomputes the countdown itself);
- scores: `importance_score`, `expected_volatility_score`, `relevance_score`, `relevance_level`, `dominance_score`, `affected_assets` `[{symbol, level, weight}]`;
- expectations: `consensus` (dict or `"NO CONSENSUS"`), `market_pricing` (dict or `"UNKNOWN"`);
- context: `known_information` (backend facts only; the source is the row's attribution, e.g. "Source: U.S. Bureau of Economic Analysis", never the internal provider ids, which stay in `source_ids`), `possible_surprises` (scenario template ids), `source_ids`, `fact_confidence`, `sub_events` `[{sub_event, time_utc, phase}]`, `parent_event_id`, `data_incomplete: [reasons]`, `verification_status` (for unscheduled events).
- the code adds: `kind`, `is_decision`, `speaker_role`, `symbol`, `timing` (`SCHEDULED` | `ANYTIME`), `date_window` (§8), `relevance_components`, `relevance_missing`, `dominance_basis`, `impact_10` (relevance / 10), `impact` (the calendar row's own 0-10 impact, the number the calendar shows; null for unscheduled events), `actual`, `scenarios`, `consequences` (unscheduled), `data_incomplete_flag`, `surprise_basis`, `surprise`, `official_documents` (§8), `status_note`, `live_assets` and `awaiting_reopen`.

Events come from calendar rows scheduled between `recent_hours` (30) ago and `horizon_hours` (168) ahead. Rows with a status in `phases.hidden_statuses` (CANCELLED, SUSPENDED, POSTPONED, STALE) are dropped. RESOLVED events leave the snapshot.

Phases: `SCHEDULED → APPROACHING → IMMINENT → RELEASED → IN_PROGRESS → POST_EVENT → RESOLVED`.
- Windows are configurable per class, with defaults: APPROACHING ≤ 360 min, IMMINENT ≤ 15 min.
- RELEASED applies at time_utc up to +2 min.
- IN_PROGRESS runs until the last timed sub-event (the press conference) + the class `in_progress_minutes`. A central-bank event is never RESOLVED before its press conference is over.
- A class with `has_press_conference` and no press-conference row gets a configured sub-event at `press_conference_offset_minutes` after the decision (FOMC and ECB: 30). Its `time_basis` says it is a configured offset.
- A sub-event's own phase uses `phases.press_conference_minutes` (60) as its in-progress span.
- POST_EVENT lasts `post_event_minutes`, then RESOLVED.
- Date-only rows follow their session window instead (§8).

Scores (0-100, every weight in config):
- **relevance** = importance 30% + expected volatility 20% + time proximity 15% + regime sensitivity 15% + asset exposure breadth 10% + market-pricing uncertainty 10%.
  - A component that cannot be computed is left out, the weights are renormalised over the others, and it is named in `relevance_missing`.
  - Importance is the row's `importance_score`, else `symbol_importance` for the class and symbol, else the class importance, else 10 × the vendor impact.
  - Proximity is a piecewise curve on minutes until the event (`proximity_curve`: 0-60 min 100, 360 min 75, 1440 min 40, 4320 min 10, 10080 min 0). RELEASED and IN_PROGRESS count 100. POST_EVENT decays from 80 to 20 over its window. A verified unscheduled event decays from 100 to 50 over `active_window_hours`. A date-only row uses the minutes until its window starts, capped at 75 (`date_only_proximity_cap`).
  - Regime sensitivity: for each regime driver, the event's highest exposure among the driver's inputs. The static mean over all drivers is raised toward 100 by `moving_exposed_boost` (0.5) × the exposure share of drivers that are moving **and** exposed. A moving driver the event is not exposed to never lowers it.
  - Breadth is 100 × the sum of exposure weights / `breadth_full_weight` (6), capped at 100.
  - Pricing uncertainty is 100 × (1 − max p) / (1 − 1/n) over at least two explicit `*_probability` values of `market_pricing`, else the configured default (50) when pricing is UNKNOWN.
- **Levels**: CRITICAL ≥ 90, VERY HIGH 75-89, HIGH 60-74, MODERATE 40-59, LOW below 40 (`below_level`). Below 40 (`dashboard_min_relevance`) the event is hidden from the dashboard; `/api/events/upcoming` still lists it.
- **next_event**: the earliest scheduled event in SCHEDULED through IN_PROGRESS with relevance ≥ `next_event_min_relevance` (60); if there is none, the earliest with relevance ≥ 40.
- **Option expiries by tenor** (`event_exposure.expiry_tenor_importance`, EVR-8): the class importance of a `CRYPTO_EXPIRY` comes from the expiry date: quarterly (last Friday of a quarter-end month) 60, monthly (last Friday of another month) 50, weekly (another Friday) 35, daily 20. A daily or weekly expiry therefore stays below the dashboard threshold (40) and is never the key event; a row's own `importance_score` still wins.
- **Unmapped events** (`event_risk.unmapped`): an event that exposes no tracked asset (class OTHER, or no exposure mapping) has relevance and dominance capped at `relevance_cap` (55), below the next-event threshold. `dominance_basis.exposure` says so.
- **dominance** = importance 25%, expected volatility 15%, proximity 20%, affected count 10% (100 × affected assets / 10), pricing uncertainty 10%, historical impact 10% and regime sensitivity 10%.
  - Historical impact is the mean stored T+1h impact of the class (`record_history`, at least `min_history_events` (8) events), else the class expected volatility as a configured prior. `dominance_basis` says which.
  - `dominant_event` is set when the highest score is strictly above `dominance.threshold` (75). Unverified events have dominance 0.
- **per-asset event_risk** = max over counted events of `relevance × (exposure_floor + (1 − exposure_floor) × exposure_weight(asset))`, with `asset_risk.exposure_floor` 0.35. An exposed asset carries at least 35% of the event's relevance; an unexposed asset carries none. The original contract used plain `relevance × exposure_weight`; the floor came with `event_risk_version` 1.1.0.
  - Counted events: RELEASED, IN_PROGRESS and POST_EVENT events, verified unscheduled (ACTIVE) events, and upcoming events within `asset_risk_horizon_minutes` (720). Potential (unverified) events are never passed in.
  - Returned with the driving `event_id`, level, `minutes_until`, `phase` and `event_name`.
- **EVENT RISK MODE** (`event_risk.risk_mode`): active when one of these holds. Date-only rows never qualify.
  - A timed event that is RELEASED or IN_PROGRESS, or 0-60 min away (`minutes`), and is CRITICAL or has a configured class importance ≥ `importance_min` (90) while exposing tracked assets.
  - A RELEASED or IN_PROGRESS decision of `live_classes` (FOMC, ECB, BOE, BOJ) at one of `live_levels` (CRITICAL, VERY HIGH): a well-priced FOMC may be VERY HIGH, yet its press conference is the risk.
  - A verified unscheduled event with status `UNRESOLVED` that is CRITICAL, or whose configured class importance is ≥ `importance_min` (GEO_ENERGY and CB_STATEMENT_UNSCHEDULED: 90) while it exposes tracked assets and was first seen at most `active_minutes` (240) ago. The composite relevance of an unscheduled class cannot reach CRITICAL with the default pricing uncertainty (maximum 89.0), so without this gate a verified Hormuz closure never switched the mode on (EVR-3). After `active_minutes` the event stays ACTIVE EVENT RISK without the mode.
  - The highest-relevance candidate is shown. The payload is `{active, event_id, name, time_bucharest, time_utc, minutes_until, impact_10, impact, relevance_score, relevance_level, affected_groups, phase}`: `impact` is the event's calendar impact (0-10, as on the calendar) and the relevance is shown beside it, never under the same label.
  - The original contract rule (a CRITICAL event within 60 min, IN_PROGRESS, or a verified unscheduled CRITICAL event) was widened in `event_risk_version` 1.1.0 by the importance gate and the live central-bank decisions, and in 1.2.0 by the same importance gate for unscheduled events.
- **Pre-event adjustment** (per published decision; the decision itself is untouched):
  - `adjusted_confidence = base_confidence × (1 − penalty)`, where `penalty = clamp((event_risk − penalty_start)/(100 − penalty_start)) × max_penalty`, with `penalty_start` 40 and `max_penalty` 0.25.
  - `base_confidence` is |decision score| (`adjustment.base_confidence: abs_decision_score`): conviction, not a probability. The block also returns `base_confidence`, `penalty` and `confidence_basis`.
  - `trade_status` (the side comes from the published category after the read-time downgrade):
    - **WAIT FOR `<EVENT>`**: a side exists and an exposing event either is a scheduled RELEASED or IN_PROGRESS event with asset event risk ≥ `in_progress_min_risk` (45), or is timed, 0-60 min away (`wait_window_minutes`), with asset event risk ≥ `wait_threshold` (60). `<EVENT>` is the class short label (`FED`, `CPI`, `AAPL EARNINGS`). The original contract's IN_PROGRESS case had no risk minimum; the code needs 45 and also covers RELEASED.
    - **CONDITIONAL LONG/SHORT**: a side exists, no wait applies, and any of: the category is a lean; `market_confirmation` < `confirmation_threshold` (50); the top asset event risk ≥ `conditional_threshold` (45); the asset is exposed to a post-event MARKET DIVERGENCE. An unmeasured confirmation is not weak (`unmeasured_confirmation_is_weak: false`). `status_reason` lists the reasons.
    - **LONG/SHORT**: an actionable setup with low event risk (test 2).
    - **WAIT**: no side.
    - `awaiting_reopen` is true when the asset's session state is not LIVE.
- **EVENT DATA INCOMPLETE**: `data_incomplete` lists the reasons: `NO CONSENSUS` (class `expects_consensus`), `MARKET PRICING UNKNOWN` (class `expects_pricing`), `EXACT TIME UNKNOWN` (date-only) and `PROVISIONAL DATE`. `data_incomplete_flag` is set only when reasons exist and the level is in `incomplete_flag_levels` (CRITICAL, VERY HIGH). The snapshot's `data_incomplete` lists the flagged dashboard and active events as `{event_id, name, level, flag, reasons}`.
- **Favored, pressured, non-confirming** (at most 5 each, never forced):
  - The base list is `regime_effects` for the active regime cause.
  - An asset is favored or pressured only when its own CURRENT change exceeds the asset's noise tolerance in the direction the regime implies. A decision side is context: it is listed in `reason_facts` ("Decision BULLISH LEAN / WAIT (+30)") and never confirms, and a saved decision or a historical close cannot replace current direction evidence. Otherwise the asset is non-confirming, and the closing reason names what is missing: "current price change does not confirm (no move beyond noise in the implied direction)" (EVR-10).
  - Each item carries `{symbol, stance, event_risk, event_id, reason_facts}`. A measured change in `reason_facts` names the basis of that value (`change_basis`): the regime feature's own source when the regime observed the symbol ("last close Fri 25 Sep", ", stale" for a STALE close, "24h via PAXG" for a proxy, "live intraday"), else the asset's quote ("live 24h" for crypto, which a last-close regime leaves out). The regime-wide basis label is never applied to a value it does not describe.
- **What can break this view**: deterministic list built from the dominant event's scenario template, the regime drivers (e.g. "US10Y reversal higher"; "VIX reversal higher (stale close Tue 22 Sep)" when the driver's main input is a STALE close), and the invalidation levels of the trade-watch assets. At most `what_can_break_max` (6) items. Each item is `{text, kind, source}` with kind `event`, `driver`, `level` or `observed`. A driver reversal is a future condition: when the current observations already show it (`regime.divergences`: the current value of the input opposes the historical close the active regime rests on), the item reads "SP500 reversal lower already under way: SP500 −0.50% (vs previous close via SPY) opposes the historical input +0.51%" with kind `observed`, never a risk still ahead (RI-1).
- **Trade watch** (spec §20), at most 8 cards: setups and leans by |score|, then the favored and pressured assets. Fields: `asset`, `trade_status`, `decision_score`, `regime_favorability` (from regime_effects + drivers), `evidence_confidence`, `market_confirmation`, `event_risk`, `historical_probability` (only if calibrated, else `null` with reason), `horizon`, `why` (facts), `upcoming_event`, `confirmation`, `invalidation`.
- **Scenarios** (spec §11 and §14):
  - Deterministic templates per class, e.g. FOMC: A = as expected / neutral, B = hawkish, C = dovish, D = unexpected hold with a "reason matters" note; CPI: below or above consensus.
  - Each template carries its expected direction per exposed asset, taken from the causal graph where one exists (FED_HAWKISH, FED_DOVISH, INFLATION_HOT, INFLATION_COOL).
  - An asset whose competing causal paths are conflicting (`causal.conflict`: |net| / gross below `conflict_net_ratio` 0.35, the rule the decision engine gates on) gets NO direction: it is left out of `expected` and listed in the scenario's `unresolved` (EVR-1). Labour releases: stronger data means DXY, US2Y, USDJPY up and EURUSD, GBPUSD down, weaker data the opposite; SP500, NASDAQ, RUSSELL, BTC and ETH are unresolved in both branches (the rates path and the growth path nearly cancel), so a rally after a weak report is never graded as a divergence. GEO_ENERGY escalation gives US10Y no direction. No asset carries the same direction in both branches of a two-sided template. The same rule applies to the `consequences` of an unscheduled event.
  - A `surprise` map is written for the class headline (labour: above consensus = stronger). `surprise_polarity` marks headline metrics of the opposite polarity: an unemployment rate above consensus resolves to the weaker branch (EVR-9).
  - Scenarios are labelled "possible", never certain.
- **Post-event** (`post_event`, config `event_risk.post_event`):
  - Only timed events with relevance ≥ 40 in RELEASED, IN_PROGRESS or POST_EVENT are measured.
  - Reactions at T+1m/5m/15m/30m/1h (`analytics.scheduled_reactions`). The baseline is the last live tick at or before `time_utc`, at most `baseline_max_age_seconds` (120) old. Each window uses the last live tick at or before its end, at most `window_max_gap_seconds` (90) old. A window that has not ended, or an asset without both ticks, is not measured.
  - The expected direction exists only when the scenario is resolved: a macro or earnings surprise needs a consensus (or a labelled vendor estimate) and the actual; a central-bank or Fed-communication event needs a validated `event.surprise` (§8), and never while a priced headline still waits for guidance.
  - Market confirmation 0-100 is the basket confirmation at the latest graded window, over the measured exposed assets with an expected direction. It needs at least `min_measured` (2) of them.
  - `MARKET DIVERGENCE` when more than `divergence_share` (0.5) of them are DIVERGENT or REVERSED. The entry sets `research_recommended`. Its exposed assets with a side become CONDITIONAL (unless a wait applies). There is never an automatic trade.
  - The runtime starts one research-planner request per diverging event through the existing corroborate path (`refresh.divergence_research`: priority 0, retried after 60 s while the planner or the search budget is unavailable). A `research_jobs` marker `divergence:<event_id>` stops repeats across restarts.
  - `record_history` (run after each analysis cycle by `HubService.publish_briefs`) stores the T+1h impact of POST_EVENT entries under `sched-history:<event_id>`: the share of measured exposed assets that moved beyond their confirm tolerance.
- **Central-bank surprise basis** for the AI (`surprise_basis`, on central-bank and assessed classes):
  - `headline_priced`: when the actual and a consensus are both known, true exactly when they are equal and false otherwise; market pricing is not consulted then. Without them, true when the most likely outcome's probability ≥ `priced_threshold` (0.9), false below it, UNKNOWN without pricing. Unlike the original contract's "equals consensus **or** pricing ≥ threshold", a known actual that differs from consensus is false even when pricing reaches the threshold.
  - `guidance_pending`: until the press conference ends (its start + `press_conference_minutes`); for speeches, testimony and minutes until the event's in-progress window ends; for a date-only decision until its release window ends; for a central bank without a press conference until `time_utc`.
  - `allowed_assessments`: before release only UNDETERMINED or NOT_APPLICABLE; while guidance is pending and the headline is not known to be unpriced, UNDETERMINED or NEUTRAL_PRICED; without an official document of the event, UNDETERMINED (plus NEUTRAL_PRICED for a priced headline); otherwise all four.
  - Test 3: a priced headline alone is never a HAWKISH_SURPRISE while guidance is pending.
- **Unscheduled or active events**:
  - Verified events of the GEO/SANCTIONS/REGULATION classes seen within `active_window_hours` (24) become `ACTIVE EVENT RISK`, with timing `ANYTIME` and status `UNRESOLVED`, plus their causal consequences on exposed assets.
  - Exposure of an unscheduled event (EVR-6): an event matched through a causal trigger exposes the class mapping. One matched only by its event type or a keyword exposes only the assets it was itself mapped to (`assets` / `affected_assets`) that the class also exposes; with none it is not an event risk at all and stays in the news stream (a routine OFAC counter-terrorism notice no longer puts event risk on oil, gold, BTC and DXY). The SANCTIONS rule matches the event type only, not the bare words "sanctions" / "sanctioned".
  - One active event per class, official publisher (the sources' `independence_group`) and publication day: the more relevant notice is kept and its `known_information` says how many were merged (an OFAC designation arrives through `ofac` and `ofac-sls`).
  - `first_seen_utc` is always the time the app first saw the event; `published_utc` is the publication stamp and `published_date_only` says it is a date without a clock time (exact midnight in one of `unscheduled.date_only_zones`). A date-only stamp is not a publication time: the active window and its decay then start at `first_seen_utc`.
  - A later confirmed de-escalation trigger of the same class marks it `DE-ESCALATING`; the de-escalation itself is not a new risk.
  - An `unscheduled_only` statement within `cb_statement_match_hours` (6) of a scheduled central-bank meeting is that meeting's release, not an unscheduled event.
  - Unverified ones seen within `potential_window_hours` (12) go to `potential_events`, labelled `POTENTIAL EVENT · UNCONFIRMED`, with no effect on risk scores for trades (test 7).
- **Weekend**: `markets_open` `{crypto, us_equities, us_rates, fx, commodities}` comes from `backend/sessions.py`. When traditional markets are closed, trade statuses for their assets carry `awaiting_reopen: true` (test 6).

Public functions:
- `snapshot(hub, now, *, assets, events, calendar, regime, interpretation) -> dict` returns the `event_risk` block below. Inputs the caller already holds are reused; missing ones are read from the hub (assets always with `event_overlay=False`).
- `overlay(hub, assets, now)` adds the per-asset block for `/api/assets`. It reads news and the calendar once and uses the latest regime stored by the analysis cycle when it is at most `stored_regime_max_age_seconds` (900) old, else none.
- `upcoming(hub, now, min_relevance)` for `/api/events/upcoming`.
- `trigger_state(snapshot) -> dict`: phases (sub-events keyed `<event_id>:<SUB_EVENT>`), window buckets (`W60`, `W15`) of timed CRITICAL / VERY HIGH / HIGH events, risk-mode flag, dominant event id, regime posture, CRITICAL ids, post-event divergences and `markets_open`, for `refresh.detect_triggers`.
- `record_history(store, snapshot)`, `build_events(...)`, `scenarios(event)`, `asset_event_risk(...)` and the other pure helpers used by tests.

**Release outcome** (EVR-4; `backend/releases.py`, config `event_risk.release_documents.classes[...].series`, `period_rule`, `attach_before_minutes`, `attach_window_hours`; `official_data.release_poll`). The outcome of a scheduled release is read from the official series the app already stores (`official_series` rows `<provider>:<series>:<period>`), never from a vendor: a class maps to its series and how each is shown: `change` (payrolls: the change of BLS CES0000000001 in thousands, "+29k"), `level` (unemployment LNS14000000, "4.2%"), `pct_change` (CPI CUSR0000SA0 m/m, PPI WPSFD4 m/m), `yoy` (CPI y/y over 12 periods); EIA_WPSR shows the weekly change of the crude, gasoline and distillate stocks. FRED's copy of a BLS series is the `fallback`. The outcome exists only when the newest stored period of a series is the release's reference period (the calendar row's `reference_period`, else `previous_month` / `previous_week` on the scheduled day in `series_zone`), so a poll made before the release never passes August off as September. Its text names every metric with its previous reading: "payrolls +29k (prev +133k), unemployment 4.2% (prev 4.1%)".

- The runtime attaches it to the calendar row (`releases.attach`: `actual` / `previous` / `unit` from the headline series, `metrics`, `values_source` = the series provider, `actual_ingested_at` = when the app first stored that period, the `release_outcome` block; the fields are lent, so a calendar re-fetch keeps them) from `attach_before_minutes` before the scheduled time to `attach_window_hours` (36) after it, stores a `macro_releases` row with the outcome, sends the MACRO_RELEASE alert ("…: payrolls +29k (prev +133k), unemployment 4.2% (prev 4.1%); no consensus: surprise not computable") and observes, so the MACRO_RELEASE refresh trigger re-runs the decisions and the interpretation. A vendor actual already on the row is never replaced. It runs after every official-data poll and, right after a mapped release, from the release watch: `poll_release_outcomes` polls the providers that serve the class series (`official_data.release_poll.providers`: BLS, FRED, EIA, Eurostat, ECB) at most `max_polls` (4) times per row, `poll_seconds` (150) apart, so the outcome is attached within minutes instead of at the next 30-minute cadence (the keyless BLS API is otherwise polled every two hours; a targeted call counts against the same daily budget).
- The event layer reads the attached row: the EventObject carries `actual` and `outcome` {text, period, headline, actual, previous, unit, metrics, source, ingested_at}, its known information says "Published: …" (once; no separate "Actual:" line), its phases run RELEASED → IN_PROGRESS → POST_EVENT on the calendar clock as before, and the post-event block measures the reactions. Consensus stays as the calendar has it: with NO CONSENSUS the resolution basis is "NO CONSENSUS: surprise direction unknown", nothing is graded and no MARKET DIVERGENCE can be raised. A snapshot built before the attach ran reads the series itself (`release_outcome` loader path), so `released_today` is right either way.
- Not covered: classes without a stored series (US_PCE, US_GDP, US_JOLTS: BEA and JOLTS are not stored) show the publisher's document or a vendor actual only; the euro-area HICP series are stored (ECB, Eurostat) but no calendar source lists the Eurostat release, so no class maps to them yet.

## 4. API

`GET /api/dashboard` gains `event_risk`:

```
{ generated_at, markets_open, mode: {active, ...}, next_event: EventObject|null, dominant_event: EventObject|null,
  events: [EventObject relevance>=40 sorted], active_events: [...unscheduled verified], potential_events: [...],
  asset_event_risk: {SYM: {score, level, event_id, minutes_until}}, favored: [...], pressured: [...],
  non_confirming: [...], what_can_break: [...], trade_watch: [...], post_event: [{event_id, phase, reactions: {SYM: {1m,5m,...}},
  confirmation, divergence: bool, summary_facts}],
  released_today: [{event_id, row_id, name, event_class, time_utc, time_known, released_utc, summary, summary_basis,
  actual, values: [{name, series, value, previous, unit, period, text}], values_text, outcome: {text, period, headline, actual, previous, unit, metrics, source, ingested_at}|null,
  reaction: {horizon, text}|null, document: {evidence_id, event_id, title, summary, published_at, url, source}|null,
  news_event_id, importance_score, affected_assets, phase}], data_incomplete: [...] }
```

`released_today` (EVR-4, config `event_risk.release_documents`): the scheduled events of the current Europe/Bucharest day that were released and for which the app holds what was published, newest first (at most `max_items`). An event is listed when its class importance is ≥ `min_importance` (60), it exposes tracked assets, and one of these exists: the **outcome** read from the stored official series (see "Release outcome" in §3; `summary_basis` "official series"), the official document of the release (a CONFIRMED news event with direct official evidence of the class source, published from `match_before_minutes` before the scheduled time to `match_after_hours` after it, whose title or summary contains a configured phrase; central-bank classes use `surprise.official_documents`; "official document"), or the calendar row's vendor `actual` ("calendar actual"). `summary` is the outcome text ("payrolls +29k (prev +133k), unemployment 4.2% (prev 4.1%)"), else the document's summary, else the actual; once a reaction window is measured (`post_event` while the event is live, the stored ticks after it resolves) `reaction` {horizon, text} is appended to it: "… · reaction T+15m: DXY +0.20%, US2Y +3.00 bp". `news_event_id` links the stored news event of the document. The entry stays after the event becomes RESOLVED and leaves `events`, until the Bucharest day ends, so the day's key release (NFP) never vanishes from the opening screen. Without anything published the event is not listed: the app does not claim a release it cannot show. A release without a consensus still records no surprise.

`GET /api/calendar` scores its rows with the same inputs as this block (display regime, news events, stored reaction history), so a row's `relevance_score`, `relevance_level` and `phase` equal the same event's on the dashboard (EVR-7).

Its `regime` block keeps per driver `{driver, direction, arrow, input, missing, inputs: [{feature, close_status, close_date}]}`. The full regime (`/api/dashboard` `regime`, `/api/regime`) gives each driver input `close_status` (`CURRENT` | `STALE` | null) and `close_date` (`YYYY-MM-DD` | null) when the input is a daily close, so the UI can mark a STALE close that still votes.

and `interpretation`:

```
{ source: 'ai'|'deterministic', model, generated_at, stale: bool, basis_hash, output: MarketInterpretationOutput }
```

Other endpoints:
- `GET /api/events/upcoming?min_relevance=0` returns the full list.
- `GET /api/event-risk` returns the same block as in the dashboard.
- `/api/assets` entries gain `event_risk` `{score, level, event_id, minutes_until, trade_status, adjusted_confidence}`.

The code adds:
- to the block: `timezone`, `regime` (the display regime with its `cause`) and `versions` (`event_risk`, `event_exposure`);
- to each `asset_event_risk` entry (and `/api/assets` `event_risk`): `phase`, `event_name`, `base_confidence`, `penalty`, `confidence_basis`, `awaiting_reopen` and `status_reason`;
- to `interpretation`: `assessments` (per-event surprise records, §8), `label`, `run_id`, `reasons` and `age_seconds`.

`/api/event-risk` and `/api/events/upcoming` answer 503 with a short reason when the event layer fails.

## 5. AI role `market_interpreter` (`backend/interpretation.py`)

The full description (schema, repair and checks, the brief the model reads, the request policy, the fallback and the stored rows) is [AI_AGENTS.md "Market interpreter"](AI_AGENTS.md#market-interpreter-backendinterpretationpy); this section keeps the contract.

- **The backend owns every structured fact**: the regime, the drivers, the favored / pressured / not-confirming lists, the events, scores, scenarios, trade statuses and the outcome of a release. The model writes a short narrative about them (prompts 3.1.0): `ai_view` (1-2 sentences, ≤ 60 words), `market_summary` (≤ 4 sentences, no percentages, prices or scores), at most 3 `key_risks` that are not what-can-break items, `dominant_event` {event_id, reason} and at most 2 `next_events` {event_id, why_it_matters, surprise_assessment, evidence_ids}. It never supplies an event, a time, a consensus, a probability, a score or a level. The served output still carries the four list keys (`favored_assets`, `pressured_assets`, `non_confirming_assets`, `trade_watch`) as empty lists for older readers.
- **Repair, then checks.** An answer is repaired against its own input (unusable sentences and items are removed) before the schema and `interpretation.violations` run: ids must be events of the input and `dominant_event` the backend's; no number that is not an input value in its unit (percentages, basis points, probabilities of that event's pricing, Bucharest clock times, spelled-out quantities); actionable wording only for an asset whose backend status is LONG or SHORT and on its side; a sentence naming a potential event says it is unverified; plain language (no identifiers, codes, event or evidence ids, provider ids, status or class codes); no countdown phrase and no unnegated certainty. A content failure is never corrected by a second request; one correction is made only for a schema-shape error, and the backup model runs only when the primary could not be reached.
- **Surprise assessments** keep the rules of §3 and §8: before release only `UNDETERMINED` or `NOT_APPLICABLE`; while `guidance_pending` is true and the headline is priced or its pricing is unknown, only `UNDETERMINED` or `NEUTRAL_PRICED`; `HAWKISH_SURPRISE` / `DOVISH_SURPRISE` need `evidence_ids` of that event's official documents; an uncited `NEUTRAL_PRICED` is stored as `UNDETERMINED`. Only a validated classification feeds `event.surprise`.
- **Input.** The model receives `interpretation.brief()` (at most 8,000 characters): the regime in plain words with its basis, confidence, coverage, moving drivers, stale closes, current drivers and divergences; at most 3 events (the dominant, the next, then released events, official documents first) with phase, Bucharest time, relevance, consensus or NO CONSENSUS, pricing or UNKNOWN, actual and outcome, surprise basis, exposed symbols and scenario lines; the lists as symbols; trade statuses; closed markets; open unscheduled and unverified events; the newest post-event entry; failing providers. Instruction-like text is removed (`screen_inputs`, redacting).
- **Requests are scarce** (the owner's daily quota): a request is made only when no stored AI text still holds (`core_hash` changed, or `basis_hash` changed and `min_ai_interval_seconds` passed), never again within `retry_failed_after_seconds` for a state whose answer was rejected, not for `retry_unavailable_after_seconds` after the provider did not answer, and within the role's daily cap (`budget.role_daily.market_interpreter`, 36); otherwise the deterministic summary of the state is stored with the reason (`ai_outcome`). The loop (`Runtime.interpretation_loop`, in-process only) ticks every `interpretation.active_seconds` while a traditional market is open, else `idle_seconds`, and at once on the event-aware triggers (`EVENT_CRITICAL_NEW`, `EVENT_WINDOW_60/15`, `EVENT_RELEASED`, `PRESS_CONFERENCE_START`, `MACRO_RELEASE`, `REGIME_CHANGE`, `LARGE_SCORE_CHANGE`, `TECHNICAL_BREAKOUT`, `FACT_STATUS_CHANGE`, `CROSS_ASSET_DIVERGENCE`, `MARKET_OPEN_CLOSE`), debounced by `min_interval_seconds`.
- **Deterministic fallback** (`deterministic_output`, `source: 'deterministic'`, label "Deterministic summary — AI unavailable"): two sentences of `ai_view` (the regime, its basis and whether today's observations confirm it; the one event to watch), a summary of the same facts said once, one `why_it_matters` sentence per event from its scenario templates (possibilities, never certainties; an unresolved asset is named without a direction), key risks only for flagged incomplete data and unconfirmed reports, dates as "Wed 30 Sep", `surprise_assessment` UNDETERMINED or NOT_APPLICABLE with `validated: false`.
- **Role assignment**: an unassigned `market_interpreter` inherits the `chief_analyst` primary and backup once at startup (`role_inherited`); an administrator who clears it keeps it cleared. Chat limits in `config/rules.d/ai_runtime.json` (300 s, one attempt, reasoning off, 4,000 output tokens); a timeout or a 429 never opens the provider circuit.
- **Storage**: table `interpretations` (`source`, `model`, `generated_at`, the hashes, `output`, `assessments`, `run_id`, `label`, `fallback_reason`, `ai_outcome`, `reasons`, `cadence_seconds`, config versions); a validated classification is carried into later runs while the event is still in the snapshot; the block is `stale` after `stale_factor` × its cadence; rows older than `retention_days` are pruned.

## 6. UI (the opening screen; docs/DASHBOARD_SIMPLIFICATION_2026-10-01.md, 2 Oct redesign)

Five sections, each fact said once (`frontend/components/dashboard/opening.ts` selects, shortens or drops text the backend sent; nothing is invented):
1. **NOW**: the regime in plain words with ONE basis tag ("live" / "from Thursday's close"), the AI VIEW or deterministic view (at most two sentences, without what the sections below state), ONE warning line (a conflict between the close-based view and today's observations, else "no current observations"), the driver chips with the kind of their observations (last close / live / via proxies).
2. **EVENTS**: the **next key event** card (name, Bucharest time and countdown, calendar impact out of 10 with the relevance beside it, consensus or "NO CONSENSUS", pricing or "UNKNOWN", the most exposed assets, phase and sub-events, EVENT DATA INCOMPLETE when flagged, one sentence of why it matters; after the release a "Published: …" line from `released_today`), the EVENT RISK MODE banner when active, and the timeline: **Coming up** lists every other scheduled event from now on, soonest first, wherever it falls relative to the key event (a date-only row stays until its release window ends), and **Latest confirmed** lists first the scheduled events released today with what was published (time, "Released <name>", the outcome text with its reaction, the exposed assets, linked to the publisher's document), then the newest confirmed facts with an identified asset impact; a fact that is the document of a released event is shown with that event only. Empty texts: "No other scheduled events in this window." and "No confirmed news with an identified asset impact yet.".
3. **MARKETS**: the compact tiles (symbol, one tag, level, change; the basis, session state and as-of line in the tooltip). A quote stamped up to 5 s after the page clock still counts as fresh.
4. **POSITIONING**: FAVORED / UNDER PRESSURE / NOT CONFIRMING with the event-risk badge, then WHAT CAN BREAK THIS VIEW (a reversal the current observations already show is not listed as a future risk).
5. **SETUPS**: at most five trade-watch cards (status chips `WAIT FOR FED`, `CONDITIONAL LONG`…, confirmation and invalidation).

Below the fold, collapsed: the POST-EVENT panel (reactions T+1m…T+1h, confirmation, MARKET DIVERGENCE; above the fold only while a measured reaction diverges), the scenario grid (an asset whose competing paths conflict is named as "No clear direction (competing effects)" without an arrow), ACTIVE EVENT RISK ("First seen" is the app's own first-seen time, never a date-only publication stamp) and POSSIBLE EVENT — UNDER VERIFICATION rows, the calendar, all news, providers and all markets.

Layout rules:
- Everything must fit 390 px without horizontal scroll.
- Times are shown in Europe/Bucharest with the zone label.
- The countdown updates every second on the client only.

## 7. Tests (spec §40, automated)

1. Risk-on, BTC favorable, FOMC in 30 min → BTC stays favored, event risk very high, `WAIT FOR FED`.
2. Risk-on, no important event within 12 h, BTC confirmed → status LONG (actionable).
3. FOMC hike 95% priced, hike as expected, press conference pending → not a hawkish surprise (validator plus deterministic basis).
4. Hike expected, statement or press conference dovish (fake LLM input) → a `DOVISH_SURPRISE` assessment is accepted; deterministic expectations for the dovish scenario apply.
5. Hot CPI (consensus injected in the test) while yields fall, DXY falls and Nasdaq rallies → `MARKET DIVERGENCE`, no automatic short.
6. Weekend Hormuz escalation → BTC interpreted live; Nasdaq, yields and FX `awaiting_reopen`.
7. Unverified X missile post → potential event only, no change to risk or trade status.
8. Apple earnings tonight → AAPL event risk high, NASDAQ high or moderate, WTI low.
9. Risk-off from a growth scare → Treasuries favored.
10. Risk-off from an inflation or oil shock → Treasuries not favored.

Plus: date-only rows never become IMMINENT; calendar dedup and replacement; the countdown is not computed by the LLM; the AI number check rejects invented numbers.

## 8. Addendum after review (binding field shapes)

- `event.consensus`:
  - either `"NO CONSENSUS"`,
  - or `{label: 'Consensus'|'Finnhub estimate', values: [{name, value, unit}], source}`.
  - `previous` is never shown as an expectation; it stays in `known_information`.
  - `consensus_of` reads the row's `consensus` (a scalar, or a dict in this shape), else Finnhub `estimates`. Any other vendor label is kept as given, never relabelled "Consensus". `source` is `consensus_source`, else `values_source` (a value lent by the calendar dedupe, §1), else the provider.
- `event.kind`: `DECISION|PRESS_CONFERENCE|MINUTES|SPEECH|TESTIMONY|PROJECTIONS|RELEASE|AUCTION|EARNINGS|EXPIRY|STATEMENT|OTHER`. A standalone sub-event row keeps its sub-event kind; otherwise the class `kind` applies.
- `event.is_decision`: bool (`kind == 'DECISION'`). The UI shows "Decision published" only for `is_decision` events.
- `event.speaker_role`: `CHAIR|VICE_CHAIR|GOVERNOR|null`. Only Chair testimony and speeches use the Chair-level classes.
  - The role is the row's own `speaker_role`, else the first `speaker_roles` title pattern (Vice Chair before Chair), else the class default.
  - `speaker_classes` then moves the event to its speaker's class: a Vice Chair to `FED_SPEECH_VICE_CHAIR` / `FED_TESTIMONY_VICE_CHAIR`, a Governor to `FED_SPEECH`.
- `mode` gains `time_utc` (and `phase`).
- `event.actual` and `event.outcome` (EVR-4): `actual` is the calendar row's actual (a vendor value, or the headline number of the attached outcome); `outcome` is `{text, period, headline, actual, previous, unit, metrics: [{name, value, previous, unit, text}], source, ingested_at}` for a row whose outcome was read from the official series (`backend/releases.py`), else null. A consensus is never derived from either.
- Post-event reaction cells: `{change, unit: 'bp'|'pct', status}`. Yields are always in bp.
- `event.surprise`: `{assessment, source: 'ai'|'none', evidence_ids: [...], validated: bool}`.
  - Post-event grading and scenario resolution use only `source 'ai'` with `validated` true and at least one evidence id of an official document (statement, minutes, press-conference transcript) that the AI actually received.
  - A deterministic fallback or an assessment without evidence is `UNDETERMINED` and never grades.
  - `event.official_documents` is filled only in RELEASED, IN_PROGRESS and POST_EVENT (`event_risk.official_documents`, config `event_risk.surprise.official_documents`). A document is evidence of a CONFIRMED news event that is `official`, direct, not contradicting, and from one of the class `sources` (Fed, ECB, BoE, BoJ).
  - It must be published from `match_before_minutes` (5) before the event time (a date-only event: its window start) to `match_after_hours` (6) after it (after its window end).
  - Its kind comes from the document title and the event summary, by the first matching `kinds` phrase list in order (PRESS_CONFERENCE_TRANSCRIPT, MINUTES, TESTIMONY, SPEECH, STATEMENT), and must be allowed for the class. At most `max_per_event` (3) are kept.
  - `classify_surprise` reads stored interpretation runs of source `ai` from the last `history_hours` (12), at most `history_rows` (200). The latest run wins that names a mapped assessment and cites at least one of the event's documents ingested before the run. It must be generated at or after the release and, unless the headline is known not to be priced, after guidance ended. A later fallback or a run without the event never removes it.
  - Every class graded here is also listed in `prompts.json` `market_interpreter.official_evidence.groups_by_class` (Vice Chair speech and testimony included since prompts 2.3.1), so the model receives the documents it must cite; `tests/test_interpretation.py` keeps the two lists aligned.
- Date-only rows anchored to a session (earnings bmo/amc, BoJ), config `event_risk.phases.date_only_sessions`:
  - The window (`date_window` `{start_utc, end_utc, label, zone}` on the event) is the class window (BOJ: the statement day in Tokyo, 00:00-24:00), else the row's session, else the publisher's local day (`country_timezones`), labelled `default_label` with the zone's name ("whole day, New York time"; `zone_names`, else the zone's city). The status note leaves that label out ("on Tue 20 Oct (time TBA)").
  - Sessions come from the row's `session` field, else from `session_patterns` in `time_basis`: `bmo` 00:00-09:30 ET, `amc` 16:00-20:00 ET (the close to the end of the extended session), `dmh` 09:30-16:00 ET. Finnhub `dmh` rows have no `session` field but match through their `time_basis`.
  - The first version of this addendum named only the boundaries (before the 09:30 ET open, after the 16:00 ET close). The code's windows are 00:00-09:30 ET and 16:00-20:00 ET.
  - The row is APPROACHING ("time TBA") from `date_only_lead_hours` (24) before the window starts until it ends. It then becomes `POST_EVENT` for the class `post_event_minutes`, then `RESOLVED`. It never becomes IMMINENT, RELEASED, IN_PROGRESS or EVENT RISK MODE.
  - `minutes_until_event` and `time_bucharest` are null for it.
  - It never stays "next event" once the window is over.
  - Exception (EVR-5): a date-only decision of a class in `surprise.official_documents.classes` (BoJ) whose official statement is already ingested (CONFIRMED official direct evidence of the bank's own source, published inside the release window) is no longer "time TBA". The statement's own `published_at` is the release time: `time_known` true, `time_basis` "official statement published at HH:MM Bucharest (source timestamp; the calendar row carried the date only)", and the event follows the normal timed timeline (IN_PROGRESS, POST_EVENT with reactions measured, surprise basis and `official_documents` from the real release) instead of waiting about 11 hours for the Tokyo day to end.
- `dashboard.interpretation` = `interpretation.latest(store)` (or `null`; a stored row that cannot be read is logged and gives `null`).
- An event-risk failure never breaks the dashboard or asset endpoints: the block is `null` plus `event_risk_error`, and every asset entry gets `event_risk: null` with the same `event_risk_error`.
- Known gap: no calendar source fills `market_pricing` yet (`event_risk.pricing_of` reads the row's `market_pricing`, and no provider writes it). Pricing is therefore UNKNOWN for every event. Pricing uncertainty uses its default (50), and `headline_priced` comes only from actual vs consensus. FOMC, ECB, BoE and BoJ (`expects_pricing`) always carry `MARKET PRICING UNKNOWN`.
