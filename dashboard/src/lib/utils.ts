import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatRelativeTime(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "never";
  const diff = Math.floor(Date.now() / 1000) - epochSeconds;
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}d ago`;
  return new Date(epochSeconds * 1000).toLocaleDateString("en-GB");
}

export function formatNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toString();
}

export function formatPct(num: number, denom: number, digits = 1): string {
  if (!denom) return "—";
  return `${((num / denom) * 100).toFixed(digits)}%`;
}

export function discordJump(url: string | null | undefined): string {
  if (!url) return "#";
  return url.replace(/^https?:\/\/discord\.com\//, "discord://discord.com/");
}
