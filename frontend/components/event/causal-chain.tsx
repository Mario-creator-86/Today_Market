"use client";
import { MarketEvent, Reaction } from "@/lib/api";
import {
  eventHeadline,
  inactiveReason,
  pathName,
  pathStrengthText,
  reactionStatusLabel,
  type PathDetail,
} from "@/lib/events";
import { humanize } from "@/lib/format";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/shared/empty-state";
import { StatusBadge } from "@/components/shared/status-badge";
import { signTone, toneText } from "@/components/asset/signal";
import { AssetSymbol, useTrackedSymbols } from "./asset-link";
import { arrow, directionWord, formatMove, reactionTone } from "./event-data";
import { ReactionTimingNote } from "./reactions";

function Connector() {
  return <div aria-hidden="true" className="ml-[11px] h-4 w-px bg-border" />;
}

function Node({
  children,
  className,
  first = false,
}: {
  children: React.ReactNode;
  className?: string;
  first?: boolean;
}) {
  return (
    <li>
      {!first && <Connector />}
      <div
        className={cn(
          "flex flex-wrap items-center gap-2 rounded-md border border-border bg-secondary/40 px-3 py-2 text-sm",
          className,
        )}
      >
        {children}
      </div>
    </li>
  );
}

/**
 * Why there is no chain. An event the causal pipeline never classified (no
 * `trigger` field at all) has simply not been analysed; a classified event
 * with no matching rule is not modelled. Neither means "no market effect".
 */
function NoPaths({ event }: { event: MarketEvent }) {
  const classified = "trigger" in event;
  return classified ? (
    <EmptyState
      title="No causal rule covers this event"
      detail="Its market effect is not modelled by the causal graph. That does not mean no effect is expected."
    />
  ) : (
    <EmptyState
      title="Causal chain unavailable"
      detail="This event has not been classified, so causal analysis has not run for it. Its market effect is unknown, not zero."
    />
  );
}

/**
 * CAUSAL CHAIN: each causal path from the deterministic graph as event →
 * steps → affected assets, with the measured move per asset at the selected
 * reaction window. Competing and suppressed paths stay visible, with the
 * reason they do not count.
 */
export function CausalChain({
  event,
  horizon,
  reactions,
  late = false,
}: {
  event: MarketEvent;
  horizon: string | undefined;
  reactions: Map<string, Reaction>;
  /** Reactions were measured too long after publication: moves stay, verdicts are hidden. */
  late?: boolean;
}) {
  const tracked = useTrackedSymbols();
  const paths = event.causal_paths as PathDetail[] | undefined;
  if (!Array.isArray(paths) || !paths.length) return <NoPaths event={event} />;
  return (
    <>
      <div className="grid gap-4 p-5 md:grid-cols-2">
        {paths.map((path, i) => {
          const inactive = path.active === false;
          const reason = inactiveReason(path);
          const name = pathName(path, i);
          const strength = pathStrengthText(path);
          const steps = path.steps ?? [];
          const effects = Object.entries(path.effects ?? {});
          return (
            <section
              key={`${path.rule_id ?? "path"}-${i}`}
              aria-label={`Causal path ${name}`}
              className="causal-path min-w-0"
            >
              <header className="mb-3 flex flex-wrap items-center gap-2">
                <h3 className="text-sm font-semibold">{name}</h3>
                {inactive && (
                  <StatusBadge
                    value={path.suppressed_by ? "SUPPRESSED" : "INACTIVE"}
                    variant="outline"
                  />
                )}
                {strength && (
                  <span className="basis-full text-[11px] text-muted-foreground">
                    {strength}
                  </span>
                )}
                {reason && (
                  <span className="basis-full text-[11px] text-warning">
                    {reason} Its expected moves no longer count.
                  </span>
                )}
              </header>
              <ol className={cn("m-0 list-none p-0", inactive && "opacity-60")}>
                <Node first className="bg-primary/10">
                  <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
                    Event
                  </span>
                  <span className="font-medium">{eventHeadline(event)}</span>
                </Node>
                {steps.map((s) => (
                  <Node key={s.variable}>
                    <span
                      aria-hidden="true"
                      className={toneText[signTone(s.direction)]}
                    >
                      {arrow(s.direction)}
                    </span>
                    <span>{s.label || humanize(s.variable)}</span>
                    <span className="sr-only">{directionWord(s.direction)}</span>
                  </Node>
                ))}
                <li>
                  <Connector />
                  <div className="rounded-md border border-border px-3 py-2 text-sm">
                    <span className="mb-2 block text-[11px] uppercase tracking-wide text-muted-foreground">
                      {inactive ? "Assets this path would move" : "Affected assets"}
                      {horizon ? ` · measured at ${horizon}` : ""}
                    </span>
                    {effects.length ? (
                      <ul className="m-0 grid list-none gap-1 p-0">
                        {effects.map(([symbol, sign]) => {
                          const r = horizon
                            ? reactions.get(`${symbol}:${horizon}`)
                            : undefined;
                          return (
                            <li
                              key={symbol}
                              className="flex flex-wrap items-center gap-2"
                            >
                              <AssetSymbol
                                symbol={symbol}
                                tracked={tracked}
                                className="w-16 font-semibold"
                              />
                              <span
                                className={
                                  inactive
                                    ? "text-muted-foreground"
                                    : toneText[signTone(sign)]
                                }
                              >
                                <span aria-hidden="true">{arrow(sign)}</span>
                                <span className="sr-only">
                                  {inactive ? "would have been" : "expected"}{" "}
                                  {directionWord(sign)}
                                </span>
                              </span>
                              <span className="font-mono tabular-nums">
                                {formatMove(r)}
                              </span>
                              {r && !inactive && !late && (
                                <StatusBadge
                                  value={reactionStatusLabel(r.status)}
                                  variant={reactionTone(r.status)}
                                />
                              )}
                            </li>
                          );
                        })}
                      </ul>
                    ) : (
                      <span className="text-muted-foreground">
                        No asset effect on this path
                      </span>
                    )}
                  </div>
                </li>
              </ol>
            </section>
          );
        })}
      </div>
      {/* The late-sighting warning is written once, under the reaction table; here only a pointer. */}
      <ReactionTimingNote event={event} full={false} />
    </>
  );
}
