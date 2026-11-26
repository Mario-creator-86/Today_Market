import { ChevronDown } from "lucide-react";
import type { EventRisk, EventScenario, Interpretation } from "@/lib/api";
import {
  direction,
  distinctRisks,
  findEvent,
  interpretationSource,
  scenarioCondition,
  scenarioSets,
  eventSurpriseView,
  staleText,
  unresolvedSymbols,
  type ScenarioSet,
} from "@/lib/event-risk";
import { eventTimeLabel } from "@/lib/event-time";
import { localTime } from "@/lib/api";
import { APP_ZONE } from "@/lib/market-data";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

const CASES: [keyof NonNullable<ScenarioSet["explanation"]>, string][] = [
  ["why_it_matters", "Why it matters"],
  ["bull_case", "Bull case"],
  ["bear_case", "Bear case"],
  ["neutral_case", "Neutral case"],
  ["what_is_priced", "What is priced"],
  ["what_is_not_priced", "What is not priced"],
  ["confirmation_after_event", "Confirmation after the event"],
];

function Expected({ scenario }: { scenario: EventScenario }) {
  const entries = Object.entries(scenario.expected ?? {})
    .map(([symbol, v]) => ({ symbol, d: direction(v) }))
    .filter((x) => x.d);
  // Assets whose competing effects leave no net direction: named, never given an arrow.
  const unresolved = unresolvedSymbols(scenario);
  if (!entries.length && !unresolved.length) return null;
  return (
    <>
      {entries.length > 0 && (
        <p className="scenario-arrows">
          {entries.map(({ symbol, d }) => (
            <span key={symbol}>
              {symbol}
              <b aria-hidden="true">{d!.arrow}</b>
              <span className="sr-only"> {d!.word}</span>
            </span>
          ))}
        </p>
      )}
      {unresolved.length > 0 && (
        <p className="text-muted-foreground">No clear direction (competing effects): {unresolved.join(", ")}</p>
      )}
    </>
  );
}

function Scenarios({ set, now }: { set: ScenarioSet; now: number }) {
  const x = set.explanation;
  // Only the texts the interpretation carries (the short output writes "why it matters" alone): no empty list.
  const cases = x ? CASES.filter(([k]) => typeof x[k] === "string" && !!x[k]?.trim()) : [];
  const surprise = eventSurpriseView(set.event, x?.surprise_assessment);
  return (
    <details className="scenario-set">
      <summary>
        <ChevronDown size={15} aria-hidden="true" className="scenario-chevron" />
        <span className="min-w-0 flex-1">
          <b>{set.event.name}</b> · {eventTimeLabel(set.event, now)}
        </span>
        {surprise && <Badge variant={surprise.tone}>{surprise.text}</Badge>}
      </summary>
      <div className="scenario-body">
        {x && cases.length > 0 && (
          <dl className="scenario-cases">
            {cases.map(([k, label]) => (
              <div key={k}>
                <dt>{label}</dt>
                <dd>{x[k]}</dd>
              </div>
            ))}
          </dl>
        )}
        {set.scenarios.length > 0 && (
          <ol className="scenario-grid" aria-label={`${set.event.name} possible scenarios`}>
            {set.scenarios.map((s, i) => {
              const condition = scenarioCondition(s);
              return (
                <li key={s.id ?? i}>
                  <strong>
                    {s.id ? `Scenario ${s.id}` : `Scenario ${i + 1}`}
                    {s.label ? ` · ${s.label}` : ""}
                  </strong>
                  {condition && <p>{condition}</p>}
                  <Expected scenario={s} />
                  {s.note && <p className="text-muted-foreground">{s.note}</p>}
                </li>
              );
            })}
          </ol>
        )}
        <p className="scenario-caption">Scenarios are possibilities, not certainties.</p>
      </div>
    </details>
  );
}

/**
 * §6 AI MARKET INTERPRETATION: the stored summary (AI, or the deterministic
 * fallback labelled "Deterministic summary — AI unavailable"), the dominant
 * event named from backend data, key risks (without those already listed
 * under "What can break this view"), and collapsible per-event scenarios.
 * Text only: times and countdowns come from time_utc.
 */
export function AiInterpretation({
  block,
  interpretation,
  now,
}: {
  block: EventRisk | null;
  interpretation: Interpretation | null;
  now: number;
}) {
  const source = interpretationSource(interpretation);
  const out = interpretation?.output ?? null;
  const dominant = out?.dominant_event ?? null;
  const dominantEvent = findEvent(block, dominant?.event_id);
  const risks = distinctRisks(out?.key_risks, block?.what_can_break);
  const sets = scenarioSets(block, interpretation);
  const stale = staleText(interpretation, now);
  return (
    <Card asChild className="ai-interpretation">
      <section id="ai-interpretation" aria-labelledby="ai-interpretation-heading">
        <CardHeader className="flex-wrap">
          <CardTitle id="ai-interpretation-heading">AI market interpretation</CardTitle>
          <CardAction className="flex-wrap">
            {interpretation && (
              <Badge variant={source.deterministic ? "outline" : "info"} data-source={interpretation.source}>
                {source.label}
              </Badge>
            )}
            {stale && <Badge variant="neutral">{stale}</Badge>}
          </CardAction>
        </CardHeader>
        <CardContent className="grid gap-4">
          {out?.market_summary ? (
            <p className="ai-summary">{out.market_summary}</p>
          ) : (
            <p className="text-sm text-muted-foreground">
              {interpretation
                ? "The stored interpretation has no summary."
                : "No interpretation has been stored yet. Event data on this page is deterministic."}
            </p>
          )}
          {dominant && (
            <div className="ai-dominant">
              <h3 className="event-subhead">Dominant event</h3>
              <p>
                <b>{dominantEvent?.name ?? dominant.event_id}</b>
                {dominant.reason ? ` — ${dominant.reason}` : ""}
              </p>
              {dominant.risk_comment && <p className="text-muted-foreground">{dominant.risk_comment}</p>}
            </div>
          )}
          {risks.length > 0 && (
            <div>
              <h3 className="event-subhead">Key risks</h3>
              <ul className="break-list">
                {risks.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </div>
          )}
          {sets.length > 0 && (
            <div className="grid gap-2">
              <h3 className="event-subhead">Event scenarios</h3>
              {sets.map((s) => (
                <Scenarios key={s.event.event_id} set={s} now={now} />
              ))}
            </div>
          )}
          {interpretation?.generated_at && (
            <small className="text-muted-foreground">
              Generated {localTime(interpretation.generated_at)} {APP_ZONE}
            </small>
          )}
        </CardContent>
      </section>
    </Card>
  );
}
