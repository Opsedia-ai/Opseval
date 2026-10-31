// Dashboard charts (ADR-0066), drawn in-house as SVG: no chart library to vet or ship.
// Every number comes from the server; this file only places marks. Two rules it enforces:
// - a null is a GAP, never a 0 on the axis: a segment with no value is not drawn, and a bucket
//   with nothing measured shows "no data" instead of a bar;
// - a partial value (complete: false) is a floor: drawn hatched, its tooltip says "≥" and how
//   many calls it covers. A bucket where any series was not measured (null with calls to count)
//   is a floor as a whole: every segment of it is hatched and said "partial".
import { Fragment, useState } from "react";
import type { Measured, Percentiles, Rate, Ratio, UsageBucket, UsageSeries } from "../lib/types";
import { NotMeasured } from "./ui";

/** The reference categorical order (dataviz skill palette.md, light surface), never cycled. */
export const SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
const OTHER = "#8a8f8f";

/** A group's colour follows the group, not its rank: its place in the server's full option list. */
export function colorFor(group: string, order: string[]): string {
  const i = order.indexOf(group);
  return i >= 0 && i < SERIES_COLORS.length ? SERIES_COLORS[i] : OTHER;
}

export type Format = (v: number) => string;
export const usd: Format = (v) => (v < 0.01 && v > 0 ? `$${v.toPrecision(2)}` : `$${v.toFixed(2)}`);
export const count: Format = (v) => Math.round(v).toLocaleString();

/** How a measured value reads in text: "not measured", "no calls", "≥ $1.20 (3 of 4 calls)". */
export function describe(m: Measured, fmt: Format, unit = "calls"): string {
  if (m.value === null) return m.of === 0 ? `no ${unit}` : `not measured (0 of ${m.of} ${unit} reported)`;
  return m.complete ? fmt(m.value) : `≥ ${fmt(m.value)} (${m.measured} of ${m.of} ${unit})`;
}

/** A measured figure: the value, "partial" when it is a floor, "not measured" when null. */
export function MeasuredValue({ m, fmt, unit = "calls" }: { m: Measured; fmt: Format; unit?: string }) {
  if (m.value === null) return <NotMeasured text={m.of === 0 ? `no ${unit}` : "not measured"} />;
  return (
    <span data-partial={m.complete ? undefined : "true"} title={describe(m, fmt, unit)}>
      {m.complete ? "" : "≥ "}{fmt(m.value)}
      {!m.complete && <span className="ml-1 text-xs font-normal text-muted-ink">({m.measured} of {m.of} {unit})</span>}
    </span>
  );
}

/** A ratio: "≥" when its numerator was a floor, its coverage when not every unit was measured. */
export function RatioValue({ r, fmt, unit, percent }: { r: Ratio; fmt: Format; unit: string; percent?: boolean }) {
  if (r.value === null) return <NotMeasured text={r.of === 0 ? `no ${unit}` : "not measured"} />;
  const shown = percent ? `${Math.round(r.value * 100)}%` : fmt(r.value);
  return (
    <span data-partial={r.complete ? undefined : "true"}>
      {r.complete ? "" : "≥ "}{shown}
      {(r.measured < r.of || !r.complete) && <span className="ml-1 text-xs font-normal text-muted-ink">({r.measured} of {r.of} {unit})</span>}
    </span>
  );
}

/** The groups a bucket is missing: not measured although they had calls to count. */
export function missingIn(series: UsageSeries[], i: number): string[] {
  return series.filter((s) => s.points[i].value === null && s.points[i].of > 0).map((s) => s.group);
}

const partialNote = (missing: string[]) => (missing.length ? ` · partial: ${missing.join(", ")} not measured` : "");

const W = 720, H = 240, PAD_L = 64, PAD_B = 28, PAD_T = 12;

