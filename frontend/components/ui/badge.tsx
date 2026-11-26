import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/** The `badge` class is kept as a stable hook for tests and page CSS. */
export const badgeVariants = cva(
  "badge inline-block whitespace-nowrap rounded-sm px-[7px] py-[3px] text-[10px] font-semibold leading-normal",
  {
    variants: {
      variant: {
        neutral: "bg-warning/8 text-warning",
        positive: "bg-positive/8 text-positive",
        negative: "bg-destructive/8 text-destructive",
        info: "bg-primary/10 text-primary",
        outline: "border border-border text-muted-foreground",
      },
    },
    defaultVariants: { variant: "neutral" },
  },
);

export type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>["variant"]>;

export type BadgeProps = React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & { asChild?: boolean };

export function Badge({
  className,
  variant,
  asChild = false,
  ...props
}: BadgeProps) {
  const Comp = asChild ? Slot : "span";
  return (
    <Comp
      data-slot="badge"
      data-variant={variant ?? "neutral"}
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  );
}
