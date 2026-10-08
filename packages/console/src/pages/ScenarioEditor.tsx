import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Save } from "lucide-react";
import { api, ApiError } from "../lib/api";
import type { ScenarioCheck } from "../lib/types";
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
  const { id } = useParams();
  const isNew = id === undefined;
  const nav = useNavigate();
  const health = useApi(api.health);
  const readOnly = health.data?.readOnly === true;
  const source = useApi(() => (isNew ? Promise.resolve(null) : api.scenarioSource(id)), [id]);
  const [text, setText] = useState(isNew ? TEMPLATE : "");
  const [etag, setEtag] = useState("");
  const [family, setFamily] = useState("uncategorised");
  const [name, setName] = useState("new-001.yaml");
  const [check, setCheck] = useState<ScenarioCheck>();
  const [err, setErr] = useState<ApiError>();
  const [saved, setSaved] = useState("");

  useEffect(() => { if (source.data) { setText(source.data.text); setEtag(source.data.etag); } }, [source.data]);
  // The loader's verdict, a moment after typing stops. The browser decides nothing about validity.
  useEffect(() => {
    if (!text) return;
    const t = setTimeout(() => { api.validateScenario(text, id).then(setCheck, (e) => setErr(e as ApiError)); }, 350);
    return () => clearTimeout(t);
  }, [text, id]);

  const save = async () => {
    setErr(undefined); setSaved("");
    try {
      if (isNew) {
        const out = await api.importScenarios(family, [{ name, text }]);
        nav(`/scenarios/${encodeURIComponent(out.imported[0])}/edit`);
      } else {
        const out = await api.saveScenarioSource(id, text, etag);
        setEtag(out.etag); setSaved(`Saved ${out.file}.`);
        if (out.id !== id) nav(`/scenarios/${encodeURIComponent(out.id)}/edit`);
      }
    } catch (e) { setErr(e as ApiError); }
  };
  const changedOnDisk = err?.code === "SCENARIO_CHANGED";

  return (
    <>
      <PageHeader title={isNew ? "New test case" : "Edit test case"} sub={isNew ? "Written to your catalogue when it is valid." : <Mono>{source.data?.file ?? id}</Mono>}
        aside={<CoralButton className="bg-ink text-white hover:bg-ink-deep" disabled={readOnly || !check?.ok} onClick={() => void save()}><Save size={16} />Save</CoralButton>} />
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
            <Card title="Checks" aside={check && (check.ok ? <Pill tone="pass">valid</Pill> : <Pill tone="fail">{check.problems.length} problem(s)</Pill>)}>
              {!check ? <p className="text-sm">Checking…</p> : check.ok
                ? <p className="text-sm text-ink">Valid: <Mono>{check.id}</Mono> · {check.title}</p>
                : <ul className="space-y-2 text-sm">{check.problems.map((p, i) => (
                    <li key={i} className="text-ink">{(p.field || p.path) && <Mono>{p.field || p.path}</Mono>} {p.message}</li>))}</ul>}
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
