import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Upload } from "lucide-react";
import { api, ApiError } from "../lib/api";
import type { Problem } from "../lib/types";
import { Callout, Card, CoralButton, Explainer, Mono, Page, PageHeader } from "../components/ui";

export default function BulkImport() {
  const input = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [family, setFamily] = useState("uncategorised");
  const [problems, setProblems] = useState<Problem[]>([]);
  const [done, setDone] = useState<string[]>();
  const [busy, setBusy] = useState(false);
  const [fatal, setFatal] = useState<string>();

  const go = async () => {
    setBusy(true); setProblems([]); setDone(undefined); setFatal(undefined);
    try {
      const payload = await Promise.all(files.map(async (f) => ({ name: f.name, text: await f.text() })));
      setDone((await api.importScenarios(family, payload)).imported);
      setFiles([]);
    } catch (e) {
      const err = e as ApiError;
      if (Array.isArray(err.detail)) setProblems(err.detail as Problem[]); else setFatal(err.message);
    }
    setBusy(false);
  };

  return (
    <>
      <PageHeader title="Bulk import" sub={<Link to="/scenarios" className="text-ink underline">← Test cases</Link>} aside={<>
        <input value={family} onChange={(e) => setFamily(e.target.value)} aria-label="Family folder" className="rounded-full border border-ink/25 bg-white px-4 py-2 text-sm text-ink" />
        <CoralButton className="bg-ink text-white hover:bg-ink-deep" disabled={!files.length || busy} onClick={() => void go()}>Import {files.length} file(s)</CoralButton></>} />
      <Page>
        <Explainer><b>Drop as many scenario files as you like.</b> Every one is validated before anything is written. The import is all-or-nothing, and a file that would replace an existing scenario is refused, never silently overwritten.</Explainer>
        <div onClick={() => input.current?.click()} onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); setFiles([...e.dataTransfer.files]); }}
          className="flex cursor-pointer flex-col items-center gap-2 rounded-[28px] border-2 border-dashed border-line bg-soft px-6 py-14 text-center">
          <Upload className="text-deep-coral" /><b className="text-ink">Choose .yaml files</b>
          <span className="text-sm">They are read in the browser and posted as text. Nothing is written until you press import. Rename a <Mono>.yml</Mono> file first: the loader reads only <Mono>.yaml</Mono>, so it would never run.</span>
          <input ref={input} type="file" multiple accept=".yaml" hidden onChange={(e) => setFiles([...(e.target.files ?? [])])} />
        </div>
        {files.length > 0 && <Card title="Selected"><ul className="font-mono text-sm">{files.map((f) => <li key={f.name}>{f.name}</li>)}</ul></Card>}
        {fatal && <Callout>{fatal}</Callout>}
        {problems.length > 0 && <Callout><b>Nothing was imported.</b> Fix these and try again:<ul className="mt-1 list-disc pl-5 text-sm">{problems.map((p, i) => <li key={i}><Mono>{p.path}</Mono> {p.field && `${p.field}: `}{p.message}</li>)}</ul></Callout>}
        {done && <Callout tone="info"><b>Imported {done.length} scenario(s):</b> {done.join(", ")}. <Link to="/scenarios">View test cases</Link></Callout>}
      </Page>
    </>
  );
}