/** Stacked bars, one per bucket. */
export function StackedBars({ buckets, series, order, fmt, title }: {
  buckets: UsageBucket[]; series: UsageSeries[]; order: string[]; fmt: Format; title: string;
}) {
  const [hover, setHover] = useState<string | null>(null);
  if (buckets.length === 0) return <p className="text-sm">No finished runs match these filters.</p>;
  // Layout only: how tall each bucket's stack is, from the server's values.
  const heights = buckets.map((_, i) => series.reduce((sum, s) => sum + (s.points[i].value ?? 0), 0));
  const max = Math.max(...heights) || 1;
  const plotW = W - PAD_L - 8, plotH = H - PAD_T - PAD_B;
  const slot = plotW / buckets.length;
  const bar = Math.min(48, slot * 0.6);
  const y = (v: number) => PAD_T + plotH - (v / max) * plotH;
  const ticks = [0, max / 2, max];
  return (
    <figure className="m-0">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title} className="h-auto w-full">
        <defs>
          <pattern id="partial" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="6" height="6" fill="white" fillOpacity="0.0" />
            <line x1="0" y1="0" x2="0" y2="6" stroke="white" strokeWidth="2.5" strokeOpacity="0.7" />
          </pattern>
        </defs>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD_L} x2={W - 8} y1={y(t)} y2={y(t)} stroke="#f3d5ca" strokeWidth="1" />
            <text x={PAD_L - 6} y={y(t) + 4} textAnchor="end" fontSize="11" fill="#3d5556">{fmt(t)}</text>
          </g>
        ))}
        {buckets.map((b, i) => {
          const cx = PAD_L + slot * i + slot / 2;
          let base = 0;
          const drawn = series.filter((s) => s.points[i].value !== null);
          const missing = missingIn(series, i);
          return (
            <g key={b.key} data-bucket={b.key} data-partial={missing.length ? "true" : undefined}>
              {drawn.length === 0 ? (
                <text x={cx} y={PAD_T + plotH - 6} textAnchor="middle" fontSize="11" fill="#3d5556" data-gap="true">{missing.length ? "not measured" : "no data"}</text>
              ) : drawn.map((s) => {
                const p = s.points[i];
                const v = p.value as number;
                const top = y(base + v), h = Math.max(y(base) - top - 2, 1);
                base += v;
                const id = `${b.key}|${s.group}`;
                const partial = !p.complete || missing.length > 0;
                return (
                  <g key={s.group} onMouseEnter={() => setHover(id)} onMouseLeave={() => setHover(null)}>
                    <rect x={cx - bar / 2} y={top} width={bar} height={h} rx="3" fill={colorFor(s.group, order)}
                      data-series={s.group} data-value={v} data-partial={partial ? "true" : undefined}
                      opacity={hover && hover !== id ? 0.6 : 1}>
                      <title>{`${b.label} · ${s.group}: ${describe(p, fmt)}${partialNote(missing)}`}</title>
                    </rect>
                    {partial && <rect x={cx - bar / 2} y={top} width={bar} height={h} rx="3" fill="url(#partial)" pointerEvents="none" />}
                  </g>
                );
              })}
              <text x={cx} y={H - 8} textAnchor="middle" fontSize="11" fill="#3d5556">{b.label}</text>
            </g>
          );
        })}
      </svg>
      <figcaption className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm">
        {series.map((s) => (
          <span key={s.group} className="inline-flex items-center gap-1.5">
            <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: colorFor(s.group, order) }} />
            <span className="text-ink">{s.group}</span>
            <span className="text-muted-ink">· {describe(s.total, fmt)}</span>
          </span>
        ))}
        <span className="inline-flex items-center gap-1.5 text-muted-ink">
          <span className="inline-block h-2.5 w-2.5 rounded-sm bg-[repeating-linear-gradient(45deg,#3d5556_0_2px,#fff_2px_4px)]" /> hatched: partial (a floor)
        </span>
      </figcaption>
      <SeriesTable buckets={buckets} series={series} fmt={fmt} title={title} />
    </figure>
  );
}

