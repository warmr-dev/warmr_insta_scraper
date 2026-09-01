"use client";

import { Fragment, useMemo, useState } from "react";
import type { TargetStory } from "@/lib/queries";

/**
 * Every story seen from one account, with a link to open the live ones.
 *
 * The hard constraint this component is built around: we keep no copy of the
 * media. It is deleted right after analysis (spec 7.4/11), and Instagram
 * stories expire after 24 hours - so most rows here can never be viewed again
 * by anyone. Showing every row with a link would hand out dead ends, and
 * showing only the live ones would hide the analysis history that explains a
 * target's score.
 *
 * So both are shown, and openable rows are made obvious: live rows get a real
 * link, expired rows say plainly that the story is gone.
 */

const STATE_STYLE: Record<string, { label: string; cls: string; hint: string }> = {
  analyzed: {
    label: "analysed",
    cls: "bg-violet-500/15 text-violet-300",
    hint: "Sent to the AI and scored",
  },
  sent: {
    label: "lead sent",
    cls: "bg-emerald-500/15 text-emerald-300",
    hint: "Scored 7+ and delivered to Slack",
  },
  skipped_video: {
    label: "video",
    cls: "bg-slate-700/40 text-slate-400",
    hint: "Videos are never analysed (spec 1) — costs nothing",
  },
  new: {
    label: "queued",
    cls: "bg-sky-500/15 text-sky-300",
    hint: "Discovered, not yet analysed",
  },
  failed: {
    label: "failed",
    cls: "bg-rose-500/15 text-rose-300",
    hint: "Download or analysis failed — never scored",
  },
  analyzing: {
    label: "analysing",
    cls: "bg-amber-500/15 text-amber-300",
    hint: "In the AI right now",
  },
  checked: {
    label: "checked",
    cls: "bg-slate-600/20 text-slate-300",
    hint: "Business check done",
  },
};

function stateStyle(state: string) {
  return (
    STATE_STYLE[state] ?? {
      label: state,
      cls: "bg-slate-700/40 text-slate-400",
      hint: "",
    }
  );
}

function when(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-GB", { dateStyle: "short", timeStyle: "short" });
}

function scoreTone(score: number | null): string {
  if (score == null) return "text-slate-600";
  if (score >= 7) return "text-emerald-300";
  if (score >= 4) return "text-amber-300";
  return "text-slate-500";
}

type Filter = "all" | "live" | "analysed" | "leads" | "best" | "photos";

// `best` and `leads` are claims about the highest-scoring stories, so those
// views sort by score. Everything else reads better newest-first.
const SCORE_SORTED: ReadonlySet<Filter> = new Set<Filter>(["best", "leads"]);

