import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ChevronDown } from "lucide-react";
import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import type { Gate, RunDetail as Detail, TestResult } from "../lib/types";
import { AreaPill, Callout, Card, Data, GatePill, Mono, NotMeasured, Page, PageHeader, Pill, ResultBar, Stat, cx } from "../components/ui";

const tone = (s: TestResult["status"]) => (s === "passed" ? "pass" : s === "failed" ? "fail" : "mute");

function Scenario({ t }: { t: TestResult }) {
  const [open, setOpen] = useState(t.status === "failed");
  return (
    <Card className="p-0 [&>div]:p-0">
      <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-4 p-5 text-left">
        <Pill tone={tone(t.status)} dashed={tone(t.status) === "mute"}>{t.status.replace("_", " ")}</Pill>
        <div className="min-w-0 flex-1">
          <div className="font-semibold text-ink">{t.name}</div>
          <div className="text-sm"><Mono>{t.id}</Mono> · {t.criticality}{t.flaky && " · flaky"}</div>
        </div>
        <div className="hidden items-center gap-1.5 sm:flex" title="attempts">
          {t.attempts.map((a) => <span key={a.index} className={cx("h-3 w-3 rounded-full", a.passed === true ? "bg-teal" : a.passed === false ? "bg-deep-coral" : "border border-dashed border-muted-ink")} />)}
        </div>
        <ChevronDown className={cx("transition", open && "rotate-180")} size={18} />
      </button>
      {open && (
        <div className="border-t border-line bg-soft px-5 py-4">
          {t.reason && <p className="mb-3 text-ink"><b>Why:</b> {t.reason}</p>}
          {t.unmeasuredScorers.length > 0 && <p className="mb-3 text-ink"><b>Not measured:</b> <Mono>{t.unmeasuredScorers.join(", ")}</Mono>. The verdict was reached without them.</p>}
          {t.attempts.length === 0 && <p>Not run, so there are no attempts to show.</p>}
          {t.attempts.map((a) => (
            <div key={a.index} className="mb-4 last:mb-0">
              <div className="mb-1 flex items-center gap-2 text-sm font-semibold text-ink">Attempt {a.index + 1}
                <Pill tone={a.passed === true ? "pass" : a.passed === false ? "fail" : "mute"} dashed={a.passed === null}>{a.passed === null ? "not judged" : a.passed ? "passed" : "failed"}</Pill>
                <span className="font-mono text-xs font-normal">{a.terminalState}</span></div>
              {a.invalidReason && <p className="text-sm">{a.invalidReason}</p>}
              <ul className="divide-y divide-line rounded-2xl border border-line bg-white">
                {a.scores.map((s) => (
                  <li key={s.scorerId} className="flex flex-wrap items-center gap-3 px-4 py-2 text-sm">
                    <AreaPill area={s.axis} /><Mono>{s.scorerId}</Mono>
                    <span className="ml-auto flex items-center gap-2">
                      {s.degraded && <Pill tone="warn">degraded</Pill>}
                      {s.vacuous && <Pill tone="mute" dashed>vacuous</Pill>}
                      {s.status === "infra_error" ? <NotMeasured text="not measured" /> : <><Mono>{s.score?.toFixed(2)}</Mono><Pill tone={s.status === "passed" ? "pass" : "fail"}>{s.status}</Pill></>}
                    </span>
                    {s.reason && <span className="basis-full text-muted-ink">{s.reason}</span>}
                  </li>))}
              </ul>
            </div>))}
        </div>)}
    </Card>
  );
}

const HEADLINE: Record<Gate, string> = {
  safe: "Safe to release.",
  gaps: "No blocking issues, but not everything passed.",
  blocked: "Blocked.",
  not_judged: "Not judged: too little could be judged to clear the gate.",
  harness_error: "Opseval could not carry out this run.",
};

