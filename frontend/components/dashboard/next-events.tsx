import { localTime, RecordData } from "@/lib/api";
import { bucharestDay, parseTime } from "@/lib/event-time";

/** "Wed 30 Sep · time TBA" for date-only rows (`time_known: false`): no clock is invented for them. */
function when(item: RecordData): string {
  const value = item.scheduled_utc ?? item.scheduled_at;
  if (item.time_known !== false) return localTime(value);
  const t = parseTime(value);
  return t === null ? "Time TBA" : `${bucharestDay(t)} · time TBA`;
}
import { text } from "@/lib/format";
import { EmptyState } from "@/components/shared/empty-state";
import { impactText, scheduledAt } from "./selectors";

/**
 * §27 Next important events: upcoming high-impact releases, Bucharest time,
 * "Impact N/10" (the calendar row's own impact, the same number the calendar
 * page and the next-event card show). Long names wrap to two lines and keep
 * the full name as a tooltip. `minImpact` is the backend's threshold; without
 * it (an older backend) the empty state names no number.
 */
export function NextEvents({
  items,
  minImpact,
}: {
  items: RecordData[];
  minImpact: number | null;
}) {
  if (!items.length)
    return (
      <EmptyState
        title="No upcoming high-impact events"
        detail={
          minImpact === null
            ? "Scheduled high-impact releases will appear here."
            : `Scheduled releases with impact ${minImpact}/10 or higher will appear here.`
        }
      />
    );
  return (
    <ol className="m-0 list-none p-0">
      {items.map((item, i) => {
        const name = text(item.name ?? item.event);
        return (
          <li
            key={text(item.id ?? i)}
            className="flex items-center gap-3 border-b border-border px-[21px] py-3.5 last:border-b-0 sm:gap-4"
          >
            <time
              dateTime={new Date(scheduledAt(item)).toISOString()}
              className="w-[68px] shrink-0 text-xs font-semibold tabular-nums sm:w-[92px]"
            >
              {when(item)}
            </time>
            <div className="min-w-0 flex-1">
              <strong className="line-clamp-2 text-[13px] font-semibold break-words" title={name}>
                {name}
              </strong>
              {item.country != null && (
                <small className="text-muted-foreground">
                  {text(item.country)}
                </small>
              )}
            </div>
            <span className="shrink-0 text-xs text-warning" title="Calendar impact out of 10">
              {impactText(item)}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
