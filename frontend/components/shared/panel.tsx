import Link from "next/link";
import { ArrowUpRight } from "lucide-react";
import { Card, CardAction, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/**
 * Titled section card (h2 title). Children render edge-to-edge, so lists and
 * tables supply their own padding; wrap free text in `CardContent`.
 */
export function Panel({
  title,
  children,
  link,
  linkLabel = "View all",
  action,
  className,
}: {
  title: string;
  children: React.ReactNode;
  /** Optional "View all" destination. */
  link?: string;
  linkLabel?: string;
  /** Extra header controls (badges, filters). */
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <Card asChild className={cn("panel", className)}>
      <section>
        <CardHeader>
          <CardTitle>{title}</CardTitle>
          {(action || link) && (
            <CardAction>
              {action}
              {link && (
                <Link
                  href={link}
                  className="flex items-center gap-[5px] text-[11px] text-primary"
                >
                  {linkLabel} <ArrowUpRight size={14} aria-hidden="true" />
                </Link>
              )}
            </CardAction>
          )}
        </CardHeader>
        {children}
      </section>
    </Card>
  );
}
