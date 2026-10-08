import type { GateBody, GatePreset, GateView } from "../lib/types";
import { api } from "../lib/api";
import { useApi, type Resource } from "../lib/useApi";
import { Callout, Card, Data, Mono, Pill, cx } from "./ui";

const PRESETS: { id: GatePreset; note: string }[] = [
  { id: "standard", note: "blocker and critical failures and inconclusive scenarios block" },
  { id: "strict", note: "every failure, outage skip, flaky or unmeasured check blocks" },
  { id: "lenient", note: "only blocker and critical failures block; the exit code follows the gate" },
];

/**
 * The release gate of one suite (ADR-0032): a preset plus per-outcome overrides. Every
 * choice comes from the server, which offers only what the loader accepts, and `preview`
 * is the server's resolution of the current value: the browser decides nothing about it.
 */
export default function GateEditor({ value, onChange, preview, readOnly }: {
  value: GateBody; onChange: (v: GateBody) => void; preview: Resource<GateView>; readOnly: boolean;
}) {
  const catalog = useApi(api.gate);
  const current = preview.error ? undefined : preview.data; // never show a stale preview beside an error
  const looser = new Set((current?.looserThanStandard ?? []).map((x) => x.split("=")[0]));
  const changed = Object.keys(value.overrides).length > 0;

  return (
    <Card title="Release gate" aside={current && <Mono>{current.describe}</Mono>}>
      <p className="mb-4 text-sm">Opseval decides what happened. This decides what each outcome <b>does</b> to the release. Two things are never up to the gate: a suite that judged nothing blocks, and a harness error is never the agent&apos;s fault.</p>
      <div className="flex items-center justify-between"><label className="text-xs font-semibold uppercase tracking-[0.08em]">Preset</label>
        {changed && !readOnly && <button type="button" className="text-sm font-semibold underline" onClick={() => onChange({ ...value, overrides: {} })}>Reset changes</button>}</div>
      <div className="mt-2 grid gap-2 sm:grid-cols-3">
        {PRESETS.map((p) => (
          // Switching preset keeps your changes; the server drops any the new preset already makes.
          <button key={p.id} type="button" disabled={readOnly} aria-pressed={value.preset === p.id}
            onClick={() => value.preset !== p.id && onChange({ ...value, preset: p.id })}
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
              const pick = (choice: string) => {
                const overrides = { ...value.overrides };
                // Choosing what the preset already does is not a change.
                if (choice === "" || choice === inherited) delete overrides[st.key];
                else overrides[st.key] = choice;
                onChange({ ...value, overrides });
              };
              return (
                <tr key={st.key} className="border-b border-line/60 align-top">
                  <td className="py-2 pr-3"><div className="font-medium text-ink">{st.label}</div><div className="text-xs text-muted-ink">{st.help}</div></td>
                  <td className="py-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <select aria-label={st.label} disabled={readOnly} value={chosen} onChange={(e) => pick(e.target.value)}
                        className={cx("rounded-xl border bg-white px-3 py-1.5 text-ink", chosen ? "border-ink" : "border-line")}>
                        <option value="">{`${inherited} (from ${value.preset})`}</option>
                        {st.choices.filter((c) => c !== inherited).map((c) => <option key={c} value={c}>{c}</option>)}
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
      {current && current.looserThanStandard.length > 0 && (
        <div className="mt-4"><Callout><b>Looser than standard:</b> <Mono>{current.looserThanStandard.join(", ")}</Mono>. Every report from this suite will say so.</Callout></div>)}
    </Card>
  );
}
