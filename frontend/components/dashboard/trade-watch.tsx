import Link from "next/link";
import type { Asset, EventObject, EventRisk, Interpretation, TradeWatchItem } from "@/lib/api";
import { number } from "@/lib/api";
import {
  commentFor,
  commentLabel,
  convictionText,
  findEvent,
  levelTone,
  list,
  riskBadge,
} from "@/lib/event-risk";
import { isNum } from "@/lib/format";
import { horizonLabel } from "@/lib/market-data";
import { decisionAge, effectiveStatus, statusKind, statusTone, type AnalysisClock } from "@/lib/trade-watch";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { EventWhen } from "@/components/shared/event-countdown";
import { probabilityLabel, signalLabel, signed } from "@/components/asset/signal";
import { percent } from "./selectors";

/** At most this many cards (§20). */
const LIMIT = 8;
const PRIMARY_LIMIT = 5;

/** What the adjusted conviction is, when the asset's overlay does not carry its own basis. */
const CONVICTION_BASIS =
  "Strength of the decision score (0-100), reduced ahead of high-risk events. A conviction, not a probability.";

/** The overlay's own `confidence_basis` (the asset's event_risk block), else the documented formula. */
function convictionBasis(asset: Asset | undefined): string {
  const basis = (asset?.event_risk as { confidence_basis?: unknown } | null | undefined)?.confidence_basis;
  return typeof basis === "string" && basis.trim() ? basis.trim() : CONVICTION_BASIS;
}

function lines(value: unknown): string[] {
  if (typeof value === "string") return value.trim() ? [value.trim()] : [];
  return list<unknown>(value).filter((x): x is string => typeof x === "string" && !!x.trim());
}

/** The upcoming event: looked up by id in the block, else the item's own fields. */
function upcoming(block: EventRisk, item: TradeWatchItem): Partial<EventObject> | null {
  const u = item.upcoming_event;
  if (!u) return null;
  if (typeof u === "string") return findEvent(block, u) ?? { name: u };
  return findEvent(block, u.event_id) ?? u;
}

