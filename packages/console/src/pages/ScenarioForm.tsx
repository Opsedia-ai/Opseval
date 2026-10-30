import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { FileCode, Plus, Save, Trash2 } from "lucide-react";
import { api, ApiError } from "../lib/api";
import type { Draft, DraftCheck, Drafted, FormCheck, FormParam, Problem, ScenarioForm as Form } from "../lib/types";
import { useApi } from "../lib/useApi";
import { Warnings } from "../components/Warnings";
import { AreaPill, Button, Callout, Card, CoralButton, Data, ErrorBox, Explainer, Mono, Page, PageHeader, Pill, cx } from "../components/ui";
import { useCan } from "../lib/session";

const field = "mt-1 w-full rounded-xl border border-line bg-white px-3 py-2 text-ink";
const lab = "text-xs font-semibold uppercase tracking-[0.08em]";

/**
 * What a typed setting becomes. Only decoding: an entry that cannot be read is left out and
 * said so here, and whether the result is valid is the loader's answer, never this one.
 */
export function parseParam(kind: FormParam["kind"], text: string): { value?: unknown; error?: string } {
  const t = text.trim();
  if (t === "") return {};
  switch (kind) {
    case "integer":
    case "number": {
      const n = Number(t);
      if (!Number.isFinite(n) || (kind === "integer" && !Number.isInteger(n))) return { error: kind === "integer" ? "a whole number" : "a number" };
      return { value: n };
    }
    case "boolean":
    case "choice":
      return { value: kind === "boolean" ? t === "true" : t };
    case "text_list":
      return { value: t.split(/[\n,]/).map((x) => x.trim()).filter(Boolean) };
    case "integer_list": {
      const ns = t.split(/[\s,]+/).filter(Boolean).map(Number);
      return ns.every(Number.isInteger) ? { value: ns } : { error: "whole numbers, separated by commas" };
    }
    case "json":
      try { return { value: JSON.parse(t) as unknown }; } catch { return { error: 'not JSON: quote text, "like this"' }; }
    default:
      return { value: t };
  }
}

