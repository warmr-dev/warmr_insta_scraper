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
      {/* Also linked inside the add-session form, but that form is collapsed by
          default — someone arriving here because collection stopped should not
          have to open it to find the instructions. */}
      <p className="mb-6 text-sm text-slate-400">
        New here?{" "}
        <a
          href="https://www.loom.com/share/37ea1ac929be44b2b6c921c278a0e0fd"
          target="_blank"
          rel="noreferrer"
          className="text-sky-400 underline-offset-2 hover:underline"
        >
          Watch how to upload a working session
        </a>{" "}
        (2 min, no sound), or see the{" "}
        <a
          href="https://www.loom.com/share/0068d8ad2a0e4a56865f0c2ff5b93093"
          target="_blank"
          rel="noreferrer"
          className="text-sky-400 underline-offset-2 hover:underline"
        >
          full walkthrough
        </a>
        .
      </p>

      <SessionManager accounts={accounts} />
    </Shell>
  );
}
