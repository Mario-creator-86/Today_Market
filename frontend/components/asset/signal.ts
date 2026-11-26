/*
 * Signal labels and signed numbers of the asset page. Its runtime imports are
 * relative (vitest does not resolve "@/"), so lib/market-data.test.ts can
 * collect it.
 */
import type { Decision, HistoricalProbability } from "@/lib/api";
import { number } from "../../lib/api";
import { isNum } from "../../lib/format";
import { horizonLabel } from "../../lib/market-data";

export { isNum };

/**
 * "+8", "-18.5", "0". With `fixed`, always shows `digits` decimals ("+0.80").
 * Missing values render as "Unavailable".
 */
export function signed(value: unknown, digits = 1, fixed = false): string {
  if (!isNum(value)) return "Unavailable";
  const rounded = Number(value.toFixed(digits));
  const body = Math.abs(rounded).toLocaleString("en-US", {
    minimumFractionDigits: fixed ? digits : 0,
    maximumFractionDigits: digits,
  });
  return rounded > 0 ? `+${body}` : rounded < 0 ? `-${body}` : body;
}

/** Signed percentage with fixed decimals, e.g. "+1.24%". */
export function signedPct(value: unknown, digits = 2): string {
  return isNum(value) ? `${signed(value, digits, true)}%` : "Unavailable";
}

/** Tone for a signed number: positive, negative or neutral (zero or missing). */
export function signTone(value: unknown): "positive" | "negative" | "neutral" {
  return !isNum(value) || value === 0
    ? "neutral"
    : value > 0
      ? "positive"
      : "negative";
}

export const toneText = {
  positive: "text-positive",
  negative: "text-destructive",
  neutral: "text-muted-foreground",
} as const;

function side(label: string): number {
  return /LONG/.test(label) ? 1 : /SHORT/.test(label) ? -1 : 0;
}

/**
 * The signal to display. The contract category ("LONG SETUP", "BEARISH LEAN /
 * WAIT") is preferred, but when the effective direction was downgraded to WAIT
 * (stale quote, closed market, audit block) a SETUP category is no longer
 * shown, so a cached setup never survives a freshness failure.
 */
export function signalLabel(
  d: Decision | null | undefined,
): string | undefined {
  if (!d) return undefined;
  const direction = d.direction || "WAIT";
  const category = d.category;
  if (!category) return direction;
  if (side(direction) === 0 && /SETUP/.test(category) && side(category) !== 0)
    return direction;
  return category;
}

/** Badge tone for a signal: setups and plain directions coloured, leans and waits neutral. */
export function signalTone(
  label: string | null | undefined,
): "positive" | "negative" | "neutral" {
  if (!label || /WAIT|LEAN/.test(label)) return "neutral";
  return side(label) > 0
    ? "positive"
    : side(label) < 0
      ? "negative"
      : "neutral";
}

/**
 * Backend `historical_probability`: when not calibrated it also names its
 * minimum and reason; `n_basis` says what n counts (backend calibration).
 */
type ProbabilityInfo = HistoricalProbability & {
  minimum?: number | null;
  reason?: string | null;
  n_basis?: string | null;
};

/** "horizon 4 hours", "horizon next session": the same words in both forms of the label. */
function horizonText(horizon: string | null | undefined): string | null {
  return typeof horizon === "string" && horizon.trim()
    ? `horizon ${horizonLabel(horizon.trim()).toLowerCase()}`
    : null;
}

/**
 * "64% · n=418 · horizon 4 hours" when calibrated (`title` then says what n
 * counts). Otherwise "Not enough data" with why (the service's reason), the
 * horizon and how many comparable past decisions there are against the
 * minimum.
 */
export function probabilityLabel(
  hp: ProbabilityInfo | null | undefined,
): { value: string; detail?: string; title?: string } {
  const basis = typeof hp?.n_basis === "string" ? hp.n_basis.trim() : "";
  if (hp && isNum(hp.p))
    return {
      value: `${number(hp.p * 100, 0)}%`,
      detail: [isNum(hp.n) ? `n=${hp.n}` : null, horizonText(hp.horizon)]
        .filter(Boolean)
        .join(" · "),
      title: basis && isNum(hp.n) ? `n: ${basis}` : undefined,
    };
  const reason = typeof hp?.reason === "string" ? hp.reason.trim() : "";
  const counted = basis || `comparable past decision${hp?.n === 1 ? "" : "s"}`;
  const detail = [
    reason ? reason.charAt(0).toLowerCase() + reason.slice(1) : null,
    horizonText(hp?.horizon),
    hp && isNum(hp.n)
      ? `${hp.n} ${counted}${isNum(hp.minimum) ? ` (minimum ${hp.minimum})` : ""}`
      : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return { value: "Not enough data", detail: detail || undefined };
}
