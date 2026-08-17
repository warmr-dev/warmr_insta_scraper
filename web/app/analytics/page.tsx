import { Shell } from "@/components/Shell";
import { Stat } from "@/components/Stat";
import { Table } from "@/components/Table";
import { getCategories, getDailyActivity, getOverview } from "@/lib/queries";

export const dynamic = "force-dynamic";

export default async function AnalyticsPage() {
  const [overview, categories, daily] = await Promise.all([
    getOverview(),
    getCategories(15),
    getDailyActivity(30),
  ]);

  const photoShare =
    overview.stories_total > 0
      ? ((overview.photos_total / overview.stories_total) * 100).toFixed(0)
      : "0";
  const analysedShare =
    overview.photos_total > 0
      ? ((overview.analysed / overview.photos_total) * 100).toFixed(0)
      : "0";
  const costPerLead =
    overview.leads > 0 ? overview.spend_usd / overview.leads : 0;

  return (
    <Shell title="Analytics" subtitle="Funnel, cost and what the classifier sees">
      <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-slate-400">
        Funnel
      </h2>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat
          label="Stories found"
          value={overview.stories_total.toLocaleString()}
          hint="deduplicated on story_id"
        />
        <Stat
          label="Photos"
          value={overview.photos_total.toLocaleString()}
          hint={`${photoShare}% — videos never reach the AI`}
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
          hint={overview.leads > 0 ? `$${costPerLead.toFixed(4)} per lead` : undefined}
        />
      </div>

      <h2 className="mb-3 mt-10 text-sm font-medium uppercase tracking-wide text-slate-400">
        Cost
      </h2>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Total AI spend" value={`$${overview.spend_usd.toFixed(4)}`} />
        <Stat label="AI calls" value={overview.ai_calls.toLocaleString()} />
        <Stat
          label="Per analysed photo"
          value={`$${(overview.analysed > 0 ? overview.spend_usd / overview.analysed : 0).toFixed(5)}`}
        />
        <Stat
          label="Videos skipped free"
          value={overview.videos_skipped.toLocaleString()}
          tone="good"
          hint="dropped before download"
        />
      </div>

      <section className="mt-10 grid gap-8 lg:grid-cols-2">
        <div>
          <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-slate-400">
            Categories detected
          </h2>
          <Table head={["Category", "Photos"]} empty="Nothing analysed yet">
            {categories.map((row) => (
              <tr key={row.service_category} className="hover:bg-slate-900/40">
                <td className="px-4 py-2.5 text-slate-300">{row.service_category}</td>
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
            Last 30 days
          </h2>
          <Table head={["Day", "Stories", "Photos", "Analysed", "Leads"]} empty="No activity">
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
      </section>
    </Shell>
  );
}
