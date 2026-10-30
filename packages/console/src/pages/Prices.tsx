import { useState, type FormEvent } from "react";
import { api, ApiError } from "../lib/api";
import { useApi } from "../lib/useApi";
import type { Price, PriceBody, PriceResolved } from "../lib/types";
import { Callout, Card, CoralButton, Data, Explainer, Mono, NotMeasured, Page, PageHeader, Pill, Table } from "../components/ui";

const UNITS: Record<Price["unit"], string> = { tokens: "per 1M tokens", characters: "per 1M characters", seconds: "per minute of audio" };
const FIELDS: { key: keyof PriceBody & string; label: string; units: Price["unit"][] }[] = [
  { key: "input", label: "Input", units: ["tokens"] },
  { key: "output", label: "Output", units: ["tokens"] },
  { key: "cachedInput", label: "Cached input", units: ["tokens"] },
  { key: "reasoning", label: "Reasoning", units: ["tokens"] },
  { key: "characters", label: "Characters", units: ["characters"] },
  { key: "audioIn", label: "Audio in", units: ["seconds"] },
  { key: "audioOut", label: "Audio out", units: ["seconds"] },
];
const money = (v: number | null) => (v === null ? "—" : `$${v}`);

function Rows({ rows }: { rows: Price[] }) {
  return (
    <Table head={["Model", "Unit", "In force from", "Prices (USD)", "Source"]}>
      {rows.map((p, i) => (
        <tr key={p.id ?? `${p.match}:${p.effectiveFrom}:${i}`}>
          <td><Mono>{p.match}</Mono> <Pill tone="mute">{p.matchKind}</Pill></td>
          <td>{UNITS[p.unit]}</td>
          <td>{p.effectiveAt ?? p.effectiveFrom}</td>
          <td className="text-sm">{FIELDS.filter((f) => f.units.includes(p.unit) && p[f.key as keyof Price] !== null).map((f) => `${f.label} ${money(p[f.key as keyof Price] as number | null)}`).join(" · ")}</td>
          <td className="text-sm">{p.source || "—"}{p.createdBy ? ` · added by ${p.createdBy}` : ""}</td>
        </tr>))}
    </Table>
  );
}

/** The price table (ADR-0066 D2): the built-in rows, a project's own, and which row prices a
 * model at a moment. Whether this caller may add a row is the server's answer (canEdit). */
export default function Prices() {
  const res = useApi(api.prices);
  const [body, setBody] = useState<PriceBody>({ pattern: "", matchKind: "exact", unit: "tokens", source: "" });
  const [error, setError] = useState("");
  const [model, setModel] = useState("");
  const [at, setAt] = useState("");
  const [resolved, setResolved] = useState<PriceResolved>();
  const add = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try { await api.addPrice(body); setBody({ pattern: "", matchKind: "exact", unit: body.unit, source: "" }); res.reload(); }
    catch (err) { setError((err as ApiError).message); }
  };
  const resolve = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try { setResolved(await api.resolvePrice(model, at)); } catch (err) { setError((err as ApiError).message); }
  };
  const priceField = (key: keyof PriceBody & string) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setBody({ ...body, [key]: e.target.value === "" ? null : Number(e.target.value) });
  return (
    <>
      <PageHeader title="Prices" sub="What each model costs, and since when." />
      <Page>
        <Explainer>Each call is priced when it is made, with the row in force then, and keeps that cost: a new row never reprices an earlier run. A project&apos;s own row wins over the built-in table. A model with no row has <b>no price</b>, never $0.</Explainer>
        {error && <Callout>{error}</Callout>}
        <Card title="Which price applies">
          <form onSubmit={(e) => void resolve(e)} className="flex flex-wrap items-end gap-3" aria-label="Resolve a price">
            <label className="text-sm">Model<input required value={model} onChange={(e) => setModel(e.target.value)} className="ml-2 rounded-full border border-ink/25 px-3 py-1" /></label>
            <label className="text-sm">At<input type="date" value={at} onChange={(e) => setAt(e.target.value)} className="ml-2 rounded-full border border-ink/25 px-3 py-1" /></label>
            <CoralButton type="submit">Resolve</CoralButton>
          </form>
          {resolved && (
            <p className="mt-3 text-sm" data-testid="resolved">
              {resolved.price === null ? <NotMeasured text="no price" /> : <><Mono>{resolved.price.match}</Mono> ({resolved.source}), in force from {resolved.price.effectiveAt ?? resolved.price.effectiveFrom}</>}
            </p>)}
        </Card>
        <Data res={res}>{(d) => (<>
          <Card title="This project's prices" className="[&>div]:p-0">
            {d.project.length === 0 ? <p className="p-4 text-sm">None: the built-in table prices every call.</p> : <Rows rows={d.project} />}
          </Card>
          {d.canEdit ? (
            <Card title="Add a price">
              <form onSubmit={(e) => void add(e)} className="space-y-3" aria-label="Add a price">
                <div className="flex flex-wrap gap-3">
                  <label className="text-sm">Model name or glob<input required maxLength={200} value={body.pattern} onChange={(e) => setBody({ ...body, pattern: e.target.value })} className="ml-2 rounded-full border border-ink/25 px-3 py-1" /></label>
                  <label className="text-sm">Match
                    <select value={body.matchKind} onChange={(e) => setBody({ ...body, matchKind: e.target.value as PriceBody["matchKind"] })} className="ml-2 rounded-full border border-ink/25 px-3 py-1">
                      <option value="exact">exactly</option><option value="glob">glob (* only)</option>
                    </select>
                  </label>
                  <label className="text-sm">Unit
                    <select value={body.unit} onChange={(e) => setBody({ ...body, unit: e.target.value as PriceBody["unit"] })} className="ml-2 rounded-full border border-ink/25 px-3 py-1">
                      {Object.entries(UNITS).map(([u, label]) => <option key={u} value={u}>{label}</option>)}
                    </select>
                  </label>
                </div>
                <div className="flex flex-wrap gap-3">
                  {FIELDS.filter((f) => f.units.includes(body.unit)).map((f) => (
                    <label key={f.key} className="text-sm">{f.label} (USD)<input type="number" min={0} step="any" value={(body[f.key] as number | null | undefined) ?? ""} onChange={priceField(f.key)} className="ml-2 w-28 rounded-full border border-ink/25 px-3 py-1" /></label>))}
                </div>
                <label className="block text-sm">Source<input maxLength={500} value={body.source ?? ""} onChange={(e) => setBody({ ...body, source: e.target.value })} className="ml-2 w-96 max-w-full rounded-full border border-ink/25 px-3 py-1" /></label>
                <CoralButton type="submit">Add the row (in force from now)</CoralButton>
              </form>
            </Card>
          ) : <p className="text-sm text-muted-ink" data-testid="read-only">{d.editNote}</p>}
          <Card title="Built-in prices" className="[&>div]:p-0"><Rows rows={d.builtin} /></Card>
        </>)}</Data>
      </Page>
    </>
  );
}
