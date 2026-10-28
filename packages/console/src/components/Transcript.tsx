import { api } from "../lib/api";
import type { TranscriptEntry } from "../lib/types";
import { useApi } from "../lib/useApi";
import { CallAudio, TurnAudio, type Where } from "./AudioPlayer";
import { Callout, Data, Mono, NotMeasured, Pill, cx } from "./ui";

export function Entry({ e, where }: { e: TranscriptEntry; where?: Where }) {
  if (e.kind === "message") {
    const customer = e.who === "customer";
    return (
      <div className={cx("flex", customer ? "justify-start" : "justify-end")}>
        <div className={cx("max-w-[80%] rounded-2xl px-4 py-2", customer ? "bg-soft text-ink" : "bg-ink text-ink-text")}>
          <div className={cx("mb-0.5 text-xs font-semibold uppercase tracking-[0.08em]", customer ? "text-muted-ink" : "text-white/70")}>
            {customer ? "customer" : "agent"}{e.phase && ` · ${e.phase}`}{e.latencyMs !== null && ` · ${Math.round(e.latencyMs)} ms`}
          </div>
          {/* dir=auto: an Arabic persona's turns read right to left */}
          {e.text
            ? <div dir="auto" className="whitespace-pre-wrap">{e.text}</div>
            : <div data-state="unheard" className="italic opacity-80">spoken; nothing was heard in it (no transcript)</div>}
          {e.audio && where && <div className="mt-1"><TurnAudio where={where} audio={e.audio} /></div>}
        </div>
      </div>
    );
  }
  const label = { checkpoint: "⏸ checkpoint raised", decision: "▶ customer decision", result: "result", error: "error from the agent", notification: "notification", unknown: "unclassified frame" }[e.kind];
  return (
    <div data-kind={e.kind} data-tone={e.kind === "error" ? "fail" : "mute"} className={cx("rounded-xl border border-dashed px-3 py-1.5 text-sm", e.kind === "error" ? "border-deep-coral text-deep-coral" : "border-line text-muted-ink")}>
      {label}{e.detail && <>: <Mono>{e.detail}</Mono></>}
    </div>
  );
}

/** The conversation behind one attempt, fetched only when opened. What was said, not how it scored. */
export default function Transcript({ run, test, index }: { run: string; test: string; index: number }) {
  const rec = useApi(() => api.attempt(run, test, index), [run, test, index]);
  return (
    <Data res={rec}>{(r) => (
      <div className="mt-3 space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-ink">
          <span>{r.userTurns} customer turn(s)</span>
          {r.language && <Pill>{r.language}{r.dialect ? ` / ${r.dialect}` : ""}</Pill>}
          <Pill tone={r.traceCoverage === "full" ? "pass" : "mute"} dashed={r.traceCoverage !== "full"}>{r.traceCoverage === "full" ? "trace-backed" : "frames only"}</Pill>
          <span>ended: <Mono>{r.terminalState}</Mono>{r.stopReason && ` (${r.stopReason})`}</span>
        </div>
        {!r.redactionChecked
          ? <Callout><b>Credential redaction was not checked on this record</b>: it was written before Opseval removed credential values. It may contain one.</Callout>
          : r.redactedKinds.length > 0 && <Callout tone="info"><b>Credentials removed from this record:</b> <Mono>{r.redactedKinds.join(", ")}</Mono>. The values are never kept; where one was, the text says so.</Callout>}
        {r.plantedInjections.length > 0 && <p className="text-sm"><b>Planted injections:</b> <Mono>{r.plantedInjections.join(", ")}</Mono></p>}
        {r.artifactsChecked && <p className="text-sm" data-state="created-artifacts"><b>Created in the system under test:</b>{" "}
          {r.createdArtifacts.length === 0 ? "nothing" : r.createdArtifacts.map((a, i) => (
            <span key={i}>{i > 0 && ", "}<Mono>{a.kind}</Mono> {a.idRedacted ? <i>(id redacted)</i> : <><Mono>{a.id}</Mono>{a.idTruncated && <i> (truncated)</i>}</>}</span>))}</p>}
        {r.audio && <CallAudio where={{ run, test, index }} audio={r.audio} />}
        <div className="space-y-2 rounded-2xl border border-line bg-white p-4">
          {r.entries.length === 0 ? <NotMeasured text="no conversation recorded" /> : r.entries.map((e) => <Entry key={`${e.seq}-${e.kind}`} e={e} where={{ run, test, index }} />)}
        </div>
        {r.toolCalls.length > 0 && (
          <div><div className="mb-1 text-sm font-semibold text-ink">Tool calls (from the trace)</div>
            <ul className="space-y-1 text-sm">{r.toolCalls.map((c, i) => (
              <li key={i} className="flex flex-wrap items-center gap-2"><Mono>{c.name}</Mono><Pill tone={c.ok ? "pass" : "fail"}>{c.ok ? "ok" : "error"}</Pill>
                {c.arguments && <code className="basis-full overflow-x-auto rounded-lg bg-soft px-2 py-1 text-xs">{c.arguments}</code>}</li>))}</ul></div>)}
        {r.infraErrors.length > 0 && <Callout><b>Harness errors (not the agent):</b><ul className="list-disc pl-5">{r.infraErrors.map((e) => <li key={e}>{e}</li>)}</ul></Callout>}
        {r.probes.length > 0 && <p className="text-sm"><b>Protocol probes:</b> {r.probes.join("; ")}</p>}
        {r.connection.length > 0 && <p className="text-sm"><b>Connection:</b> {r.connection.join(" → ")}</p>}
      </div>)}</Data>
  );
}
