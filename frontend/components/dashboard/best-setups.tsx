import { Asset } from "@/lib/api";
import { decisionAge, type AnalysisClock } from "@/lib/trade-watch";
import { StatusBadge } from "@/components/shared/status-badge";
import { NO_SETUPS_REASON } from "./opening";
import { SetupCard } from "./setup-card";

/**
 * SETUPS (§27 today's best setups): at most five actionable cards ranked by
 * |score|. With nothing actionable the section is ONE line saying so and what
 * it waits for (`waitReason`: the next key event when one is pending, else
 * the standing reason). WAIT-with-lean cards are listed separately and never
 * counted as setups. A decision that may be out of date (the analysis is
 * stale, or its last successful cycle did not replace it) shows when it was
 * made.
 */
export function BestSetups({
  candidates,
  leans,
  now,
  clock = null,
  waitReason = NO_SETUPS_REASON,
}: {
  candidates: Asset[];
  leans: Asset[];
  /** Client clock, for the decisions' age. */
  now: number;
  /** The full analysis cycle (lib/trade-watch analysisClock); null applies the fallback age. */
  clock?: AnalysisClock | null;
  /** What the empty state waits for (opening.ts noSetupsReason). */
  waitReason?: string;
}) {
  const age = (a: Asset) => decisionAge(a.decision?.created_at, now, clock);
  return (
    <section aria-labelledby="best-setups-heading">
      {candidates.length ? (
        <>
          <div className="section-title">
            <div>
              <h2 id="best-setups-heading">Today&apos;s best setups</h2>
              <p>
                Only evidence-backed decisions clear the gates. Strongest first.
              </p>
            </div>
            <StatusBadge value={`${candidates.length} setups`} />
          </div>
          <div className="setups">
            {candidates.map((a) => (
              <SetupCard key={a.symbol} asset={a} decided={age(a)} />
            ))}
          </div>
        </>
      ) : (
        <div className="setups-wait">
          <h2 id="best-setups-heading">Today&apos;s best setups</h2>
          <div className="setups-wait-line">
            <h3>No actionable setups</h3>
            <p>— {waitReason}.</p>
          </div>
          <StatusBadge value="WAIT" />
        </div>
      )}
      {leans.length > 0 && (
        <section aria-labelledby="lean-heading" className="mt-6">
          <div className="mb-3.5">
            <h3 id="lean-heading">WAIT — leaning</h3>
            <p className="mt-1 text-xs text-muted-foreground">
              Directional lean without enough confirmation to act. Not a setup.
            </p>
          </div>
          <div className="setups grid-cols-[repeat(auto-fill,minmax(270px,1fr))]!">
            {leans.map((a) => (
              <SetupCard key={a.symbol} asset={a} lean decided={age(a)} />
            ))}
          </div>
        </section>
      )}
    </section>
  );
}
