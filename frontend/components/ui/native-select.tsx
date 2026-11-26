import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Styled native <select>. Preferred for filters: native mobile pickers,
 * form semantics, and Playwright `selectOption` / `toHaveValue` support.
 */
export function NativeSelect({
  className,
  ...props
}: React.ComponentProps<"select">) {
  return (
    <select
      data-slot="native-select"
      className={cn(
        "cursor-pointer rounded-md border border-input bg-secondary px-[13px] py-[9px] text-sm text-foreground disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}
