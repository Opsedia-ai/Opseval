import { Link } from "react-router-dom";
import { GitCompareArrows, Play } from "lucide-react";
import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import { Button, Card, CoralButton, Data, Explainer, GatePill, Mono, NotMeasured, Page, PageHeader, ResultBar, StatePill, Table } from "../components/ui";

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
            <Explainer><b>A run is one execution of a set of scenarios.</b> It either clears the ship gate or it does not. Runs that could not be judged are shown as such, never as failures.</Explainer>
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
                      <td>{r.state === "running" ? <NotMeasured text="in progress" /> : <GatePill gate={r.gate} />}</td>
                      <td><ResultBar passed={r.passed} failed={r.failed} unjudged={r.unjudged} /></td>
                      <td><Mono>{r.passed}</Mono></td><td><Mono>{r.failed}</Mono></td><td><Mono>{r.unjudged}</Mono></td><td><Mono>{r.flaky}</Mono></td>
                    </tr>))}
                </Table>)}
            </Card></>);
        }}</Data>
      </Page>
    </>
  );
}
