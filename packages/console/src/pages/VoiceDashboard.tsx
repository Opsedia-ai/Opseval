import { useState } from "react";
import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import type { DashQuery, VoiceMetric } from "../lib/types";
import { Card, Data, Explainer, Page, PageHeader, Stat, Table } from "../components/ui";
import { PercentileStats, ms, num, type Format } from "../components/Charts";
import { DashFilters, DashScope, NO_DASH_FILTERS } from "../components/DashFilters";

const LABELS: Record<string, string> = {
  "voice.barge_in_yield_ms": "Barge-in yield",
  "voice.talk_over_ratio": "Talk-over",
  "voice.dead_air_ms": "Dead air",
  "voice.asr_confidence": "ASR confidence",
  "voice.agent_asr_wer": "Agent's WER",
};
const fmtFor = (m: VoiceMetric): Format => (m.unit === "ms" ? ms : num);

export default function VoiceDashboard() {
  const [query, setQuery] = useState<DashQuery>(NO_DASH_FILTERS);
  const res = useApi(() => api.voiceDashboard(query), [JSON.stringify(query)]);
  return (
    <>
      <PageHeader title="Voice" sub="How the agent behaves on a call, by language and dialect." />
      <Page>
        <Explainer>From the voice metrics of each call (ADR-0055). A metric the call could not measure is <b>not measured</b> with a reason, counted apart and never as 0; a value measured on partial evidence is counted as <b>degraded</b>.</Explainer>
        <Card><DashFilters query={query} options={res.data?.options} onChange={setQuery} /></Card>
        <Data res={res}>{(d) => (<>
          <Stat label="Voice attempts" value={d.voiceAttempts} note={`of ${d.scope.attempts} attempts in scope`} />
          {d.metrics.map((m) => (
            <Card key={m.name} title={LABELS[m.name] ?? m.name} className="[&>div]:p-0">
              <div className="p-4 text-sm"><PercentileStats p={m.total} fmt={fmtFor(m)} />{m.degraded > 0 && <span className="ml-2">· {m.degraded} degraded</span>}</div>
              <Table head={["Language", "Dialect", "Value", "Degraded"]}>
                {m.byLanguage.map((g) => (
                  <tr key={`${g.language}:${g.dialect}`}><td>{g.language ?? "unknown"}</td><td>{g.dialect ?? "—"}</td><td><PercentileStats p={g.stats} fmt={fmtFor(m)} /></td><td>{g.degraded}</td></tr>))}
              </Table>
            </Card>))}
          <Card><DashScope scope={d.scope} options={d.options}> Percentiles: {d.rule}.</DashScope></Card>
        </>)}</Data>
      </Page>
    </>
  );
}
