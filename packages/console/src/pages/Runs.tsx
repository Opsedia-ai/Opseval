import { Link } from "react-router-dom";
import { GitCompareArrows, Play } from "lucide-react";
import { api } from "../lib/api";
import { ProgressLine } from "../components/Progress";
import { useApi } from "../lib/useApi";
import { Button, Card, CoralButton, Count, Data, Explainer, GatePill, Mono, Page, PageHeader, ResultBar, StatePill, Table } from "../components/ui";

export default function Runs() {
  const runs = useApi(api.runs, [], (r) => (r.some((x) => x.state === "running") ? 2500 : false));
  return (
    <>
      <PageHeader title="Runs" sub="Every execution of a suite against your agent." aside={<>
        <Button to="/compare"><GitCompareArrows size={16} />Compare</Button>
        <CoralButton to="/runs/new" className="bg-ink text-white hover:bg-ink-deep"><Play size={16} />New run</CoralButton>
      </>} />
      <Page>
        <Data res={runs}>{(rows) => {
          return (<>
            <Explainer><b>A run is one execution of a set of scenarios.</b> Only a run where everything passed and was measured is <b>safe to release</b>. A passing gate with something failed, skipped or unmeasured is <b>passed with gaps</b>. Runs Opseval could not carry out are harness errors, never failures.</Explainer>
            <Card className="p-0 [&>div]:p-0">
              {rows.length === 0 ? (
                <div className="p-10 text-center">No runs yet. <Link to="/runs/new" className="font-semibold">Start one</Link> or run <Mono>opseval demo</Mono>.</div>
              ) : (
                <Table head={["Run", "Suite", "State", "Ship gate", "Results", "Passed", "Failed", "Not judged", "Flaky"]}>
                  {rows.map((r) => (
                    <tr key={r.id}>
                      <td><Link to={`/runs/${r.id}`} className="font-mono text-[13px] font-medium underline">{r.id}</Link>
                        <div className="text-xs">{r.startedAt ? new Date(r.startedAt).toLocaleString() : ""}</div></td>
                      <td className="text-ink">{r.suite}</td>
                      <td><StatePill state={r.state} /></td>
                      <td title={r.error ?? undefined}><GatePill gate={r.gate} />{r.error && <div className="mt-1 max-w-64 truncate text-xs">{r.error}</div>}</td>
                      <td>{r.progress ? <ProgressLine p={r.progress} /> : <ResultBar passed={r.passed} failed={r.failed} unjudged={r.unjudged} />}</td>
                      <td><Count n={r.passed} /></td><td><Count n={r.failed} /></td><td><Count n={r.unjudged} /></td><td><Count n={r.flaky} /></td>
                    </tr>))}
                </Table>)}
            </Card></>);
        }}</Data>
      </Page>
    </>
  );
}
