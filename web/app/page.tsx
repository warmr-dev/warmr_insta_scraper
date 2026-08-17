import { Shell } from "@/components/Shell";
import { Stat } from "@/components/Stat";
import { Table } from "@/components/Table";
import {
  getDailyActivity,
  getOverview,
  getPipelineStates,
  getScoreDistribution,
} from "@/lib/queries";

export const dynamic = "force-dynamic";

export default async function OverviewPage() {
  const [overview, daily, states, scores] = await Promise.all([
    getOverview(),
    getDailyActivity(14),
    getPipelineStates(),
    getScoreDistribution(),
  ]);

  // Videos are dropped before download and before any AI call, so this share is
  // the single biggest cost saving in the pipeline.
  const videoShare =
    overview.stories_total > 0
      ? Math.round((overview.videos_skipped / overview.stories_total) * 100)
      : 0;

  const leadRate =
    overview.analysed > 0
      ? ((overview.leads / overview.analysed) * 100).toFixed(1)
      : "0.0";

  const costPerPhoto =
    overview.analysed > 0 ? overview.spend_usd / overview.analysed : 0;

  return (
    <Shell
      title="Overview"
      subtitle="Everything the pipeline has seen, and what it cost"
    >
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Stat
          label="Stories found"
          value={overview.stories_total.toLocaleString()}
          hint={`${overview.photos_total.toLocaleString()} photos`}
        />
        <Stat
          label="Videos skipped"
          value={overview.videos_skipped.toLocaleString()}
          hint={`${videoShare}% of all stories — cost nothing`}
          tone="good"
        />
        <Stat
          label="Photos analysed"
          value={overview.analysed.toLocaleString()}
          hint="reached the AI"
        />
        <Stat
          label="Leads"
          value={overview.leads.toLocaleString()}
          hint={`${leadRate}% of analysed`}
          tone={overview.leads > 0 ? "good" : "default"}
        />
        <Stat
          label="AI spend"
          value={`$${overview.spend_usd.toFixed(4)}`}
          hint={`$${costPerPhoto.toFixed(5)} per photo`}
        />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat
          label="Sessions live"
          value={overview.accounts_live}
          tone={overview.accounts_live > 0 ? "good" : "bad"}
          hint={overview.accounts_live === 0 ? "collection is stopped" : undefined}
        />
        <Stat
          label="Sessions dead"
          value={overview.accounts_dead}
          tone={overview.accounts_dead > 0 ? "warn" : "default"}
          hint={overview.accounts_dead > 0 ? "need fresh cookies" : undefined}
        />
        <Stat label="Monitored accounts" value={overview.targets} />
        <Stat label="AI calls" value={overview.ai_calls.toLocaleString()} />
      </div>

      <section className="mt-10 grid gap-8 lg:grid-cols-2">
        <div>
          <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-slate-400">
            Last 14 days
          </h2>
          <Table
            head={["Day", "Stories", "Photos", "Analysed", "Leads"]}
            empty="No activity recorded yet"
          >
            {daily.map((row) => (
              <tr key={row.day} className="hover:bg-slate-900/40">
                <td className="px-4 py-2.5 text-slate-300">{row.day}</td>
                <td className="px-4 py-2.5 tabular-nums">{row.stories}</td>
                <td className="px-4 py-2.5 tabular-nums">{row.photos}</td>
                <td className="px-4 py-2.5 tabular-nums">{row.analysed}</td>
                <td className="px-4 py-2.5 tabular-nums">
                  {Number(row.leads) > 0 ? (
                    <span className="text-emerald-400">{row.leads}</span>
                  ) : (
                    row.leads
                  )}
                </td>
              </tr>
            ))}
          </Table>
        </div>

        <div className="space-y-8">
          <div>
            <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-slate-400">
              Pipeline state
            </h2>
            <Table head={["State", "Stories"]}>
              {states.map((row) => (
                <tr key={row.pipeline_state} className="hover:bg-slate-900/40">
                  <td className="px-4 py-2.5">
                    <code className="text-xs text-slate-300">
                      {row.pipeline_state}
                    </code>
                  </td>
                  <td className="px-4 py-2.5 tabular-nums">{row.count}</td>
                </tr>
              ))}
            </Table>
          </div>

          <div>
            <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-slate-400">
              Score distribution
            </h2>
            <Table head={["Score", "Photos", ""]} empty="Nothing analysed yet">
              {scores.map((row) => {
                const max = Math.max(...scores.map((s) => Number(s.count)), 1);
                const width = (Number(row.count) / max) * 100;
                const isLead = row.final_score >= 7;
                return (
                  <tr key={row.final_score} className="hover:bg-slate-900/40">
                    <td className="px-4 py-2.5 tabular-nums">
                      <span className={isLead ? "text-emerald-400" : ""}>
                        {row.final_score}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 tabular-nums">{row.count}</td>
                    <td className="px-4 py-2.5">
                      <div
                        className={`h-2 rounded ${isLead ? "bg-emerald-500" : "bg-slate-600"}`}
                        style={{ width: `${Math.max(width, 2)}%` }}
                      />
                    </td>
                  </tr>
                );
              })}
            </Table>
          </div>
        </div>
      </section>
    </Shell>
  );
}