function Caveats({ r }: { r: Detail }) {
  const rows: [string, string[], string][] = [
    ["Not measured", r.unmeasuredScorers, "could not produce a score in at least one judged attempt (harness error). The verdict above was reached without them."],
    ["Degraded evidence", r.degradedScorers, "scored on weaker evidence than they ask for. These scores counted toward the verdict."],
    ["Passed vacuously", r.vacuousScorers, "passed because the situation they check never arose. Those passes are evidence of nothing."],
  ];
  const failedSinks = r.sinks.filter((s) => !s.published);
  return (<>
    {rows.filter(([, ids]) => ids.length > 0).map(([title, ids, text]) => (
      <Callout key={title} tone="warn"><b>{title}:</b> <Mono>{ids.join(", ")}</Mono> {text}</Callout>))}
    {failedSinks.length > 0 && <Callout tone="warn"><b>Not published:</b> {failedSinks.map((s) => `${s.sink} (${s.detail})`).join("; ")}. The verdict is unaffected.</Callout>}
  </>);
}

export default function RunDetail() {
  const { id = "" } = useParams();
  const res = useApi(() => api.run(id), [id]);
  // A run in progress has no detail yet: wait for it. Any other error (an unknown id
  // included) is shown, never waited on for ever.
  const pending = res.error?.code === "RUN_IN_PROGRESS";
  useEffect(() => {
    if (!pending) return;
    const t = setTimeout(res.reload, 2500);
    return () => clearTimeout(t);
  }, [pending, res.reload, res.error]);
  const gate = res.data?.summary.gate ?? null;
  return (
    <>
      <PageHeader title="Run" sub={<Mono>{id}</Mono>} aside={res.data && <GatePill gate={gate} />} />
      <Page>
        {pending ? <Callout>This run is still in progress, or has not started yet. This page refreshes on its own.</Callout> : (
        <Data res={res}>{(r) => (<>
          <Callout tone={r.summary.gate === "safe" ? "info" : "warn"}><b>{r.summary.gate ? HEADLINE[r.summary.gate] : "In progress."}</b> {r.gateSummary}</Callout>
          {r.gatePolicy && <p className="text-sm">Gate policy: <Mono>{r.gatePolicy}</Mono>{r.ignored.length > 0 && <> · ignored by the policy: {r.ignored.join("; ")}</>}</p>}
          {r.looserThanStandard.length > 0 && <Callout tone="warn"><b>This run was gated with a policy looser than standard:</b> <Mono>{r.looserThanStandard.join(", ")}</Mono>. Its result is not comparable with a standard or strict run.</Callout>}
          <Caveats r={r} />
          {r.loadErrors.length > 0 && <Callout><b>Load errors (harness, not the agent):</b><ul className="list-disc pl-5">{r.loadErrors.map((e) => <li key={e}>{e}</li>)}</ul></Callout>}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Stat tone="teal" label="Passed" value={r.summary.passed ?? <NotMeasured text="—" />} />
            <Stat tone="blush" label="Failed" value={r.summary.failed ?? <NotMeasured text="—" />} />
            <Stat label="Not judged" value={r.summary.unjudged ?? <NotMeasured text="—" />} note="harness or precondition, not the agent" />
            <Stat tone="ink" label="Target" value={<span className="text-lg">{r.target || "—"}</span>} note={`profile ${r.profile || "—"}`} />
          </div>
          <ResultBar passed={r.summary.passed} failed={r.summary.failed} unjudged={r.summary.unjudged} />
          <div className="space-y-3">{r.tests.map((t) => <Scenario key={t.id} t={t} />)}</div>
          {r.usage.length > 0 && <Card title="Model usage"><ul className="text-sm">{r.usage.map((u) => <li key={u.role} className="flex justify-between py-1"><span>{u.role} · <Mono>{u.model}</Mono></span><span><Mono>{(u.inputTokens + u.outputTokens).toLocaleString()}</Mono> tokens · {u.costUsd === null ? <NotMeasured /> : <Mono>${u.costUsd.toFixed(3)}</Mono>}</span></li>)}</ul></Card>}
        </>)}</Data>)}
      </Page>
    </>
  );
}
