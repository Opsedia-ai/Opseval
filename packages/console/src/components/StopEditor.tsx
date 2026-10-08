import type { StopBody, StopView } from "../lib/types";
import { Card, Mono, Pill, cx } from "./ui";

/** Never stopping early: what a suite without a `stop:` block does. */
export const NEVER: StopView = {
  failFast: false, maxConsecutiveFailures: null, stopOnCriticalFailure: false, stopOnRateLimit: false, maxWallClockSeconds: null,
};

type Flag = "failFast" | "stopOnCriticalFailure" | "stopOnRateLimit";
const FLAGS: { key: Flag; label: string; help: string }[] = [
  { key: "failFast", label: "First failure the gate blocks on", help: "Stop after the first failed scenario that would block the release." },
  { key: "stopOnCriticalFailure", label: "A critical or blocker scenario fails", help: "Stop as soon as a scenario marked critical or blocker fails." },
  { key: "stopOnRateLimit", label: "A rate limit outlasts its retries", help: "The agent's endpoint or an LLM kept answering 429 and an attempt could not be judged." },
];

/** Whole minutes as seconds; anything under one minute is no budget, never a 0 the server refuses. */
export function budget(minutesText: string): number | null {
  const whole = Math.round(Number(minutesText));
  return Number.isFinite(whole) && whole >= 1 ? whole * 60 : null;
}

/** Only the fields that were set: each one replaces the same field of what it falls back to. */
export function stopFields(v: StopBody): StopBody {
  return Object.fromEntries(Object.entries(v).filter(([, x]) => x !== null && x !== undefined)) as StopBody;
}

/**
 * When a run stops early (ADR-0035). Every choice says what it falls back to, so "the suite's
 * setting" is never a mystery. The runner decides what stopping does; this only says when.
 */
export default function StopEditor({ value, onChange, base, baseLabel, readOnly = false }: {
  value: StopBody; onChange: (v: StopBody) => void; base: StopView; baseLabel: string; readOnly?: boolean;
}) {
  const set = (patch: StopBody) => onChange({ ...value, ...patch });
  const onOff = (b: boolean) => (b ? "stop" : "keep going");
  const minutes = (s: number | null) => (s === null ? "no limit" : `${+(s / 60).toFixed(2)} min`);
  const changed = Object.keys(stopFields(value)).length > 0;
  const select = "rounded-xl border bg-white px-3 py-1.5 text-ink";

  return (
    <Card title="When to stop early" aside={changed && <Pill tone="neutral">changed</Pill>}>
      <p className="mb-4 text-sm">A stop never changes a verdict. Scenarios it leaves out are listed as <b>skipped</b>, never passed, so a stopped run is never <i>safe to release</i>. Cancel a running run from its page.</p>
      <table className="w-full text-sm">
        <thead><tr className="border-b border-line text-left text-xs uppercase tracking-[0.08em] text-muted-ink"><th className="py-2">When</th><th className="py-2">The run will</th></tr></thead>
        <tbody>
          {FLAGS.map((f) => {
            const chosen = value[f.key];
            return (
              <tr key={f.key} className="border-b border-line/60 align-top">
                <td className="py-2 pr-3"><div className="font-medium text-ink">{f.label}</div><div className="text-xs text-muted-ink">{f.help}</div></td>
                <td className="py-2">
                  <select aria-label={f.label} disabled={readOnly} value={chosen === null || chosen === undefined ? "" : chosen ? "on" : "off"}
                    onChange={(e) => set({ [f.key]: e.target.value === "" ? null : e.target.value === "on" })}
                    className={cx(select, chosen !== null && chosen !== undefined ? "border-ink" : "border-line")}>
                    <option value="">{`${onOff(base[f.key])} (${baseLabel})`}</option>
                    <option value="on">stop</option>
                    <option value="off">keep going</option>
                  </select>
                </td>
              </tr>);
          })}
          <tr className="border-b border-line/60 align-top">
            <td className="py-2 pr-3"><div className="font-medium text-ink">Failures in a row</div><div className="text-xs text-muted-ink">Stop after this many failed scenarios in a row. Only a pass resets the count.</div></td>
            <td className="py-2"><input aria-label="Failures in a row" type="number" min={1} disabled={readOnly} className={cx(select, "w-44", value.maxConsecutiveFailures ? "border-ink" : "border-line")}
              value={value.maxConsecutiveFailures ?? ""} placeholder={`${base.maxConsecutiveFailures ?? "no limit"} (${baseLabel})`}
              onChange={(e) => set({ maxConsecutiveFailures: e.target.value === "" ? null : Math.max(1, Math.round(+e.target.value)) })} /></td>
          </tr>
          <tr className="align-top">
            <td className="py-2 pr-3"><div className="font-medium text-ink">Time budget</div><div className="text-xs text-muted-ink">Start no scenario once the run has taken this long (whole minutes). A scenario that has started finishes.</div></td>
            <td className="py-2"><input aria-label="Time budget in minutes" type="number" min={1} step={1} disabled={readOnly} className={cx(select, "w-44", value.maxWallClockSeconds ? "border-ink" : "border-line")}
              value={value.maxWallClockSeconds ? Math.max(1, Math.round(value.maxWallClockSeconds / 60)) : ""} placeholder={`${minutes(base.maxWallClockSeconds)} (${baseLabel})`}
              onChange={(e) => set({ maxWallClockSeconds: budget(e.target.value) })} /></td>
          </tr>
        </tbody>
      </table>
      {changed && !readOnly && <button type="button" className="mt-3 text-sm font-semibold underline" onClick={() => onChange({})}>Reset to {baseLabel}</button>}
      <p className="mt-3 text-xs text-muted-ink">Same as <Mono>stop:</Mono> in a suite, or <Mono>opseval run --max-consecutive-failures …</Mono> on the command line.</p>
    </Card>
  );
}
