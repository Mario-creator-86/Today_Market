import type { Alert, Quote, Reaction } from "@/lib/api";

/**
 * Response shapes read by the section pages. Fields marked optional may be
 * absent while the backend is extended; pages render them only when present
 * and never substitute invented values.
 */

/** GET /api/fx row. `yield_differential`/`next_meeting` are the current keys. */
export type FxRow = {
  pair?: string;
  symbol: string;
  price?: number | null;
  /** The pair's quote with its provenance (data_class, close_status, as_of_label, provider). */
  quote?: Quote | null;
  bias?: string | null;
  /** Fed policy rate (the FOMC target range midpoint). */
  fed_rate?: number | null;
  /** [lower, upper] FOMC target range, when the backend sends it. */
  fed_rate_range?: number[] | null;
  /** How the Fed rate is derived, e.g. "FOMC target range midpoint (FRED DFEDTARL/DFEDTARU)". */
  fed_rate_basis?: string | null;
  other_rate?: number | null;
  /** Id (config decision.fx_banks, e.g. "ecb") or display name of the non-Fed central bank. */
  other_bank?: string | null;
  /** Display name of the non-Fed central bank, e.g. "ECB" (config central_bank_meetings short name). */
  other_bank_name?: string | null;
  other_rate_basis?: string | null;
  policy_differential?: number | null;
  yield_differential?: number | null;
  yield_2y_differential?: number | null;
  /** Change in expected policy (base minus quote); null while a rate expectation is missing. */
  repricing_differential?: number | null;
  /** The relative-policy verdict, e.g. "Both central banks, expectations and yields are required". */
  relative_basis?: string | null;
  /** Soonest next rate decision of the pair: text or {bank, bank_name, name, scheduled_at, time_known}. */
  next_meeting?: unknown;
  /** Each bank's next rate decision: {bank, bank_name, name, scheduled_at, time_known, reason}; reason when unknown. */
  next_meetings?: unknown[] | null;
  next_meeting_expectation?: unknown;
  confirmation?: string | null;
  invalidation?: string | null;
  reason?: string | null;
  as_of?: string | null;
};

/** Contract Reaction (docs/CONTRACTS.md); single definition lives in lib/api. */
export type { Reaction };

/** One implication of a calendar scenario, e.g. {asset: "USD", direction: -1}. */
export type ScenarioImplication = {
  asset: string;
  direction?: -1 | 0 | 1 | null;
  text?: string | null;
};

/** Deterministic calendar scenario band around consensus. */
export type CalendarScenario = {
  id?: string;
  label?: string | null;
  /** Human condition such as "≤ 2.6". */
  condition?: string | null;
  lower?: number | null;
  upper?: number | null;
  interpretation?: string | null;
  implications?: ScenarioImplication[] | Record<string, unknown> | null;
};

/** Release window of a date-only row (event_risk.date_window). */
export type DateWindow = {
  start_utc?: string | null;
  end_utc?: string | null;
  /** e.g. "after the US close", "publisher local day". */
  label?: string | null;
  zone?: string | null;
};

export type CalendarItem = {
  id?: string;
  name?: string;
  country?: string | null;
  scheduled_utc?: string;
  scheduled_at?: string;
  impact?: number | string | null;
  /** A scalar consensus, the §8 shape {label, values, source}, or "NO CONSENSUS". */
  consensus?: number | string | Record<string, unknown> | null;
  /** Vendor estimates kept apart from consensus, e.g. {label: "Finnhub estimate", values, note}. */
  estimates?: Record<string, unknown> | null;
  category?: string | null;
  /** §8 kind: DECISION, SPEECH, RELEASE, EARNINGS, … */
  kind?: string | null;
  is_decision?: boolean | null;
  /** "Source: U.S. Bureau of Economic Analysis". */
  attribution?: string | null;
  source_url?: string | null;
  /** source | config | rule | none. */
  time_source?: string | null;
  date_window?: DateWindow | null;
  previous?: number | string | null;
  actual?: number | string | null;
  unit?: string | null;
  surprise?: number | null;
  raw_surprise?: number | null;
  surprise_z_score?: number | null;
  z_score?: number | null;
  scenarios?: CalendarScenario[] | null;
  reactions?: Reaction[] | null;
  reaction?: Reaction[] | null;
  provider?: string | null;
  /* Event-awareness extras (docs/EVENT_AWARENESS.md §1, §3); all optional, shown when the backend sends them. */
  event_class?: string | null;
  /** False for date-only rows: "Time TBA", never a clock or a countdown. */
  time_known?: boolean | null;
  time_basis?: string | null;
  /** SCHEDULED | APPROACHING | IMMINENT | RELEASED | IN_PROGRESS | POST_EVENT | RESOLVED. */
  phase?: string | null;
  relevance_level?: string | null;
  relevance_score?: number | null;
};

/** GET /api/sources row. */
export type SourceRecord = {
  id: string;
  name?: string;
  domain?: string | null;
  kind?: string | null;
  tier?: number | null;
  trust_weight?: number | null;
  official?: boolean | null;
  independence_group?: string | null;
  rss?: string | null;
  /** The source's X account (X sources), e.g. "@federalreserve". */
  x_account?: string | null;
  /** Live provider status of the source, e.g. "ONLINE". */
  status?: string | null;
  last_success?: string | null;
};

