import type { Problem } from "../lib/types";
import { Mono, Pill } from "./ui";

/**
 * What a valid scenario will not tell you (FR-AUT). Never an error, and saving is never
 * blocked by one: a warning only says the run will measure less than the file looks like.
 */
export function Warnings({ items }: { items: Problem[] }) {
  if (items.length === 0) return null;
  return (
    <div data-state="warnings" className="mt-4 border-t border-line pt-3">
      <div className="mb-2 flex items-center gap-2"><Pill tone="warn">{items.length} warning(s)</Pill><span className="text-xs text-muted-ink">valid, but it will measure less than it looks</span></div>
      <ul className="space-y-2 text-sm">{items.map((w, i) => (
        <li key={i} className="text-ink">{w.field && <Mono>{w.field}</Mono>} {w.message}</li>))}</ul>
    </div>);
}

/**
 * What a `dataset:` or `matrix:` scenario expands to (ADR-0060), exactly as the loader said:
 * the browser expands nothing. Each id is a scenario of its own in a run and in the gate.
 */
export function Expansion({ ids }: { ids: string[] }) {
  if (ids.length === 0) return null;
  return (
    <div data-state="expansion" className="mt-4 border-t border-line pt-3">
      <div className="mb-2 flex items-center gap-2"><Pill tone="neutral">expands to {ids.length} scenario(s)</Pill><span className="text-xs text-muted-ink">one per dataset row and matrix cell; each is judged on its own</span></div>
      <ul className="max-h-40 space-y-1 overflow-auto text-sm">{ids.map((id) => <li key={id}><Mono>{id}</Mono></li>)}</ul>
    </div>);
}
