import { useState } from "react";
import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import type { Ratio, UsageQuery } from "../lib/types";
import { Card, Data, Explainer, Mono, NotMeasured, Page, PageHeader, Stat, Table } from "../components/ui";
import { MeasuredValue, StackedBars, count } from "../components/Charts";
import { NO_FILTERS, ScopeNote, UsageFilters } from "../components/UsageFilters";

const KINDS = ["input", "cached_input", "output", "reasoning"];
const KIND_LABELS: Record<string, string> = { input: "input (not cached)", cached_input: "cached input", output: "output (not reasoning)", reasoning: "reasoning" };

export function RatioValue({ r, percent, unit }: { r: Ratio; percent?: boolean; unit: string }) {
  if (r.value === null) return <NotMeasured />;
  return <span>{percent ? `${Math.round(r.value * 100)}%` : count(r.value)}<span className="ml-1 text-xs font-normal text-muted-ink">({r.measured} of {r.of} {unit})</span></span>;
}

export default function TokensDashboard() {
  const [query, setQuery] = useState<UsageQuery>(NO_FILTERS);
  const res = useApi(() => api.tokensDashboard(query), [JSON.stringify(query)]);
  const [by, setBy] = useState<"kind" | "model">("kind");
  return (
    <>
      <PageHeader title="Tokens" sub="Input, output, reasoning and cached tokens the harness used." />
      <Page>
        <Explainer>Counts are what the provider reported. A count it did not report is <b>not measured</b>, never 0: reasoning and cached tokens are often missing. The stack splits input into cached and not cached, and output into reasoning and the rest, so the segments add up to every token counted.</Explainer>
        <Card><UsageFilters query={query} options={res.data?.options} onChange={setQuery} /></Card>
        <Data res={res}>{(d) => (<>
          <div className="grid gap-4 md:grid-cols-4">
            <Stat label="Tokens" value={<MeasuredValue m={d.totals.total} fmt={count} />} />
            <Stat label="Reasoning tokens" value={<MeasuredValue m={d.totals.reasoning} fmt={count} />} />
            <Stat label="Reasoning share of output" value={<RatioValue r={d.reasoningShare} percent unit="calls" />} />
            <Stat label="Tokens per attempt" value={<RatioValue r={d.tokensPerAttempt} unit="attempts" />} />
          </div>
          <Card title="Tokens over time" aside={
            <select aria-label="Group by" value={by} onChange={(e) => setBy(e.target.value as "kind" | "model")} className="rounded-full border border-ink/25 bg-white px-3 py-1 text-sm text-ink">
              <option value="kind">by kind</option><option value="model">by model</option>
            </select>}>
            {by === "kind"
              ? <StackedBars title="Tokens by kind" buckets={d.buckets} fmt={count} order={KINDS.map((k) => KIND_LABELS[k])}
                  series={d.byKind.map((s) => ({ ...s, group: KIND_LABELS[s.group] ?? s.group }))} />
              : <StackedBars title="Tokens by model" buckets={d.buckets} fmt={count} series={d.byModel} order={d.options.models} />}
          </Card>
          <Card title="Totals" className="[&>div]:p-0">
            <Table head={["Input (cached included)", "Cached input", "Output (reasoning included)", "Reasoning"]}>
              <tr>{[d.totals.input, d.totals.cachedInput, d.totals.output, d.totals.reasoning].map((m, i) => <td key={i}><MeasuredValue m={m} fmt={count} /></td>)}</tr>
            </Table>
          </Card>
          <Card title="Per scenario" className="[&>div]:p-0">
            <Table head={["Scenario", "Attempts", "Tokens", "Per attempt"]}>
              {d.scenarios.map((s) => (
                <tr key={s.scenario}><td><Mono>{s.scenario}</Mono></td>
                  <td>{s.attemptsMeasured === s.attempts ? s.attempts : `${s.attemptsMeasured} of ${s.attempts} measured`}</td>
                  <td><MeasuredValue m={s.tokens} fmt={count} /></td>
                  <td>{s.tokensPerAttempt === null ? <NotMeasured /> : <Mono>{count(s.tokensPerAttempt)}</Mono>}</td></tr>))}
            </Table>
          </Card>
          <Card><ScopeNote scope={d.scope} serverOnly={d.options.serverOnly} /></Card>
        </>)}</Data>
      </Page>
    </>
  );
}
