// The D2 dashboards' filter row (ADR-0066). The choices come from the server (options); nothing
// here filters or recomputes, a change refetches. A filter the server cannot apply is refused
// by it (400), and filters no store records yet (label) are named, never offered.
import type { ReactNode } from "react";
import type { DashQuery, DashboardOptions, DashboardScope } from "../lib/types";

export const NO_DASH_FILTERS: DashQuery = { suite: "", task: "", language: "", dialect: "", since: "", until: "", bucket: "day" };

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

export function DashFilters({ query, options, onChange, languages = true }: {
  query: DashQuery; options?: DashboardOptions; onChange: (q: DashQuery) => void; languages?: boolean;
}) {
  const set = (k: keyof DashQuery) => (v: string) => onChange({ ...query, [k]: v });
  return (
    <div className="flex flex-wrap items-center gap-3">
      <Pick label="Suite" value={query.suite} options={options?.suites ?? []} onChange={set("suite")} />
      {(options?.tasks.length ?? 0) > 0 && <Pick label="Task" value={query.task} options={options?.tasks ?? []} onChange={set("task")} />}
      {languages && <Pick label="Language" value={query.language} options={options?.languages ?? []} onChange={set("language")} />}
      {languages && <Pick label="Dialect" value={query.dialect} options={options?.dialects ?? []} onChange={set("dialect")} />}
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
export function DashScope({ scope, options, children }: { scope: DashboardScope; options: DashboardOptions; children?: ReactNode }) {
  return (
    <div className="space-y-1 text-sm">
      <p>{scope.runs} finished run{scope.runs === 1 ? "" : "s"} · {scope.attempts} attempts. Runs still in flight are not counted.{children}</p>
      {scope.unreadableRuns.length > 0 && <p data-unreadable="true">Left out entirely, records unreadable: {scope.unreadableRuns.join(", ")}.</p>}
      {options.serverOnly.length > 0 && <p className="text-muted-ink">Filtering by {options.serverOnly.join(", ")} needs server mode.</p>}
      {options.notRecorded.length > 0 && <p className="text-muted-ink">Filtering by {options.notRecorded.join(", ")} is not possible yet: runs do not record it.</p>}
    </div>
  );
}
