import { Link } from "react-router-dom";
import type { ScenarioTrend, TrendCell } from "../lib/types";
import { Mono, Pill, cx } from "./ui";

type Tone = "pass" | "fail" | "warn" | "mute";
const SHORT: Record<string, [string, Tone]> = {
  passed: ["pass", "pass"],
  failed: ["fail", "fail"],
  inconclusive: ["n/j", "mute"],
  skipped_precondition: ["skip", "mute"],
  blocked: ["blk", "mute"],
};

/**
 * One scenario in one run. Its meaning comes from the server's verdict, never from a number:
 * a flaky pass is a warning, anything not judged is muted and dashed (never a failure), and a
 * scenario that was not in the run is an empty dot, never a result.
 */
export function Cell({ cell }: { cell: TrendCell }) {
  if (cell.status === null) {
    return <span data-state="absent" title="not in this run" className="text-muted-ink">·</span>;
  }
  const [label, tone] = cell.status === "passed" && cell.flaky ? ["flaky", "warn" as Tone] : SHORT[cell.status];
  const title = `${cell.status.replace("_", " ")}${cell.passRate === null ? "" : ` · ${Math.round(cell.passRate * 100)}% of attempts`}`;
  return (
    <Link to={`/runs/${cell.run}`} title={title}>
      <Pill tone={tone} dashed={tone === "mute"}>{label}</Pill>
    </Link>);
}

/** Rows are scenarios, columns runs (oldest left). Nothing here is computed in the browser. */
export default function ScenarioTrendGrid({ data }: { data: ScenarioTrend }) {
  if (data.runs.length === 0) return <p>No finished runs for this suite yet.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="text-sm">
        <thead>
          <tr className="border-b border-line text-left text-xs uppercase tracking-[0.08em] text-muted-ink">
            <th className="py-2 pr-4">Scenario</th>
            {data.runs.map((r) => <th key={r.run} className="whitespace-nowrap px-1 py-2 text-center font-normal normal-case tracking-normal"><Link to={`/runs/${r.run}`} title={r.run}>{r.label}</Link></th>)}
            <th className="px-2 py-2" title="times its judged verdict changed between runs">Flips</th>
          </tr>
        </thead>
        <tbody>
          {data.scenarios.map((row) => (
            <tr key={row.id} className="border-b border-line/60">
              <td className="py-2 pr-4"><div className="font-medium text-ink">{row.name}</div><Mono>{row.id}</Mono></td>
              {row.cells.map((c) => <td key={c.run} className="px-1 py-2 text-center"><Cell cell={c} /></td>)}
              <td className={cx("px-2 py-2 text-center font-mono", row.flips > 0 && "font-bold text-deep-coral")} data-flips={row.flips}>{row.flips}</td>
            </tr>))}
        </tbody>
      </table>
    </div>);
}
