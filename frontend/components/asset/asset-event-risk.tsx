import type { Asset, AssetEventRisk as AssetEventRiskFields, Dashboard, EventObject } from "@/lib/api";
import { number } from "@/lib/api";
import {
  allEvents,
  eventRiskOf,
  exposureWord,
  findEvent,
  levelOf,
  levelTone,
  list,
  riskBadge,
} from "@/lib/event-risk";
import { isNum } from "@/lib/format";
import { effectiveStatus, statusTone } from "@/lib/trade-watch";
import type { TradeWatchItem } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { EventWhen } from "@/components/shared/event-countdown";
import { Panel } from "@/components/shared/panel";

/** Events whose exposure map names the symbol, soonest first. */
function exposingEvents(events: EventObject[], symbol: string): { event: EventObject; level: string | null }[] {
  return events
    .flatMap((event) => {
      const hit = list<{ symbol?: unknown; level?: unknown }>(event.affected_assets).find(
        (a) => a && typeof a === "object" && a.symbol === symbol,
      );
      return hit ? [{ event, level: exposureWord(hit.level) }] : [];
    })
    .sort((a, b) => (Date.parse(a.event.time_utc ?? "") || Infinity) - (Date.parse(b.event.time_utc ?? "") || Infinity))
    .slice(0, 5);
}

/**
 * Asset page: the asset's event risk, the event driving it (with a live
 * countdown), its trade-watch status and adjusted confidence, and the other
 * events it is exposed to. Nothing renders for an older backend.
 */
export function AssetEventRisk({
  asset,
  dashboard,
  now,
}: {
  /** Already passed through `currentAsset`. */
  asset: Asset;
  dashboard: Dashboard | undefined;
  now: number;
}) {
  const block = eventRiskOf(dashboard);
  // `base_confidence`: the |decision score| before the pre-event penalty (backend event_risk).
  // `penalty`: the pre-event penalty share actually applied (0 when none).
  const own: (AssetEventRiskFields & { base_confidence?: number | null; penalty?: number | null }) | null =
    asset.event_risk && typeof asset.event_risk === "object" ? asset.event_risk : null;
  const watch = list<TradeWatchItem>(block?.trade_watch).find((t) => t && t.asset === asset.symbol) ?? null;
  const risk = riskBadge(block, asset.symbol, own ?? watch?.event_risk);
  const driving = risk?.event ?? findEvent(block, own?.event_id);
  const tradeStatus = own?.trade_status ?? watch?.trade_status ?? null;
  const adjusted = isNum(own?.adjusted_confidence) ? own.adjusted_confidence : watch?.adjusted_confidence;
  const base = isNum(own?.base_confidence) ? own.base_confidence : null;
  // Reduced only when a penalty was applied (the service's own figure); an older service without it is compared at
  // the shown precision, since the adjusted value is rounded and the base is not.
  const reduced =
    base !== null &&
    (isNum(own?.penalty) ? own.penalty > 0 : isNum(adjusted) && Math.round(base) > Math.round(adjusted));
  const exposing = exposingEvents(allEvents(block), asset.symbol);
  if (!risk && !tradeStatus && !exposing.length) return null;
  const shown = tradeStatus
    ? effectiveStatus({ trade_status: tradeStatus, awaiting_reopen: own?.awaiting_reopen ?? watch?.awaiting_reopen }, asset)
    : null;
  const level = levelOf(risk?.level, risk?.score);
  return (
    <div className="mb-6">
      <Panel title="Event risk">
        <dl className="detail-grid">
          <div>
            <dt>Event risk</dt>
            <dd>
              {isNum(risk?.score) ? `${number(risk.score, 0)}/100` : "Unavailable"}
              {level && (
                <Badge variant={levelTone(level)} className="ml-2">
                  {level}
                </Badge>
              )}
            </dd>
          </div>
          <div>
            <dt>Driving event</dt>
            <dd>
              {driving ? (
                <>
                  <b>{driving.name}</b> · <EventWhen event={driving} now={now} />
                </>
              ) : isNum(own?.minutes_until) ? (
                own.minutes_until > 0 ? (
                  `In ${number(own.minutes_until, 0)} min (backend estimate)`
                ) : (
                  "Due or under way (backend estimate)"
                )
              ) : (
                "None within the watch window"
              )}
            </dd>
          </div>
          <div>
            <dt>Trade status</dt>
            <dd>
              {shown ? (
                <>
                  <Badge variant={statusTone(shown.status)} className="asset-trade-status">
                    {shown.status}
                  </Badge>
                  {shown.heldBack && (
                    <span className="mt-1 block text-xs text-muted-foreground">
                      Held back from {shown.heldBack}: {shown.reason}
                    </span>
                  )}
                </>
              ) : (
                "Not on today's trade watch"
              )}
            </dd>
          </div>
          {isNum(adjusted) && (
            // |decision score| x (1 - pre-event penalty): a conviction on 0-100, not a percentage or a probability.
            <div>
              <dt>Event-adjusted conviction</dt>
              <dd>
                {number(adjusted, 0)}/100 · published decision unchanged
                <span className="mt-1 block text-xs text-muted-foreground">
                  {reduced
                    ? `Decision score strength, ${number(base, 0)}/100 before the event, reduced by the pre-event penalty.`
                    : base !== null
                      ? "Decision score strength; no pre-event penalty applied."
                      : "Decision score strength after any pre-event penalty."}{" "}
                  A ranking, not a probability.
                </span>
              </dd>
            </div>
          )}
        </dl>
        {exposing.length > 0 && (
          <ul className="m-0 list-none border-t border-border p-0" aria-label={`Events exposing ${asset.symbol}`}>
            {exposing.map(({ event, level: exposure }) => (
              <li key={event.event_id} className="event-risk-row">
                <div className="flex flex-wrap items-center gap-2">
                  <b className="text-[13px]">{event.name}</b>
                  {exposure && <Badge variant="outline">{exposure} exposure</Badge>}
                </div>
                <small className="text-muted-foreground">
                  <EventWhen event={event} now={now} />
                </small>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
