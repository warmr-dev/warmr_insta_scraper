"use client";

import { useEffect, useMemo, useState } from "react";
import type { ActivityAccount, ActivityEvent } from "@/lib/queries";

/**
 * The live activity feed.
 *
 * Client-side so it can poll: the whole point of this page is watching a cycle
 * happen, and a server component would need a manual reload to show the next
 * step. Polling every 5s matches the query cache TTL, so extra viewers cost
 * nothing beyond one query per 5s regardless of how many tabs are open.
 */

const PHASE_STYLE: Record<string, { label: string; cls: string }> = {
  poll: { label: "POLL", cls: "bg-sky-500/15 text-sky-300 ring-sky-500/30" },
  stories_found: {
    label: "STORIES",
    cls: "bg-indigo-500/15 text-indigo-300 ring-indigo-500/30",
  },
  ai_scoring: {
    label: "AI →",
    cls: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  },
  ai_scored: {
    label: "SCORED",
    cls: "bg-violet-500/15 text-violet-300 ring-violet-500/30",
  },
  skipped: {
    label: "SKIPPED",
    cls: "bg-slate-600/20 text-slate-400 ring-slate-600/30",
  },
  lead: { label: "LEAD", cls: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30" },
  error: { label: "ERROR", cls: "bg-rose-500/15 text-rose-300 ring-rose-500/30" },
  cycle: { label: "CYCLE", cls: "bg-slate-500/15 text-slate-300 ring-slate-500/30" },
};

function phaseStyle(phase: string) {
  return (
    PHASE_STYLE[phase] ?? {
      label: phase.toUpperCase(),
      cls: "bg-slate-500/15 text-slate-300 ring-slate-500/30",
    }
  );
}

/** "14:22:07" in the viewer's own timezone - the DB hands us UTC. */
function clock(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "--:--:--" : d.toLocaleTimeString();
}

function ago(iso: string | null): string {
  if (!iso) return "never";
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${Math.round(secs)}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  return `${Math.round(secs / 3600)}h ago`;
}

export function ActivityFeed({
  initialEvents,
  initialAccounts,
}: {
  initialEvents: ActivityEvent[];
  initialAccounts: ActivityAccount[];
}) {
  const [events, setEvents] = useState(initialEvents);
  const [accounts, setAccounts] = useState(initialAccounts);
  const [selected, setSelected] = useState<string | null>(null);
  const [live, setLive] = useState(true);
  const [phases, setPhases] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!live) return;
    let cancelled = false;

    async function tick() {
      try {
        const qs = selected ? `?username=${encodeURIComponent(selected)}` : "";
        const res = await fetch(`/api/activity${qs}`, { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (cancelled) return;
        setEvents(data.events ?? []);
        setAccounts(data.accounts ?? []);
        setError(null);
      } catch (e) {
        // A failed poll must not blank the feed - keep showing the last good
        // data and say so, rather than flashing an empty table.
        if (!cancelled) setError(e instanceof Error ? e.message : "refresh failed");
      }
    }

    const id = setInterval(tick, 5000);
    void tick();
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [live, selected]);

  const visible = useMemo(
    () => (phases.size === 0 ? events : events.filter((e) => phases.has(e.phase))),
    [events, phases],
  );

  function togglePhase(p: string) {
    setPhases((prev) => {
      const next = new Set(prev);
      if (next.has(p)) next.delete(p);
      else next.add(p);
      return next;
    });
  }

  return (
    <div className="space-y-6">
      {/* Per-account strip: who is active, and what they last did. */}
      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => setSelected(null)}
          className={`rounded-lg px-3 py-1.5 text-sm ring-1 transition ${
            selected === null
              ? "bg-sky-600/20 text-sky-300 ring-sky-500/40"
              : "text-slate-400 ring-slate-700 hover:bg-slate-800"
          }`}
        >
          All sessions
        </button>
        {accounts.map((a) => (
          <button
            key={a.username}
            onClick={() => setSelected(a.username)}
            title={a.last_message ?? ""}
            className={`rounded-lg px-3 py-1.5 text-left text-sm ring-1 transition ${
              selected === a.username
                ? "bg-sky-600/20 text-sky-300 ring-sky-500/40"
                : "text-slate-400 ring-slate-700 hover:bg-slate-800"
            }`}
          >
            <span className="font-medium">@{a.username}</span>
            <span className="ml-2 text-xs text-slate-500">
              {a.events} · {ago(a.last_seen)}
            </span>
          </button>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm text-slate-400">
          <input
            type="checkbox"
            checked={live}
            onChange={(e) => setLive(e.target.checked)}
            className="h-4 w-4 rounded border-slate-600 bg-slate-800"
          />
          Live (5s)
          {live && (
            <span className="ml-1 inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-400" />
          )}
        </label>

        <div className="flex flex-wrap gap-1">
          {Object.keys(PHASE_STYLE).map((p) => {
            const on = phases.has(p);
            return (
              <button
                key={p}
                onClick={() => togglePhase(p)}
                className={`rounded px-2 py-1 text-xs ring-1 transition ${
                  on
                    ? phaseStyle(p).cls
                    : "text-slate-500 ring-slate-800 hover:text-slate-300"
                }`}
              >
                {phaseStyle(p).label}
              </button>
            );
          })}
          {phases.size > 0 && (
            <button
              onClick={() => setPhases(new Set())}
              className="rounded px-2 py-1 text-xs text-slate-500 hover:text-slate-300"
            >
              clear
            </button>
          )}
        </div>

        <span className="ml-auto text-xs text-slate-500">
          {visible.length} events
          {error && <span className="ml-2 text-amber-400">· stale: {error}</span>}
        </span>
      </div>

      <div className="overflow-x-auto rounded-xl border border-slate-800">
        <table className="w-full text-sm">
          <thead className="bg-slate-900/70 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="whitespace-nowrap px-4 py-3 font-medium">Time</th>
              <th className="whitespace-nowrap px-4 py-3 font-medium">Account</th>
              <th className="whitespace-nowrap px-4 py-3 font-medium">Phase</th>
              <th className="px-4 py-3 font-medium">What happened</th>
              <th className="px-4 py-3 font-medium">Targets</th>
              <th className="whitespace-nowrap px-4 py-3 text-right font-medium">Took</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {visible.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-slate-500">
                  No activity yet — start the collector and events appear here.
                </td>
              </tr>
            ) : (
              visible.map((e) => {
                const style = phaseStyle(e.phase);
                const bad = e.status === "error" || e.status === "expired";
                const muted = e.phase === "skipped";
                return (
                  <tr
                    key={e.id}
                    className={
                      bad ? "bg-rose-500/5" : muted ? "opacity-60" : undefined
                    }
                  >
                    <td className="whitespace-nowrap px-4 py-2 font-mono text-xs text-slate-500">
                      {clock(e.occurred_at)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2">
                      {e.username === "system" ? (
                        <span className="text-slate-500">system</span>
                      ) : (
                        <span className="text-slate-300">@{e.username}</span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2">
                      <span
                        className={`rounded px-2 py-0.5 text-xs font-medium ring-1 ${style.cls}`}
                      >
                        {style.label}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-slate-300">
                      {e.message}
                      {e.status === "lead" && (
                        <span className="ml-2 rounded bg-emerald-500/15 px-1.5 py-0.5 text-xs text-emerald-300">
                          lead
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-xs text-slate-500">
                      {e.targets && e.targets.length > 0 ? (
                        <span className="break-words">
                          {e.targets.map((t) => (t.startsWith("...") ? t : `@${t}`)).join(", ")}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-right font-mono text-xs text-slate-500">
                      {e.duration_ms != null ? `${(e.duration_ms / 1000).toFixed(1)}s` : "—"}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
