import { Link } from "react-router-dom";
import { AlertTriangle, Info } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import type { ApiError } from "../lib/api";
import type { Resource } from "../lib/useApi";
import type { Area, Gate, RunState } from "../lib/types";

export function cx(...p: (string | false | undefined)[]): string {
  return p.filter(Boolean).join(" ");
}

/** Every page opens on a coral band. */
export function PageHeader({ title, aside, sub }: { title: string; sub?: ReactNode; aside?: ReactNode }) {
  return (
    <header className="bg-coral px-5 pb-10 pt-8 md:px-10">
      <div className="mx-auto flex max-w-6xl flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-4xl font-extrabold leading-tight text-ink md:text-5xl">{title}</h1>
          {sub && <p className="mt-1 text-ink">{sub}</p>}
        </div>
        <div className="flex flex-wrap items-center gap-2">{aside}</div>
      </div>
    </header>
  );
}

export function Page({ children }: { children: ReactNode }) {
  return <div className="mx-auto -mt-6 max-w-6xl space-y-5 px-5 pb-16 md:px-10">{children}</div>;
}

export function Card({ title, aside, children, tone = "plain", className }: {
  title?: ReactNode; aside?: ReactNode; children: ReactNode; tone?: "plain" | "ink" | "blush" | "teal" | "soft"; className?: string;
}) {
  const tones = {
    plain: "bg-white border border-line text-muted-ink",
    soft: "bg-soft border border-line text-muted-ink",
    blush: "bg-blush text-ink",
    teal: "bg-teal text-ink-deep",
    ink: "bg-ink text-ink-text",
  };
  return (
    <section className={cx("overflow-hidden rounded-[28px]", tones[tone], className)}>
      {title && (
        <div className="flex items-center justify-between gap-3 border-b border-line/70 px-6 py-4">
          <h2 className={cx("text-xl font-bold", tone === "ink" && "text-white")}>{title}</h2>
          {aside}
        </div>
      )}
      <div className="p-6">{children}</div>
    </section>
  );
}

export function Explainer({ children }: { children: ReactNode }) {
  return (
    <p className="flex items-start gap-3 rounded-2xl border border-line bg-white px-4 py-3 text-[15px] text-muted-ink shadow-sm [&_b]:font-bold [&_b]:text-ink">
      <Info size={18} className="mt-0.5 shrink-0 text-deep-coral" /><span>{children}</span>
    </p>
  );
}

export function Callout({ children, tone = "warn" }: { children: ReactNode; tone?: "warn" | "info" }) {
  return (
    <div className={cx("rounded-2xl border-l-4 px-5 py-4 text-ink", tone === "warn" ? "border-deep-coral bg-blush" : "border-teal bg-teal-tint")}>
      {children}
    </div>
  );
}

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "ghost"; to?: string };
export function Button({ variant = "secondary", to, className, children, ...rest }: BtnProps) {
  const cls = cx(
    "inline-flex items-center justify-center gap-2 rounded-full px-5 py-2 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-50",
    variant === "primary" && "bg-ink text-white hover:bg-ink-deep", // on coral: ink primary, white secondary
    variant === "secondary" && "border border-ink/25 bg-white text-ink hover:border-ink",
    variant === "ghost" && "text-ink hover:bg-blush",
    className,
  );
  return to ? (
    <Link to={to} className={cls}>{children}</Link>
  ) : (
    <button className={cls} {...rest}>{children}</button>
  );
}

/** Coral primary button for light surfaces (dark text, never white on coral). */
export function CoralButton({ className, children, to, ...rest }: BtnProps) {
  const cls = cx(
    "inline-flex items-center justify-center gap-2 rounded-full bg-coral px-5 py-2 text-sm font-bold text-ink hover:bg-coral-tint disabled:cursor-not-allowed disabled:opacity-50",
    className,
  );
  return to ? <Link to={to} className={cls}>{children}</Link> : <button className={cls} {...rest}>{children}</button>;
}

export function Pill({ children, tone = "neutral", dashed }: {
  children: ReactNode; tone?: "neutral" | "pass" | "fail" | "warn" | "mute" | "harness"; dashed?: boolean;
}) {
  const tones = {
    neutral: "bg-soft text-ink border-line",
    pass: "bg-teal-tint text-ink border-teal",
    fail: "bg-blush text-deep-coral border-deep-coral",
    warn: "bg-blush text-ink border-coral",
    mute: "bg-white text-muted-ink border-line",
    // Deliberately not the failure red: a harness problem is not the agent's fault.
    harness: "bg-white text-ink border-ink border-dashed",
  };
  return (
    <span className={cx("inline-flex items-center rounded-full border px-3 py-0.5 text-xs font-semibold uppercase tracking-[0.08em]", tones[tone], dashed && "border-dashed")}>
      {children}
    </span>
  );
}

export const NotMeasured = ({ text = "not measured" }: { text?: string }) => <Pill tone="mute" dashed>{text}</Pill>;

