import type { Asset, Decision } from "@/lib/api";
import type { DecisionTimingFields } from "@/lib/freshness";
import { Card } from "@/components/ui/card";
import { StatusBadge } from "@/components/shared/status-badge";
import { DecisionTime } from "./decision-time";
import { probabilityLabel, signalLabel, signalTone } from "./signal";

/**
 * Setup card of the asset page: the published signal (the same label as the
 * summary), when it was decided, the WHY bullets and the historical
 * probability. Score, evidence, confirmation and invalidation are shown once,
 * in the summary and the "Confirmation & invalidation" panel. The `.setup`
 * class is a test hook.
 */
export function DecisionCard({
  asset,
  now,
  stale,
}: {
  asset: Asset;
  now: number;
  /** The dashboard's verdict for this decision (lib/trade-watch decisionAge); false while unknown. */
  stale?: boolean;
}) {
  const d = asset.decision as (Decision & DecisionTimingFields) | null;
  const why = d?.why?.length ? d.why : d?.reasons;
  // When verified WHY bullets exist, the first blocking reason is shown separately.
  const heldBack =
    d?.why?.length && d.direction === "WAIT" ? d.reasons?.[0] : undefined;
  const probability = probabilityLabel(d?.historical_probability);
  const signal = signalLabel(d) ?? "WAIT";
  return (
    <Card asChild className="setup p-5">
      <article aria-label={`${asset.symbol} setup`}>
        <div className="setup-head">
          <div>
            <strong>{asset.symbol}</strong>
            <span className="block text-[11px] text-muted-foreground">{asset.name}</span>
          </div>
          <StatusBadge value={signal} variant={signalTone(signal)} />
        </div>
        {d && (
          <DecisionTime
            decision={d}
            now={now}
            stale={stale}
            className="mt-3"
          />
        )}
        <ul className="reasons">
          {why?.length ? (
            why.slice(0, 3).map((r, i) => <li key={i}>{r}</li>)
          ) : (
            <li>
              Waiting for sufficient verified evidence and fresh market data.
            </li>
          )}
        </ul>
        {heldBack && (
          <p className="mt-2 text-xs text-warning">Held back: {heldBack}</p>
        )}
        <div className="setup-footer gap-3">
          Historical probability{" "}
          <span className="text-right" title={probability.title}>
            {probability.value}
            {probability.detail && ` · ${probability.detail}`}
          </span>
        </div>
      </article>
    </Card>
  );
}