/** Evidence row inside GET /api/events items. */
export type EvidenceRow = {
  id: string;
  source_id?: string;
  source?: string;
  url?: string | null;
  title?: string | null;
  published_at?: string | null;
  ingested_at?: string | null;
  relation?: "supports" | "contradicts" | string;
  group?: string | null;
  weight?: number | null;
  official?: boolean | null;
  discovery_only?: boolean | null;
};

/** GET /api/models row (backend/ai.py discover). */
export type AiModel = {
  name: string;
  display_name?: string;
  capabilities?: string[] | null;
  tools_supported?: boolean | null;
  thinking_supported?: boolean | null;
  context_window?: number | null;
  availability?: boolean | null;
  last_seen?: string | null;
  status?: string | null;
};

/** evaluation.summarize() output. */
export type MetricSummary = {
  /** Measured directional outcomes: published signals plus counterfactual ones. */
  observations?: number;
  /** Of those, outcomes of published (actionable) signals. */
  actionable?: number;
  /** Of those, counterfactual outcomes: WAIT and lean decisions measured on the sign of their score. */
  counterfactual?: number;
  /** Every measured outcome of the row is counterfactual. */
  counterfactual_only?: boolean | null;
  /**
   * The SIGNAL hit rate: over published signals only, null without one. (An
   * older service sent one rate over every measured outcome and no
   * counterfactual_hit_rate; page-logic hitRates reads both shapes.)
   */
  hit_rate?: number | null;
  /** "published" when hit_rate has a population behind it, else null. */
  hit_rate_basis?: string | null;
  /** What following the score sign of WAIT and lean decisions would have returned; null without such an outcome. */
  counterfactual_hit_rate?: number | null;
  mean_return_pct?: number | null;
  median_return_pct?: number | null;
  mean_mfe_pct?: number | null;
  mean_mae_pct?: number | null;
  brier_score?: number | null;
  calibration_error?: number | null;
  /** Failed published signals / published signals; null without a published signal. */
  false_positive_rate?: number | null;
  max_drawdown_return_points?: number | null;
  forecast_observations?: number;
};

/** evaluation._paper_metrics() output. */
export type PaperMetrics = {
  closed_trades?: number;
  open_trades?: number;
  hit_rate?: number | null;
  mean_return_pct?: number | null;
  median_return_pct?: number | null;
  profit_factor?: number | null;
  max_drawdown_return_points?: number | null;
  mean_mfe_pct?: number | null;
  mean_mae_pct?: number | null;
  total_pnl?: number | null;
  equity?: number | null;
  max_drawdown_equity_pct?: number | null;
  basis?: string;
};

export type PaperTrade = {
  id?: string;
  decision_id?: string;
  symbol: string;
  side?: 1 | -1 | number;
  direction?: string;
  status?: string;
  opened_at?: string;
  closed_at?: string | null;
  entry_price?: number | null;
  exit_price?: number | null;
  stop?: number | null;
  target?: number | null;
  exit_reason?: string | null;
  return_pct?: number | null;
  mfe_pct?: number | null;
  mae_pct?: number | null;
};

/** GET /api/performance (backend evaluation.performance). */
export type PerformanceReport = {
  /**
   * Decisions with a measured directional outcome (a side and a signed return),
   * the rows every metric is computed from. An older payload without
   * decisions_recorded sent the count of decisions with any outcome row here.
   */
  observations?: number;
  /** Decisions with any recorded outcome row, including no-side (WAIT) decisions. */
  decisions_recorded?: number;
  /** Explicit directional count (the same number as observations). */
  directional_observations?: number;
  /** Decisions measured as published signals. */
  published_signals?: number | null;
  /** Decisions measured as counterfactuals (WAIT and lean decisions on the sign of their score). */
  counterfactual_decisions?: number | null;
  /** On the report it is a sentence saying what every hit_rate covers. */
  hit_rate_basis?: string | null;
  probability?: number | null;
  status?: string;
  population?: string;
  /** The horizon of the breakdowns (echoes a requested horizon). */
  breakdown_horizon?: string;
  /** The configured report horizon, independent of the request. */
  default_horizon?: string;
  dimensions?: string[];
  horizons?: string[];
  paper_trades?: PaperTrade[];
  metrics?: Record<string, MetricSummary | PaperMetrics> & {
    paper?: PaperMetrics;
  };
  breakdowns?: Record<string, Record<string, MetricSummary>>;
};

/**
 * Contract Alert (docs/CONTRACTS.md) as read defensively from GET /api/alerts.
 * `label` is the provider's display name on PROVIDER alerts (e.g. "Finnhub (equities)").
 */
export type AlertItem = Partial<Omit<Alert, "id">> & { id: string; label?: string | null };

/** Expected GET /api/social row (X posts with derived display status). */
export type SocialPost = {
  id: string;
  text?: string;
  author?: string | null;
  author_username?: string | null;
  author_name?: string | null;
  created_at?: string | null;
  url?: string | null;
  verification_status?: string | null;
  event_id?: string | null;
  official?: boolean | null;
  /** Development stub record (PROVIDER_MODE=stub); never a real post. */
  stub?: boolean | null;
};
