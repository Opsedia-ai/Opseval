import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import type { Check } from "../lib/types";
import { AreaPill, Card, Data, Explainer, Mono, Page, PageHeader, Pill, Table } from "../components/ui";

const section = (title: string, rows: Check[]) => (
  <Card title={<>{title} <span className="font-sans text-sm font-normal">{rows.length}</span></>} className="p-0 [&>div:last-child]:p-0">
    <Table head={["Check", "What it asks", "Area", "Reads"]}>
      {rows.map((c) => <tr key={c.id}><td><Mono>{c.id}</Mono></td><td>{c.question}</td><td><AreaPill area={c.area} /></td><td><Pill>{c.reads}</Pill></td></tr>)}
    </Table>
  </Card>
);

export default function Checks() {
  const res = useApi(api.checks);
  return (
    <>
      <PageHeader title="Checks & judges" sub={res.data && `${res.data.length} checks · ${res.data.filter((c) => c.kind === "judge").length} use an LLM`} />
      <Page>
        <Explainer><b>A check asks one question about one conversation and answers 0 to 1, with evidence.</b> A conversation passes only if <i>all</i> its checks met their threshold.</Explainer>
        <div className="grid gap-5 md:grid-cols-2">
          <Card tone="blush"><h3 className="mb-1 text-lg">Deterministic checks</h3>Plain code reading the recorded messages and tool calls. No model, and the answer is identical every time.</Card>
          <Card tone="blush"><h3 className="mb-1 text-lg">LLM judges</h3>For questions code cannot answer. A model fills a fixed scorecard and quotes its evidence. <b>Our code does the arithmetic.</b></Card>
        </div>
        <Data res={res}>{(c) => <>{section("LLM judges", c.filter((x) => x.kind === "judge"))}{section("Deterministic checks", c.filter((x) => x.kind === "deterministic"))}</>}</Data>
      </Page>
    </>
  );
}
