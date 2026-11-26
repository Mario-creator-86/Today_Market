"use client";
import { localTime, MarketEvent, number, Reaction } from "@/lib/api";
import {
  confirmationState,
  lagText,
  reactionStatusLabel,
  reactionTiming,
  type ReactionTiming,
  ungradedReaction,
} from "@/lib/events";
import { EmptyState } from "@/components/shared/empty-state";
import { StatusBadge } from "@/components/shared/status-badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { isNum, signTone, toneText } from "@/components/asset/signal";
import { cn } from "@/lib/utils";
import { AssetSymbol, useTrackedSymbols } from "./asset-link";
import {
  arrow,
  directionWord,
  expectedEffects,
  formatMove,
  reactionTone,
  sortHorizons,
} from "./event-data";

/**
 * "first seen 62 h after it was published (reactions are measured only for
 * events first seen within 15 min of publication)"; the limit is the
 * configured one the API sends (lib/events reactionTiming).
 */
function lateText(timing: ReactionTiming): string {
  return `first seen ${lagText(timing.publicationLagMs ?? 0)} after it was published (reactions are measured only for events first seen within ${lagText(timing.maxLagMs)} of publication)`;
}

/** Where the full timing explanation of a late event is: said once, under the reaction table. */
const TIMING_NOTE_POINTER = "see the timing note under Expected vs actual reaction";

/**
 * When the reaction baseline was taken. Reactions are measured from the
 * prices at the event's first sighting; when that is later than the
 * configured publication lag (a backfilled release), the moves are not the
 * release reaction. The reaction table says why in full (`full`); other
 * panels (the causal chain) only point to it, so the warning is read once.
 */
export function ReactionTimingNote({
  event,
  full = true,
}: {
  event: MarketEvent;
  /** Explain a late first sighting in full (the reaction table), or only point to that explanation. */
  full?: boolean;
}) {
  const timing = reactionTiming(event);
  if (!timing.measuredFrom) return null;
  const lag =
    timing.baselineLagMs !== null && timing.baselineLagMs >= 60_000
      ? `, ${lagText(timing.baselineLagMs)} after publication`
      : "";
  return (
    <div className="border-t border-border px-5 py-3 text-[11px]">
      <p className="text-muted-foreground">
        Measured from the prices at {localTime(timing.measuredFrom)} (Bucharest)
        {lag}.
      </p>
      {timing.late &&
        (full ? (
          <p role="note" className="mt-1 text-warning">
            This event was {lateText(timing)}, so these moves are not the
            market&apos;s reaction to the release. Confirmed / divergent verdicts
            are not shown.
          </p>
        ) : (
          <p className="mt-1 text-muted-foreground">
            Verdicts are not shown: this event was first seen too late after
            publication ({TIMING_NOTE_POINTER}).
          </p>
        ))}
    </div>
  );
}

