import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// Lightweight gate: applies to everything except the /auth handshake,
// /api/auth (the cookie setter), and the static asset paths Next.js owns.
// We don't import lib/auth here because Next.js middleware runs in the
// edge runtime and lib/auth uses node:cookies. Instead we read the
// cookie + env directly.

const AUTH_COOKIE = "potion_dash_auth";

function expectedToken(): string | null {
  const t = (process.env.DASHBOARD_BEARER_TOKEN || "").trim();
  return t || null;
}

function allowedIps(): string[] {
  const raw = (process.env.DASHBOARD_ALLOWED_IPS || "").trim();
  if (!raw) return [];
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
}

function clientIp(req: NextRequest): string {
  const xf = req.headers.get("x-forwarded-for") || "";
  if (xf) return xf.split(",")[0]!.trim();
  const xr = req.headers.get("x-real-ip") || "";
  return xr.trim();
}

export function middleware(req: NextRequest) {
  const expected = expectedToken();
  if (!expected) return NextResponse.next();

  const ips = allowedIps();
  if (ips.length) {
    const ip = clientIp(req);
    if (ip && !ips.includes(ip)) {
      return new NextResponse("Forbidden (ip)", { status: 403 });
    }
  }

  const cookie = req.cookies.get(AUTH_COOKIE);
  if (!cookie || cookie.value !== expected) {
    if (req.nextUrl.pathname.startsWith("/api/")) {
      return new NextResponse(JSON.stringify({ error: "unauthorized" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      });
    }
    const loginUrl = new URL("/auth", req.url);
    loginUrl.searchParams.set("returnTo", req.nextUrl.pathname);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  // Skip /auth (the token entry page), /api/auth (the cookie-setter
  // endpoint), Next.js internals + static files, and /email-assets
  // (public banner images referenced from outbound emails — must be
  // reachable to Gmail's image proxy without the dashboard auth cookie).
  matcher: [
    "/((?!auth|api/auth|email-assets/|_next/static|_next/image|favicon.ico).*)",
  ],
};
