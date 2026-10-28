import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import { Callout, Card, Data, Explainer, Mono, NotMeasured, Page, PageHeader, Table } from "../components/ui";
import { Amount, RunCost } from "../components/UsageFigures";

export default function Usage() {
  const res = useApi(api.usage);
  return (
    <>
      <PageHeader title="Usage & cost" sub="What every conversation costs: simulator, judges and, for voice, speech." />
      <Page>
        <Explainer>Every conversation costs money twice: once for the simulator playing the customer and again for the judges scoring it. A voice run also pays for speech: the customer's lines spoken (TTS) and the agent's voice heard (STT).</Explainer>
        <Callout>A model with no known price shows <NotMeasured /> rather than $0.00. When only some models have a price, the total is what those cost, marked as partial: a floor, never the whole bill.</Callout>
        <Card className="p-0 [&>div]:p-0"><Data res={res}>{(rows) => (
          <Table head={["Run", "Simulator tokens", "Judge tokens", "Audio heard (s)", "Audio spoken (s)", "Characters spoken", "Estimated cost"]}>
            {rows.map((r) => (<tr key={r.run}><td><Mono>{r.run}</Mono></td><td><Amount v={r.simulatorTokens} /></td><td><Amount v={r.judgeTokens} /></td>
              <td><Amount v={r.audioInSeconds} digits={1} /></td><td><Amount v={r.audioOutSeconds} digits={1} /></td><td><Amount v={r.characters} /></td>
              <td><RunCost row={r} /></td></tr>))}
          </Table>)}</Data></Card>
      </Page>
    </>
  );
}
