import { NavLink, Outlet } from "react-router-dom";
import { Activity, BarChart3, ClipboardList, Cog, Coins, DollarSign, FlaskConical, GitCompareArrows, KeyRound, Layers, ListChecks, LogOut, MessageSquare, Mic, Play, PieChart, ShieldCheck, Tag, Timer, Upload } from "lucide-react";
import type { ReactNode } from "react";
import { api } from "../lib/api";
import { useSession } from "../lib/session";
import type { Capability } from "../lib/types";
import { useApi } from "../lib/useApi";
import { Button, Callout, CoralButton, cx } from "./ui";

/** `needs`: shown only when the server grants it (GET /auth/session); the server refuses it anyway. */
const nav: { heading: string; items: { to: string; label: string; icon: ReactNode; end?: boolean; needs?: Capability }[] }[] = [
  { heading: "Evaluate", items: [
    { to: "/", label: "Runs", icon: <ClipboardList size={18} />, end: true },
    { to: "/runs/new", label: "New run", icon: <Play size={18} />, needs: "runs:start" },
  ] },
  { heading: "Results", items: [
    { to: "/compare", label: "Compare runs", icon: <GitCompareArrows size={18} /> },
    { to: "/trend", label: "Trend", icon: <BarChart3 size={18} /> },
    { to: "/quality", label: "Quality", icon: <ShieldCheck size={18} /> },
    { to: "/latency", label: "Latency", icon: <Timer size={18} /> },
    { to: "/voice", label: "Voice", icon: <Mic size={18} /> },
    { to: "/cost", label: "Cost", icon: <DollarSign size={18} /> },
    { to: "/tokens", label: "Tokens", icon: <Coins size={18} /> },
    { to: "/usage-totals", label: "Usage", icon: <Activity size={18} /> },
    { to: "/usage", label: "Usage per run", icon: <PieChart size={18} /> },
  ] },
  { heading: "Author", items: [
    { to: "/simulator", label: "Simulator", icon: <MessageSquare size={18} />, needs: "runs:start" },
    { to: "/scenarios", label: "Test cases", icon: <ListChecks size={18} /> },
    { to: "/import", label: "Bulk import", icon: <Upload size={18} />, needs: "catalogue:write" },
    { to: "/suites", label: "Suites", icon: <Layers size={18} /> },
  ] },
  { heading: "Reference", items: [
    { to: "/checks", label: "Checks & judges", icon: <FlaskConical size={18} /> },
    { to: "/settings", label: "Settings", icon: <Cog size={18} /> },
    { to: "/prices", label: "Prices", icon: <Tag size={18} /> },
    { to: "/keys", label: "API keys", icon: <KeyRound size={18} />, needs: "manage" },
  ] },
];

/** Teal four-point star: the logo mark. */
const Logo = () => (
  <svg width="30" height="30" viewBox="0 0 32 32" aria-hidden>
    <circle cx="16" cy="16" r="14" fill="none" stroke="#fff" strokeWidth="2" />
    <path d="M16 6l2.6 7.4L26 16l-7.4 2.6L16 26l-2.6-7.4L6 16l7.4-2.6z" fill="#2da3a1" />
  </svg>
);

/** Who is signed in, in accounts mode (ADR-0050), and the way out. */
function Who() {
  const { session } = useSession();
  if (!session || session.mode !== "accounts" || !session.user) return null;
  const signOut = async () => {
    try { await api.logout(); } finally { window.location.assign("/login"); }
  };
  return (
    <div className="flex items-center gap-3 font-mono text-xs text-muted-ink" data-testid="who">
      <span><b className="text-ink">{session.user.name}</b>{session.user.role && <> · {session.user.role}</>}</span>
      <Button variant="ghost" onClick={() => void signOut()}><LogOut size={14} />Sign out</Button>
    </div>
  );
}

export default function Shell() {
  const { session } = useSession();
  const can = (c?: Capability) => !c || !session || session.capabilities.includes(c);
  const noAccess = session?.mode === "accounts" && !session.hasAccess;
  const health = useApi(api.health, [], () => 15000);
  const up = !!health.data;
  return (
    <div className="flex min-h-full">
      <aside className="sticky top-0 hidden h-screen w-64 shrink-0 flex-col overflow-y-auto bg-ink-deep px-4 py-6 md:flex">
        <div className="mb-8 flex items-center gap-3 px-2">
          <Logo />
          <div className="leading-tight">
            <div className="font-display text-xl font-extrabold tracking-tight text-white">opseval</div>
            <div className="font-mono text-[11px] text-ink-text/70">console</div>
          </div>
        </div>
        <nav className="flex-1 space-y-6">
          {nav.map((g) => (
            <div key={g.heading}>
              <div className="mb-1.5 px-3 text-[11px] font-semibold uppercase tracking-[0.1em] text-teal">{g.heading}</div>
              {g.items.filter((i) => can(i.needs)).map((i) => (
                <NavLink key={i.to} to={i.to} end={i.end}
                  className={({ isActive }) => cx(
                    "flex items-center gap-3 rounded-xl px-3 py-2 text-[15px] font-medium transition",
                    isActive ? "bg-ink font-semibold text-white shadow-[inset_3px_0_0_#2da3a1]" : "text-ink-text hover:bg-ink/60 hover:text-white")}>
                  {i.icon}{i.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
      </aside>

      <div className="min-w-0 flex-1 bg-white">
        <div className="sticky top-0 z-10 flex items-center justify-between gap-4 border-b border-line bg-white/90 px-5 py-3 backdrop-blur md:px-10">
          <div className="flex items-center gap-2 font-mono text-xs text-muted-ink">
            <span className={cx("inline-block h-2.5 w-2.5 rounded-full", up ? "bg-teal" : "bg-deep-coral")} />
            {up ? <>api ok <span className="ml-2">profile</span><b className="text-ink">{health.data?.profile ?? "none"}</b><span className="ml-2">v{health.data?.version}</span></> : "api offline"}
          </div>
          <div className="flex items-center gap-4">
            <Who />
            {can("runs:start") && <CoralButton to="/runs/new"><Play size={16} />New run</CoralButton>}
          </div>
        </div>
        {noAccess
          ? <main className="p-10"><Callout><b>No access to {session?.project}.</b> You are signed in, but have no role in this project. Ask an admin of its organisation.</Callout></main>
          : <main><Outlet /></main>}
      </div>
    </div>
  );
}