/** EXPECTED vs ACTUAL reaction per asset and window. */
export function ReactionTable({
  event,
  reactions,
}: {
  event: MarketEvent;
  reactions: Map<string, Reaction>;
}) {
  const tracked = useTrackedSymbols();
  const rows = event.reactions ?? [];
  const timing = reactionTiming(event);
  const late = timing.late;
  const expected = expectedEffects(event.causal_paths);
  const horizons = sortHorizons(rows.map((r) => r.horizon));
  const symbols = [
    ...new Set([...Object.keys(expected), ...rows.map((r) => r.symbol)]),
  ];
  if (!symbols.length)
    return late ? (
      <EmptyState
        title="Not measured"
        detail={`This event was ${lateText(timing)}. A move after a late first sighting is not the release reaction.`}
      />
    ) : (
      <EmptyState
        title="No expected or measured reaction"
        detail="Reactions are measured per asset at 1m to 4h windows once a causal rule applies and fresh prices exist."
      />
    );
  return (
    <>
      <Table aria-label="Expected and actual market reaction">
        <TableHeader>
          <TableRow>
            <TableHead>Asset</TableHead>
            <TableHead>Expected</TableHead>
            {horizons.map((h) => (
              <TableHead key={h}>Actual {h}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {symbols.map((symbol) => {
            const first = rows.find((r) => r.symbol === symbol);
            const sign =
              expected[symbol] ??
              (isNum(first?.expected_direction)
                ? first.expected_direction
                : null);
            return (
              <TableRow key={symbol}>
                <TableCell className="font-medium">
                  <AssetSymbol symbol={symbol} tracked={tracked} />
                </TableCell>
                <TableCell>
                  {sign === null || sign === 0 ? (
                    <span className="text-muted-foreground">
                      No expectation
                    </span>
                  ) : (
                    <span className={toneText[signTone(sign)]}>
                      <span aria-hidden="true">{arrow(sign)} </span>
                      {directionWord(sign)}
                    </span>
                  )}
                </TableCell>
                {horizons.map((h) => {
                  const r = reactions.get(`${symbol}:${h}`);
                  return (
                    <TableCell key={h}>
                      {r ? (
                        <span className="flex flex-col items-start gap-1">
                          <span className="font-mono tabular-nums">
                            {formatMove(r)}
                          </span>
                          {/* Without an expectation there is no verdict; a late baseline has none either. */}
                          {!late && r.status !== "NOT_EXPECTED" && (
                            <StatusBadge
                              value={reactionStatusLabel(r.status)}
                              variant={reactionTone(r.status)}
                            />
                          )}
                          {!late && ungradedReaction(event, r.status, sign) && (
                            <span className="text-[11px] text-muted-foreground">
                              Not graded
                            </span>
                          )}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">
                          Not measured
                        </span>
                      )}
                    </TableCell>
                  );
                })}
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      {!horizons.length && (
        <p className="px-5 py-3 text-[11px] text-muted-foreground">
          {late
            ? `Not measured: this event was ${lateText(timing)}.`
            : "No reaction has been measured yet; windows close 1m to 4h after the event."}
        </p>
      )}
      {!late &&
        rows.some((r) =>
          ungradedReaction(event, r.status, expected[r.symbol]),
        ) && (
          <p role="note" className="px-5 py-3 text-[11px] text-muted-foreground">
            Not graded: these moves were measured before this event was
            classified, so they were never compared with the expected
            direction.
          </p>
        )}
      <ReactionTimingNote event={event} />
    </>
  );
}

function names(list: string[] | undefined): string {
  return list?.length ? list.join(", ") : "None";
}

/** MARKET CONFIRMATION: share of the expected basket moving as expected, per window. */
export function MarketConfirmation({
  event,
  horizon,
}: {
  event: MarketEvent;
  horizon: string | undefined;
}) {
  const order = sortHorizons((event.baskets ?? []).map((b) => b.horizon));
  const baskets = [...(event.baskets ?? [])].sort(
    (a, b) => order.indexOf(a.horizon) - order.indexOf(b.horizon),
  );
  const timing = reactionTiming(event);
  // A late event: a short title and a pointer; the reaction panel explains the timing once.
  if (!baskets.length && timing.late)
    return (
      <EmptyState
        title="Not measured"
        detail={`There is no release reaction to confirm: this event was first seen too late after publication (${TIMING_NOTE_POINTER}).`}
      />
    );
  if (!baskets.length)
    return (
      <EmptyState
        title="Not computed yet"
        detail="Market confirmation needs measured reactions for the assets the causal chain expects to move."
      />
    );
  if (baskets.every((b) => confirmationState(b) === "nothing-expected"))
    return (
      <EmptyState
        title="Not applicable"
        detail="No active causal rule expects any asset to move on this event, so there is nothing to confirm. Measured moves are listed in the reaction table."
      />
    );
  if (timing.late)
    return (
      <EmptyState
        title="Not the release reaction"
        detail={`The measured moves cannot confirm or contradict this event: it was first seen too late after publication (${TIMING_NOTE_POINTER}).`}
      />
    );
  const headline =
    baskets.find((b) => b.horizon === horizon) ?? baskets[baskets.length - 1];
  const state = confirmationState(headline);
  const chip = (b: (typeof baskets)[number]) => {
    const s = confirmationState(b);
    return s === "measured"
      ? `${number(b.confirmation_pct, 0)}%`
      : s === "nothing-expected"
        ? "not applicable"
        : "no data";
  };
  return (
    <div className="p-5">
      <div className="market-confirmation flex items-baseline gap-3">
        <strong
          className={cn(
            "text-3xl font-medium",
            state !== "measured" && "text-lg text-muted-foreground",
          )}
        >
          {state === "measured"
            ? `${number(headline.confirmation_pct, 0)}%`
            : state === "nothing-expected"
              ? "Not applicable"
              : "Not enough data"}
        </strong>
        <span className="text-xs text-muted-foreground">
          at {headline.horizon}
        </span>
        {headline.confirmation_level && (
          <StatusBadge value={headline.confirmation_level} variant="outline" />
        )}
      </div>
      {isNum(headline.measured) && isNum(headline.expected) && headline.expected > 0 && (
        <p className="mt-1 text-xs text-muted-foreground">
          Measured {headline.measured} of {headline.expected} expected asset
          {headline.expected === 1 ? "" : "s"}
        </p>
      )}
      {headline.divergence && (
        <p role="note" className="mt-2 text-xs text-warning">
          Divergence: {(headline.diverging ?? []).join(", ")} moved against the
          expected direction.
        </p>
      )}
      {!headline.divergence &&
        !!headline.diverging?.length &&
        isNum(headline.measured) &&
        headline.diverging.length === headline.measured && (
          <p role="note" className="mt-2 text-xs text-warning">
            All measured assets moved against the expected direction.
          </p>
        )}
      <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
        <dt className="text-muted-foreground">Confirming</dt>
        <dd className="m-0">{names(headline.confirming)}</dd>
        {!!headline.partial?.length && (
          <>
            <dt className="text-muted-foreground">Partial</dt>
            <dd className="m-0">{headline.partial.join(", ")}</dd>
          </>
        )}
        <dt className="text-muted-foreground">Diverging</dt>
        <dd className="m-0">{names(headline.diverging)}</dd>
        <dt className="text-muted-foreground">No reaction</dt>
        <dd className="m-0">{names(headline.no_reaction)}</dd>
        <dt className="text-muted-foreground">Not measured</dt>
        <dd className="m-0">{names(headline.missing)}</dd>
      </dl>
      {baskets.length > 1 && (
        <ul
          className="mt-4 flex list-none flex-wrap gap-2 p-0 text-xs"
          aria-label="Confirmation by window"
        >
          {baskets.map((b) => (
            <li
              key={b.horizon}
              className={cn(
                "rounded-md border border-border px-2 py-1",
                b === headline && "border-primary",
              )}
            >
              {b.horizon}: {chip(b)}
            </li>
          ))}
        </ul>
      )}
      <p className="mt-3 text-[11px] text-muted-foreground">
        Missing assets never count as confirming.
      </p>
    </div>
  );
}
