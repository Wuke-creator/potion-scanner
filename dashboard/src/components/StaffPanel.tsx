"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { formatRelativeTime } from "@/lib/utils";
import type { StaffMember } from "@/lib/types";

export function StaffPanel() {
  const [data, setData] = useState<StaffMember[] | null>(null);
  useEffect(() => {
    fetch("/api/staff")
      .then((r) => (r.ok ? r.json() : Promise.reject(r.statusText)))
      .then(setData)
      .catch(() => setData([]));
  }, []);

  if (data === null) {
    return (
      <Card>
        <CardContent className="p-6 text-sm text-zinc-500 text-center">
          Loading…
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg text-zinc-100">Staff performance (30d)</CardTitle>
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <div className="text-sm text-zinc-500 py-8 text-center">
            No senior staff configured. Set POTION_SENIOR_STAFF_IDS in your env.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {data.map((s) => (
              <div
                key={s.staff_user_id}
                className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-3"
              >
                <div className="flex items-start justify-between mb-2">
                  <div>
                    <div className="font-medium text-zinc-100">{s.display_name}</div>
                    <div className="text-xs text-zinc-500 font-mono">{s.staff_user_id}</div>
                  </div>
                  {s.unacked_mentions > 0 && (
                    <Badge variant="warn">{s.unacked_mentions} unacked</Badge>
                  )}
                </div>
                <div className="grid grid-cols-3 gap-2 text-xs mt-3">
                  <div>
                    <div className="text-zinc-500">Messages</div>
                    <div className="text-lg font-semibold">{s.total_messages_30d.toLocaleString()}</div>
                  </div>
                  <div>
                    <div className="text-zinc-500">Active days</div>
                    <div className="text-lg font-semibold">{s.active_days_30d}/30</div>
                  </div>
                  <div>
                    <div className="text-zinc-500">Pings recv'd</div>
                    <div className="text-lg font-semibold">{s.leadership_mentions_30d}</div>
                  </div>
                </div>
                <div className="text-xs text-zinc-500 mt-2">
                  Last seen: {formatRelativeTime(s.last_seen_at)}
                </div>
                {s.per_channel.length > 0 && (
                  <div className="mt-2 pt-2 border-t border-zinc-800/60">
                    <div className="text-xs text-zinc-500 mb-1">By channel</div>
                    <div className="flex flex-wrap gap-1">
                      {s.per_channel.slice(0, 5).map((c) => (
                        <span
                          key={c.channel_id}
                          className="text-xs bg-zinc-800/60 rounded px-2 py-0.5"
                        >
                          {c.channel_name}{" "}
                          <span className="text-zinc-400">{c.count}</span>
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
