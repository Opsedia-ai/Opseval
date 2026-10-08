import { Upload } from "lucide-react";
import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import { Button, Callout, Card, Data, Explainer, Mono, Page, PageHeader, Pill, Table } from "../components/ui";

export default function Scenarios() {
  const res = useApi(api.scenarios);
  return (
    <>
      <PageHeader title="Test cases" sub="One situation each, as YAML in your catalogue." aside={<>
        <Button to="/suites">Suites</Button><Button to="/import"><Upload size={16} />Bulk import</Button></>} />
      <Page>
        <Explainer><b>A test case describes one situation to put the agent in:</b> who the customer is, what they want, how they behave at approval checkpoints and which checks decide whether the agent handled it.</Explainer>
        <Data res={res}>{(c) => (<>
          {c.problems.length > 0 && <Callout><b>{c.problems.length} file(s) failed validation and will not run:</b><ul className="mt-1 list-disc pl-5 text-sm">{c.problems.map((p, i) => <li key={i}><Mono>{p.path}</Mono> {p.field}: {p.message}</li>)}</ul></Callout>}
          <Card className="p-0 [&>div]:p-0">
            <Table head={["Scenario", "Difficulty", "Criticality", "Stops at", "Repeats", "Tags"]}>
              {c.scenarios.map((s) => (
                <tr key={s.id}>
                  <td className="text-ink">{s.title}<div><Mono>{s.id}</Mono></div></td>
                  <td><Pill tone={s.difficulty === "high" ? "fail" : "neutral"}>{s.difficulty}</Pill></td>
                  <td><Pill tone={s.criticality === "normal" ? "mute" : "warn"}>{s.criticality}</Pill></td>
                  <td><Mono>{s.stopAt}</Mono>{s.allowCommit && <> <Pill tone="warn">allow_commit</Pill></>}</td>
                  <td><Mono>{s.repeats}</Mono></td>
                  <td className="space-x-1">{s.tags.map((t) => <Pill key={t}>{t}</Pill>)}</td>
                </tr>))}
            </Table></Card></>)}</Data>
      </Page>
    </>
  );
}
