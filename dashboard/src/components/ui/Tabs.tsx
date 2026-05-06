"use client";

import { cn } from "@/lib/utils";
import type { HTMLAttributes, ReactNode } from "react";

interface TabsProps {
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: ReactNode; count?: number }[];
  className?: string;
}

export function Tabs({ value, onChange, options, className }: TabsProps) {
  return (
    <div className={cn("inline-flex items-center gap-1 rounded-lg bg-zinc-900 p-1", className)}>
      {options.map((opt) => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          className={cn(
            "px-3 py-1 text-sm rounded-md transition-colors flex items-center gap-2",
            value === opt.value
              ? "bg-zinc-700 text-zinc-50"
              : "text-zinc-400 hover:text-zinc-200"
          )}
        >
          {opt.label}
          {typeof opt.count === "number" && (
            <span className="text-xs px-1.5 py-0 rounded bg-zinc-800 text-zinc-300">
              {opt.count}
            </span>
          )}
        </button>
      ))}
    </div>
  );
}

export function TabPanel({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("mt-4", className)} {...props} />;
}