const areaStyle: Record<Area, string> = {
  output: "bg-blush text-deep-coral border-coral",
  flow: "bg-teal-tint text-ink border-teal",
  tools: "bg-soft text-ink border-ink/40",
  security: "bg-ink text-white border-teal",
  harness: "bg-white text-muted-ink border-line border-dashed",
};
export const AreaPill = ({ area }: { area: Area }) => (
  <span className={cx("inline-flex rounded-full border px-3 py-0.5 text-xs font-semibold capitalize", areaStyle[area])}>{area}</span>
);

export const StatePill = ({ state }: { state: RunState }) =>
  state === "error" ? <Pill tone="harness">harness error</Pill> : <Pill tone={state === "running" ? "warn" : "neutral"}>{state}</Pill>;

/** The gate, coloured by what it means.
 *
 *  `harness_error` must never wear the failure colour and `failed` must never wear the
 *  pass colour. An earlier version did both, which is the one rule inverted in the one
 *  place anybody looks. */
export const GatePill = ({ gate }: { gate: Gate }) => {
  if (gate === "passed") return <Pill tone="pass">passed</Pill>;
  if (gate === "failed") return <Pill tone="fail">failed</Pill>;
  if (gate === "harness_error") return <Pill tone="harness">harness error</Pill>;
  return <NotMeasured text="not judged" />;
};

export function Table({ head, children }: { head: ReactNode[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <thead>
          <tr className="border-b border-line text-xs uppercase tracking-[0.08em] text-muted-ink">
            {head.map((h, i) => <th key={i} className="px-4 py-3 font-semibold">{h}</th>)}
          </tr>
        </thead>
        <tbody className="[&_td]:px-4 [&_td]:py-3 [&_tr]:border-b [&_tr]:border-line/60 [&_tr:hover]:bg-soft">{children}</tbody>
      </table>
    </div>
  );
}

export const Mono = ({ children }: { children: ReactNode }) => <code className="font-mono text-[13px]">{children}</code>;

export const Loading = () => (
  <div className="flex items-center gap-3 p-6 text-muted-ink"><span className="h-4 w-4 animate-spin rounded-full border-2 border-line border-t-coral" />Loading…</div>
);

export function ErrorBox({ error }: { error: ApiError }) {
  return (
    <div className="flex items-start gap-3 rounded-2xl border border-deep-coral bg-blush p-5 text-ink">
      <AlertTriangle className="mt-0.5 shrink-0 text-deep-coral" size={20} />
      <div>
        <b>{error.code === "OFFLINE" ? "The API is not running" : error.code}</b>
        <p className="mt-1">{error.message}</p>
        {error.code === "OFFLINE" && <pre className="mt-3 rounded-xl bg-ink p-3 font-mono text-sm text-ink-text">uv run --extra serve opseval serve</pre>}
      </div>
    </div>
  );
}

/** Renders loading, error or the data. Unmeasured is the caller's job to show, never 0. */
export function Data<T>({ res, children }: { res: Resource<T>; children: (d: T) => ReactNode }) {
  if (res.error) return <ErrorBox error={res.error} />;
  if (res.loading || !res.data) return <Loading />;
  return <>{children(res.data)}</>;
}

export function Stat({ label, value, note, tone = "plain" }: { label: string; value: ReactNode; note?: ReactNode; tone?: "plain" | "ink" | "teal" | "blush" }) {
  const tones = { plain: "bg-white border border-line", ink: "bg-ink text-ink-text", teal: "bg-teal text-ink-deep", blush: "bg-blush text-ink" };
  return (
    <div className={cx("rounded-[28px] p-5", tones[tone])}>
      <div className={cx("text-xs font-semibold uppercase tracking-[0.08em]", tone === "plain" && "text-muted-ink")}>{label}</div>
      <div className={cx("mt-1 font-display text-4xl font-extrabold tracking-tight", tone === "plain" ? "text-ink" : tone === "ink" ? "text-white" : "")}>{value}</div>
      {note && <div className="mt-1 text-sm opacity-90">{note}</div>}
    </div>
  );
}

/** Pass / fail / unjudged as one bar. Unjudged is hatched: it is not a failure. */
export function ResultBar({ passed, failed, unjudged }: { passed: number; failed: number; unjudged: number }) {
  const total = passed + failed + unjudged || 1;
  const w = (n: number) => `${(n / total) * 100}%`;
  return (
    <div className="flex h-2.5 w-40 overflow-hidden rounded-full bg-line" title={`${passed} passed · ${failed} failed · ${unjudged} not judged`}>
      <span className="bg-teal" style={{ width: w(passed) }} />
      <span className="bg-deep-coral" style={{ width: w(failed) }} />
      <span className="bg-[repeating-linear-gradient(45deg,#3d5556_0_2px,#fff_2px_5px)] opacity-50" style={{ width: w(unjudged) }} />
    </div>
  );
}
