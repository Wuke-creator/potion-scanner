"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Tabs, TabPanel } from "@/components/ui/Tabs";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/Table";
import { discordJump, formatRelativeTime } from "@/lib/utils";
import { ChevronDown } from "lucide-react";
import type { TicketRow, TicketStatus, ComplaintSeverity } from "@/lib/types";

const STATUSES: { value: TicketStatus | "all"; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "in_progress", label: "In progress" },
  { value: "resolved", label: "Resolved" },
  { value: "all", label: "All" },
];

const SOURCES: { value: "ticket" | "general" | "alpha" | "all"; label: string }[] = [
  { value: "all", label: "All sources" },
  { value: "ticket", label: "Support threads" },
  { value: "general", label: "General chat" },
  { value: "alpha", label: "Alpha chat" },
];

const MODES: { value: "complaints" | "all"; label: string }[] = [
  { value: "complaints", label: "Complaints only" },
  { value: "all", label: "Every message" },
];

export function TicketsPanel() {
  const [status, setStatus] = useState<TicketStatus | "all">("open");
  const [source, setSource] = useState<"ticket" | "general" | "alpha" | "all">("all");
  const [mode, setMode] = useState<"complaints" | "all">("complaints");
  const [rows, setRows] = useState<TicketRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);

  const load = () => {
    const qs = new URLSearchParams({ status, source, mode });
    fetch(`/api/tickets?${qs.toString()}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.statusText)))
      .then((rows) => {
        setRows(rows);
        setError(null);
      })
      .catch((e) => setError(String(e)));
  };

  useEffect(() => {
    setRows(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, source, mode]);

  const updateStatus = async (id: number, next: TicketStatus) => {
    await fetch(`/api/tickets/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ status: next }),
    });
    load();
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <CardTitle className="text-lg text-zinc-100">Customer complaints</CardTitle>
            <div className="text-xs text-zinc-500 mt-0.5">
              {mode === "complaints"
                ? "Support threads + flagged messages from #general / #alpha"
                : "Every captured message in the monitored channels"}
            </div>
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <Tabs
              value={mode}
              onChange={(v) => setMode(v as "complaints" | "all")}
              options={MODES.map((m) => ({ value: m.value, label: m.label }))}
            />
          </div>
        </div>
        <div className="flex items-center gap-3 flex-wrap mt-3">
          <Tabs
            value={status}
            onChange={(v) => setStatus(v as TicketStatus | "all")}
            options={STATUSES.map((s) => ({ value: s.value, label: s.label }))}
          />
          <select
            value={source}
            onChange={(e) =>
              setSource(e.target.value as "ticket" | "general" | "alpha" | "all")
            }
            className="rounded-md bg-zinc-900 border border-zinc-800 px-2 py-1 text-sm"
          >
            {SOURCES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </div>
      </CardHeader>
      <CardContent>
        <TabPanel className="mt-0">
          {error && <div className="text-sm text-red-400 mb-3">{error}</div>}
          {rows === null ? (
            <div className="text-sm text-zinc-500">Loading…</div>
          ) : rows.length === 0 ? (
            <div className="text-sm text-zinc-500 py-8 text-center max-w-md mx-auto">
              No {mode === "complaints" ? "complaints" : "messages"} match this filter.
              {" "}
              If the bot's ops capture isn't enabled yet, set OPS_CAPTURE_ENABLED=true with the channel IDs configured and restart it.
            </div>
          ) : (
            <Table>
              <THead>
                <TR>
                  <TH className="w-28">When</TH>
                  <TH className="w-20">Source</TH>
                  <TH className="w-24">Severity</TH>
                  <TH className="w-44">Channel</TH>
                  <TH className="w-32">Author</TH>
                  <TH>Message</TH>
                  <TH className="w-32">Status</TH>
                </TR>
              </THead>
              <TBody>
                {rows.map((t) => {
                  const isOpen = expanded === t.message_id;
                  const stale = t.status === "open" && (Date.now() / 1000 - t.created_at) > 48 * 3600;
                  return (
                    <TR
                      key={t.message_id}
                      className={
                        t.complaint_severity === "high"
                          ? "bg-red-500/5"
                          : stale
                            ? "bg-amber-500/5"
                            : ""
                      }
                    >
                      <TD className="text-zinc-400">{formatRelativeTime(t.created_at)}</TD>
                      <TD>
                        <Badge variant={t.source === "ticket" ? "default" : "outline"}>
                          {t.source === "ticket" ? "thread" : t.source}
                        </Badge>
                      </TD>
                      <TD>
                        <SeverityBadge severity={t.complaint_severity} />
                      </TD>
                      <TD className="text-zinc-300">
                        {t.thread_name ? (
                          <span className="font-mono text-xs">{t.thread_name}</span>
                        ) : (
                          `#${t.channel_name}`
                        )}
                      </TD>
                      <TD className="text-zinc-300">
                        {t.author_name}
                        {t.author_is_staff && (
                          <Badge variant="success" className="ml-1">staff</Badge>
                        )}
                      </TD>
                      <TD className="text-zinc-200 align-top">
                        <div className="flex items-start gap-2">
                          <MessageLink
                            content={t.content}
                            jumpUrl={t.jump_url}
                            expanded={isOpen}
                          />
                          {t.content.length > 120 && (
                            <button
                              onClick={() =>
                                setExpanded(isOpen ? null : t.message_id)
                              }
                              className="text-zinc-500 hover:text-zinc-300 shrink-0 mt-0.5"
                              title={isOpen ? "Collapse" : "Expand full message"}
                            >
                              <ChevronDown
                                className={
                                  "w-4 h-4 transition-transform " +
                                  (isOpen ? "rotate-180" : "")
                                }
                              />
                            </button>
                          )}
                        </div>
                        {t.staff_notes && (
                          <div className="mt-1 text-xs text-zinc-400">
                            Notes: {t.staff_notes}
                          </div>
                        )}
                      </TD>
                      <TD>
                        <div className="flex items-center gap-1">
                          <StatusBadge status={t.status} />
                          {t.status !== "resolved" && (
                            <>
                              {t.status !== "in_progress" && (
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  onClick={() => updateStatus(t.message_id, "in_progress")}
                                  title="Mark in progress"
                                >
                                  ▶
                                </Button>
                              )}
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => updateStatus(t.message_id, "resolved")}
                                title="Mark resolved"
                              >
                                ✓
                              </Button>
                            </>
                          )}
                          {t.status === "resolved" && (
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => updateStatus(t.message_id, "open")}
                              title="Reopen"
                            >
                              ↺
                            </Button>
                          )}
                        </div>
                      </TD>
                    </TR>
                  );
                })}
              </TBody>
            </Table>
          )}
        </TabPanel>
      </CardContent>
    </Card>
  );
}

