import { Shell } from "@/components/Shell";
import { ActivityFeed } from "@/components/ActivityFeed";
import { SkippedPanel } from "@/components/SkippedPanel";
import { getLogsPageData } from "@/lib/queries";

export const dynamic = "force-dynamic";

export default async function LogsPage() {
  // Rendered server-side once so the page is populated on arrival; the feed
  // component takes over polling from there. The skip panel is a standing
  // verdict rather than a running commentary, so it is not polled.
  //
  // One call, one pooled connection: `Promise.all` over four queries exhausted
  // Supabase's session-mode pooler (15 clients project-wide, and every
  // serverless instance holds its own pool).
  const { events, accounts, skipSummary, skipped } = await getLogsPageData(200);

  return (
    <Shell
      title="Activity Logs"
      subtitle="What each session is doing, step by step — which accounts it polls, which stories it pulls, what goes to the AI, and what gets skipped."
    >
      <div className="space-y-10">
        <ActivityFeed initialEvents={events} initialAccounts={accounts} />

        <section>
          <h2 className="text-lg font-semibold tracking-tight">
            Skipped — what never reached the AI
          </h2>
          <p className="mt-1 mb-5 text-sm text-slate-400">
            A skip is money not spent. These are the photos the pipeline decided
            were not worth a model call, and the evidence behind each decision.
          </p>
          <SkippedPanel summary={skipSummary} targets={skipped} />
        </section>
      </div>
    </Shell>
  );
}
