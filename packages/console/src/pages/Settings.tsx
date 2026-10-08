import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import { Card, Data, Explainer, Mono, NotMeasured, Page, PageHeader, Pill, Table } from "../components/ui";

export default function Settings() {
  const res = useApi(api.config);
  return (
    <>
      <PageHeader title="Settings" sub="Read-only. Configured by OPSEVAL_* environment variables." />
      <Page>
        <Explainer><b>The resolved configuration this service is running with.</b> Secrets are redacted on the server by the same <Mono>config_digest()</Mono> that stamps every run, so what you see is what a run records about itself.</Explainer>
        <Card title="Resolved configuration" aside={res.data && <span className="space-x-2"><Pill>{res.data.length} settings</Pill><Pill tone="warn">{res.data.filter((r) => r.redacted).length} redacted</Pill></span>} className="p-0 [&>div:last-child]:p-0">
          <Data res={res}>{(rows) => (
            <Table head={["Setting", "Value"]}>{rows.map((r) => (
              <tr key={r.key}><td><Mono>{r.key}</Mono></td>
                <td>{r.redacted ? <Pill tone="warn">redacted</Pill> : r.value === null ? <NotMeasured text="not set" /> : <Mono>{r.value}</Mono>}</td></tr>))}</Table>)}</Data>
        </Card>
      </Page>
    </>
  );
}
