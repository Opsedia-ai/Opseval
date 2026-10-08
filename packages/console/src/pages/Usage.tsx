import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import { Callout, Card, Data, Explainer, Mono, NotMeasured, Page, PageHeader, Table } from "../components/ui";

const n = (v: number | null) => (v === null ? <NotMeasured /> : <Mono>{v.toLocaleString()}</Mono>);

export default function Usage() {
  const res = useApi(api.usage);
  return (
    <>
      <PageHeader title="Usage & cost" sub="What every conversation costs: simulator plus judges." />
      <Page>
        <Explainer>Every conversation costs money twice: once for the simulator playing the customer and again for the judges scoring it.</Explainer>
        <Callout>A model with no known price shows <NotMeasured /> rather than $0.00, so a total that covers unpriced models is a floor, and says so.</Callout>
        <Card className="p-0 [&>div]:p-0"><Data res={res}>{(rows) => (
          <Table head={["Run", "Simulator tokens", "Judge tokens", "Estimated cost"]}>
            {rows.map((r) => (<tr key={r.run}><td><Mono>{r.run}</Mono></td><td>{n(r.simulatorTokens)}</td><td>{n(r.judgeTokens)}</td>
              <td>{r.costUsd === null ? <NotMeasured /> : <Mono>${r.costUsd.toFixed(2)}</Mono>}</td></tr>))}
          </Table>)}</Data></Card>
      </Page>
    </>
  );
}
