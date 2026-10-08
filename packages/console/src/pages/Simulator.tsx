import { useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import { useApi } from "../lib/useApi";
import type { Turn } from "../lib/types";
import { Button, Callout, Card, CoralButton, Data, ErrorBox, Explainer, Mono, NotMeasured, Page, PageHeader, Pill, cx } from "../components/ui";

export default function Simulator() {
  const scenarios = useApi(api.scenarios);
  const [id, setId] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError>();
  const [model, setModel] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const persona = useApi(() => (id ? api.persona(id) : Promise.resolve(undefined)), [id]);

  useEffect(() => { if (!id && scenarios.data?.scenarios[0]) setId(scenarios.data.scenarios[0].id); }, [scenarios.data, id]);

  const restart = async () => {
    setTurns([]); setReply(""); setErr(undefined); setDone(false);
    if (!id) return;
    setBusy(true);
    try { const r = await api.rehearse(id, []); setTurns([r.turn]); setModel(r.model); }
    catch (e) { setErr(e as ApiError); }
    setBusy(false);
  };
  useEffect(() => { void restart(); }, [id]); // eslint-disable-line react-hooks/exhaustive-deps

  const send = async () => {
    if (!reply.trim() || busy) return;
    const next = [...turns, { who: "agent", text: reply } as Turn];
    setTurns(next); setReply(""); setBusy(true); setErr(undefined);
    try { const r = await api.rehearse(id, next); setDone(r.goalReached); if (!r.goalReached) setTurns([...next, r.turn]); setModel(r.model); }
    catch (e) { setErr(e as ApiError); }
    setBusy(false);
  };
  const p = persona.data;
  const rtl = p?.language === "ar";

  return (
    <>
      <PageHeader title="Simulator" sub="The customer side of every conversation." aside={
        <select value={id} onChange={(e) => setId(e.target.value)} className="max-w-xs rounded-full border border-ink/25 bg-white px-4 py-2 text-sm text-ink">
          {scenarios.data?.scenarios.map((s) => <option key={s.id} value={s.id}>{s.id} · {s.title}</option>)}</select>} />
      <Page>
        <Explainer><b>The simulator is the model that plays the customer.</b> It is our instrument, not part of your product. Rehearse a persona here <i>before</i> spending real runs on it. Nothing here touches your agent or a test account.</Explainer>
        <Data res={scenarios}>{(c) => c.scenarios.length === 0 ? <Callout>No scenarios found in the catalogue.</Callout> : (
          <div className="grid gap-5 lg:grid-cols-[3fr_2fr]">
            <Card title="Rehearsal" aside={<Pill tone="pass">no test account used</Pill>}>
              <div className="space-y-3">
                {turns.map((t, i) => (
                  <div key={i} className={cx("max-w-[85%] rounded-2xl px-4 py-3 text-ink", t.who === "customer" ? "bg-blush" : "ml-auto bg-teal-tint")}>
                    <div className="mb-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-ink">{t.who === "customer" ? "Simulated customer" : "You, as the agent"}</div>
                    <div dir={t.who === "customer" && rtl ? "rtl" : undefined}>{t.text}</div>
                  </div>))}
                {busy && <div className="text-sm">The customer is typing…</div>}
                {done && <Callout tone="info">The persona says its goal is reached and would end the chat here.</Callout>}
              </div>
              {err && <div className="mt-4"><ErrorBox error={err} /></div>}
              <div className="mt-5 flex gap-2">
                <input value={reply} onChange={(e) => setReply(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void send()} disabled={!!err && err.status === 503}
                  placeholder="Reply as the agent and see how the persona reacts…" className="min-w-0 flex-1 rounded-full border border-line bg-white px-4 py-2" />
                <CoralButton onClick={() => void send()} disabled={busy || done}>Send</CoralButton>
                <Button onClick={() => void restart()}>Restart</Button>
              </div>
              <p className="mt-3 text-sm">{model ? <>One model call per turn (<Mono>{model}</Mono>).</> : "Scripted persona: no model is called."}</p>
            </Card>
            <div className="space-y-5">
              <Card title="Who it is playing">
                <Data res={persona}>{(x) => !x ? null : (<>
                  <p className="font-semibold text-ink">{x.role}</p>
                  <p className="mt-2 text-sm"><b className="text-ink">Goal:</b> {x.goal}</p>
                  <dl className="mt-4 divide-y divide-line text-sm">
                    {([["Language", x.language], ["Dialect", x.dialect], ["Timezone", x.timezone], ["Typing noise", String(x.typingNoise)], ["Mode", x.scripted ? "scripted" : "LLM"]] as const).map(([k, v]) => (
                      <div key={k} className="flex justify-between py-2"><dt>{k}</dt><dd className="font-bold text-ink">{v ?? <NotMeasured text="not set" />}</dd></div>))}
                  </dl></>)}</Data>
              </Card>
              <Card title="Which models">
                <div className="space-y-2 text-sm">
                  <div className="flex justify-between"><span>Customer (simulator)</span>{p?.simulatorModel ? <Mono>{p.simulatorModel}</Mono> : <NotMeasured text="not configured" />}</div>
                  <div className="flex justify-between"><span>Judge</span>{p?.judgeModel ? <Mono>{p.judgeModel}</Mono> : <NotMeasured text="not configured" />}</div>
                </div>
                <div className="mt-4"><Callout tone="info">The judge should run on a <b>different model family</b> from both the agent and this simulator. Models score their own family too kindly.</Callout></div>
              </Card>
            </div>
          </div>)}</Data>
      </Page>
    </>
  );
}
