"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Tabs } from "@/components/ui/Tabs";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { discordJump, formatRelativeTime } from "@/lib/utils";
import type { LeadershipMention } from "@/lib/types";

export function LeadershipPanel() {
  const [tab, setTab] = useState<"unacked" | "all">("unacked");
  const [data, setData] = useState<LeadershipMention[] | null>(null);

  const load = () => {
    setData(null);
    const qs = tab === "unacked" ? "?acknowledged=false" : "";
    fetch(`/api/leadership${qs}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.statusText)))
      .then(setData)
      .catch(() => setData([]));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  const ack = async (id: number) => {
    await fetch(`/api/leadership/${id}/ack`, { method: "POST" });
    load();
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between flex-wrap gap-3">
          <CardTitle className="text-lg text-zinc-100">Leadership pings</CardTitle>
          <Tabs
            value={tab}
            onChange={(v) => setTab(v as "unacked" | "all")}
            options={[
              { value: "unacked", label: "Unacked" },
              { value: "all", label: "All" },
            ]}
          />
        </div>
      </CardHeader>
      <CardContent>
        {data === null ? (
          <Loading />
        ) : data.length === 0 ? (
          <Empty msg="No leadership pings to show. Either no senior staff have been @mentioned in the configured channels, or the bot's ops capture isn't running yet." />
        ) : (
          <div className="space-y-2">
            {data.map((m) => (
              <div
                key={m.id}
                className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-3"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 text-xs text-zinc-400 mb-1">
                      <span className="font-medium text-zinc-200">{m.author_name}</span>
                      <span>pinged</span>
                      <Badge variant="warn">@{m.mentioned_name}</Badge>
                      <span>in</span>
                      <span className="font-mono">
                        {m.thread_name ?? `#${m.channel_name}`}
                      </span>
                      <span>·</span>
                      <span>{formatRelativeTime(m.created_at)}</span>
                    </div>
                    <div className="text-sm text-zinc-200 whitespace-pre-wrap break-words">
                      {m.content || <span className="italic text-zinc-500">(empty content)</span>}
                    </div>
                    {m.jump_url && (
                      <a
                        href={discordJump(m.jump_url)}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs text-blue-400 hover:underline mt-1 inline-block"
                      >
                        Jump to Discord →
                      </a>
                    )}
                  </div>
                  <div className="flex flex-col items-end gap-1">
                    {m.acknowledged ? (
                      <Badge variant="success">acked</Badge>
                    ) : (
                      <Button size="sm" onClick={() => ack(m.id)}>
                        Acknowledge
                      </Button>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Loading() {
  return <div className="text-sm text-zinc-500 py-8 text-center">Loading…</div>;
}

function Empty({ msg }: { msg: string }) {
  return <div className="text-sm text-zinc-500 py-8 text-center max-w-lg mx-auto">{msg}</div>;
}
