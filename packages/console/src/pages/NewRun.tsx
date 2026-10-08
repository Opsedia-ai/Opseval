import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Play } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { useApi } from "../lib/useApi";
import type { StopBody } from "../lib/types";
import StopEditor, { NEVER, stopFields } from "../components/StopEditor";
import { Callout, Card, CoralButton, Data, Explainer, Mono, NotMeasured, Page, PageHeader, Pill } from "../components/ui";

const input = "mt-1 w-full rounded-xl border border-line bg-white px-3 py-2 text-ink";
const lab = "text-xs font-semibold uppercase tracking-[0.08em]";

export default function NewRun() {
  const nav = useNavigate();
  const suites = useApi(api.suites);
  const catalogue = useApi(api.scenarios);
  const readiness = useApi(api.readiness);
  const health = useApi(api.health);
  const commitAllowed = health.data?.allowCommit === true;
  const [suite, setSuite] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [target, setTarget] = useState("");
  const [repeats, setRepeats] = useState<number | "">("");
  const [allowCommit, setAllowCommit] = useState(false);
  const [stop, setStop] = useState<StopBody>({});
  const [err, setErr] = useState<ApiError>();
  const [busy, setBusy] = useState(false);

  const chosenSuite = suite || suites.data?.[0]?.file || "";
  // A selection runs outside any suite, so there is no suite policy to fall back to.
  const suiteStop = picked.length ? NEVER : (suites.data?.find((x) => x.file === chosenSuite)?.stop ?? NEVER);
  const overrides = stopFields(stop);
  const body = { ...(picked.length ? { scenarios: picked } : { suite: chosenSuite }), target: target || undefined, repeats: repeats === "" ? undefined : repeats, allowCommit: allowCommit && commitAllowed, ...(Object.keys(overrides).length ? { stop: overrides } : {}) };
  const start = async () => {
    setBusy(true); setErr(undefined);
    try { const { runId } = await api.startRun(body); nav(`/runs/${runId}`); } catch (e) { setErr(e as ApiError); }
    setBusy(false);
  };
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));

  return (
    <>
      <PageHeader title="Start a run" sub="Pick what to run, point it at an agent, press start." />
      <Page>
        <Explainer><b>What happens when you press start.</b> For each scenario the simulator opens a real conversation with the agent, plays the customer to the end and records everything. Each scenario repeats, because passing once proves nothing. Then every recording is scored.</Explainer>
        <div className="grid gap-5 lg:grid-cols-[3fr_2fr]">
          <div className="space-y-5">
            <Card title="Agent under test">
              <label className={lab}>Target</label>
              <input className={input} value={target} onChange={(e) => setTarget(e.target.value)} placeholder="ws://localhost:8765  (blank uses the server's --target)" />
            </Card>
            <Card title="What to run">
              <label className={lab}>Suite</label>
              <select value={chosenSuite} onChange={(e) => { setSuite(e.target.value); setPicked([]); }} className={input}>
                {suites.data?.map((s) => <option key={s.file} value={s.file}>{s.displayName} ({s.scenarioCount} scenarios)</option>)}</select>
              <p className="mb-2 mt-5 font-semibold text-ink">Or select individually</p>
              <Data res={catalogue}>{(c) => (<ul className="space-y-2">{c.scenarios.map((s) => (
                <li key={s.id} className="flex items-start gap-3"><input type="checkbox" checked={picked.includes(s.id)} onChange={() => toggle(s.id)} className="mt-1.5 h-4 w-4 accent-coral" />
                  <span className="text-ink">{s.title} <Mono>{s.id}</Mono> {s.tags.slice(0, 3).map((t) => <Pill key={t}>{t}</Pill>)}</span></li>))}</ul>)}</Data>
            </Card>
            <Card title="Options">
              <div className="grid gap-4 sm:grid-cols-2">
                <div><label className={lab}>Repeats (override)</label><input type="number" min={1} max={20} className={input} value={repeats} placeholder="as authored" onChange={(e) => setRepeats(e.target.value === "" ? "" : +e.target.value)} /></div>
              </div>
              <div className="mt-4"><Callout><b>Stopping at the commit gate keeps this safe.</b> The irreversible outcome needs <Mono>allow_commit: true</Mono> in the scenario, the server started with <Mono>opseval serve --allow-commit</Mono>, <b>and</b> this switch (ADR-0008).</Callout></div>
              <label className={`mt-4 flex items-center gap-3 ${commitAllowed ? "text-ink" : "text-muted-ink"}`}><input type="checkbox" disabled={!commitAllowed} checked={allowCommit && commitAllowed} onChange={(e) => setAllowCommit(e.target.checked)} className="h-4 w-4 accent-coral" />Allow the committed outcome {allowCommit && commitAllowed && <Pill tone="fail">irreversible</Pill>}{!commitAllowed && <span className="text-sm">(the server was not started with <Mono>--allow-commit</Mono>)</span>}</label>
            </Card>
            <StopEditor value={stop} onChange={setStop} base={suiteStop} baseLabel={picked.length ? "default" : "suite"} />
          </div>
          <div className="space-y-5">
            <Card title="Readiness"><Data res={readiness}>{(items) => (<ul className="space-y-3">{items.map((r) => (
              <li key={r.id} className="flex items-center justify-between gap-3 text-ink">{r.label}
                {r.ready ? <Pill tone="pass">ready</Pill> : <NotMeasured text="limited" />}</li>))}
              {items.filter((r) => !r.ready).map((r) => <li key={r.id} className="text-sm"><b className="text-ink">{r.label}:</b> {r.detail}</li>)}</ul>)}</Data></Card>
            <Card title="Request preview" tone="ink"><pre className="overflow-x-auto font-mono text-[13px] leading-relaxed text-ink-text">{JSON.stringify(body, null, 2)}</pre></Card>
            {err && <Callout><b>{err.code}</b> {err.message}</Callout>}
            <CoralButton className="w-full py-3 text-base" disabled={busy} onClick={() => void start()}><Play size={16} />Start run</CoralButton>
          </div>
        </div>
      </Page>
    </>
  );
}
