// Usage figures as the server measured them (ADR-0054). A speech row has no tokens and an LLM
// row no audio: either is drawn as "not measured", never as 0. Nothing is added up here; the
// run totals and whether the cost covers every model come from the server.
import { Mono, NotMeasured, Pill } from "./ui";
import type { RunUsage, Usage } from "../lib/types";

export const Amount = ({ v, digits }: { v: number | null; digits?: number }) =>
  v === null ? <NotMeasured text="—" /> : <Mono>{digits === undefined ? v.toLocaleString() : v.toFixed(digits)}</Mono>;

/** The run's cost: unknown, complete, or a floor (some model had no price). */
export function Cost({ usd, complete, digits = 2 }: { usd: number | null; complete: boolean; digits?: number }) {
  if (usd === null) return <NotMeasured />;
  return (
    <span className="inline-flex items-center gap-2">
      <Mono>{complete ? "" : "≥ "}${usd.toFixed(digits)}</Mono>
      {!complete && <Pill tone="mute" dashed>partial: a model has no price</Pill>}
    </span>
  );
}

export const RunCost = ({ row }: { row: RunUsage }) => <Cost usd={row.costUsd} complete={row.costComplete} />;

/** One role's line in a run's usage: tokens for an LLM, audio and characters for speech. */
export function UsageLine({ u }: { u: Usage }) {
  const parts: [string, number | null, number | undefined][] = [
    ["tokens in", u.inputTokens, undefined],
    ["tokens out", u.outputTokens, undefined],
    ["reasoning", u.reasoningTokens, undefined],
    ["cached in", u.cachedInputTokens, undefined],
    ["s heard", u.audioInSeconds, 1],
    ["s spoken", u.audioOutSeconds, 1],
    ["characters", u.characters, undefined],
  ];
  const shown = parts.filter(([, v]) => v !== null);
  return (
    <span className="flex flex-wrap justify-end gap-x-2">
      {shown.length === 0 ? <NotMeasured /> : shown.map(([label, v, d]) => <span key={label}><Amount v={v} digits={d} /> {label}</span>)}
      <span>· {u.costUsd === null ? <NotMeasured /> : <Mono>${u.costUsd.toFixed(3)}</Mono>}</span>
    </span>
  );
}
