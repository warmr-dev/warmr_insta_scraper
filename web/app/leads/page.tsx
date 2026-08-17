import { Shell } from "@/components/Shell";
import { Table } from "@/components/Table";
import { getLeads } from "@/lib/queries";

export const dynamic = "force-dynamic";

export default async function LeadsPage() {
  const leads = await getLeads(100);
  return (
    <Shell
      title="Leads"
      subtitle="Photo stories scoring 7 or higher — someone asking to hire or buy, in an allowed category"
    >
      <Table
        head={["Account", "Score", "Category", "Intent", "Explanation", "Posted"]}
        empty="No leads yet. The classifier only accepts genuine service requests in the allowed B2B categories."
      >
        {leads.map((lead) => (
          <tr key={lead.story_id} className="hover:bg-slate-900/40">
            <td className="whitespace-nowrap px-4 py-3">
              <a
                href={lead.instagram_url ?? `https://instagram.com/${lead.username}`}
                target="_blank"
                rel="noreferrer"
                className="font-medium text-sky-400 hover:underline"
              >
                @{lead.username}
              </a>
            </td>
            <td className="px-4 py-3">
              <span className="rounded-full bg-emerald-950/60 px-2.5 py-1 text-xs font-medium text-emerald-300 tabular-nums">
                {lead.final_score}/10
              </span>
            </td>
            <td className="px-4 py-3 text-slate-300">{lead.service_category ?? "—"}</td>
            <td className="px-4 py-3 text-slate-400">{lead.intent_type ?? "—"}</td>
            <td className="max-w-md px-4 py-3 text-xs text-slate-400">
              {lead.ai_explanation ?? "—"}
            </td>
            <td className="whitespace-nowrap px-4 py-3 text-slate-500">
              {new Date(lead.taken_at).toLocaleString("en-GB", {
                dateStyle: "short",
                timeStyle: "short",
              })}
            </td>
          </tr>
        ))}
      </Table>
    </Shell>
  );
}
