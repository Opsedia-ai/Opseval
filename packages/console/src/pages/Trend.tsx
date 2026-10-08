import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useApi } from "../lib/useApi";
import { Card, Data, Explainer, NotMeasured, Page, PageHeader } from "../components/ui";

export default function Trend() {
  const res = useApi(api.trend);
  return (
    <>
      <PageHeader title="Trend" sub="Pass rate across judged runs." />
      <Page>
        <Explainer>A run that could not be judged is a gap in the chart, never a zero.</Explainer>
        <Data res={res}>{(pts) => (
          <Card title="Pass rate per run">
            {pts.length === 0 ? "No runs yet." : (
              <div className="flex h-60 items-end gap-3 overflow-x-auto border-b border-line px-2">
                {pts.map((p) => (
                  <Link key={p.run} to={`/runs/${p.run}`} className="flex min-w-14 flex-1 flex-col items-center justify-end gap-2" title={p.run}>
                    {p.passRate === null ? <NotMeasured text="n/j" /> : (<>
                      <span className="font-mono text-xs text-ink">{Math.round(p.passRate * 100)}%</span>
                      <div className="w-full max-w-14 rounded-t-xl" style={{ height: `${Math.max(p.passRate * 170, 4)}px`, background: p.passRate === 1 ? "#2da3a1" : p.passRate >= 0.5 ? "#f27655" : "#b8472a" }} /></>)}
                  </Link>))}
              </div>)}
            <div className="mt-2 flex gap-3 overflow-x-auto px-2 text-center text-[11px]">{pts.map((p) => <span key={p.run} className="min-w-14 flex-1">{p.label}</span>)}</div>
          </Card>)}</Data>
      </Page>
    </>
  );
}
