"use client";

import { useEffect, useState } from "react";
import { StatsCards } from "@/components/StatsCards";
import { TicketsPanel } from "@/components/TicketsPanel";
import { SignalsPanel } from "@/components/SignalsPanel";
import { EmailPanel } from "@/components/EmailPanel";
import { LeadershipPanel } from "@/components/LeadershipPanel";
import { StaffPanel } from "@/components/StaffPanel";
import type { SummaryCounts } from "@/lib/types";

type Panel = "tickets" | "signals" | "email" | "leadership" | "staff" | null;

export default function Home() {
  const [summary, setSummary] = useState<SummaryCounts | null>(null);
  const [panel, setPanel] = useState<Panel>("tickets");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () =>
      fetch("/api/summary")
        .then((r) => (r.ok ? r.json() : Promise.reject(r.statusText)))
        .then((s) => {
          setSummary(s);
          setError(null);
        })
        .catch((e) => setError(String(e)));
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, []);

  return (
    <main className="min-h-screen px-4 py-6 max-w-screen-2xl mx-auto">
      <header className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Potion Ops</h1>
          <p className="text-xs text-zinc-500">
            Tickets, signals, email, leadership, staff
          </p>
        </div>
        {error && (
          <div className="text-xs text-red-400">
            {error}
          </div>
        )}
      </header>

      <StatsCards
        data={summary}
        active={panel}
        onSelect={(key) => setPanel(panel === key ? null : (key as Panel))}
      />

      <div className="mt-6 space-y-6">
        {panel === "tickets" && <TicketsPanel />}
        {panel === "signals" && <SignalsPanel />}
        {panel === "email" && <EmailPanel />}
        {panel === "leadership" && <LeadershipPanel />}
        {panel === "staff" && <StaffPanel />}
        {panel === null && (
          <div className="text-sm text-zinc-500 py-12 text-center">
            Pick a stat card above to drill in.
          </div>
        )}
      </div>
    </main>
  );
}
