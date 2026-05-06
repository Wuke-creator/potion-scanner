import { cn } from "@/lib/utils";
import type { HTMLAttributes } from "react";

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: "default" | "success" | "warn" | "destructive" | "outline";
}

export function Badge({ className, variant = "default", ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
        variant === "default" && "bg-zinc-800 text-zinc-300",
        variant === "success" && "bg-emerald-500/15 text-emerald-300",
        variant === "warn" && "bg-amber-500/15 text-amber-300",
        variant === "destructive" && "bg-red-500/15 text-red-300",
        variant === "outline" && "border border-zinc-700 text-zinc-300",
        className
      )}
      {...props}
    />
  );
}
