import { redirect } from "next/navigation";
import { getSession } from "@/lib/auth";
import { Navbar } from "./Navbar";

/**
 * Every authenticated page wraps itself in this. Redirecting here rather than in
 * middleware keeps the check next to the data access it guards.
 */
export async function Shell({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  const email = await getSession();
  if (!email) redirect("/login");

  return (
    <div className="min-h-screen">
      <Navbar email={email} />
      <main className="mx-auto max-w-7xl px-6 py-8">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-400">{subtitle}</p>}
        <div className="mt-8">{children}</div>
      </main>
    </div>
  );
}
