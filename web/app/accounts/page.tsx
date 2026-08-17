import { Shell } from "@/components/Shell";
import { SessionManager } from "@/components/SessionManager";
import { getAccounts } from "@/lib/queries";

export const dynamic = "force-dynamic";

export default async function AccountsPage() {
  const accounts = await getAccounts();
  return (
    <Shell
      title="Accounts & Sessions"
      subtitle="Web sessions expire after a few weeks and cannot be renewed from code — a dead session stops collection until you paste fresh cookies."
    >
      <SessionManager accounts={accounts} />
    </Shell>
  );
}