function StatusBadge({ status }: { status: TicketStatus }) {
  if (status === "resolved") return <Badge variant="success">resolved</Badge>;
  if (status === "in_progress") return <Badge variant="warn">in progress</Badge>;
  return <Badge>open</Badge>;
}

// Renders the message text as a discord:// link so the desktop app
// captures the click. Falls back to plain text when the bot didn't
// record a jump_url (eg. seeded test rows). The text is truncated to
// 120 chars unless ``expanded`` is true; the chevron next to it owns
// the expand/collapse toggle.
function MessageLink({
  content,
  jumpUrl,
  expanded,
}: {
  content: string;
  jumpUrl: string;
  expanded: boolean;
}) {
  if (!content) {
    return <span className="italic text-zinc-500">(empty content)</span>;
  }
  const text =
    expanded || content.length <= 120 ? content : content.slice(0, 120) + "…";
  if (!jumpUrl) {
    return <span className="text-zinc-200 break-words">{text}</span>;
  }
  return (
    <a
      href={discordJump(jumpUrl)}
      target="_blank"
      rel="noreferrer"
      className="text-zinc-200 hover:text-blue-400 underline-offset-4 hover:underline break-words"
      title="Open in Discord desktop app"
    >
      {text}
    </a>
  );
}

function SeverityBadge({ severity }: { severity: ComplaintSeverity }) {
  if (severity === "high") return <Badge variant="destructive">high</Badge>;
  if (severity === "medium") return <Badge variant="warn">medium</Badge>;
  if (severity === "low") return <Badge variant="outline">low</Badge>;
  return <span className="text-xs text-zinc-600">—</span>;
}
