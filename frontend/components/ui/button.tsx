import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

export const buttonVariants = cva(
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "border border-input bg-secondary text-secondary-foreground hover:bg-accent",
        primary:
          "border border-primary bg-primary text-primary-foreground hover:bg-primary/90",
        outline:
          "border border-input bg-transparent text-foreground hover:bg-secondary",
        ghost:
          "bg-transparent text-muted-foreground hover:bg-secondary hover:text-foreground",
        destructive:
          "border border-destructive/50 bg-destructive/10 text-destructive hover:bg-destructive/20",
        link: "text-primary underline-offset-4 hover:underline",
      },
      size: {
        default: "px-[13px] py-[9px]",
        sm: "px-2.5 py-1.5 text-xs",
        icon: "size-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export type ButtonProps = React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & { asChild?: boolean };

export function Button({
  className,
  variant,
  size,
  asChild = false,
  ...props
}: ButtonProps) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp
      data-slot="button"
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  );
}
