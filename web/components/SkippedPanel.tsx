import type { SkippedTarget, SkipSummary } from "@/lib/queries";

/**
 * Why photos never reached the AI.
 *
 * Server component: unlike the live feed, this is a standing verdict rather
 * than a running commentary - it changes once per cycle at most, so polling it
 * would be waste.
 *
 * The point is accountability for money NOT spent. A skip is the pipeline
 * deciding an account is not worth another model call, and that decision should
 * be as visible as the calls it makes - otherwise "processed 3 photos" looks
 * like a bug rather than a saving.
 */

const REASON_LABEL: Record<string, { label: string; hint: string; cls: string }> = {
  irrelevant: {
    label: "No signal",
    hint: "Analysed repeatedly, never scored — stopped paying for them",
    cls: "text-amber-300",
  },
  duplicate: {
    label: "Already analysed",
    hint: "Seen in an earlier cycle — deduplicated on story_id",
    cls: "text-sky-300",
  },
  video: {
    label: "Video",
    hint: "Photos only (spec 1) — never reaches the AI",
    cls: "text-slate-400",
  },
  over_limit: {
    label: "Over cycle limit",
    hint: "Deferred to the next cycle by the per-cycle spend cap",
    cls: "text-violet-300",
  },
};

function ago(iso: string | null): string {
  if (!iso) return "—";
  const hours = (Date.now() - new Date(iso).getTime()) / 3_600_000;
  if (hours < 1) return `${Math.round(hours * 60)}m ago`;
  if (hours < 24) return `${Math.round(hours)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function SkippedPanel({
  summary,
  targets,
}: {
  summary: SkipSummary[];
  targets: SkippedTarget[];
}) {
  const totalSaved = summary.reduce((n, s) => n + Number(s.items ?? 0), 0);

  return (
    <div className="space-y-6">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {summary.length === 0 ? (
          <p className="text-sm text-slate-500">
            Nothing skipped in the last 24 hours.
          </p>
        ) : (
          summary.map((s) => {
            const meta = REASON_LABEL[s.status] ?? {
              label: s.status,
              hint: "",
              cls: "text-slate-300",
            };
            return (
              <div
                key={s.status}
                className="rounded-xl border border-slate-800 bg-slate-900/40 p-4"
                title={meta.hint}
              >
                <div className={`text-2xl font-semibold ${meta.cls}`}>{s.items}</div>
                <div className="mt-1 text-sm text-slate-300">{meta.label}</div>
                <div className="mt-1 text-xs text-slate-500">{meta.hint}</div>
              </div>
            );
          })
        )}
      </div>

      {totalSaved > 0 && (
        <p className="text-sm text-slate-400">
          <span className="font-medium text-slate-200">{totalSaved} photos</span> did
          not reach the AI in the last 24 hours. Every one is a model call not paid
          for.
        </p>
      )}

      <div>
        <h2 className="mb-1 text-sm font-medium text-slate-200">
          Accounts no longer worth analysing
        </h2>
        <p className="mb-3 text-xs text-slate-500">
          Each was analysed at least{" "}
          <code className="text-slate-400">PRIORITY_MIN_SAMPLES</code> times and never
          scored. They are re-checked every{" "}
          <code className="text-slate-400">PRIORITY_RECHECK_HOURS</code> hours rather
          than blocked — people change what they post.
        </p>

        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/70 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="whitespace-nowrap px-4 py-3 font-medium">Account</th>
                <th className="whitespace-nowrap px-4 py-3 text-right font-medium">
                  Photos skipped
                </th>
                <th className="whitespace-nowrap px-4 py-3 text-right font-medium">
                  Times
                </th>
                <th className="whitespace-nowrap px-4 py-3 text-right font-medium">
                  Analysed
                </th>
                <th className="whitespace-nowrap px-4 py-3 text-right font-medium">
                  Best
                </th>
                <th className="whitespace-nowrap px-4 py-3 text-right font-medium">
                  Avg
                </th>
                <th className="whitespace-nowrap px-4 py-3 font-medium">Last skip</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {targets.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-slate-500">
                    No accounts have been deprioritised yet — every target is still
                    getting analysed.
                  </td>
                </tr>
              ) : (
                targets.map((t) => (
                  <tr key={t.handle} title={t.last_reason ?? ""}>
                    <td className="px-4 py-2 text-slate-300">@{t.handle}</td>
                    <td className="px-4 py-2 text-right font-medium text-amber-300">
                      {t.photos_skipped}
                    </td>
                    <td className="px-4 py-2 text-right text-slate-400">
                      {t.times_skipped}
                    </td>
                    <td className="px-4 py-2 text-right text-slate-400">
                      {t.analysed}
                    </td>
                    <td className="px-4 py-2 text-right text-slate-400">
                      {t.best_score}
                    </td>
                    <td className="px-4 py-2 text-right text-slate-400">
                      {Number(t.avg_score).toFixed(1)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-xs text-slate-500">
                      {ago(t.last_skipped)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
