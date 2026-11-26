import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

export const alertVariants = cva(
  "relative mb-5 w-full rounded-md border px-5 py-[15px] text-sm [&>svg]:mr-2 [&>svg]:inline [&>svg]:align-[-3px]",
  {
    variants: {
      variant: {
        default: "border-border text-muted-foreground",
        info: "border-primary/40 bg-primary/5 text-foreground",
        warning: "border-warning/40 bg-warning/5 text-warning",
        destructive: "border-[#74363e] bg-[#401e26]/20 text-[#efbbbb]",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

/**
 * Inline notice. Uses role="alert" for the destructive and warning variants
 * and role="status" otherwise; pass `role` to override.
 */
export function Alert({
  className,
  variant,
  role,
  ...props
}: React.ComponentProps<"div"> & VariantProps<typeof alertVariants>) {
  return (
    <div
      data-slot="alert"
      role={
        role ??
        (variant === "destructive" || variant === "warning" ? "alert" : "status")
      }
      className={cn(alertVariants({ variant }), className)}
      {...props}
    />
  );
}

export function AlertTitle({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-title"
      className={cn("font-semibold text-foreground", className)}
      {...props}
    />
  );
}

export function AlertDescription({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-description"
      className={cn("text-sm", className)}
      {...props}
    />
  );
}