function Metric({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function WatchCard({
  item,
  asset,
  block,
  comment,
  commentSource,
  clock,
  now,
}: {
  item: TradeWatchItem;
  asset: Asset | undefined;
  block: EventRisk;
  /** A model's comment; null for the deterministic fallback, which only restates the card. */
  comment: string | null;
  commentSource: string;
  /** The full analysis cycle (lib/trade-watch analysisClock), for the decision's age. */
  clock: AnalysisClock | null;
  now: number;
}) {
  const shown = effectiveStatus(item, asset);
  const risk = riskBadge(block, item.asset, item.event_risk);
  const event = upcoming(block, item);
  const why = lines(item.why);
  const confirmation = lines(item.confirmation);
  const invalidation = lines(item.invalidation);
  const published = signalLabel(asset?.decision);
  const decided = decisionAge(asset?.decision?.created_at, now, clock);
  const conviction = convictionText(item.adjusted_confidence);
  const hist = item.historical_probability;
  return (
    <Card asChild className="watch-card p-5">
      <article aria-label={`${item.asset} ${shown.status}`} data-status={statusKind(shown.status)}>
        <div className="setup-head">
          <Link href={`/assets/${encodeURIComponent(item.asset)}`}>
            <strong>{item.asset}</strong>
            {asset?.name && <span>{asset.name}</span>}
          </Link>
          <Badge variant={statusTone(shown.status)} className="watch-status">
            {shown.status}
          </Badge>
        </div>
        {(shown.heldBack || item.awaiting_reopen) && (
          <p className="watch-held">
            {shown.heldBack && <>Held back from {shown.heldBack}: </>}
            {shown.reason ?? "Market closed — awaiting reopen."}
          </p>
        )}
        {item.status_reason && <p className="watch-reason">{item.status_reason}</p>}
        {decided && (
          <p className="watch-decided mt-1 text-[11px] text-warning" title={decided.reason}>
            <time dateTime={decided.at}>{decided.text}</time>
          </p>
        )}
        <h3 className="watch-label">Why</h3>
        <ul className="watch-why">
          {why.length ? (
            why.slice(0, 5).map((r) => <li key={r}>{r}</li>)
          ) : (
            <li className="none text-muted-foreground">No verified reasons were supplied.</li>
          )}
        </ul>
        <dl className="decision-details">
          <dt>Upcoming event</dt>
          <dd>
            {event?.name ? (
              <>
                <b>{event.name}</b> · <EventWhen event={event} now={now} />
              </>
            ) : (
              "None within the watch window"
            )}
          </dd>
          <dt>Confirmation</dt>
          <dd>{confirmation.length ? confirmation.join(" AND ") : "Not established"}</dd>
          <dt>Invalidation</dt>
          <dd>{invalidation.length ? invalidation.join(" OR ") : "Not established"}</dd>
        </dl>
        <details className="watch-details">
          <summary>Scores and evidence</summary>
          <dl className="watch-metrics">
            <Metric label="Decision score">{isNum(item.decision_score) ? signed(item.decision_score, 0) : "Unavailable"}</Metric>
            <Metric label="Regime favorability">
              {isNum(item.regime_favorability) ? `${number(item.regime_favorability, 0)}/100` : "Unavailable"}
            </Metric>
            <Metric label="Evidence confidence">{percent(item.evidence_confidence)}</Metric>
            <Metric label="Market confirmation">{percent(item.market_confirmation)}</Metric>
            <Metric label="Event risk">
              {risk?.score !== null && risk?.score !== undefined ? `${number(risk.score, 0)}/100` : "Unavailable"}
              {risk?.level && (
                <Badge variant={levelTone(risk.level)} className="ml-1.5 align-[2px]">
                  {risk.level}
                </Badge>
              )}
            </Metric>
            {conviction && (
              <Metric label="Adjusted conviction">
                <span title={convictionBasis(asset)}>{conviction}</span>
                <small className="block text-[10px] font-normal text-muted-foreground">not a probability</small>
              </Metric>
            )}
            {hist && isNum(hist.p) && (
              <Metric label="Historical probability">
                {[probabilityLabel(hist).value, probabilityLabel(hist).detail].filter(Boolean).join(" · ")}
              </Metric>
            )}
            {item.horizon && <Metric label="Horizon">{horizonLabel(item.horizon)}</Metric>}
          </dl>
          {published && (
            <p className="watch-published">
              Published signal <b>{published}</b> · unchanged by the event overlay
            </p>
          )}
        </details>
        {comment && (
          <p className="watch-comment">
            <span>{commentSource}</span> {comment}
          </p>
        )}
      </article>
    </Card>
  );
}

/**
 * §6 TODAY'S TRADE WATCH: five primary event-aware cards; up to three more
 * remain available in the disclosure below them.
 * Statuses are the backend's, downgraded locally only when the quote is no
 * longer fresh. A published decision that may be out of date (the analysis
 * is stale, or its last successful cycle did not replace it) shows when it
 * was made. Only a model's comment is added to a card (the
 * deterministic fallback's note repeats the card's own fields). Actionable
 * published setups the watch does not cover are still linked so no published
 * decision disappears from view.
 */
export function TradeWatch({
  block,
  items,
  assets,
  setups,
  interpretation,
  clock = null,
  now,
}: {
  block: EventRisk;
  items: TradeWatchItem[];
  /** Assets already passed through `currentAsset`. */
  assets: Asset[];
  /** Actionable published setups (bestSetups). */
  setups: Asset[];
  interpretation: Interpretation | null;
  /** The full analysis cycle (lib/trade-watch analysisClock); null applies the fallback age. */
  clock?: AnalysisClock | null;
  now: number;
}) {
  // The deterministic fallback's per-asset note restates status, event, confirmation and invalidation shown above it.
  const comments = interpretation?.source === "ai" ? interpretation.output?.trade_watch : undefined;
  const bySymbol = new Map(assets.map((a) => [a.symbol, a]));
  const cards = items.slice(0, LIMIT);
  const covered = new Set(cards.map((c) => c.asset));
  const others = setups.filter((a) => !covered.has(a.symbol));
  const renderCard = (item: TradeWatchItem) => (
    <WatchCard
      key={item.asset}
      item={item}
      asset={bySymbol.get(item.asset)}
      block={block}
      comment={commentFor(comments, item.asset)}
      commentSource={commentLabel(interpretation)}
      clock={clock}
      now={now}
    />
  );
  return (
    <section aria-labelledby="trade-watch-heading">
      <div className="section-title">
        <div>
          <h2 id="trade-watch-heading">Today&apos;s trade watch</h2>
          <p>What to watch, why, and what confirms or invalidates it.</p>
        </div>
      </div>
      <div className="watch-grid">
        {cards.slice(0, PRIMARY_LIMIT).map(renderCard)}
      </div>
      {cards.length > PRIMARY_LIMIT && (
        <details className="dashboard-details watch-extra">
          <summary>More trade-watch candidates ({cards.length - PRIMARY_LIMIT})</summary>
          <div className="watch-grid dashboard-details-body">{cards.slice(PRIMARY_LIMIT).map(renderCard)}</div>
        </details>
      )}
      {others.length > 0 && (
        <p className="mt-3 text-xs text-muted-foreground">
          Other published setups:{" "}
          {others.map((a, i) => (
            <span key={a.symbol}>
              {i > 0 && ", "}
              <Link href={`/assets/${encodeURIComponent(a.symbol)}`} className="text-primary">
                {a.symbol} {signalLabel(a.decision)}
              </Link>
            </span>
          ))}
        </p>
      )}
    </section>
  );
}
