import { Fragment } from "react";
import type { BadgeVariant } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { statusTone } from "@/components/shared/status-badge";
import { EmptyState } from "@/components/shared/empty-state";
import { isNotFound } from "@/lib/api";
import { isNum } from "@/lib/format";
import { cn } from "@/lib/utils";
import { fmt, fmtRatio, yesNo } from "./page-logic";

export { fmt, fmtRatio, isNotFound, isNum, yesNo };

const EXTRA_TONES: Record<string, BadgeVariant> = {
  "OFFICIAL STATEMENT": "info",
  OFFICIAL_STATEMENT: "info",
  CORROBORATED: "info",
  PARTIALLY_CONFIRMED: "info",
  PARTIAL: "info",
  "NOT CONFIGURED": "outline",
  NOT_CONFIGURED: "outline",
  PENDING: "outline",
  // Switched off by an administrator: quiet, never a fault.
  DISABLED: "outline",
  CONFIGURED: "positive",
  SENT: "positive",
  TIMEOUT: "negative",
  DEVELOPMENT_STUB: "outline",
  "DEVELOPMENT STUB": "outline",
  TARGET: "positive",
  STOP: "negative",
  AVAILABLE: "positive",
  UNAVAILABLE: "negative",
  RATE_LIMITED: "neutral",
  DEGRADED: "neutral",
  HIGH: "negative",
  MEDIUM: "neutral",
  LOW: "outline",
  DIVERGENT: "negative",
  REVERSED: "negative",
};

/** Badge tone for statuses the shared mapping does not cover. */
export function tone(value: unknown): BadgeVariant {
  const label = typeof value === "string" ? value.toUpperCase() : "";
  return EXTRA_TONES[label] ?? statusTone(label);
}

export type Column<T> = {
  key: string;
  label: string;
  render: (row: T) => React.ReactNode;
  /** Right-align numeric columns. */
  numeric?: boolean;
  className?: string;
};

/*
 * Below the md breakpoint a stacked table shows each row as a card of
 * "label  value" lines (the label comes from the cell's data-label), so no
 * column is hidden off-screen on a phone. One DOM for both layouts.
 */
const STACK = {
  table: "max-md:block",
  head: "max-md:hidden",
  body: "max-md:block",
  row: "max-md:block max-md:border-t max-md:border-border max-md:px-4 max-md:py-2 max-md:first:border-t-0",
  cell: cn(
    "max-md:grid max-md:min-w-0 max-md:max-w-none max-md:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] max-md:gap-3",
    "max-md:border-t-0 max-md:px-0 max-md:py-1 max-md:text-left max-md:whitespace-normal",
    "max-md:before:text-muted-foreground max-md:before:content-[attr(data-label)]",
  ),
  detail: "max-md:block max-md:min-w-0 max-md:max-w-none max-md:px-0 max-md:pt-1 max-md:pb-1",
};

/**
 * Typed table with explicit per-column rendering. `dense` narrows padding and
 * lets numeric headers wrap (wide metric tables); `detail` adds a full-width
 * line under each row (long texts that would otherwise widen the table);
 * `stack` (default on) turns rows into label/value cards on phones.
 */
export function DataTable<T>({
  rows,
  columns,
  rowKey,
  empty,
  caption,
  dense = false,
  stack = true,
  detail,
}: {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T, index: number) => string;
  empty?: React.ReactNode;
  caption?: string;
  dense?: boolean;
  stack?: boolean;
  detail?: (row: T) => React.ReactNode;
}) {
  if (!rows.length) return <>{empty ?? <EmptyState />}</>;
  return (
    <Table label={caption} className={cn(stack && STACK.table)}>
      {caption && <caption className="sr-only">{caption}</caption>}
      <TableHeader className={cn(stack && STACK.head)}>
        <TableRow>
          {columns.map((c) => (
            <TableHead
              key={c.key}
              scope="col"
              className={cn(
                c.numeric && "text-right",
                dense && "px-3 py-3",
                dense && c.numeric && "whitespace-normal",
                c.className,
              )}
            >
              {c.label}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody className={cn(stack && STACK.body)}>
        {rows.map((row, i) => {
          const extra = detail?.(row);
          return (
            <Fragment key={rowKey(row, i)}>
              <TableRow className={cn(stack && STACK.row, extra && stack && "max-md:pb-0")}>
                {columns.map((c) => (
                  <TableCell
                    key={c.key}
                    data-label={c.label}
                    className={cn(
                      c.numeric && "whitespace-nowrap text-right tabular-nums",
                      dense && "px-3 py-2.5",
                      dense && c.numeric && "min-w-0",
                      extra ? "pb-2" : undefined,
                      c.className,
                      stack && STACK.cell,
                    )}
                  >
                    <div className="min-w-0">{c.render(row)}</div>
                  </TableCell>
                ))}
              </TableRow>
              {extra ? (
                <TableRow data-kind="detail" className={cn(stack && STACK.row, stack && "max-md:border-t-0 max-md:pt-0")}>
                  <TableCell
                    colSpan={columns.length}
                    className={cn("max-w-none border-t-0 pt-0 text-muted-foreground", dense && "px-3", stack && STACK.detail)}
                  >
                    {extra}
                  </TableCell>
                </TableRow>
              ) : null}
            </Fragment>
          );
        })}
      </TableBody>
    </Table>
  );
}

const MISSING = new Set(["Not enough data", "Unavailable"]);

/** Small label/value statistic used in page overviews. Missing values render muted. */
export function Stat({
  label,
  value,
  detail,
}: {
  label: string;
  value: React.ReactNode;
  detail?: React.ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-md border border-border bg-muted/60 px-4 py-3">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <strong
        className={cn(
          "text-lg font-semibold tabular-nums",
          MISSING.has(String(value)) && "text-sm font-normal text-muted-foreground",
        )}
      >
        {value}
      </strong>
      {detail && (
        <small className="text-muted-foreground">{detail}</small>
      )}
    </div>
  );
}

/** "Read-only" marker for configuration that has no admin endpoint yet. */
export function ReadOnlyNote({ children }: { children: React.ReactNode }) {
  return (
    <p className="mb-4 rounded-md border border-border bg-muted/60 px-4 py-2 text-xs text-muted-foreground">
      <strong className="mr-1 text-foreground">Read-only.</strong>
      {children}
    </p>
  );
}
