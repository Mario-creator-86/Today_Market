import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { humanize, statusTone, text } from "@/lib/format";

/**
 * Status -> badge tone (one colour per status on every page). The rule lives
 * in lib/format so vitest covers it; re-exported for page-utils and pages.
 */
export { statusTone };

/**
 * Badge for any backend status/enum value. Missing values render as
 * "Unavailable"; underscores are shown as spaces.
 */
export function StatusBadge({
  value,
  className,
  variant,
}: {
  value: unknown;
  className?: string;
  /** Override the automatic tone. */
  variant?: BadgeVariant;
}) {
  const label = text(value);
  return (
    <Badge variant={variant ?? statusTone(label)} className={className}>
      {humanize(label)}
    </Badge>
  );
}
