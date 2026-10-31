import { useState } from "react";
import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import type { DashQuery } from "../lib/types";
import { Card, Data, Explainer, Page, PageHeader, Stat, Table } from "../components/ui";
import { GapLines, PercentileChart, PercentileStats, ms } from "../components/Charts";
import { DashFilters, DashScope, NO_DASH_FILTERS } from "../components/DashFilters";

export default function LatencyDashboard() {
  const [query, setQuery] = useState<DashQuery>(NO_DASH_FILTERS);
  const res = useApi(() => api.latencyDashboard(query), [JSON.stringify(query)]);
  return (
    <>
      <PageHeader title="Latency" sub="How fast the agent answers, and how long the harness's own calls take." />
      <Page>
        <Explainer>Percentiles are computed on the server, <b>nearest rank</b>: a p95 is a reply time that was measured, never an interpolation. A reply the clocks could not time (a voice barge-in) is <b>not measured</b> and counted apart, never as 0 ms; a day with nothing measured is a gap in the line.</Explainer>
        <Card><DashFilters query={query} options={res.data?.options} onChange={setQuery} /></Card>
        <Data res={res}>{(d) => (<>
          <div className="grid gap-4 md:grid-cols-3">
            <Stat label="Agent reply time" value={<span className="text-lg"><PercentileStats p={d.turnLatency.total} /></span>}
              note={<>from each message sent to the first frame back{d.turnLatency.total.unanswered > 0 && <> · <b data-testid="unanswered">{d.turnLatency.total.unanswered} turn{d.turnLatency.total.unanswered === 1 ? "" : "s"} never answered</b> (a timeout on the agent)</>}</>} />
            <Stat label="Voice turn latency" value={<span className="text-lg"><PercentileStats p={d.voiceTurnLatency.total} /></span>} note="the caller's end of speech to the agent's audio" />
            <Stat label="Time to first audio" value={<span className="text-lg"><PercentileStats p={d.timeToFirstAudio.total} /></span>} note="voice calls only" />
          </div>
          <Card title="Agent reply time over time"><PercentileChart title="Agent reply time" buckets={d.buckets} points={d.turnLatency.points} /></Card>
          <Card title="Time to first audio over time"><PercentileChart title="Time to first audio" buckets={d.buckets} points={d.timeToFirstAudio.points} /></Card>
          <Card title="The harness's LLM and speech calls, per role">
            {d.llmLatency.length === 0 ? <p className="text-sm">No answered calls in these runs.</p> : (<>
              <GapLines title="p95 per role" buckets={d.buckets} fmt={ms}
                lines={d.llmLatency.map((s) => ({ name: s.group, values: s.points.map((p) => p.p95) }))} />
              {d.llmCallsUnattributed > 0 && <p className="mb-2 text-sm" data-testid="unattributed">{d.llmCallsUnattributed} call{d.llmCallsUnattributed === 1 ? " was" : "s were"} made outside any attempt and {d.llmCallsUnattributed === 1 ? "has" : "have"} no language: left out under this filter.</p>}
              <Table head={["Role", "Latency (retries included)"]}>
                {d.llmLatency.map((s) => <tr key={s.group}><td>{s.group}</td><td><PercentileStats p={s.total} /></td></tr>)}
              </Table>
            </>)}
          </Card>
          <Card><DashScope scope={d.scope} options={d.options}> Percentiles: {d.rule}.</DashScope></Card>
        </>)}</Data>
      </Page>
    </>
  );
}
