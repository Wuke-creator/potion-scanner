import { NextResponse, type NextRequest } from "next/server";
import { setAuthCookie, expectedToken } from "@/lib/auth";

// Sets the auth cookie when a valid token is provided. Used by the /auth
// page (form GET) and by anyone bookmarking the URL with ?token=.
//
// In dev mode (no DASHBOARD_BEARER_TOKEN set), this just redirects.

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const token = sp.get("token") || "";
  const returnTo = sp.get("returnTo") || "/";
  const safeReturn = returnTo.startsWith("/") ? returnTo : "/";

  if (!expectedToken()) {
    return NextResponse.redirect(new URL(safeReturn, req.url));
  }
  const ok = await setAuthCookie(token);
  if (!ok) {
    return NextResponse.redirect(new URL("/auth?bad=1", req.url));
  }
  return NextResponse.redirect(new URL(safeReturn, req.url));
}
