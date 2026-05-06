// Bearer-token gate. The dashboard reads a single shared token from
// DASHBOARD_BEARER_TOKEN. Visit /auth?token=... once and the server sets
// a long-lived httpOnly cookie; every subsequent request is checked
// against that cookie. If DASHBOARD_BEARER_TOKEN is unset (dev mode),
// the gate is disabled and everything passes.
//
// Optional second layer: DASHBOARD_ALLOWED_IPS = comma-separated list
// of IPs. Empty = no IP filtering. Trusts x-forwarded-for first hop.

import { cookies } from "next/headers";
import type { NextRequest } from "next/server";

export const AUTH_COOKIE = "potion_dash_auth";
const ONE_YEAR = 60 * 60 * 24 * 365;

export function expectedToken(): string | null {
  const t = (process.env.DASHBOARD_BEARER_TOKEN || "").trim();
  return t || null;
}

function allowedIps(): string[] {
  const raw = (process.env.DASHBOARD_ALLOWED_IPS || "").trim();
  if (!raw) return [];
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
}

function clientIp(req: NextRequest | Request): string {
  const xf = req.headers.get("x-forwarded-for") || "";
  if (xf) return xf.split(",")[0]!.trim();
  const xr = req.headers.get("x-real-ip") || "";
  return xr.trim();
}

export interface GateResult {
  ok: boolean;
  reason?: "no_cookie" | "bad_cookie" | "ip_blocked";
}

export async function checkRequest(req: NextRequest | Request): Promise<GateResult> {
  const expected = expectedToken();
  // Dev mode: token gate disabled.
  if (!expected) return { ok: true };

  const ips = allowedIps();
  if (ips.length) {
    const ip = clientIp(req);
    if (ip && !ips.includes(ip)) {
      return { ok: false, reason: "ip_blocked" };
    }
  }

  const cookieStore = await cookies();
  const cookie = cookieStore.get(AUTH_COOKIE);
  if (!cookie) return { ok: false, reason: "no_cookie" };
  if (cookie.value !== expected) return { ok: false, reason: "bad_cookie" };
  return { ok: true };
}

export async function setAuthCookie(token: string): Promise<boolean> {
  const expected = expectedToken();
  if (!expected) return true; // dev mode
  if (token !== expected) return false;
  const cookieStore = await cookies();
  cookieStore.set(AUTH_COOKIE, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    maxAge: ONE_YEAR,
    path: "/",
  });
  return true;
}

export async function clearAuthCookie(): Promise<void> {
  const cookieStore = await cookies();
  cookieStore.delete(AUTH_COOKIE);
}

export const COOKIE_TTL_SECONDS = ONE_YEAR;
