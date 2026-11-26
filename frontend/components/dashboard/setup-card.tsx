"use client";
import Link from "next/link";
import { Info } from "lucide-react";
import { Asset, Decision, number } from "@/lib/api";
import { Card } from "@/components/ui/card";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { StatusBadge } from "@/components/shared/status-badge";
import { horizonLabel } from "@/lib/market-data";
import type { DecisionAge } from "@/lib/trade-watch";
import { cn } from "@/lib/utils";
import { percent } from "./selectors";

/** "64% · n=418 · horizon 4 hours" (worded like the asset page), or NOT ENOUGH DATA (with n when known). */
export function historicalText(d: Decision | null): string {
  const h = d?.historical_probability;
  if (h && typeof h.p === "number" && Number.isFinite(h.p)) {
    const parts = [`${number(h.p * 100, 0)}%`];
    if (typeof h.n === "number") parts.push(`n=${number(h.n, 0)}`);
    if (h.horizon) parts.push(`horizon ${horizonLabel(h.horizon).toLowerCase()}`);
    return parts.join(" · ");
  }
  return typeof h?.n === "number"
    ? `NOT ENOUGH DATA · n=${number(h.n, 0)}`
    : "NOT ENOUGH DATA";
}

function why(d: Decision | null): string[] {
  if (!d) return [];
  if (d.why?.length) return d.why;
  if (d.explanation?.reasons?.length) return d.explanation.reasons;
  return [];
}

function level(label: string, value: unknown): string | null {
  return typeof value === "number" && Number.isFinite(value)
    ? `${label} ${number(value, 5)}`
    : null;
}

/**
 * Setup card (§27): category, decision score, evidence confidence, market
 * confirmation, historical probability, WHY, CONFIRMATION and INVALIDATION.
 * Actionable cards carry the `.setup` hook; lean cards use `.lean-setup`.
 */
export function SetupCard({
  asset,
  lean = false,
  decided = null,
}: {
  asset: Asset;
  lean?: boolean;
  /** When the decision may be out of date: its time, age and why (lib/trade-watch decisionAge). */
  decided?: DecisionAge | null;
}) {
  const d = asset.decision;
  const label = lean
    ? d?.direction
    : d?.category && !/WAIT/.test(d.category)
      ? d.category
      : d?.direction;
  const bullets = why(d);
  const confirmation =
    d?.confirmation || d?.explanation?.confirmation || "Not established";
  const invalidation =
    d?.invalidation || d?.explanation?.invalidation || "Not established";
  const confirmationLevel = level("Level", d?.confirmation_level);
  const invalidationLevel = level("Level", d?.invalidation_level);
  const hist = historicalText(d);
  return (
    <Card asChild className={cn(lean ? "lean-setup" : "setup", "p-5")}>
      <article aria-label={`${asset.symbol} ${label ?? "WAIT"}`}>
        <div className="setup-head">
          <Link href={`/assets/${encodeURIComponent(asset.symbol)}`}>
            <strong>{asset.symbol}</strong>
            <span>{asset.name}</span>
          </Link>
          <StatusBadge
            value={label || "WAIT"}
            variant={
              lean
                ? "outline"
                : /SHORT/.test(label ?? "")
                  ? "negative"
                  : /LONG/.test(label ?? "")
                    ? "positive"
                    : undefined
            }
          />
        </div>
        <div className="score">
          <strong>{d ? number(d.score, 0) : "—"}</strong>
          <span>
            Decision score
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  className="ml-1 inline-flex cursor-help align-[-2px] text-muted-foreground hover:text-foreground"
                  aria-label="About the decision score"
                >
                  <Info size={12} aria-hidden="true" />
                </button>
              </TooltipTrigger>
              <TooltipContent>
                A -100 to +100 ranking of evidence strength and market
                confirmation. It is not a probability of success.
              </TooltipContent>
            </Tooltip>
            <small>Not a probability</small>
          </span>
        </div>
        {decided && (
          <p className="setup-decided mt-2 text-[11px] text-warning" title={decided.reason}>
            <time dateTime={decided.at}>{decided.text}</time>
          </p>
        )}
        <div className="metrics">
          <div>
            Evidence confidence
            <strong>{percent(d?.evidence_confidence)}</strong>
          </div>
          <div>
            Market confirmation
            <strong>{percent(d?.market_confirmation)}</strong>
          </div>
        </div>
        <h3 className="mt-4 text-[10px] font-semibold tracking-wider text-muted-foreground">
          {lean ? "WHY IT LEANS" : "WHY"}
        </h3>
        <ul className="reasons mt-1.5!">
          {bullets.length ? (
            bullets.slice(0, 5).map((r, i) => <li key={i}>{r}</li>)
          ) : (
            <li className="text-muted-foreground">
              No verified WHY bullets were supplied.
            </li>
          )}
        </ul>
        {lean && d?.reasons?.length ? (
          <>
            <h3 className="text-[10px] font-semibold tracking-wider text-muted-foreground">
              NOT YET A SETUP
            </h3>
            <ul className="reasons mt-1.5!">
              {(Array.isArray(d.reasons) ? d.reasons : []).slice(0, 3).map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </>
        ) : null}
        <dl className="decision-details">
          <dt>CONFIRMATION</dt>
          <dd>
            {confirmation}
            {confirmationLevel && (
              <span className="block text-muted-foreground">
                {confirmationLevel}
              </span>
            )}
          </dd>
          <dt>INVALIDATION</dt>
          <dd>
            {invalidation}
            {invalidationLevel && (
              <span className="block text-muted-foreground">
                {invalidationLevel}
              </span>
            )}
          </dd>
        </dl>
        <div className="setup-footer">
          Historical probability{" "}
          <span className={cn(!hist.startsWith("NOT") && "text-foreground!")}>
            {hist}
          </span>
        </div>
      </article>
    </Card>
  );
}
