// Dashboard charts (ADR-0066), drawn in-house as SVG: no chart library to vet or ship.
// Every number comes from the server; this file only places marks. Two rules it enforces:
// - a null is a GAP, never a 0 on the axis: a segment with no value is not drawn, and a bucket
//   with nothing measured shows "no data" instead of a bar;
// - a partial value (complete: false) is a floor: drawn hatched, its tooltip says "≥" and how
//   many calls it covers.
import { Fragment, useState } from "react";
import type { Measured, UsageBucket, UsageSeries } from "../lib/types";
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
          return (
            <g key={b.key} data-bucket={b.key}>
              {drawn.length === 0 ? (
                <text x={cx} y={PAD_T + plotH - 6} textAnchor="middle" fontSize="11" fill="#3d5556" data-gap="true">no data</text>
              ) : drawn.map((s) => {
                const p = s.points[i];
                const v = p.value as number;
                const top = y(base + v), h = Math.max(y(base) - top - 2, 1);
                base += v;
                const id = `${b.key}|${s.group}`;
                return (
                  <g key={s.group} onMouseEnter={() => setHover(id)} onMouseLeave={() => setHover(null)}>
                    <rect x={cx - bar / 2} y={top} width={bar} height={h} rx="3" fill={colorFor(s.group, order)}
                      data-series={s.group} data-value={v} data-partial={p.complete ? undefined : "true"}
                      opacity={hover && hover !== id ? 0.6 : 1}>
                      <title>{`${b.label} · ${s.group}: ${describe(p, fmt)}`}</title>
                    </rect>
                    {!p.complete && <rect x={cx - bar / 2} y={top} width={bar} height={h} rx="3" fill="url(#partial)" pointerEvents="none" />}
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
              <tr key={b.key}><td className="pr-3">{b.label}</td>
                {series.map((s) => <Fragment key={s.group}><td className="pr-3">{describe(s.points[i], fmt)}</td></Fragment>)}
              </tr>))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
