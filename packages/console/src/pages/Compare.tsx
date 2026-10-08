import { useEffect, useState } from "react";
import { ArrowRight } from "lucide-react";
import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import type { Change } from "../lib/types";
import { AreaPill, Callout, Card, Data, Explainer, Mono, NotMeasured, Page, PageHeader, Pill } from "../components/ui";

const pct = (v: number | null) => (v === null ? <NotMeasured text="no data" /> : <Mono>{Math.round(v * 100)}%</Mono>);

function Changes({ title, rows, tone }: { title: string; rows: Change[]; tone: "fail" | "pass" | "mute" }) {
  return (
    <Card title={title} aside={<Pill tone={tone} dashed={tone === "mute"}>{rows.length}</Pill>}>
      {rows.length === 0 ? <span>None.</span> : <ul className="divide-y divide-line">{rows.map((c) => (
        <li key={c.testId} className="py-2 text-ink">{c.name} <Mono>{c.testId}</Mono><div className="text-sm text-muted-ink">{c.before} → {c.after}</div></li>))}</ul>}
    </Card>
  );
}

export default function Compare() {
  const runs = useApi(api.runs);
  const ids = runs.data?.filter((r) => r.state === "finished").map((r) => r.id) ?? [];
  const [base, setBase] = useState("");
  const [cand, setCand] = useState("");
  useEffect(() => { if (ids.length >= 2 && !base) { setCand(ids[0]); setBase(ids[1]); } }, [ids.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const cmp = useApi(() => (base && cand ? api.compare(base, cand) : Promise.resolve(undefined)), [base, cand]);
  const sel = (v: string, set: (s: string) => void) => (
    <select value={v} onChange={(e) => set(e.target.value)} className="rounded-xl border border-line bg-white px-3 py-1.5 font-mono text-sm text-ink">
      {ids.map((i) => <option key={i}>{i}</option>)}</select>);
  return (
    <>
      <PageHeader title="Compare runs" sub="Is this build worse than the last one?" />
      <Page>
        <Explainer>This is a different question from "did the run pass". Scenarios that could not be judged are listed separately: they are outages, not regressions.</Explainer>
        <Data res={runs}>{() => ids.length < 2 ? <Card>You need at least two finished runs to compare.</Card> : (<>
          <Card><div className="flex flex-wrap items-center gap-4 text-ink">Baseline {sel(base, setBase)}<ArrowRight size={18} className="text-deep-coral" />Candidate {sel(cand, setCand)}</div></Card>
          <Data res={cmp}>{(c) => !c ? null : (<>
            {c.regressed.length === 0
              ? <Callout tone="info"><b className="font-display text-xl">Nothing regressed.</b> <Pill tone="pass">no regression</Pill></Callout>
              : <Callout><b className="font-display text-xl">{c.regressed.length} regression{c.regressed.length > 1 && "s"}.</b> <Pill tone="fail">regressed</Pill></Callout>}
            <div className="grid gap-5 md:grid-cols-3">
              <Changes title="Regressed" rows={c.regressed} tone="fail" />
              <Changes title="Improved" rows={c.improved} tone="pass" />
              <Changes title="Not judged" rows={c.unjudged} tone="mute" />
            </div>
            <Card title="Quality areas (mean score)"><ul className="divide-y divide-line">{c.areas.map((a) => (
              <li key={a.area} className="flex items-center justify-between py-3"><AreaPill area={a.area} /><span className="flex items-center gap-3">{pct(a.before)}<ArrowRight size={14} />{pct(a.after)}</span></li>))}</ul></Card>
          </>)}</Data></>)}</Data>
      </Page>
    </>
  );
}
