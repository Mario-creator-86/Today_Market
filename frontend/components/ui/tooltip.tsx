"use client";
import * as React from "react";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { cn } from "@/lib/utils";

export function TooltipProvider({
  delayDuration = 200,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return <TooltipPrimitive.Provider delayDuration={delayDuration} {...props} />;
}

/**
 * Self-contained tooltip (brings its own provider). Tooltips only supplement
 * visible text; never put the only copy of important information in one.
 * The trigger must be focusable (use `asChild` on a button) for keyboard users.
 */
export function Tooltip(
  props: React.ComponentProps<typeof TooltipPrimitive.Root>,
) {
  return (
    <TooltipProvider>
      <TooltipPrimitive.Root data-slot="tooltip" {...props} />
    </TooltipProvider>
  );
}

export const TooltipTrigger = TooltipPrimitive.Trigger;

export function TooltipContent({
  className,
  sideOffset = 6,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Content>) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        data-slot="tooltip-content"
        sideOffset={sideOffset}
        className={cn(
          "z-50 max-w-xs rounded-md border border-border bg-popover px-3 py-1.5 text-xs leading-relaxed text-popover-foreground shadow-lg shadow-black/40",
          className,
        )}
        {...props}
      />
    </TooltipPrimitive.Portal>
  );
}
