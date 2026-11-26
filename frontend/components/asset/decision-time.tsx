import type { Decision } from "@/lib/api";
import {
  ageText,
  decisionAge,
  priceSinceDecision,
  type DecisionTimingFields,
} from "@/lib/freshness";
import { levelText, signedChange, zonedTime } from "@/lib/market-data";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { signedPct } from "./signal";

type Props = {
  decision: (Decision & DecisionTimingFields) | null | undefined;
  now: number;
  /** The dashboard's verdict for this decision (lib/trade-watch decisionAge); false while unknown. */
  stale?: boolean;
  className?: string;
};

/**
 * When the published decision was made and how old it is, with STALE DECISION
 * once no analysis cycle replaced it within the service's limit. The
 * breakdown, WHY bullets and levels below it describe that moment, not the
 * live price.
 */
export function DecisionTime({ decision, now, stale, className }: Props) {
  const age = decisionAge(decision, now, stale);
  if (!age)
    return (
      <span className={cn("text-[11px] text-muted-foreground", className)}>
        Decision time not recorded
      </span>
    );
  return (
    <span
      className={cn(
        "decision-time inline-flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px]",
        age.stale ? "text-warning" : "text-muted-foreground",
        className,
      )}
    >
      <span>
        Decision as of {zonedTime(age.at)} · {ageText(age.ageMs)}
      </span>
      {age.stale && (
        <Badge
          variant="negative"
          title="The last full analysis did not replace this decision, or the analysis itself is out of date."
        >
          STALE DECISION
        </Badge>
      )}
    </span>
  );
}

/**
 * The current level against the one the decision saw: "Price at decision
 * 84,166 · now 83,442 (-0.86%)", or for a yield "Yield at decision 4.21% ·
 * now 4.25% (+4 bp)", as yield moves are quoted everywhere else; and a
 * warning once it left the decision's support-resistance range. Nothing when
 * either price is missing.
 */
export function PriceSinceDecision({
  decision,
  price,
  assetClass,
  className,
}: {
  decision: (Decision & DecisionTimingFields) | null | undefined;
  price: number | null | undefined;
  assetClass?: string | null;
  className?: string;
}) {
  const moved = priceSinceDecision(decision, price);
  if (!moved) return null;
  const bond = assetClass === "bond";
  const noun = bond ? "yield" : "price";
  const change = bond
    ? (signedChange({ change_bp: moved.changeBp })?.text ?? "Unavailable")
    : signedPct(moved.changePct);
  return (
    <p className={cn("text-[11px] text-muted-foreground", className)}>
      {bond ? "Yield" : "Price"} at decision {levelText(moved.decisionPrice, assetClass)} · now{" "}
      {levelText(price, assetClass)} ({change})
      {moved.leftRange && (
        <span className="mt-1 block text-warning">
          {moved.support !== null && (price ?? 0) < moved.support
            ? `The ${noun} is below the ${levelText(moved.support, assetClass)} support this decision saw`
            : `The ${noun} is above the ${levelText(moved.resistance, assetClass)} resistance this decision saw`}
          ; its technical reading is out of date.
        </span>
      )}
    </p>
  );
}
