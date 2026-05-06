import { cn } from "@/lib/utils";
import type { HTMLAttributes, TdHTMLAttributes, ThHTMLAttributes } from "react";

export function Table({ className, ...props }: HTMLAttributes<HTMLTableElement>) {
  return (
    <div className="overflow-x-auto">
      <table className={cn("w-full text-sm", className)} {...props} />
    </div>
  );
}

export function THead({ className, ...props }: HTMLAttributes<HTMLTableSectionElement>) {
  return <thead className={cn("border-b border-zinc-800 text-zinc-400", className)} {...props} />;
}

export function TBody({ className, ...props }: HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody className={cn("divide-y divide-zinc-800/60", className)} {...props} />;
}

export function TR({ className, ...props }: HTMLAttributes<HTMLTableRowElement>) {
  return <tr className={cn("hover:bg-zinc-900/40", className)} {...props} />;
}

export function TH({ className, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      className={cn("px-3 py-2 text-left text-xs font-medium uppercase tracking-wider", className)}
      {...props}
    />
  );
}

export function TD({ className, ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn("px-3 py-2", className)} {...props} />;
}