function shown(kind: FormParam["kind"], value: unknown): string {
  if (value === undefined) return "";
  if (kind === "json") return JSON.stringify(value);
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function Field({ label, help, children }: { label: string; help?: ReactNode; children: ReactNode }) {
  return <div><span className={lab}>{label}</span>{children}{help && <p className="mt-1 text-sm">{help}</p>}</div>;
}

function Lines({ label, items, onChange, add, placeholder }: { label: string; items: string[]; onChange: (v: string[]) => void; add: string; placeholder?: string }) {
  const rows = items.length ? items : [""];
  return (
    <Field label={label}>
      <div className="mt-1 space-y-2">
        {rows.map((t, i) => (
          <div key={i} className="flex items-start gap-2">
            <textarea aria-label={`${label} ${i + 1}`} rows={1} dir="auto" value={t} placeholder={placeholder}
              onChange={(e) => onChange(rows.map((x, j) => (j === i ? e.target.value : x)))} className={cx(field, "mt-0 min-h-[42px] flex-1 resize-y")} />
            <button type="button" aria-label={`Remove ${label} ${i + 1}`} onClick={() => onChange(rows.filter((_, j) => j !== i))} className="mt-2 text-muted-ink hover:text-ink"><Trash2 size={16} /></button>
          </div>))}
        <Button onClick={() => onChange([...rows, ""])}><Plus size={16} />{add}</Button>
      </div>
    </Field>
  );
}

/** Marks a field whose text could not be decoded, so Save waits; cleared when it unmounts. */
type OnBad = (bad: boolean) => void;

function ParamInput({ p, value, onChange, onBad }: { p: FormParam; value: unknown; onChange: (v: unknown) => void; onBad: OnBad }) {
  const [text, setText] = useState(() => shown(p.kind, value));
  const [error, setError] = useState<string>();
  useEffect(() => () => onBad(false), []); // eslint-disable-line react-hooks/exhaustive-deps
  const change = (t: string) => { setText(t); const r = parseParam(p.kind, t); setError(r.error); onBad(r.error !== undefined); onChange(r.value); };
  const label = `${p.name}${p.required ? " · required" : ""}`;
  const unset = p.required ? "choose…" : p.default === null || p.default === undefined ? "not set" : `default: ${JSON.stringify(p.default)}`;
  let input: ReactNode;
  if (p.kind === "choice" || p.kind === "boolean") {
    input = (
      <select aria-label={p.name} value={text} onChange={(e) => change(e.target.value)} className={field}>
        <option value="">{unset}</option>
        {(p.choices ?? ["true", "false"]).map((c) => <option key={c} value={c}>{c}</option>)}
      </select>);
  } else if (p.kind === "json" || p.kind === "text_list") {
    input = <textarea aria-label={p.name} rows={2} value={text} placeholder={unset} onChange={(e) => change(e.target.value)} className={cx(field, "font-mono text-[13px]")} />;
  } else {
    const numeric = p.kind === "integer" || p.kind === "number";
    input = <input aria-label={p.name} type={numeric ? "number" : "text"} min={p.minimum ?? undefined} max={p.maximum ?? undefined} step={p.kind === "number" ? "any" : undefined}
      value={text} placeholder={unset} onChange={(e) => change(e.target.value)} className={field} />;
  }
  return <Field label={label} help={error ? <span className="text-deep-coral">{error}</span> : p.description}>{input}</Field>;
}

/**
 * A number field that keeps what is typed. Clearing it no longer snaps it to 1 (so typing 5
 * after a clear gave 15): until it reads as a number it is marked and Save waits. Whether the
 * number is allowed is the server's answer, at this field.
 */
function NumberField({ label, value, onChange, onBad, whole = true, help, min, max, step }: {
  label: string; value: number; onChange: (n: number) => void; onBad: OnBad; whole?: boolean; help?: ReactNode; min?: number; max?: number; step?: number;
}) {
  const [text, setText] = useState(String(value));
  const r = parseParam(whole ? "integer" : "number", text);
  const error = r.error ?? (r.value === undefined ? "required" : undefined);
  return (
    <Field label={label} help={error ? <span className="text-deep-coral">{error}</span> : help}>
      <input aria-label={label} type="number" min={min} max={max} step={step ?? (whole ? 1 : "any")} className={field} value={text}
        onChange={(e) => { const t = e.target.value; setText(t); const n = parseParam(whole ? "integer" : "number", t); const ok = typeof n.value === "number"; onBad(!ok); if (ok) onChange(n.value as number); }} />
    </Field>
  );
}

function CheckRow({ check, chosen, onToggle, onChange, onBad }: { check: FormCheck; chosen?: DraftCheck; onToggle: () => void; onChange: (c: DraftCheck) => void; onBad: (field: string, bad: boolean) => void }) {
  return (
    <li className="py-3">
      <label className="flex cursor-pointer items-start gap-3">
        <input type="checkbox" checked={!!chosen} onChange={onToggle} className="mt-1" aria-label={check.id} />
        <span className="flex-1"><Mono>{check.id}</Mono> <AreaPill area={check.area} /> <Pill>{check.reads}</Pill><span className="mt-0.5 block text-sm">{check.question}</span></span>
      </label>
      {chosen && (
        <div className="ml-7 mt-3 grid gap-3 sm:grid-cols-2">
          <ParamInput p={{ name: "threshold", kind: "number", required: false, description: "The score that counts as a pass, 0 to 1.", choices: null, minimum: 0, maximum: 1, default: 1 }}
            value={chosen.threshold ?? undefined} onChange={(v) => onChange({ ...chosen, threshold: typeof v === "number" ? v : null })} onBad={(b) => onBad(`${check.id}.threshold`, b)} />
          {check.params === null
            ? <p className="text-sm sm:col-span-2">This check does not describe its settings. Set them in the YAML editor after saving.</p>
            : check.params.map((p) => (
                <ParamInput key={p.name} p={p} value={chosen.params[p.name]} onBad={(b) => onBad(`${check.id}.${p.name}`, b)} onChange={(v) => {
                  const params = { ...chosen.params };
                  if (v === undefined) delete params[p.name]; else params[p.name] = v;
                  onChange({ ...chosen, params });
                }} />))}
        </div>)}
    </li>
  );
}

function ToolPicks({ label, tools, value, onChange }: { label: string; tools: Form["profile"]["tools"]; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <Field label={label}>
      <div className="mt-2 flex flex-wrap gap-2">
        {tools.map((t) => {
          const on = value.includes(t.name);
          return (
            <button key={t.name} type="button" aria-pressed={on} onClick={() => onChange(on ? value.filter((x) => x !== t.name) : [...value, t.name])}
              className={cx("rounded-full border px-3 py-1 font-mono text-[13px]", on ? "border-ink bg-ink text-white" : "border-line bg-white text-ink")}>
              {t.name}{t.commits && " · commits"}
            </button>);
        })}
      </div>
    </Field>
  );
}

function Problems({ items }: { items: Problem[] }) {
  return <ul className="space-y-2 text-sm">{items.map((p, i) => <li key={i} className="text-ink">{(p.field || p.path) && <Mono>{p.field || p.path}</Mono>} {p.message}</li>)}</ul>;
}

/**
 * The guided way to add a test case (ADR-0048). Every choice comes from the server (the
 * profile's checkpoints and tools, the checks and their settings), the YAML is written by the
 * server and checked by the loader, and Save goes through the same never-overwrite import as
 * any new file. The result is an ordinary scenario file.
 */
function Editor({ form }: { form: Form }) {
  const canWrite = useCan("catalogue:write");
  const nav = useNavigate();
  const [template, setTemplate] = useState(form.templates[0]?.id ?? "");
  const [draft, setDraft] = useState<Draft>(() => form.templates[0].draft);
  const [generation, setGeneration] = useState(0); // remounts the inputs that keep their own text
  const [result, setResult] = useState<{ key: string; out: Drafted }>();
  const [draftErr, setDraftErr] = useState<ApiError>();
  const [saveErr, setSaveErr] = useState<ApiError>();
  const [busy, setBusy] = useState(false);
  const [bad, setBad] = useState<Record<string, boolean>>({});
  const markBad = useCallback((name: string, b: boolean) => setBad((m) => (!!m[name] === b ? m : { ...m, [name]: b })), []);
  const undecoded = Object.keys(bad).filter((k) => bad[k]);
  const asked = useRef(0);
  const key = JSON.stringify(draft);
  const set = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }));
  const { profile } = form;
  const language = form.languages.find((l) => l.code === draft.language);

  // The server's YAML and the loader's verdict, a moment after the last change.
  useEffect(() => {
    const n = ++asked.current;
    const t = setTimeout(() => {
      api.draftScenario(JSON.parse(key) as Draft).then(
        (out) => { if (n === asked.current) { setResult({ key, out }); setDraftErr(undefined); } },
        (e) => { if (n === asked.current) setDraftErr(e as ApiError); },
      );
    }, 350);
    return () => clearTimeout(t);
  }, [key]);
  // Save is enabled only for the verdict on exactly what is in the form now.
  const fresh = result?.key === key ? result.out : undefined;

  const start = (id: string) => {
    const t = form.templates.find((x) => x.id === id);
    if (!t) return;
    setTemplate(id); setDraft(t.draft); setBad({}); setGeneration((g) => g + 1);
  };
  const answer = (checkpoint: string, action: string) => {
    const rest = draft.answers.filter((a) => a.checkpoint !== checkpoint);
    set({ answers: action ? [...rest, { checkpoint, action: action as Draft["answers"][number]["action"], feedback: null }] : rest });
  };
  const toggle = (id: string) => {
    const on = draft.checks.some((c) => c.id === id);
    set({ checks: on ? draft.checks.filter((c) => c.id !== id) : [...draft.checks, { id, threshold: null, params: {} }] });
  };
  // What `scorers: default` runs for this draft, as the server last said.
  const pack = result?.out.defaultChecks ?? form.defaultChecks;
  const blankCheck = (id: string): DraftCheck => ({ id, threshold: null, params: {} });
  // By hand: start from the pack (with any settings already given), then what was added.
  const byHand = () => set({ useDefaultChecks: false, checks: [...pack.map((id) => draft.checks.find((c) => c.id === id) ?? blankCheck(id)), ...draft.checks.filter((c) => !pack.includes(c.id))] });
  // Back to the defaults: keep only what was added to them, so no hidden setting lingers.
  const defaults = () => set({ useDefaultChecks: true, checks: draft.checks.filter((c) => !pack.includes(c.id)) });
  // With the defaults on, the list offers what is not in the pack, and anything added to it.
  const listed = draft.useDefaultChecks ? form.checks.filter((c) => !pack.includes(c.id) || draft.checks.some((x) => x.id === c.id)) : form.checks;
  const stop = form.stopAt.find((c) => c.value === draft.stopAt);

  const save = async () => {
    if (!fresh?.check.ok || undecoded.length) return;
    setSaveErr(undefined); setBusy(true);
    try {
      const file = await api.saveNewScenario(fresh.family, fresh.fileName, fresh.yaml);
      nav(`/scenarios/edit?file=${encodeURIComponent(file)}`);
    } catch (e) { setSaveErr(e as ApiError); }
    setBusy(false);
  };
  const saveProblems = Array.isArray(saveErr?.detail) ? (saveErr.detail as Problem[]) : null;
  const requestProblems = Array.isArray(draftErr?.detail) ? (draftErr.detail as Problem[]) : null;

  return (
    <>
      <PageHeader title="New test case" sub={<>For profile <Mono>{profile.name}</Mono>. Saved as YAML in your catalogue.</>} aside={<>
        <Button to="/scenarios/new/yaml"><FileCode size={16} />Write YAML instead</Button>
        <CoralButton className="bg-ink text-white hover:bg-ink-deep" disabled={form.readOnly || !canWrite || busy || !fresh?.check.ok || undecoded.length > 0} onClick={() => void save()}><Save size={16} />Save</CoralButton></>} />
      <Page>
        <Explainer><b>Fill in the situation; Opseval writes the YAML.</b> The choices come from your profile (its checkpoints and tools) and from the checks Opseval has. What is written is checked exactly as <Mono>opseval validate</Mono> would check it, and saved only when it is valid and does not replace a file.</Explainer>
        {form.readOnly && <Callout><b>The built-in demo catalogue is read-only.</b> You can try the form; to save, start <Mono>opseval serve --catalogue ./my-catalogue</Mono>.</Callout>}

        <Card title="Start from">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {form.templates.map((t) => (
              <button key={t.id} type="button" aria-pressed={template === t.id} onClick={() => start(t.id)}
                className={cx("rounded-2xl border p-4 text-left transition", template === t.id ? "border-ink bg-soft" : "border-line bg-white hover:border-ink/40")}>
                <span className="block font-semibold text-ink">{t.title}</span><span className="mt-1 block text-sm">{t.description}</span>
              </button>))}
          </div>
        </Card>

        <div className="grid items-start gap-5 lg:grid-cols-[3fr_2fr]">
          <div key={generation} className="space-y-5">
            <Card title="The test">
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Id" help="Letters, digits, '.', '_' and '-'; unique in the catalogue."><input aria-label="Id" className={field} value={draft.id} onChange={(e) => set({ id: e.target.value })} /></Field>
                <Field label="Criticality"><select aria-label="Criticality" className={field} value={draft.criticality} onChange={(e) => set({ criticality: e.target.value as Draft["criticality"] })}>
                  <option value="normal">normal</option><option value="critical">critical: a failure is named first</option><option value="blocker">blocker</option></select></Field>
                <div className="sm:col-span-2"><Field label="Name"><input aria-label="Name" dir="auto" className={field} value={draft.name} onChange={(e) => set({ name: e.target.value })} /></Field></div>
                <div className="sm:col-span-2"><Field label="Goal" help="What the customer wants, in a sentence. Judges grade against it."><textarea aria-label="Goal" dir="auto" rows={2} className={field} value={draft.goal} onChange={(e) => set({ goal: e.target.value })} /></Field></div>
                <div className="sm:col-span-2"><Field label="Tags" help="Separated by commas; suites can select by tag.">
                  <input aria-label="Tags" className={field} defaultValue={draft.tags.join(", ")} onChange={(e) => set({ tags: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} /></Field></div>
              </div>
            </Card>

            <Card title="The customer">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="sm:col-span-2"><Field label="Who they are"><textarea aria-label="Who they are" dir="auto" rows={2} className={field} value={draft.role} onChange={(e) => set({ role: e.target.value })} /></Field></div>
                <Field label="Language"><select aria-label="Language" className={field} value={draft.language} onChange={(e) => set({ language: e.target.value, dialect: null })}>
                  {form.languages.map((l) => <option key={l.code} value={l.code}>{l.code}{!l.stable && " (preview)"}</option>)}</select></Field>
                {language && language.dialects.length > 0 && (
                  <Field label="Dialect"><select aria-label="Dialect" className={field} value={draft.dialect ?? ""} onChange={(e) => set({ dialect: e.target.value || null })}>
                    <option value="">standard</option>{language.dialects.map((d) => <option key={d} value={d}>{d}</option>)}</select></Field>)}
                <NumberField label="Typing noise" whole={false} min={0} max={1} step={0.05} value={draft.typingNoise} onChange={(n) => set({ typingNoise: n })} onBad={(b) => markBad("typingNoise", b)}
                  help="0 types cleanly; 1 adds a typo to most messages." />
                <div className="sm:col-span-2">
                  <span className={lab}>Who writes the customer's messages</span>
                  <div className="mt-2 grid gap-2 sm:grid-cols-2">
                    {([["scripted", "A fixed script", "The same turns every time. No LLM needed, so it runs in CI for free."], ["simulated", "A simulated customer", "An LLM plays the customer from the goal and the behaviour below."]] as const).map(([m, t, d]) => (
                      <label key={m} className={cx("cursor-pointer rounded-2xl border p-3", draft.mode === m ? "border-ink bg-soft" : "border-line")}>
                        <input type="radio" name="mode" className="mr-2" checked={draft.mode === m} onChange={() => set({ mode: m })} /><b className="text-ink">{t}</b><span className="mt-1 block text-sm">{d}</span>
                      </label>))}
                  </div>
                </div>
              </div>
            </Card>

            <Card title="The conversation">
              <div className="space-y-4">
                {draft.mode === "scripted"
                  ? <Lines label="Customer turns" items={draft.scriptedTurns} onChange={(v) => set({ scriptedTurns: v })} add="Add a turn" placeholder="What the customer says" />
                  : <>
                      <Field label="Opening message" help="Optional: the simulated customer writes its own when left empty."><input aria-label="Opening message" dir="auto" className={field} value={draft.openingMessage ?? ""} onChange={(e) => set({ openingMessage: e.target.value || null })} /></Field>
                      <Field label="How they behave" help="What they say when, what they hold back, when they give up."><textarea aria-label="How they behave" dir="auto" rows={4} className={field} value={draft.flow} onChange={(e) => set({ flow: e.target.value })} /></Field>
                    </>}
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Stop at" help={stop?.help}>
                    <select aria-label="Stop at" className={field} value={draft.stopAt} onChange={(e) => set({ stopAt: e.target.value })}>
                      {form.stopAt.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}</select></Field>
                  <NumberField label="Most customer turns" min={1} max={100} value={draft.maxTurns} onChange={(n) => set({ maxTurns: n })} onBad={(b) => markBad("maxTurns", b)} />
                </div>
                {profile.checkpoints.length > 0 && (
                  <Field label="At each checkpoint the customer…" help="A scripted customer can only approve or reject. An approval step it has no answer for ends the attempt as not judged, never as an agent failure.">
                    <ul className="mt-2 divide-y divide-line">
                      {profile.checkpoints.map((c) => {
                        const a = draft.answers.find((x) => x.checkpoint === c.name);
                        return (
                          <li key={c.name} className="py-2">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <span><Mono>{c.name}</Mono>{c.phase && <span className="text-sm"> · {c.phase}</span>}</span>
                              {c.commitGate
                                ? <Pill tone="warn">commit gate: the run stops before it</Pill>
                                : <select aria-label={`Answer at ${c.name}`} className="rounded-xl border border-line bg-white px-3 py-1.5 text-ink" value={a?.action ?? ""} onChange={(e) => answer(c.name, e.target.value)}>
                                    <option value="">not set</option>
                                    {form.answerActions.filter((x) => x.modes.includes(draft.mode) || x.value === a?.action).map((x) => <option key={x.value} value={x.value}>{x.label}</option>)}</select>}
                            </div>
                            {a?.action === "reject" && <input aria-label={`What the customer says when rejecting at ${c.name}`} dir="auto" placeholder="What the customer says when rejecting" className={field} value={a.feedback ?? ""}
                              onChange={(e) => set({ answers: draft.answers.map((x) => (x.checkpoint === c.name ? { ...x, feedback: e.target.value || null } : x)) })} />}
                          </li>);
                      })}
                    </ul>
                  </Field>)}
              </div>
            </Card>

            <Card title="What should happen">
              <div className="space-y-4">
                <label className="flex items-start gap-3"><input type="checkbox" className="mt-1" checked={draft.refusal} onChange={(e) => set({ refusal: e.target.checked })} />
                  <span><b className="text-ink">The agent should refuse</b><span className="block text-sm">The correct outcome is declining the request, and the goal judge grades it that way.</span></span></label>
                <Field label="Expected outcome" help="Optional: what a good ending looks like, for the judges."><textarea aria-label="Expected outcome" dir="auto" rows={2} className={field} value={draft.expectedOutcome ?? ""} onChange={(e) => set({ expectedOutcome: e.target.value || null })} /></Field>
                {profile.tools.length > 0
                  ? <div className="grid gap-4 sm:grid-cols-2">
                      <ToolPicks label="Must call" tools={profile.tools} value={draft.mustCall} onChange={(v) => set({ mustCall: v })} />
                      <ToolPicks label="Must not call" tools={profile.tools} value={draft.mustNotCall} onChange={(v) => set({ mustNotCall: v })} />
                    </div>
                  : <p className="text-sm">The profile declares no tools, so there is nothing to require or forbid here.</p>}
                <Lines label="Rules the agent must follow" items={draft.policy} onChange={(v) => set({ policy: v })} add="Add a rule" placeholder="e.g. Never promise a delivery date." />
                <Field label="Prompt injection" help="Optional: planted word for word in the first customer turn. Add security.no_injection_takeover to grade it.">
                  <textarea aria-label="Prompt injection" rows={2} className={cx(field, "font-mono text-[13px]")} value={draft.injection ?? ""} onChange={(e) => set({ injection: e.target.value || null })} /></Field>
              </div>
            </Card>

            <Card title="Checks">
              <label className="flex items-start gap-3"><input type="checkbox" className="mt-1" checked={draft.useDefaultChecks} onChange={(e) => (e.target.checked ? defaults() : byHand())} />
                <span><b className="text-ink">The default checks</b><span className="block text-sm">Chosen for this test and your profile, the same as <Mono>scorers: default</Mono>: every tool rule, refusal and policy above is graded. Untick to choose every check by hand.</span></span></label>
              {draft.useDefaultChecks && <ul aria-label="Default checks" className="mt-3 flex flex-wrap gap-2">{pack.map((c) => <li key={c} className="rounded-full border border-line bg-soft px-3 py-1"><Mono>{c}</Mono></li>)}</ul>}
              <h3 className={cx(lab, "mt-5")}>{draft.useDefaultChecks ? "Also run" : "Checks to run"}</h3>
              <ul className="divide-y divide-line">
                {listed.map((c) => <CheckRow key={c.id} check={c} chosen={draft.checks.find((x) => x.id === c.id)} onToggle={() => toggle(c.id)} onBad={markBad}
                  onChange={(next) => set({ checks: draft.checks.map((x) => (x.id === c.id ? next : x)) })} />)}
              </ul>
            </Card>

            <Card title="Runs and file">
              <div className="grid gap-4 sm:grid-cols-3">
                <NumberField label="Repeats" min={1} max={20} value={draft.repeats} onChange={(n) => set({ repeats: n })} onBad={(b) => markBad("repeats", b)}
                  help="Attempts per run; a flaky agent shows up as disagreement." />
                <Field label="Folder"><input aria-label="Folder" className={field} value={draft.family} onChange={(e) => set({ family: e.target.value })} /></Field>
                <Field label="File name"><input aria-label="File name" className={field} placeholder={result?.out.fileName} value={draft.fileName ?? ""} onChange={(e) => set({ fileName: e.target.value || null })} /></Field>
              </div>
            </Card>
          </div>

          <div className="space-y-5 lg:sticky lg:top-5">
            <Card title="Validation" aside={draftErr ? <Pill tone="fail">not checked</Pill> : fresh && (fresh.check.ok ? <Pill tone="pass">valid</Pill> : <Pill tone="fail">{fresh.check.problems.length} problem(s)</Pill>)}>
              {draftErr
                ? (requestProblems ? <><p className="mb-2 text-sm text-ink">These fields cannot be sent as they are:</p><Problems items={requestProblems} /></> : <ErrorBox error={draftErr} />)
                : !fresh ? <p className="text-sm">Checking…</p> : fresh.check.ok
                  ? <p className="text-sm text-ink">Valid: <Mono>{fresh.check.id}</Mono> · saved as <Mono>{fresh.family}/{fresh.fileName}</Mono></p>
                  : <Problems items={fresh.check.problems} />}
              {undecoded.length > 0 && <p className="mt-3 text-sm text-deep-coral">Not read as typed: {undecoded.join(", ")}. Fix the field to save.</p>}
              {fresh && !draftErr && <Warnings items={fresh.check.warnings} />}
            </Card>
            {saveErr && (saveProblems ? <Callout><b>Nothing was saved.</b><div className="mt-2"><Problems items={saveProblems} /></div></Callout> : <ErrorBox error={saveErr} />)}
            <Card title="YAML" aside={<Pill tone="mute">written by the server</Pill>}>
              <pre aria-label="Generated YAML" dir="auto" className={cx("max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-2xl bg-soft p-4 font-mono text-[13px] leading-relaxed text-ink", !fresh && "opacity-60")}>{result?.out.yaml ?? ""}</pre>
            </Card>
          </div>
        </div>
      </Page>
    </>
  );
}

export default function ScenarioForm() {
  const res = useApi(api.scenarioForm);
  return <Data res={res}>{(form) => <Editor form={form} />}</Data>;
}
