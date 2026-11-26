import type { ImpactView } from "@/lib/event-risk";

const RELEVANCE_BASED_TITLE = "This event has no calendar impact of its own: relevance / 10 is shown instead.";
const CALENDAR_TITLE = "Calendar impact out of 10: the same number the calendar shows for this release.";

/**
 * An event's impact out of 10: the calendar's own number ("9/10"), or
 * relevance / 10 labelled "(relevance-based)" when the event has none, so the
 * two scales never share one unlabelled "Impact". `strong` bolds the number.
 */
export function ImpactValue({ view, strong = false }: { view: ImpactView; strong?: boolean }) {
  const value = view.text ?? "Unavailable";
  const title = view.text && !view.relevanceBased ? CALENDAR_TITLE : undefined;
  return (
    <>
      {strong ? <b title={title}>{value}</b> : <span title={title}>{value}</span>}
      {view.relevanceBased && (
        <>
          {" "}
          <small
            className="text-[10px] font-normal tracking-normal text-muted-foreground"
            title={RELEVANCE_BASED_TITLE}
          >
            (relevance-based)
          </small>
        </>
      )}
    </>
  );
}
