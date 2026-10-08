import { api, getToken, setToken } from "../lib/api";
import { useApi } from "../lib/useApi";
import { useState } from "react";
import { Card, CoralButton, Data, Explainer, Mono, NotMeasured, Page, PageHeader, Pill, Table } from "../components/ui";

export default function Settings() {
  const res = useApi(api.config);
  const [token, setLocalToken] = useState(getToken());
  const [saved, setSaved] = useState(false);
  return (
    <>
      <PageHeader title="Settings" sub="Read-only. Configured by OPSEVAL_* environment variables." />
      <Page>
        <Explainer><b>The resolved configuration this service is running with.</b> Secrets are redacted on the server by the same <Mono>config_digest()</Mono> that stamps every run, so what you see is what a run records about itself.</Explainer>
        <Card title="Access token" aside={<span className="text-sm">only when the server was started with <Mono>--token</Mono></span>}>
          <div className="flex gap-3">
            <input type="password" value={token} aria-label="Access token" placeholder="paste the server's --token"
              onChange={(e) => { setLocalToken(e.target.value); setSaved(false); }}
              className="min-w-0 flex-1 rounded-xl border border-line px-3 py-2" />
            <CoralButton className="bg-ink text-white hover:bg-ink-deep" onClick={() => { setToken(token); setSaved(true); }}>Save</CoralButton>
          </div>
          <p className="mt-3 text-sm">{saved ? "Saved. Reload to use it." : "Kept in this browser only and sent as a bearer header on same-origin requests. A loopback server without --token needs nothing here."}</p>
        </Card>
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
