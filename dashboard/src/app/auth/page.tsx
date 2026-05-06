import { redirect } from "next/navigation";
import { expectedToken } from "@/lib/auth";

interface PageProps {
  searchParams: Promise<{ token?: string; returnTo?: string; bad?: string }>;
}

export default async function AuthPage({ searchParams }: PageProps) {
  const { token, returnTo, bad } = await searchParams;
  const expected = expectedToken();
  if (!expected) {
    redirect("/");
  }
  if (token) {
    // Hand the token to the API route which actually sets the cookie
    // (server components can't write cookies in Next 15+).
    const qs = new URLSearchParams({ token, returnTo: returnTo || "/" });
    redirect(`/api/auth?${qs.toString()}`);
  }
  return (
    <main className="min-h-screen grid place-items-center p-6">
      <div className="max-w-md w-full">
        <h1 className="text-xl font-semibold mb-1">Potion Ops</h1>
        <p className="text-zinc-400 text-sm mb-4">
          {bad
            ? "Bad token. Try again."
            : "Append ?token=YOUR_TOKEN to this URL to sign in. Token comes from the DASHBOARD_BEARER_TOKEN env var on the server."}
        </p>
        <form method="GET" action="/api/auth" className="flex gap-2">
          <input
            type="text"
            name="token"
            placeholder="Paste token"
            className="flex-1 rounded-md bg-zinc-900 border border-zinc-800 px-3 py-2 text-sm"
            autoComplete="off"
          />
          <input type="hidden" name="returnTo" value={returnTo || "/"} />
          <button
            type="submit"
            className="rounded-md bg-blue-600 hover:bg-blue-500 text-white px-3 py-2 text-sm font-medium"
          >
            Sign in
          </button>
        </form>
      </div>
    </main>
  );
}
