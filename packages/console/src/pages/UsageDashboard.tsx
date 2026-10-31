import { useState } from "react";
import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import type { DashQuery } from "../lib/types";
import { Card, Data, Explainer, Mono, Page, PageHeader, Table } from "../components/ui";
import { GapLines, MeasuredValue, count, num } from "../components/Charts";
import { DashFilters, DashScope, NO_DASH_FILTERS } from "../components/DashFilters";

export default function UsageDashboard() {
  const [query, setQuery] = useState<DashQuery>(NO_DASH_FILTERS);
  const res = useApi(() => api.usageDashboard(query), [JSON.stringify(query)]);
  return (
    <>
      <PageHeader title="Usage" sub="Runs, attempts and audio minutes, per project and task." />
      <Page>
        <Explainer>Audio minutes are the speech the harness synthesised or transcribed. A run with no speech call has <b>no audio minutes</b>, never 0; a call that did not report its length makes the figure a floor (≥).</Explainer>
        <Card><DashFilters query={query} options={res.data?.options} onChange={setQuery} languages={false} /></Card>
        <Data res={res}>{(d) => (<>
          <Card title="Per project and task" className="[&>div]:p-0">
            <Table head={["Project", "Task", "Runs", "Attempts", "Voice attempts", "Audio minutes"]}>
              {d.byTask.map((t) => (
                <tr key={`${t.project}:${t.task}`}><td>{t.project === null ? "this folder" : <Mono>{t.project}</Mono>}</td><td>{t.task ?? "—"}</td><td>{t.runs}</td><td>{t.attempts}</td><td>{t.voiceAttempts}</td><td><MeasuredValue m={t.audioMinutes} fmt={num} unit="speech calls" /></td></tr>))}
            </Table>
          </Card>
          <Card title="Attempts over time">
            <GapLines title="Attempts" buckets={d.buckets} fmt={count} lines={[{ name: "attempts", values: d.attempts }]} />
          </Card>
          <Card title="Audio minutes over time">
            <GapLines title="Audio minutes" buckets={d.buckets} fmt={num} lines={[{ name: "audio minutes", values: d.audioMinutes.map((m) => m.value) }]}
              gap={(i) => (d.audioMinutes[i].of > 0 ? "not measured" : "no speech")} />
          </Card>
          <Card><DashScope scope={d.scope} options={d.options} /></Card>
        </>)}</Data>
      </Page>
    </>
  );
}
