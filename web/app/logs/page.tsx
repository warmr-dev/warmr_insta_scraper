import { Shell } from "@/components/Shell";
import { ActivityFeed } from "@/components/ActivityFeed";
import { getActivity, getActivityAccounts } from "@/lib/queries";

export const dynamic = "force-dynamic";

export default async function LogsPage() {
  // Rendered server-side once so the page is populated on arrival; the client
  // component takes over polling from there.
  const [events, accounts] = await Promise.all([
    getActivity(undefined, 200),
    getActivityAccounts(),
  ]);

  return (
    <Shell
      title="Activity Logs"
      subtitle="What each session is doing, step by step — which accounts it polls, which stories it pulls, and what goes to the AI."
    >
      <ActivityFeed initialEvents={events} initialAccounts={accounts} />
    </Shell>
  );
}
