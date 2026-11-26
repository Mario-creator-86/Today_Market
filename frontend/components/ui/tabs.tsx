"use client";
import * as React from "react";
import * as TabsPrimitive from "@radix-ui/react-tabs";
import { useEdgeFade } from "@/lib/use-edge-fade";
import { cn } from "@/lib/utils";

export function Tabs(props: React.ComponentProps<typeof TabsPrimitive.Root>) {
  return <TabsPrimitive.Root data-slot="tabs" {...props} />;
}

/**
 * The tab strip scrolls sideways when the tabs do not fit (phones, narrow windows): the edge with more tabs behind
 * it fades (data-fade, globals.css) and the selected tab is scrolled into view whenever the selection changes.
 */
export function TabsList({
  className,
  ref,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List>) {
  const ownRef = useEdgeFade<HTMLDivElement>('[data-state="active"]', undefined, "data-state");
  const setRef = React.useCallback(
    (node: HTMLDivElement | null) => {
      ownRef.current = node;
      if (typeof ref === "function") ref(node);
      else if (ref) ref.current = node;
    },
    [ownRef, ref],
  );
  return (
    <TabsPrimitive.List
      ref={setRef}
      data-slot="tabs-list"
      className={cn(
        "mb-5 flex gap-[5px] overflow-auto border-b border-border",
        className,
      )}
      {...props}
    />
  );
}

export function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      className={cn(
        "inline-flex cursor-pointer items-center gap-2 whitespace-nowrap bg-transparent px-4 py-[13px] text-sm font-semibold text-muted-foreground transition-colors hover:bg-secondary/60 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50 data-[state=active]:text-primary data-[state=active]:shadow-[inset_0_-2px_var(--color-primary)]",
        className,
      )}
      {...props}
    />
  );
}

export function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      data-slot="tabs-content"
      className={cn(
        "focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-primary",
        className,
      )}
      {...props}
    />
  );
}
