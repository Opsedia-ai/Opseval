import { api } from "../lib/api";
import type { GateBody, GatePreset } from "../lib/types";
import { useApi } from "../lib/useApi";
import { Callout, Card, Data, Mono, Pill, cx } from "./ui";

const PRESETS: { id: GatePreset; note: string }[] = [
  { id: "standard", note: "blocker and critical failures and inconclusive scenarios block" },
  { id: "strict", note: "every failure, outage skip, flaky or unmeasured check blocks" },
  { id: "lenient", note: "only blocker and critical failures block; the exit code follows the gate" },
];

/**
 * The release gate of one suite (ADR-0032): a preset plus per-outcome overrides. Every
 * choice comes from the server, which offers only what the loader accepts, and the effect
 * of an edit is resolved by the server too: the browser decides nothing about the gate.
 */
export default function GateEditor({ value, onChange, readOnly }: { value: GateBody; onChange: (v: GateBody) => void; readOnly: boolean }) {
  const catalog = useApi(api.gate);
  const preview = useApi(() => api.resolveGate(value), [JSON.stringify(value)]);
  const set = (key: string, choice: string) => {
    const overrides = { ...value.overrides };
    if (choice === "") delete overrides[key];
    else overrides[key] = choice;
    onChange({ ...value, overrides });
  };
  const looser = new Set((preview.data?.looserThanStandard ?? []).map((x) => x.split("=")[0]));

  return (
    <Card title="Release gate" aside={preview.data && <Mono>{preview.data.describe}</Mono>}>
      <p className="mb-4 text-sm">Opseval decides what happened. This decides what each outcome <b>does</b> to the release. Two things are never up to the gate: a suite that judged nothing blocks, and a harness error is never the agent&apos;s fault.</p>
      <label className="text-xs font-semibold uppercase tracking-[0.08em]">Preset</label>
      <div className="mt-2 grid gap-2 sm:grid-cols-3">
        {PRESETS.map((p) => (
          <button key={p.id} type="button" disabled={readOnly} onClick={() => onChange({ preset: p.id, overrides: {} })}
            className={cx("rounded-2xl border px-4 py-3 text-left text-sm", value.preset === p.id ? "border-ink bg-ink text-white" : "border-line bg-white text-ink hover:border-ink/50")}>
            <div className="font-semibold capitalize">{p.id}</div><div className={cx("mt-1 text-xs", value.preset === p.id ? "text-white/80" : "text-muted-ink")}>{p.note}</div>
          </button>))}
      </div>
      <Data res={catalog}>{(cat) => (
        <table className="mt-5 w-full text-sm">
          <thead><tr className="border-b border-line text-left text-xs uppercase tracking-[0.08em] text-muted-ink"><th className="py-2">When</th><th className="py-2">The gate will</th></tr></thead>
          <tbody>
            {cat.settings.map((st) => {
              const inherited = cat.presets[value.preset][st.key];
              const chosen = value.overrides[st.key] ?? "";
              return (
                <tr key={st.key} className="border-b border-line/60 align-top">
                  <td className="py-2 pr-3"><div className="font-medium text-ink">{st.label}</div><div className="text-xs text-muted-ink">{st.help}</div></td>
                  <td className="py-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <select aria-label={st.label} disabled={readOnly} value={chosen} onChange={(e) => set(st.key, e.target.value)}
                        className={cx("rounded-xl border bg-white px-3 py-1.5 text-ink", chosen ? "border-ink" : "border-line")}>
                        <option value="">{`${inherited} (from ${value.preset})`}</option>
                        {st.choices.map((c) => <option key={c} value={c}>{c}</option>)}
                      </select>
                      {chosen && <Pill tone="neutral">changed</Pill>}
                      {looser.has(st.key) && <Pill tone="warn">looser than standard</Pill>}
                    </div>
                  </td>
                </tr>);
            })}
          </tbody>
        </table>)}</Data>
      {preview.error && <div className="mt-4"><Callout><b>{preview.error.code}</b> {preview.error.message}</Callout></div>}
      {preview.data && preview.data.looserThanStandard.length > 0 && (
        <div className="mt-4"><Callout><b>Looser than standard:</b> <Mono>{preview.data.looserThanStandard.join(", ")}</Mono>. Every report from this suite will say so.</Callout></div>)}
    </Card>
  );
}
