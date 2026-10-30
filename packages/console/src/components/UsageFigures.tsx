// Usage figures as the server measured them (ADR-0054). A speech row has no tokens and an LLM
// row no audio: either is drawn as "not measured", never as 0. Nothing is added up here; the
// run totals and whether the cost covers every model come from the server.
import { Mono, NotMeasured, Pill } from "./ui";
import type { CostSplit, RunUsage, Usage } from "../lib/types";

export const Amount = ({ v, digits }: { v: number | null; digits?: number }) =>
  v === null ? <NotMeasured text="—" /> : <Mono>{digits === undefined ? v.toLocaleString() : v.toFixed(digits)}</Mono>;

/** A token total: unknown, complete, or a floor ("≥": a model's calls did not all report it). */
export function Tokens({ v, complete }: { v: number | null; complete: boolean }) {
  if (v === null) return <NotMeasured text="—" />;
  return <span data-partial={complete ? undefined : "true"} title={complete ? undefined : "partial: a model did not report its tokens"}><Mono>{complete ? "" : "≥ "}{v.toLocaleString()}</Mono></span>;
}

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
      <span>· <RowCost u={u} /></span>
    </span>
  );
}

/** A row's cost: the total when every call had a price, else the known part and the calls with
 * no price (ADR-0066 D2), never one figure that pretends to be complete. */
export function RowCost({ u }: { u: Usage }) {
  if (u.costUsd !== null) return <Mono>${u.costUsd.toFixed(3)}</Mono>;
  const unpriced = u.unpricedCalls ? ` · ${u.unpricedCalls} call${u.unpricedCalls === 1 ? "" : "s"} with no price` : "";
  if (u.costKnownUsd === null || u.costKnownUsd === undefined) return <NotMeasured text={u.unpricedCalls ? `no price${unpriced}` : "not measured"} />;
  return <span data-partial="true"><Mono>≥ ${u.costKnownUsd.toFixed(3)}</Mono><span className="text-xs text-muted-ink">{unpriced}</span></span>;
}

/** What the run cost: the same split as the dashboards (the server's), named partial when any
 * call had no price. */
export function CostSplitLine({ c }: { c: CostSplit }) {
  if (!c.recorded) return <span className="text-sm"><NotMeasured text="cost split not recorded" /> ({c.pricedCalls} priced, {c.unpricedCalls} not)</span>;
  if (c.totalUsd !== null) return <span className="text-sm">Cost <Mono>${c.totalUsd.toFixed(4)}</Mono></span>;
  if (c.knownUsd === null) return <span className="text-sm" data-testid="cost-split"><NotMeasured text="no price" /> · {c.unpricedCalls} calls with no price</span>;
  return (
    <span className="text-sm" data-partial="true" data-testid="cost-split">
      Cost <Mono>≥ ${c.knownUsd.toFixed(4)}</Mono> known ({c.pricedCalls} calls) · {c.unpricedCalls} call{c.unpricedCalls === 1 ? "" : "s"} with no price
    </span>
  );
}
