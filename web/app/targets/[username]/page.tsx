import { notFound } from "next/navigation";
import { Shell } from "@/components/Shell";
import { StoryList } from "@/components/StoryList";
import { getTargetDetail } from "@/lib/queries";

export const dynamic = "force-dynamic";

const STATUS_STYLE: Record<string, string> = {
  proven: "bg-emerald-950/60 text-emerald-300",
  promising: "bg-sky-950/60 text-sky-300",
  unproven: "bg-slate-800 text-slate-400",
  exhausted: "bg-amber-950/60 text-amber-300",
};

function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string | number;
  hint?: string;
  tone?: string;
}) {
  return (
    <div
      className="rounded-xl border border-slate-800 bg-slate-900/40 p-4"
      title={hint}
    >
      <div className={`text-2xl font-semibold ${tone ?? "text-slate-200"}`}>
        {value}
      </div>
      <div className="mt-1 text-sm text-slate-400">{label}</div>
      {hint && <div className="mt-1 text-xs text-slate-500">{hint}</div>}
    </div>
  );
}

export default async function TargetDetailPage({
  params,
  searchParams,
}: {
  // Next 16 hands params and searchParams to the page as promises.
  params: Promise<{ username: string }>;
  searchParams: Promise<{ view?: string }>;
}) {
  const { username } = await params;
  // Which column was clicked on /targets. "best" and "leads" are claims about
  // specific stories, so the page opens on those rather than on a chronological
  // list the reader would have to search.
  const { view } = await searchParams;
  const { target, stories } = await getTargetDetail(decodeURIComponent(username));

  if (!target) notFound();

  const profileUrl =
    target.instagram_url ?? `https://www.instagram.com/${target.username}/`;

  return (
    <Shell
      title={`@${target.username}`}
      subtitle="Every story we have seen from this account, and what the AI made of it."
    >
      <div className="mb-6 flex flex-wrap items-center gap-3 text-sm">
        <a
          href="/targets"
          className="text-slate-400 underline-offset-2 hover:text-slate-200 hover:underline"
        >
          ← All monitored accounts
        </a>
        <span className={`rounded px-2 py-0.5 text-xs ${STATUS_STYLE[target.status]}`}>
          {target.status}
        </span>
        <a
          href={profileUrl}
          target="_blank"
          rel="noreferrer"
          className="text-sky-400 underline-offset-2 hover:underline"
        >
          Open profile on Instagram ↗
        </a>
      </div>

      <div className="mb-8 grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="Stories seen" value={target.stories} />
        <Stat label="Photos" value={target.photos} hint="Sent to the AI" />
        <Stat
          label="Videos"
          value={target.videos}
          hint="Skipped — photos only"
          tone="text-slate-400"
        />
        <Stat label="Analysed" value={target.analysed} hint="Cost a model call" />
        <Stat
          label="Leads"
          value={target.leads}
          hint="Scored 7 or above"
          tone={Number(target.leads) > 0 ? "text-emerald-300" : undefined}
        />
        <Stat
          label="Best score"
          value={`${target.best_score}/10`}
          hint={`avg ${Number(target.avg_score).toFixed(1)}`}
        />
      </div>

      <StoryList
        stories={stories}
        username={target.username}
        liveCount={Number(target.live_stories)}
        initialView={view}
      />
    </Shell>
  );
}