export function StoryList({
  stories,
  username,
  liveCount,
  initialView,
}: {
  stories: TargetStory[];
  username: string;
  liveCount: number;
  initialView?: string;
}) {
  const [filter, setFilter] = useState<Filter>(() => {
    const wanted = initialView as Filter | undefined;
    // Honour the column that was clicked on /targets. Falling back to "live"
    // would hide the very story the number referred to: a score-8 story is
    // usually older than 24h and so not in the live set at all.
    if (wanted && ["all", "live", "analysed", "leads", "best", "photos"].includes(wanted)) {
      return wanted;
    }
    return liveCount > 0 ? "live" : "all";
  });
  const [open, setOpen] = useState<string | null>(null);

  const visible = useMemo(() => {
    let rows: TargetStory[];
    switch (filter) {
      case "live":
        rows = stories.filter((s) => s.is_live);
        break;
      case "analysed":
        rows = stories.filter((s) => s.final_score != null);
        break;
      case "leads":
        rows = stories.filter((s) => (s.final_score ?? 0) >= 7);
        break;
      case "best":
        // Only scored stories, best first - the "Best" column is a claim about
        // one story and this is the view that shows which.
        rows = stories.filter((s) => s.final_score != null);
        break;
      case "photos":
        rows = stories.filter((s) => s.media_type === 1);
        break;
      default:
        rows = stories;
    }
    if (SCORE_SORTED.has(filter)) {
      rows = [...rows].sort((a, b) => (b.final_score ?? 0) - (a.final_score ?? 0));
    }
    return rows;
  }, [stories, filter]);

  const counts = useMemo(
    () => ({
      all: stories.length,
      live: stories.filter((s) => s.is_live).length,
      analysed: stories.filter((s) => s.final_score != null).length,
      leads: stories.filter((s) => (s.final_score ?? 0) >= 7).length,
      photos: stories.filter((s) => s.media_type === 1).length,
      best: stories.reduce((m, s) => Math.max(m, s.final_score ?? 0), 0),
    }),
    [stories],
  );

  const TABS: { key: Filter; label: string }[] = [
    { key: "live", label: `Open now (${counts.live})` },
    { key: "best", label: `Best first (${counts.best}/10)` },
    { key: "analysed", label: `Analysed (${counts.analysed})` },
    { key: "leads", label: `Leads (${counts.leads})` },
    { key: "photos", label: `Photos (${counts.photos})` },
    { key: "all", label: `All (${counts.all})` },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setFilter(t.key)}
            className={`rounded-lg px-3 py-1.5 text-sm ring-1 transition ${
              filter === t.key
                ? "bg-sky-600/20 text-sky-300 ring-sky-500/40"
                : "text-slate-400 ring-slate-700 hover:bg-slate-800"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {SCORE_SORTED.has(filter) && visible.length > 0 && (
        <p className="text-xs text-slate-500">
          Sorted by score, highest first — the top row is the story behind this
          account&apos;s best result.
        </p>
      )}

      <p className="text-xs text-slate-500">
        Instagram stories disappear after 24 hours, and we delete the media as
        soon as it has been analysed — so only the{" "}
        <span className="text-slate-300">{counts.live}</span> story
        {counts.live === 1 ? "" : "s"} still inside that window can be opened.
        Older rows keep their analysis, but the image itself is gone.
      </p>

      <div className="overflow-x-auto rounded-xl border border-slate-800">
        <table className="w-full text-sm">
          <thead className="bg-slate-900/70 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="whitespace-nowrap px-4 py-3 font-medium">Posted</th>
              <th className="whitespace-nowrap px-4 py-3 font-medium">Type</th>
              <th className="whitespace-nowrap px-4 py-3 font-medium">State</th>
              <th className="whitespace-nowrap px-4 py-3 text-right font-medium">
                Score
              </th>
              <th className="px-4 py-3 font-medium">What the AI saw</th>
              <th className="whitespace-nowrap px-4 py-3 font-medium">Open</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {visible.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-slate-500">
                  {filter === "live"
                    ? "No stories from this account are still within Instagram's 24-hour window."
                    : filter === "leads"
                      ? "No story from this account has scored 7 or above."
                      : filter === "best" || filter === "analysed"
                        ? "No story from this account has been analysed yet."
                        : "Nothing here yet."}
                </td>
              </tr>
            ) : (
              visible.map((s) => {
                const style = stateStyle(s.pipeline_state);
                const expanded = open === s.story_id;
                const hasDetail =
                  s.ai_explanation || s.ocr_text || s.service_category;
                return (
                  // Fragment carries the key: a row and its detail row are two
                  // siblings from one item.
                  <Fragment key={s.story_id}>
                    <tr
                      className={`hover:bg-slate-900/40 ${
                        hasDetail ? "cursor-pointer" : ""
                      }`}
                      onClick={() =>
                        hasDetail && setOpen(expanded ? null : s.story_id)
                      }
                    >
                      <td className="whitespace-nowrap px-4 py-3 text-slate-400">
                        {when(s.taken_at)}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-slate-400">
                        {s.media_type === 1 ? "photo" : "video"}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <span
                          className={`rounded px-2 py-0.5 text-xs ${style.cls}`}
                          title={style.hint}
                        >
                          {style.label}
                        </span>
                      </td>
                      <td
                        className={`whitespace-nowrap px-4 py-3 text-right tabular-nums ${scoreTone(
                          s.final_score,
                        )}`}
                      >
                        {s.final_score != null ? `${s.final_score}/10` : "—"}
                      </td>
                      <td className="px-4 py-3 text-slate-400">
                        {s.service_category ? (
                          <span className="text-slate-300">{s.service_category}</span>
                        ) : s.pipeline_state === "skipped_video" ? (
                          <span className="text-slate-600">not analysed</span>
                        ) : (
                          <span className="text-slate-600">—</span>
                        )}
                        {hasDetail && (
                          <span className="ml-2 text-xs text-slate-600">
                            {expanded ? "▾ hide" : "▸ details"}
                          </span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        {s.is_live ? (
                          <a
                            href={`https://www.instagram.com/stories/${username}/${s.story_id}/`}
                            target="_blank"
                            rel="noreferrer"
                            onClick={(e) => e.stopPropagation()}
                            className="text-sky-400 underline-offset-2 hover:underline"
                          >
                            View story ↗
                          </a>
                        ) : (
                          <span
                            className="text-xs text-slate-600"
                            title="Instagram deleted it after 24 hours, and we keep no copy"
                          >
                            expired
                          </span>
                        )}
                      </td>
                    </tr>
                    {expanded && (
                      <tr className="bg-slate-900/60">
                        <td colSpan={6} className="px-4 py-4">
                          <div className="space-y-3 text-sm">
                            {s.ai_explanation && (
                              <div>
                                <div className="text-xs uppercase tracking-wide text-slate-500">
                                  AI verdict
                                </div>
                                <p className="mt-1 text-slate-300">
                                  {s.ai_explanation}
                                </p>
                              </div>
                            )}
                            {s.ocr_text && (
                              <div>
                                <div className="text-xs uppercase tracking-wide text-slate-500">
                                  Text read from the image
                                </div>
                                <p className="mt-1 whitespace-pre-wrap text-slate-400">
                                  {s.ocr_text}
                                </p>
                              </div>
                            )}
                            <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-500">
                              {s.cheap_score != null && (
                                <span>cheap model: {s.cheap_score}/10</span>
                              )}
                              {s.smart_score != null && (
                                <span>smart model: {s.smart_score}/10</span>
                              )}
                              {s.intent_type && <span>intent: {s.intent_type}</span>}
                              {s.analyzed_at && (
                                <span>analysed {when(s.analyzed_at)}</span>
                              )}
                              <span>id {s.story_id}</span>
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
