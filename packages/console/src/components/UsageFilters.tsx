// The dashboards' filter row (ADR-0066). The choices come from the server (options); nothing
// here filters or recomputes: a change refetches. Filters only server mode can apply
// (organisation, project, task, label) are named, not offered.
import type { ReactNode } from "react";
import type { UsageFilterOptions, UsageQuery, UsageScope } from "../lib/types";

export const NO_FILTERS: UsageQuery = { suite: "", role: "", model: "", since: "", until: "", bucket: "day" };

const field = "rounded-full border border-ink/25 bg-white px-3 py-1 text-sm text-ink";

function Pick({ label, value, options, onChange }: { label: string; value: string; options: string[]; onChange: (v: string) => void }) {
  return (
    <label className="flex items-center gap-1.5 text-sm">
      {label}
      <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)} className={field}>
        <option value="">All</option>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </label>
  );
}

export function UsageFilters({ query, options, onChange }: { query: UsageQuery; options?: UsageFilterOptions; onChange: (q: UsageQuery) => void }) {
  const set = (k: keyof UsageQuery) => (v: string) => onChange({ ...query, [k]: v });
  return (
    <div className="flex flex-wrap items-center gap-3">
      <Pick label="Suite" value={query.suite} options={options?.suites ?? []} onChange={set("suite")} />
      <Pick label="Role" value={query.role} options={options?.roles ?? []} onChange={set("role")} />
      <Pick label="Model" value={query.model} options={options?.models ?? []} onChange={set("model")} />
      <label className="flex items-center gap-1.5 text-sm">From<input aria-label="From" type="date" value={query.since} onChange={(e) => set("since")(e.target.value)} className={field} /></label>
      <label className="flex items-center gap-1.5 text-sm">To<input aria-label="To" type="date" value={query.until} onChange={(e) => set("until")(e.target.value)} className={field} /></label>
      <label className="flex items-center gap-1.5 text-sm">Per
        <select aria-label="Per" value={query.bucket} onChange={(e) => set("bucket")(e.target.value)} className={field}>
          <option value="day">day</option><option value="week">week</option><option value="run">run</option>
        </select>
      </label>
    </div>
  );
}

/** What the answer covered, so nothing left out is invisible. */
export function ScopeNote({ scope, serverOnly, children }: { scope: UsageScope; serverOnly: string[]; children?: ReactNode }) {
  return (
    <div className="space-y-1 text-sm">
      <p>
        {scope.runs} finished run{scope.runs === 1 ? "" : "s"} · {scope.calls} calls{scope.failedCalls ? ` (${scope.failedCalls} failed: nothing to count)` : ""} · {scope.attempts} attempts.
        Runs still in flight are not counted.{children}
      </p>
      {scope.legacyRuns.length > 0 && (
        <p data-legacy="true">{scope.legacyRuns.length} run{scope.legacyRuns.length === 1 ? " was" : "s were"} written before per-call usage: per-model totals as written (a count the provider did not report was then added as 0), no reasoning or cached tokens, no per-scenario figures.</p>
      )}
      {scope.attemptsWithoutUsage > 0 && <p>{scope.attemptsWithoutUsage} attempts have no per-call usage: not measured.</p>}
      {scope.unreadableRuns.length > 0 && <p data-unreadable="true">Left out entirely, records unreadable (no calls, no passed tests): {scope.unreadableRuns.join(", ")}.</p>}
      {scope.rescoredRuns.length > 0 && <p data-rescored="true">Re-grading: the latest re-score of {scope.rescoredRuns.join(", ")} is counted with its run ({scope.rescoreCalls} calls, shown as &quot;(rescore)&quot;), not as a run of its own; per-scenario figures are the attempts as they ran.</p>}
      {scope.unreadableRescores.length > 0 && <p>Re-scores left out, unreadable: {scope.unreadableRescores.join(", ")}.</p>}
      <p className="text-muted-ink">Filtering by {serverOnly.join(", ")} needs server mode.</p>
    </div>
  );
}
