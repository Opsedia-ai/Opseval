import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../lib/api";
import { useApi } from "../lib/useApi";
import GateEditor from "../components/GateEditor";
import type { GateBody } from "../lib/types";
import { Callout, Card, CoralButton, Data, Explainer, Mono, Page, PageHeader, Pill } from "../components/ui";

const field = "mt-1 w-full rounded-xl border border-line bg-white px-3 py-2 text-ink";
const lab = "text-xs font-semibold uppercase tracking-[0.08em]";

export default function Suites() {
  const suites = useApi(api.suites);
  const health = useApi(api.health);
  const readOnly = health.data?.readOnly === true;
  const catalogue = useApi(api.scenarios);
  const [sel, setSel] = useState("");
  const [file, setFile] = useState("");
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [repeats, setRepeats] = useState<number | "">("");
  const [gate, setGate] = useState<GateBody>({ preset: "standard", overrides: {} });
  // Sent only once edited: a suite listed with a load problem has no gate to show, and
  // saving it must not replace the gate in its file with the standard one.
  const [gateEdited, setGateEdited] = useState(false);
  const gatePreview = useApi(() => api.resolveGate(gate), [JSON.stringify(gate)]);
  const editGate = (g: GateBody) => { setGate(g); setGateEdited(true); };
  const [msg, setMsg] = useState<{ ok: boolean; text: string }>();

  // "include" patterns are file paths relative to the catalogue, so the picker works on scenario ids via the tag-free path list
  const ids = catalogue.data?.scenarios ?? [];
  useEffect(() => {
    const s = suites.data?.find((x) => x.file === sel);
    if (!s) return;
    setFile(s.file); setName(s.displayName); setDesc(s.description); setPicked(s.include); setRepeats(s.repeats ?? "");
    setGate({ preset: s.gate?.preset ?? "standard", overrides: s.gate?.overrides ?? {} });
    setGateEdited(false);
  }, [sel, suites.data]);
  useEffect(() => { if (!sel && suites.data?.[0]) setSel(suites.data[0].file); }, [suites.data, sel]);

  // The gate block is the server's own serialisation (one serialiser, ADR-0032).
  const gateYaml = gatePreview.error ? "" : (gatePreview.data?.yaml ?? "");
  const yaml = useMemo(() => [`name: ${name}`, `description: ${desc}`, "include:", ...picked.map((f) => `  - ${f}`), ...(repeats === "" ? [] : ["overrides:", `  execution.repeats: ${repeats}`])].join("\n") + (gateYaml ? `\n${gateYaml.trimEnd()}` : ""), [name, desc, picked, repeats, gateYaml]);
  const save = async () => {
    try {
      // Saving over the suite that is open in the editor is an edit; any other existing name is refused.
      // An empty repeats box clears the override (null); stop_at and every key this form does
      // not manage are left out, so the server keeps them as they are in the file.
      const s = await api.saveSuite({ file, displayName: name, description: desc, include: picked, repeats: repeats === "" ? null : repeats, ...(gateEdited ? { gate } : {}), overwrite: file === sel });
      setMsg({ ok: true, text: `Saved. Resolves to ${s.scenarioCount} scenario(s).` }); suites.reload();
    } catch (e) { setMsg({ ok: false, text: (e as ApiError).message }); }
  };
  const toggle = (p: string) => setPicked((x) => (x.includes(p) ? x.filter((y) => y !== p) : [...x, p]));
  // every scenario file pattern the catalogue offers: one per scenario id (matched by id prefix in its file name)
  const patterns = ids.map((s) => `scenarios/${s.id}*.yaml`);

  return (
    <>
      <PageHeader title="Suites" sub="Named sets of scenarios plus the settings to run them with." aside={<>
        <select value={sel} onChange={(e) => setSel(e.target.value)} className="rounded-full border border-ink/25 bg-white px-4 py-2 text-sm text-ink">
          {suites.data?.map((s) => <option key={s.file} value={s.file}>{s.displayName} ({s.scenarioCount})</option>)}</select>
        <CoralButton className="bg-ink text-white hover:bg-ink-deep" disabled={readOnly} onClick={() => void save()}>Save suite</CoralButton></>} />
      <Page>
        <Explainer><b>A suite selects cases by pattern,</b> then applies settings. It is YAML today; here it is a form and the YAML is generated. Saving validates it against your catalogue first.</Explainer>
        {readOnly && <Callout><b>This is the built-in demo catalogue, which is read-only.</b> Create your own with <Mono>opseval init ./my-catalogue</Mono> and start <Mono>opseval serve --catalogue ./my-catalogue</Mono> to edit suites.</Callout>}
        {suites.data?.filter((x) => x.problem).map((x) => <Callout key={x.file} tone="warn"><b>{x.file}.yaml could not be read:</b> {x.problem}</Callout>)}
        <Data res={suites}>{() => (
          <div className="grid gap-5 lg:grid-cols-[3fr_2fr]">
            <div className="space-y-5">
              <Card title="Identity">
                <div className="grid gap-4 sm:grid-cols-2">
                  <div><label className={lab}>File name</label><input className={field} value={file} onChange={(e) => setFile(e.target.value)} /></div>
                  <div><label className={lab}>Display name</label><input className={field} value={name} onChange={(e) => setName(e.target.value)} /></div>
                </div>
                <label className={`${lab} mt-4 block`}>Description</label><input className={field} value={desc} onChange={(e) => setDesc(e.target.value)} />
                <div className="mt-4"><Callout><b>The file name labels your runs</b>, not the display name. Renaming a suite means a new file, otherwise old runs stop lining up in Compare and Trend.</Callout></div>
              </Card>
              <Card title="Settings"><label className={lab}>Repeats per scenario</label><input type="number" min={1} max={20} className={`${field} max-w-40`} value={repeats} placeholder="as authored" onChange={(e) => setRepeats(e.target.value === "" ? "" : +e.target.value)} /></Card>
              <GateEditor value={gate} onChange={editGate} preview={gatePreview} readOnly={readOnly} />
              <Card title="Which scenarios" aside={<Pill tone="pass">{picked.length} pattern(s)</Pill>}>
                <ul className="divide-y divide-line">{ids.map((s, i) => (
                  <li key={s.id}><label className="flex items-center justify-between gap-3 py-2 text-ink">
                    <span className="flex items-center gap-3"><input type="checkbox" className="h-4 w-4 accent-coral" checked={picked.includes(patterns[i])} onChange={() => toggle(patterns[i])} />{s.title}</span><Mono>{s.id}</Mono></label></li>))}</ul>
              </Card>
            </div>
            <div className="space-y-5">
              <Card title="Generated YAML" tone="ink"><pre className="overflow-x-auto font-mono text-[13px] leading-relaxed text-ink-text">{yaml}</pre></Card>
              {msg && <Callout tone={msg.ok ? "info" : "warn"}>{msg.text}</Callout>}
            </div>
          </div>)}</Data>
      </Page>
    </>
  );
}
