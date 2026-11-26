import { MarketEvent } from "@/lib/api";
import { directionLabel, interpretationModel } from "@/lib/events";
import { labelCase } from "@/lib/format";
import type { BadgeVariant } from "@/components/ui/badge";
import { DetailList } from "@/components/shared/detail-list";
import { EmptyState } from "@/components/shared/empty-state";
import { StatusBadge } from "@/components/shared/status-badge";
import { AnnotatedText, EvidenceRefs } from "./annotated-text";

/** Specialist roles (backend schemas.ROLE_SCHEMAS) by name. */
const ROLE_NAMES: Record<string, string> = {
  macro: "macro",
  fx: "FX",
  crypto: "crypto",
  equity_credit: "equity and credit",
  geopolitical_energy: "geopolitics and energy",
};

function directionTone(direction: string): BadgeVariant {
  const value = direction.toLowerCase();
  return value === "positive"
    ? "positive"
    : value === "negative"
      ? "negative"
      : "outline";
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-3 mb-1 text-[11px] uppercase tracking-wide text-muted-foreground">
      {children}
    </p>
  );
}

/**
 * AI INTERPRETATION: the specialist's hypotheses (or FX pairs, crypto assets,
 * developments) as cards with direction, causal path, confirmation and
 * invalidation; uncertainties as a list. Cited evidence ids become numbered
 * links to the evidence cards. Never raw JSON.
 */
export function InterpretationView({
  event,
  numbers,
}: {
  event: MarketEvent;
  numbers: Map<string, number>;
}) {
  const model = interpretationModel(event.interpretation);
  if (!model)
    return (
      <EmptyState
        title="Interpretation not available"
        detail="Causal analysis requires verified evidence and observed market reaction."
      />
    );
  const role = (event as { interpretation_role?: unknown }).interpretation_role;
  const other = Object.keys(model.other).length > 0;
  return (
    <div className="grid gap-4 p-5 text-sm">
      {model.heading && (
        <section aria-label={model.heading}>
          <h3 className="text-sm font-semibold">{model.heading}</h3>
          {model.cards.length ? (
            <ul className="m-0 mt-3 grid list-none gap-3 p-0 md:grid-cols-2">
              {model.cards.map((card, i) => (
                <li
                  key={`${card.title}-${i}`}
                  className="min-w-0 rounded-md border border-border bg-secondary/40 p-4"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <strong className="min-w-0 wrap-break-word">
                      <AnnotatedText text={card.title} numbers={numbers} />
                    </strong>
                    {card.direction && (
                      <StatusBadge
                        value={directionLabel(card.direction).toUpperCase()}
                        variant={directionTone(card.direction)}
                      />
                    )}
                  </div>
                  {card.facts.map((fact) => (
                    <p key={fact.label} className="mt-2">
                      <span className="text-muted-foreground">{fact.label}: </span>
                      <AnnotatedText text={fact.value} numbers={numbers} />
                    </p>
                  ))}
                  {card.path.length > 0 && (
                    <>
                      <Label>Causal path</Label>
                      <ol className="m-0 list-decimal space-y-1 pl-5">
                        {card.path.map((step, j) => (
                          <li key={j}>
                            <AnnotatedText text={step} numbers={numbers} />
                          </li>
                        ))}
                      </ol>
                    </>
                  )}
                  {card.lists.map((list) => (
                    <div key={list.label}>
                      <Label>{list.label}</Label>
                      <ul className="m-0 list-disc space-y-1 pl-5">
                        {list.items.map((item, j) => (
                          <li key={j}>
                            <AnnotatedText text={item} numbers={numbers} />
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                  {card.confirmation && (
                    <>
                      <Label>Confirmed if</Label>
                      <p>
                        <AnnotatedText text={card.confirmation} numbers={numbers} />
                      </p>
                    </>
                  )}
                  {card.invalidation && (
                    <>
                      <Label>Invalidated if</Label>
                      <p>
                        <AnnotatedText text={card.invalidation} numbers={numbers} />
                      </p>
                    </>
                  )}
                  <EvidenceRefs ids={card.evidenceIds} numbers={numbers} />
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-muted-foreground">
              The specialist proposed none for this event.
            </p>
          )}
        </section>
      )}
      <section aria-label="Uncertainties">
        <h3 className="text-sm font-semibold">Uncertainties</h3>
        {model.uncertainties.length ? (
          <ul className="m-0 mt-2 list-disc space-y-1 pl-5">
            {model.uncertainties.map((item, i) => (
              <li key={i}>
                <AnnotatedText text={item} numbers={numbers} />
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-muted-foreground">None stated.</p>
        )}
      </section>
      {other && (
        <div className="-mx-5">
          <DetailList data={model.other} />
        </div>
      )}
      <p className="text-[11px] text-muted-foreground">
        AI interpretation
        {typeof role === "string" && role.trim()
          ? ` by the ${ROLE_NAMES[role] ?? labelCase(role)} specialist`
          : ""}
        . Hypotheses only: they never change the verified fact or its
        confidence.
      </p>
    </div>
  );
}
