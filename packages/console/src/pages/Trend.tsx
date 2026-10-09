import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import ScenarioTrendGrid from "../components/ScenarioTrend";
import { useApi } from "../lib/useApi";
import { Card, Data, Explainer, NotMeasured, Page, PageHeader } from "../components/ui";

export default function Trend() {
  const res = useApi(api.trend);
  const [suite, setSuite] = useState("");
  const perScenario = useApi(() => api.scenarioTrend(suite), [suite]);
  return (
    <>
      <PageHeader title="Trend" sub="Pass rate across judged runs, and each scenario over time." />
      <Page>
        <Explainer>A run that could not be judged is a gap in the chart, never a zero.</Explainer>
        <Data res={res}>{(pts) => (
          <Card title="Pass rate per run">
            {pts.length === 0 ? "No runs yet." : (
              <div className="flex h-60 items-end gap-3 overflow-x-auto border-b border-line px-2">
                {pts.map((p) => (
                  <Link key={p.run} to={`/runs/${p.run}`} className="flex min-w-14 flex-1 flex-col items-center justify-end gap-2" title={p.run}>
                    {p.passRate === null ? <NotMeasured text="n/j" /> : (<>
                      <span className="font-mono text-xs text-ink">{Math.round(p.passRate * 100)}%</span>
                      <div className="w-full max-w-14 rounded-t-xl" style={{ height: `${Math.max(p.passRate * 170, 4)}px`, background: p.passRate === 1 ? "#2da3a1" : p.passRate >= 0.5 ? "#f27655" : "#b8472a" }} /></>)}
                  </Link>))}
              </div>)}
            <div className="mt-2 flex gap-3 overflow-x-auto px-2 text-center text-[11px]">{pts.map((p) => <span key={p.run} className="min-w-14 flex-1">{p.label}</span>)}</div>
          </Card>)}</Data>
        <Card title="Per scenario" aside={perScenario.data && perScenario.data.suites.length > 1 && (
          <select aria-label="Suite" value={suite} onChange={(e) => setSuite(e.target.value)} className="rounded-full border border-ink/25 bg-white px-3 py-1 text-sm text-ink">
            <option value="">All suites</option>
            {perScenario.data.suites.map((x) => <option key={x} value={x}>{x}</option>)}
          </select>)}>
          <p className="mb-3 text-sm">Each scenario&apos;s verdict in the last 20 runs, oldest on the left. <b>n/j</b> is not judged and <b>·</b> means the scenario was not in that run: neither is a failure. <b>Flips</b> counts how often a judged verdict changed within a suite, a sign of a flaky scenario or a regression.</p>
          <Data res={perScenario}>{(d) => <ScenarioTrendGrid data={d} />}</Data>
        </Card>
      </Page>
    </>
  );
}
