import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cn } from "@/lib/utils";

type WithAsChild<T extends keyof React.JSX.IntrinsicElements> =
  React.ComponentProps<T> & { asChild?: boolean };

/** Surface container. Use `asChild` to render a semantic element (section, article). */
export function Card({ className, asChild, ...props }: WithAsChild<"div">) {
  const Comp = asChild ? Slot : "div";
  return (
    <Comp
      data-slot="card"
      className={cn(
        "overflow-hidden rounded-lg border border-border bg-card text-card-foreground",
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-header"
      className={cn(
        "flex items-center justify-between gap-3 border-b border-border px-[21px] py-[18px]",
        className,
      )}
      {...props}
    />
  );
}

/** Renders an h2 by default; use `asChild` to render another heading level. */
export function CardTitle({ className, asChild, ...props }: WithAsChild<"h2">) {
  const Comp = asChild ? Slot : "h2";
  return (
    <Comp
      data-slot="card-title"
      className={cn("text-[17px] font-semibold", className)}
      {...props}
    />
  );
}

export function CardDescription({
  className,
  ...props
}: React.ComponentProps<"p">) {
  return (
    <p
      data-slot="card-description"
      className={cn("mt-1 text-xs text-muted-foreground", className)}
      {...props}
    />
  );
}

export function CardAction({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-action"
      className={cn("ml-auto flex items-center gap-2", className)}
      {...props}
    />
  );
}

export function CardContent({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div data-slot="card-content" className={cn("p-5", className)} {...props} />
  );
}

export function CardFooter({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-footer"
      className={cn(
        "flex items-center gap-3 border-t border-border px-5 py-4 text-xs text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}
