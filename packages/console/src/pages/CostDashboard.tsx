import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import type { UsageQuery } from "../lib/types";
import { Card, Data, Explainer, Mono, Page, PageHeader, Stat, Table } from "../components/ui";
import { MeasuredValue, RatioValue, StackedBars, usd } from "../components/Charts";
import { NO_FILTERS, ScopeNote, UsageFilters } from "../components/UsageFilters";

export default function CostDashboard() {
  const [query, setQuery] = useState<UsageQuery>(NO_FILTERS);
  const res = useApi(() => api.costDashboard(query), [JSON.stringify(query)]);
  const [by, setBy] = useState<"role" | "model">("role");
  return (
    <>
      <PageHeader title="Cost" sub="What evaluation spends: simulator, judges and speech, per role and model." />
      <Page>
        <Explainer>Each call is priced with the price in force on the day it was made, so an old run keeps its cost. A model with no price is <b>not measured</b>, never $0, and a total that misses some calls is a <b>floor</b> (≥), hatched in the chart. This is the harness&apos;s own spend, never the agent&apos;s.</Explainer>
        <Card><UsageFilters query={query} options={res.data?.options} onChange={setQuery} /></Card>
        <Data res={res}>{(d) => (<>
          <div className="grid gap-4 md:grid-cols-3">
            <Stat label="Total spend" value={<MeasuredValue m={d.totalUsd} fmt={usd} />} />
            <Stat label="Per passed test" value={<RatioValue r={d.costPerPassedTest} fmt={usd} unit="passed tests" />} note={`${d.passedTests} passed test${d.passedTests === 1 ? "" : "s"}; a passed test with no per-call usage is left out of the division`} />
            <Stat label="Priced from" value={<span className="text-2xl">{Object.entries(d.sources).map(([k, n]) => `${n} ${k.replace("_", " ")}`).join(" · ") || "—"}</span>} />
          </div>
          <Card title="Spend over time" aside={
            <select aria-label="Group by" value={by} onChange={(e) => setBy(e.target.value as "role" | "model")} className="rounded-full border border-ink/25 bg-white px-3 py-1 text-sm text-ink">
              <option value="role">by role</option><option value="model">by model</option>
            </select>}>
            <StackedBars title={`Spend by ${by}`} buckets={d.buckets} fmt={usd}
              series={by === "role" ? d.byRole : d.byModel} order={by === "role" ? d.options.roles : d.options.models} />
          </Card>
          <Card title="Per run" className="[&>div]:p-0">
            <Table head={["Run", "Suite", "Cost", "Passed", "Per passed test"]}>
              {d.runs.map((r) => (
                <tr key={r.run}><td><Link to={`/runs/${r.run}`}><Mono>{r.run}</Mono></Link>{!r.perCall && <span className="ml-2 text-xs">(before per-call usage)</span>}</td>
                  <td>{r.suite}</td><td><MeasuredValue m={r.costUsd} fmt={usd} /></td><td>{r.passedTests}</td><td><RatioValue r={r.costPerPassedTest} fmt={usd} unit="passed" /></td></tr>))}
            </Table>
          </Card>
          <Card title="Per scenario" className="[&>div]:p-0">
            <Table head={["Scenario", "Attempts", "Cost", "Per attempt", "Per passed test"]}>
              {d.scenarios.map((s) => (
                <tr key={s.scenario}><td><Mono>{s.scenario}</Mono></td>
                  <td>{s.attemptsMeasured === s.attempts ? s.attempts : `${s.attemptsMeasured} of ${s.attempts} measured`}</td>
                  <td><MeasuredValue m={s.costUsd} fmt={usd} /></td><td><RatioValue r={s.costPerAttempt} fmt={usd} unit="attempts" /></td><td><RatioValue r={s.costPerPassedTest} fmt={usd} unit="passed runs" /></td></tr>))}
            </Table>
          </Card>
          <Card><ScopeNote scope={d.scope} serverOnly={d.options.serverOnly}> Budgets against actual come with tasks (server mode).</ScopeNote></Card>
        </>)}</Data>
      </Page>
    </>
  );
}
