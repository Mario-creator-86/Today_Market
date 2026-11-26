"use client";
import * as React from "react";
// Relative import: the pure label helper below is unit-tested by vitest, which has no "@/" alias.
import { cn } from "../../lib/utils";

/** True while the element's content is wider than its box (re-measured on resize). */
function useOverflow(ref: React.RefObject<HTMLElement | null>): boolean {
  const [overflow, setOverflow] = React.useState(false);
  React.useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    // The observer reports once on observe(), then on every size change of the box or the table.
    const observer = new ResizeObserver(() =>
      setOverflow(el.scrollWidth > el.clientWidth + 1),
    );
    observer.observe(el);
    if (el.firstElementChild) observer.observe(el.firstElementChild);
    return () => observer.disconnect();
  }, [ref]);
  return overflow;
}

/**
 * Accessible name of an overflowing table's scroll region: the explicit label,
 * else the table's own aria-label, else a generic name.
 */
export function scrollRegionLabel(label?: string | null, tableLabel?: string | null): string {
  const name = [label, tableLabel].map((v) => v?.trim()).find(Boolean);
  return `${name ?? "Table"} (scrolls sideways)`;
}

/**
 * Horizontally scrollable table; it never widens the page on mobile. While the
 * table is wider than its box, the box is a labelled, keyboard-focusable
 * region (arrow keys scroll it) and a hint says more columns sit to the side.
 * A table that fits is not a region and not a tab stop.
 */
export function Table({
  className,
  label,
  ...props
}: React.ComponentProps<"table"> & {
  /** Accessible name of the scroll region, e.g. the table caption; defaults to the table's aria-label. */
  label?: string;
}) {
  const box = React.useRef<HTMLDivElement>(null);
  const overflow = useOverflow(box);
  return (
    <>
      <div
        ref={box}
        data-slot="table-container"
        data-overflow={overflow ? "true" : undefined}
        className="table-wrap relative w-full overflow-auto focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary"
        {...(overflow
          ? {
              role: "region",
              tabIndex: 0,
              "aria-label": scrollRegionLabel(label, props["aria-label"]),
            }
          : {})}
      >
        <table
          data-slot="table"
          className={cn("w-full border-collapse text-left text-xs", className)}
          {...props}
        />
      </div>
      {overflow && (
        <p
          data-slot="table-scroll-hint"
          className="border-t border-border px-[18px] py-2 text-[11px] text-muted-foreground"
        >
          Scroll sideways to see every column.
        </p>
      )}
    </>
  );
}

export function TableHeader(props: React.ComponentProps<"thead">) {
  return <thead data-slot="table-header" {...props} />;
}

export function TableBody(props: React.ComponentProps<"tbody">) {
  return <tbody data-slot="table-body" {...props} />;
}

export function TableFooter({
  className,
  ...props
}: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn("bg-muted font-medium", className)}
      {...props}
    />
  );
}

export function TableRow(props: React.ComponentProps<"tr">) {
  return <tr data-slot="table-row" {...props} />;
}

export function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        "whitespace-nowrap bg-muted px-[18px] py-[15px] font-medium text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

export function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        "min-w-[100px] max-w-[400px] border-t border-border px-[18px] py-[15px] wrap-anywhere",
        className,
      )}
      {...props}
    />
  );
}

export function TableCaption({
  className,
  ...props
}: React.ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn("mt-3 text-xs text-muted-foreground", className)}
      {...props}
    />
  );
}
