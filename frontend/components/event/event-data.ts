import type {
  CausalPath,
  Extraction,
  MarketEvent,
  Reaction,
  RecordData,
} from "@/lib/api";
import { formatFactConfidence } from "@/lib/events";
import type { BadgeVariant } from "@/components/ui/badge";
import { isNum, signed, signedPct } from "@/components/asset/signal";

export const WINDOW_ORDER = [
  "1m",
  "5m",
  "15m",
  "30m",
  "1h",
  "4h",
  "close",
  "next_session",
];

export function sortHorizons(list: Iterable<string>): string[] {
  const rank = (h: string) => {
    const i = WINDOW_ORDER.indexOf(h);
    return i < 0 ? WINDOW_ORDER.length : i;
  };
  return [...new Set(list)].sort((a, b) => rank(a) - rank(b));
}

/** Fact confidence for display, at one precision on every page (one decimal, rounded down). STALE evidence is
 * outside the freshness window: the number is 0 by rule, which would read as "false", so say "not current". */
export function factConfidenceLabel(
  event: Pick<MarketEvent, "verification_status" | "fact_confidence">,
): string {
  if (event.verification_status === "STALE")
    return "not current (evidence outside the freshness window)";
  return formatFactConfidence(event.fact_confidence);
}

export function str(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

export function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string" && !!v.trim())
    : [];
}

export function isContradicting(item: RecordData): boolean {
  const relation = String(item.relation ?? item.stance ?? "").toLowerCase();
  return relation === "contradicts" || relation === "contradicted";
}

/** "Direct", "Second-hand" or undefined when the source does not say. */
export function directness(item: RecordData): string | undefined {
  if (item.direct === true) return "Direct";
  if (item.direct === false || item.relation === "second_hand")
    return "Second-hand";
  return undefined;
}

/** Trust tier label when the API provides one; never inferred from weight. */
export function trustTier(item: RecordData): string | undefined {
  const tier = item.trust_tier ?? item.source_tier ?? item.tier;
  return typeof tier === "string" || isNum(tier) ? String(tier) : undefined;
}

export function extraction(event: MarketEvent): Extraction | null {
  const raw = event.extraction;
  return raw && typeof raw === "object" ? (raw as Extraction) : null;
}

/** "+4.20%" or "+7.0 bp" for a measured reaction. */
export function formatMove(r: Reaction | undefined): string {
  if (!r) return "Not measured";
  const value = isNum(r.change) ? r.change : r.change_pct;
  if (!isNum(value)) return "Not measured";
  return r.unit === "bp" ? `${signed(value, 1, true)} bp` : signedPct(value);
}

export function reactionTone(status: string | undefined): BadgeVariant {
  switch (status) {
    case "CONFIRMED":
      return "positive";
    case "PARTIAL":
      return "info";
    case "DIVERGENT":
    case "REVERSED":
      return "negative";
    default:
      return "outline";
  }
}

export function arrow(direction: number | null | undefined): string {
  return direction === 1 ? "↑" : direction === -1 ? "↓" : "→";
}

export function directionWord(direction: number | null | undefined): string {
  return direction === 1 ? "up" : direction === -1 ? "down" : "no change";
}

/** Net expected sign per symbol across active causal paths. */
export function expectedEffects(
  paths: CausalPath[] | null | undefined,
): Record<string, number> {
  const net: Record<string, number> = {};
  for (const p of paths ?? []) {
    if (p.active === false) continue;
    for (const [symbol, sign] of Object.entries(p.effects ?? {}))
      net[symbol] =
        (net[symbol] ?? 0) + sign * (isNum(p.strength) ? p.strength : 1);
  }
  return Object.fromEntries(
    Object.entries(net).map(([s, v]) => [s, v > 0 ? 1 : v < 0 ? -1 : 0]),
  );
}

export function reactionIndex(
  reactions: Reaction[] | undefined,
): Map<string, Reaction> {
  const map = new Map<string, Reaction>();
  for (const r of reactions ?? []) map.set(`${r.symbol}:${r.horizon}`, r);
  return map;
}
