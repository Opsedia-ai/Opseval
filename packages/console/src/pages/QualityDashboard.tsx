import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import type { DashQuery } from "../lib/types";
import { Card, Data, Explainer, GatePill, Mono, NotMeasured, Page, PageHeader, Stat, Table } from "../components/ui";
import { GapLines, RateValue, num, pct } from "../components/Charts";
import { DashFilters, DashScope, NO_DASH_FILTERS } from "../components/DashFilters";
import ScenarioTrendGrid from "../components/ScenarioTrend";

export default function QualityDashboard() {
  const [query, setQuery] = useState<DashQuery>(NO_DASH_FILTERS);
  const res = useApi(() => api.qualityDashboard(query), [JSON.stringify(query)]);
  return (
    <>
      <PageHeader title="Quality" sub="Pass rate, gates, flaky scenarios and every check over time." />
      <Page>
        <Explainer>A pass rate counts <b>judged</b> verdicts only: an outage, a skip or a check that could not run is counted apart, never as a failure, so a harness problem never shows as the agent&apos;s bad week, and a period with nothing judged is a gap, never 0%.</Explainer>
        <Card><DashFilters query={query} options={res.data?.options} onChange={setQuery} /></Card>
        <Data res={res}>{(d) => (<>
          <div className="grid gap-4 md:grid-cols-3">
            <Stat label="Pass rate" value={<span className="text-2xl"><RateValue r={d.passRateTotal} /></span>} />
            <Stat label="Flaky scenarios" value={d.flaky.length} note="marked flaky, or a judged verdict changed within a suite" />
            <Stat label="Judge agreement" value={d.judgeAgreement === null ? <NotMeasured /> : "—"} note={d.judgeAgreementNote} />
          </div>
          <Card title="Pass rate over time">
            <GapLines title="Pass rate" buckets={d.buckets} fmt={pct} yMax={1}
              gap={(i) => (d.passRate[i].notJudged > 0 ? "nothing judged" : "no data")}
              lines={[{ name: "pass rate", values: d.passRate.map((r) => r.value) }]} />
          </Card>
          <Card title="Gate per run">
            {d.gateHistory.length === 0 ? <p className="text-sm">No finished runs match these filters.</p> : (
              <ul className="flex flex-wrap gap-2">{d.gateHistory.map((g) => (
                <li key={g.run}><Link to={`/runs/${g.run}`} title={`${g.run} · ${g.suite}`}><GatePill gate={g.gate} /></Link></li>))}
              </ul>)}
          </Card>
          <Card title="Flaky scenarios" className="[&>div]:p-0">
            <Table head={["Scenario", "Judged runs", "Marked flaky", "Flips"]}>
              {d.flaky.map((f) => <tr key={f.scenario}><td><Mono>{f.scenario}</Mono> {f.name}</td><td>{f.judgedRuns}</td><td>{f.flakyRuns}</td><td>{f.flips}</td></tr>)}
            </Table>
          </Card>
          <Card title="Every check" className="[&>div]:p-0">
            <Table head={["Check", "Area", "Pass rate", "Mean score"]}>
              {d.checks.map((c) => <tr key={`${c.scorerId}:${c.axis}`}><td><Mono>{c.scorerId}</Mono></td><td>{c.axis}</td><td><RateValue r={c.passRate} /></td><td>{c.meanScore === null ? <NotMeasured /> : num(c.meanScore)}</td></tr>)}
            </Table>
          </Card>
          <Card title="Scenarios by run"><ScenarioTrendGrid data={{ suites: [], runs: d.matrix.runs, scenarios: d.matrix.scenarios }} /></Card>
          <Card><DashScope scope={d.scope} options={d.options} /></Card>
        </>)}</Data>
      </Page>
    </>
  );
}
