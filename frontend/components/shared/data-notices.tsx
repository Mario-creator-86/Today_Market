import type { DataNotice } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Notices a data provider's terms require while its data is on screen (e.g.
 * the FRED® API notice), rendered as sent by the service; nothing when none apply.
 */
export function DataNotices({
  notices,
  className,
}: {
  notices: DataNotice[];
  className?: string;
}) {
  if (!notices.length) return null;
  return (
    <ul
      aria-label="Data provider notices"
      className={cn("m-0 list-none p-0 text-[11px] leading-relaxed text-muted-foreground", className)}
    >
      {notices.map((n) => (
        <li key={n.id}>{n.text}</li>
      ))}
    </ul>
  );
}
