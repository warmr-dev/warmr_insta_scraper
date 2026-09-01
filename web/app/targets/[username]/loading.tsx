/**
 * Shown the instant a tab is clicked, before its data arrives.
 *
 * The database is in Sydney - roughly 2.5s per round-trip - so without this the
 * browser sits on the previous page and navigation feels broken rather than
 * merely slow.
 */
export default function Loading() {
  return (
    <div className="min-h-screen">
      <div className="h-[57px] border-b border-slate-800 bg-slate-900/50" />
      <main className="mx-auto max-w-7xl animate-pulse px-6 py-8">
        <div className="h-8 w-48 rounded bg-slate-800" />
        <div className="mt-2 h-4 w-96 rounded bg-slate-900" />
        <div className="mt-8 grid grid-cols-2 gap-4 lg:grid-cols-5">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-24 rounded-xl border border-slate-800 bg-slate-900/50" />
          ))}
        </div>
        <div className="mt-8 h-64 rounded-xl border border-slate-800 bg-slate-900/50" />
      </main>
    </div>
  );
}
