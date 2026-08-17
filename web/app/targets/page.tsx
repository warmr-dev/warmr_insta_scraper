import { Shell } from "@/components/Shell";
import { Table } from "@/components/Table";
import { getTargetActivity } from "@/lib/queries";

export const dynamic = "force-dynamic";

const STATUS_STYLE: Record<string, string> = {
  proven: "bg-emerald-950/60 text-emerald-300",
  promising: "bg-sky-950/60 text-sky-300",
  unproven: "bg-slate-800 text-slate-400",
  exhausted: "bg-amber-950/60 text-amber-300",
};

const STATUS_HINT: Record<string, string> = {
  proven: "produced a lead — always analysed",
  promising: "scored 4-6 — always analysed",
  unproven: "not enough history — analysed",
  exhausted: "8+ photos, no signal — skipped until recheck",
};

export default async function TargetsPage() {
  const targets = await getTargetActivity(200);
  return (
    <Shell
      title="Monitored Accounts"
      subtitle="Who posts regularly, and who the pipeline has stopped paying to analyse"
    >
      <div className="mb-6 flex flex-wrap gap-3 text-xs text-slate-400">
        {Object.entries(STATUS_HINT).map(([status, hint]) => (
          <span key={status} className="flex items-center gap-1.5">
            <span className={`rounded px-2 py-0.5 ${STATUS_STYLE[status]}`}>
              {status}
            </span>
            {hint}
          </span>
        ))}
      </div>

      <Table
        head={["Account", "Status", "Stories", "Photos", "Analysed", "Leads", "Best", "Last story"]}
        empty="No monitored accounts have posted yet"
      >
        {targets.map((target) => (
          <tr key={target.username} className="hover:bg-slate-900/40">
            <td className="whitespace-nowrap px-4 py-3">
              <a
                href={target.instagram_url ?? `https://instagram.com/${target.username}`}
                target="_blank"
                rel="noreferrer"
                className="text-sky-400 hover:underline"
              >
                @{target.username}
              </a>
            </td>
            <td className="px-4 py-3">
              <span className={`rounded px-2 py-0.5 text-xs ${STATUS_STYLE[target.status]}`}>
                {target.status}
              </span>
            </td>
            <td className="px-4 py-3 tabular-nums">{target.stories}</td>
            <td className="px-4 py-3 tabular-nums">{target.photos}</td>
            <td className="px-4 py-3 tabular-nums">{target.analysed}</td>
            <td className="px-4 py-3 tabular-nums">
              {Number(target.leads) > 0 ? (
                <span className="text-emerald-400">{target.leads}</span>
              ) : (
                "0"
              )}
            </td>
            <td className="px-4 py-3 tabular-nums text-slate-400">{target.best_score}</td>
            <td className="whitespace-nowrap px-4 py-3 text-slate-500">
              {target.last_story_at
                ? new Date(target.last_story_at).toLocaleString("en-GB", {
                    dateStyle: "short",
                    timeStyle: "short",
                  })
                : "—"}
            </td>
          </tr>
        ))}
      </Table>
    </Shell>
  );
}
