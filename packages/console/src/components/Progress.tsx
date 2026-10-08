import type { RunProgress } from "../lib/types";
import { Mono, Pill, cx } from "./ui";

/** One line for the runs table: how far a run in flight has got. */
export function ProgressLine({ p }: { p: RunProgress }) {
  const pct = p.scenariosPlanned ? (100 * p.scenariosDone) / p.scenariosPlanned : 0;
  return (
    <div className="min-w-40">
      <div className="h-2 w-40 overflow-hidden rounded-full bg-line" title={`${p.scenariosDone} of ${p.scenariosPlanned} scenarios done`}>
        <span className="block h-full bg-coral transition-all" style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1 text-xs">{p.scenariosDone}/{p.scenariosPlanned} scenarios · {p.attemptsRecorded} attempt(s)
        {p.stage === "finishing" ? " · writing the gate, reports and sinks" : p.current && <> · now <Mono>{p.current}</Mono></>}
        {p.cancelRequested && p.stage !== "finishing" && <> · <b data-state="cancelling">cancelling</b></>}</div>
    </div>
  );
}

/** Every planned scenario of a run in flight. Nothing here is a verdict: those come when it ends. */
export function ProgressTable({ p }: { p: RunProgress }) {
  return (
    <ul className="divide-y divide-line rounded-2xl border border-line bg-white">
      {p.scenarios.map((sc) => (
        <li key={sc.id} className="flex items-center justify-between gap-3 px-4 py-2 text-sm">
          <Mono>{sc.id}</Mono>
          <span className="flex items-center gap-2">
            {sc.repeats !== null && <span className="text-muted-ink">{sc.attempts}/{sc.repeats} attempt(s)</span>}
            <Pill tone={sc.state === "running" ? "warn" : "mute"} dashed={sc.state === "waiting" || sc.state === "skipped"}>
              <span className={cx(sc.state === "running" && "animate-pulse")}>{sc.state}</span>
            </Pill>
          </span>
        </li>))}
    </ul>
  );
}
