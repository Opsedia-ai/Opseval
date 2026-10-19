import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Save } from "lucide-react";
import { api, ApiError } from "../lib/api";
import type { ScenarioCheck } from "../lib/types";
import { Warnings } from "../components/Warnings";
import { useApi } from "../lib/useApi";
import { Callout, Card, CoralButton, Data, Explainer, Mono, Page, PageHeader, Pill } from "../components/ui";

const TEMPLATE = `id: new-001
name: A short name for the situation
goal: What the customer wants, in one sentence.
criticality: normal

persona:
  role: Who the customer is and how they behave.
  language: en

conversation:
  scripted_turns:
    - "The customer's first message."
  stop_at: goal_reached
  checkpoint_policy: {}

scorers:
  - id: conversation.no_errors
`;

const field = "mt-1 w-full rounded-xl border border-line bg-white px-3 py-2 text-ink";
const lab = "text-xs font-semibold uppercase tracking-[0.08em]";

/** Edit one scenario's YAML (or write a new one) with the loader's own checks as you type. */
export default function ScenarioEditor() {
  const [params] = useSearchParams();
  const file = params.get("file") ?? undefined; // a catalogue-relative file; absent for a new one
  const isNew = file === undefined;
  const nav = useNavigate();
  const health = useApi(api.health);
  const readOnly = health.data?.readOnly === true;
  const source = useApi(() => (isNew ? Promise.resolve(null) : api.scenarioFile(file)), [file]);
  const [text, setText] = useState(isNew ? TEMPLATE : "");
  const [etag, setEtag] = useState("");
  const [family, setFamily] = useState("uncategorised");
  const [name, setName] = useState("new-001.yaml");
  // A verdict is kept with the exact text it judged: Save is enabled only for that text, so
  // a stale or out-of-order reply can never vouch for what is in the box now.
  const [check, setCheck] = useState<{ text: string; result: ScenarioCheck }>();
  const asked = useRef(0);
  const [err, setErr] = useState<ApiError>();
  const [saved, setSaved] = useState("");

  useEffect(() => { if (source.data) { setText(source.data.text); setEtag(source.data.etag); } }, [source.data]);
  // The loader's verdict, a moment after typing stops. The browser decides nothing about validity.
  useEffect(() => {
    if (!text) return;
    const n = ++asked.current;
    const t = setTimeout(() => {
      const body = isNew ? { text, family, fileName: name } : { text, file };
      api.validateScenario(body).then(
        (result) => { if (n === asked.current) { setCheck({ text, result }); setErr(undefined); } },
        (e) => { if (n === asked.current) setErr(e as ApiError); },
      );
    }, 350);
    return () => clearTimeout(t);
  }, [text, file, family, name, isNew]);
  const verdict = check?.text === text ? check.result : undefined;

  const save = async () => {
    setErr(undefined); setSaved("");
    try {
      if (isNew) {
        const out = await api.importScenarios(family, [{ name, text }]);
        if (out.imported.length !== 1) throw new ApiError(422, "NOT_IMPORTED", "nothing was written; check the folder and file name");
        nav(`/scenarios/edit?file=${encodeURIComponent(`scenarios/${family}/${name}`)}`);
      } else {
        const out = await api.saveScenarioFile(file, text, etag);
        setEtag(out.etag); setSaved(`Saved ${out.file}.`);
      }
    } catch (e) { setErr(e as ApiError); }
  };
  const changedOnDisk = err?.code === "SCENARIO_CHANGED";

  return (
    <>
      <PageHeader title={isNew ? "New test case" : "Edit test case"} sub={isNew ? "Written to your catalogue when it is valid." : <Mono>{file}</Mono>}
        aside={<CoralButton className="bg-ink text-white hover:bg-ink-deep" disabled={readOnly || !verdict?.ok} onClick={() => void save()}><Save size={16} />Save</CoralButton>} />
      <Page>
        <Explainer><b>The same checks as <Mono>opseval validate</Mono>, as you type:</b> the schema, scorer and checkpoint names against your profile, and ids already used elsewhere. A file is only written when it is valid, and never over a newer version of itself.</Explainer>
        {readOnly && <Callout><b>The built-in demo catalogue is read-only.</b> Start <Mono>opseval serve --catalogue ./my-catalogue</Mono> to edit your own.</Callout>}
        <div className="grid gap-5 lg:grid-cols-[3fr_2fr]">
          <Card title="YAML">
            {isNew && (
              <div className="mb-4 grid gap-4 sm:grid-cols-2">
                <div><label className={lab}>Folder (family)</label><input className={field} value={family} onChange={(e) => setFamily(e.target.value)} /></div>
                <div><label className={lab}>File name</label><input className={field} value={name} onChange={(e) => setName(e.target.value)} /></div>
              </div>)}
            {!isNew && source.error ? <Data res={source}>{() => null}</Data> : (
              <textarea aria-label="Scenario YAML" dir="auto" spellCheck={false} value={text} onChange={(e) => setText(e.target.value)}
                className="h-[60vh] w-full resize-y rounded-2xl border border-line bg-soft p-4 font-mono text-[13px] leading-relaxed text-ink" />)}
          </Card>
          <div className="space-y-5">
            <Card title="Checks" aside={verdict && (verdict.ok ? <Pill tone="pass">valid</Pill> : <Pill tone="fail">{verdict.problems.length} problem(s)</Pill>)}>
              {!verdict ? <p className="text-sm">Checking…</p> : verdict.ok
                ? <p className="text-sm text-ink">Valid: <Mono>{verdict.id}</Mono> · {verdict.title}</p>
                : <ul className="space-y-2 text-sm">{verdict.problems.map((p, i) => (
                    <li key={i} className="text-ink">{(p.field || p.path) && <Mono>{p.field || p.path}</Mono>} {p.message}</li>))}</ul>}
              {verdict && <Warnings items={verdict.warnings} />}
            </Card>
            {saved && <Callout tone="info">{saved}</Callout>}
            {err && <Callout><b>{err.code}</b> {err.message}
              {changedOnDisk && <div className="mt-2"><button type="button" className="font-semibold underline" onClick={() => { setErr(undefined); source.reload(); }}>Reload the file (your edits here are replaced)</button></div>}</Callout>}
          </div>
        </div>
      </Page>
    </>
  );
}
