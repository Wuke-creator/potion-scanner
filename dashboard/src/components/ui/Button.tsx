import { cn } from "@/lib/utils";
import type { ButtonHTMLAttributes } from "react";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost" | "destructive";
  size?: "sm" | "md";
}

export function Button({
  className,
  variant = "primary",
  size = "md",
  ...props
}: ButtonProps) {
  return (
    <button
      className={cn(
        "inline-flex items-center justify-center rounded-md font-medium transition-colors disabled:opacity-50 disabled:pointer-events-none",
        size === "sm" ? "h-7 px-2 text-xs" : "h-9 px-3 text-sm",
        variant === "primary" && "bg-blue-600 text-white hover:bg-blue-500",
        variant === "secondary" && "bg-zinc-800 text-zinc-100 hover:bg-zinc-700",
        variant === "ghost" && "text-zinc-300 hover:bg-zinc-800",
        variant === "destructive" && "bg-red-600 text-white hover:bg-red-500",
        className
      )}
      {...props}
    />
  );
}