/** The same numbers as a table, for screen readers and anyone who wants the figures. */
export function SeriesTable({ buckets, series, fmt, title }: { buckets: UsageBucket[]; series: UsageSeries[]; fmt: Format; title: string }) {
  return (
    <details className="mt-2 text-sm">
      <summary className="cursor-pointer text-muted-ink">Table: {title}</summary>
      <div className="overflow-x-auto">
        <table className="mt-2 w-full text-left">
          <thead><tr><th className="pr-3">Bucket</th>{series.map((s) => <th key={s.group} className="pr-3">{s.group}</th>)}</tr></thead>
          <tbody>
            {buckets.map((b, i) => (
              <tr key={b.key} data-partial={missingIn(series, i).length ? "true" : undefined}>
                <td className="pr-3">{b.label}{missingIn(series, i).length ? ` (partial: ${missingIn(series, i).join(", ")} not measured)` : ""}</td>
                {series.map((s) => <Fragment key={s.group}><td className="pr-3">{describe(s.points[i], fmt)}</td></Fragment>)}
              </tr>))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

// --- D2: lines over time, percentiles and rates (ADR-0066) ---------------------------------

export const ms: Format = (v) => (v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`);
export const pct: Format = (v) => `${Math.round(v * 100)}%`;
export const num: Format = (v) => (Math.abs(v) < 10 ? v.toFixed(2) : Math.round(v).toLocaleString());

/** One line: a value per bucket, null where nothing was measured (drawn as a gap). */
export interface Line { name: string; values: (number | null)[] }

/** Lines over the buckets. A null breaks the line: no point is drawn and the segment on either
 * side ends there, so a week with nothing measured is a gap, never a dip to 0. */
export function GapLines({ buckets, lines, fmt, title, gap = () => "no data", yMax }: {
  buckets: UsageBucket[]; lines: Line[]; fmt: Format; title: string; gap?: (i: number) => string; yMax?: number;
}) {
  const [hover, setHover] = useState<string | null>(null);
  if (buckets.length === 0) return <p className="text-sm">No finished runs match these filters.</p>;
  const known = lines.flatMap((l) => l.values.filter((v): v is number => v !== null));
  const max = yMax ?? (Math.max(0, ...known) || 1);
  const plotW = W - PAD_L - 8, plotH = H - PAD_T - PAD_B;
  const slot = plotW / buckets.length;
  const x = (i: number) => PAD_L + slot * i + slot / 2;
  const y = (v: number) => PAD_T + plotH - (v / max) * plotH;
  const order = lines.map((l) => l.name);
  return (
    <figure className="m-0">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title} className="h-auto w-full">
        {[0, max / 2, max].map((t) => (
          <g key={t}>
            <line x1={PAD_L} x2={W - 8} y1={y(t)} y2={y(t)} stroke="#f3d5ca" strokeWidth="1" />
            <text x={PAD_L - 6} y={y(t) + 4} textAnchor="end" fontSize="11" fill="#3d5556">{fmt(t)}</text>
          </g>
        ))}
        {lines.map((l) => {
          // Consecutive measured points form a segment; a null ends it.
          const segments: [number, number][][] = [];
          let current: [number, number][] = [];
          l.values.forEach((v, i) => {
            if (v === null) { if (current.length) segments.push(current); current = []; }
            else current.push([x(i), y(v)]);
          });
          if (current.length) segments.push(current);
          const color = colorFor(l.name, order);
          return (
            <g key={l.name} opacity={hover && hover !== l.name ? 0.35 : 1} onMouseEnter={() => setHover(l.name)} onMouseLeave={() => setHover(null)}>
              {segments.filter((seg) => seg.length > 1).map((seg, k) => (
                <path key={k} d={seg.map(([px, py], j) => `${j ? "L" : "M"}${px},${py}`).join(" ")} fill="none" stroke={color} strokeWidth="2" data-line={l.name} />
              ))}
              {l.values.map((v, i) => v === null ? null : (
                <circle key={i} cx={x(i)} cy={y(v)} r="4" fill={color} stroke="white" strokeWidth="2" data-line={l.name} data-value={v}>
                  <title>{`${buckets[i].label} · ${l.name}: ${fmt(v)}`}</title>
                </circle>
              ))}
            </g>
          );
        })}
        {buckets.map((b, i) => (
          <g key={b.key} data-bucket={b.key}>
            {lines.every((l) => l.values[i] === null) && (
              <text x={x(i)} y={PAD_T + plotH - 6} textAnchor="middle" fontSize="11" fill="#3d5556" data-gap="true">{gap(i)}</text>
            )}
            <text x={x(i)} y={H - 8} textAnchor="middle" fontSize="11" fill="#3d5556">{b.label}</text>
          </g>
        ))}
      </svg>
      {lines.length > 1 && (
        <figcaption className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm">
          {lines.map((l) => (
            <span key={l.name} className="inline-flex items-center gap-1.5">
              <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: colorFor(l.name, order) }} />
              <span className="text-ink">{l.name}</span>
            </span>
          ))}
        </figcaption>
      )}
      <details className="mt-2 text-sm">
        <summary className="cursor-pointer text-muted-ink">Table: {title}</summary>
        <div className="overflow-x-auto">
          <table className="mt-2 w-full text-left">
            <thead><tr><th className="pr-3">Bucket</th>{lines.map((l) => <th key={l.name} className="pr-3">{l.name}</th>)}</tr></thead>
            <tbody>
              {buckets.map((b, i) => (
                <tr key={b.key}>
                  <td className="pr-3">{b.label}</td>
                  {lines.map((l) => <td key={l.name} className="pr-3">{l.values[i] === null ? gap(i) : fmt(l.values[i] as number)}</td>)}
                </tr>))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}

/** The three percentile lines of a series, gaps where a bucket measured nothing. */
export function PercentileChart({ buckets, points, title, fmt = ms }: { buckets: UsageBucket[]; points: Percentiles[]; title: string; fmt?: Format }) {
  return (
    <GapLines title={title} buckets={buckets} fmt={fmt}
      gap={(i) => (points[i].unanswered > 0 ? "no reply" : points[i].unmeasured > 0 ? "not measured" : "no data")}
      lines={[
        { name: "p50", values: points.map((p) => p.p50) },
        { name: "p95", values: points.map((p) => p.p95) },
        { name: "p99", values: points.map((p) => p.p99) },
      ]} />
  );
}

/** Percentiles in text: "p50 120 ms · p95 … (n = 40, 2 not measured, 1 unanswered)", or not
 * measured. A turn the agent never answered is counted beside n, never as a time. */
export function PercentileStats({ p, fmt = ms }: { p: Percentiles; fmt?: Format }) {
  const unanswered = p.unanswered ? `${p.unanswered} unanswered` : "";
  if (p.n === 0 || p.p50 === null || p.p95 === null || p.p99 === null) {
    const parts = [p.unmeasured ? `${p.unmeasured}` : "", unanswered].filter(Boolean);
    return <NotMeasured text={parts.length ? `not measured (${parts.join(", ")})` : "no data"} />;
  }
  const apart = [p.unmeasured ? `${p.unmeasured} not measured` : "", unanswered].filter(Boolean).map((x) => `, ${x}`).join("");
  return (
    <span data-n={p.n} data-unanswered={p.unanswered || undefined}>
      p50 {fmt(p.p50)} · p95 {fmt(p.p95)} · p99 {fmt(p.p99)}
      <span className="ml-1 text-xs font-normal text-muted-ink">(n = {p.n}{apart})</span>
    </span>
  );
}

/** A pass rate over judged results: "50% (2 of 4 judged)", or "nothing judged", never 0%. */
export function RateValue({ r }: { r: Rate }) {
  const apart = r.notJudged ? `, ${r.notJudged} not judged` : "";
  if (r.value === null) return <NotMeasured text={r.notJudged ? `nothing judged (${r.notJudged} not judged)` : "no data"} />;
  return (
    <span>
      {pct(r.value)}<span className="ml-1 text-xs font-normal text-muted-ink">({r.passed} of {r.judged} judged{apart})</span>
    </span>
  );
}
