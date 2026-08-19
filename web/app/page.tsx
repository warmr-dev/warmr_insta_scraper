import { Shell } from "@/components/Shell";
import { Stat } from "@/components/Stat";
import { Table } from "@/components/Table";
import {
  getCategories,
  getDailyActivity,
  getOverview,
  getPipelineStates,
  getScoreDistribution,
} from "@/lib/queries";

export const dynamic = "force-dynamic";

/**
 * Overview and Analytics used to be two pages showing the same funnel, the same
 * cost figures and the same daily table. They are one page now: each number
 * appears exactly once, ordered by the question it answers — is it running,
 * what did it find, what did it cost, what is the classifier seeing.
 */
export default async function OverviewPage() {
  // Parallel, not sequential: at ~2.5s per round-trip to the database that is
  // the difference between 3s and 15s on this page.
  const [overview, daily, states, scores, categories] = await Promise.all([
    getOverview(),
    getDailyActivity(30),
    getPipelineStates(),
    getScoreDistribution(),
    getCategories(15),
  ]);

  const pct = (part: number, whole: number) =>
    whole > 0 ? Math.round((part / whole) * 100) : 0;

  const videoShare = pct(overview.videos_skipped, overview.stories_total);
  const photoShare = pct(overview.photos_total, overview.stories_total);
  const analysedShare = pct(overview.analysed, overview.photos_total);
  const leadRate =
    overview.analysed > 0
      ? ((overview.leads / overview.analysed) * 100).toFixed(1)
      : "0.0";
  const costPerPhoto =
    overview.analysed > 0 ? overview.spend_usd / overview.analysed : 0;
  const costPerLead =
    overview.leads > 0 ? overview.spend_usd / overview.leads : 0;

  const collectionStopped = overview.accounts_live === 0;

  return (
    <Shell
      title="Overview"
      subtitle="What the pipeline has collected, what it found, and what it cost"
    >
      {/* Health first: every number below is meaningless if collection stopped. */}
      {collectionStopped && (
        <div className="mb-6 rounded-lg border border-amber-900/60 bg-amber-950/30 px-4 py-3 text-sm text-amber-200">
          <strong className="font-medium">Collection is stopped.</strong> No
          session has working cookies, so nothing new is being found. Refresh
          them on the Accounts &amp; Sessions tab — the figures below are
          historical until then.
        </div>
      )}

      <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-slate-400">
        Right now
      </h2>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat
          label="Sessions live"
          value={overview.accounts_live}
          tone={overview.accounts_live > 0 ? "good" : "bad"}
          hint={collectionStopped ? "collection is stopped" : "collecting"}
        />
        <Stat
          label="Sessions dead"
          value={overview.accounts_dead}
          tone={overview.accounts_dead > 0 ? "warn" : "default"}
          hint={overview.accounts_dead > 0 ? "need fresh cookies" : undefined}
        />
        <Stat
          label="Monitored accounts"
          value={overview.targets.toLocaleString()}
          hint="followed by our sessions"
        />
        <Stat
          label="Leads"
          value={overview.leads.toLocaleString()}
          tone={overview.leads > 0 ? "good" : "default"}
          hint={`${leadRate}% of analysed photos`}
        />
      </div>

      {/* The funnel: each step drops volume, and each drop is a cost saving. */}
      <h2 className="mb-3 mt-10 text-sm font-medium uppercase tracking-wide text-slate-400">
        Funnel
      </h2>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Stat
          label="Stories found"
          value={overview.stories_total.toLocaleString()}
          hint="deduplicated on story_id"
        />
        <Stat
          label="Videos skipped"
          value={overview.videos_skipped.toLocaleString()}
          hint={`${videoShare}% — dropped before download`}
          tone="good"
        />
        <Stat
          label="Photos"
          value={overview.photos_total.toLocaleString()}
          hint={`${photoShare}% of all stories`}
        />
        <Stat
          label="Analysed"
          value={overview.analysed.toLocaleString()}
          hint={`${analysedShare}% of photos — rest skipped or pending`}
        />
        <Stat
          label="Leads"
          value={overview.leads.toLocaleString()}
          tone={overview.leads > 0 ? "good" : "default"}
          hint="score 7 or above"
        />
      </div>

      <h2 className="mb-3 mt-10 text-sm font-medium uppercase tracking-wide text-slate-400">
        Cost
      </h2>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat
          label="Total AI spend"
          value={`$${overview.spend_usd.toFixed(4)}`}
          hint={`${overview.ai_calls.toLocaleString()} AI calls`}
        />
        <Stat
          label="Per analysed photo"
          value={`$${costPerPhoto.toFixed(5)}`}
        />
        <Stat
          label="Per lead"
          value={overview.leads > 0 ? `$${costPerLead.toFixed(4)}` : "—"}
          hint={overview.leads > 0 ? undefined : "no leads yet"}
        />
        <Stat
          label="Videos skipped free"
          value={overview.videos_skipped.toLocaleString()}
          tone="good"
          hint="never reached the AI"
        />
      </div>

      <section className="mt-10 grid gap-8 lg:grid-cols-2">
        <div>
          <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-slate-400">
            Last 30 days
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
            <p className="mt-2 text-xs text-slate-500">
              7 and above becomes a lead. Everything below is stored but never
              sent on.
            </p>
          </div>

          <div>
            <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-slate-400">
              Categories detected
            </h2>
            <Table head={["Category", "Photos"]} empty="Nothing analysed yet">
              {categories.map((row) => (
                <tr key={row.service_category} className="hover:bg-slate-900/40">
                  <td className="px-4 py-2.5 text-slate-300">
                    {row.service_category}
                  </td>
                  <td className="px-4 py-2.5 tabular-nums">{row.count}</td>
                </tr>
              ))}
            </Table>
            <p className="mt-2 text-xs text-slate-500">
              A category is detected even when the story is rejected — a florist
              advertising their own services scores low because they are selling,
              not buying.
            </p>
          </div>

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
        </div>
      </section>
    </Shell>
  );
}
