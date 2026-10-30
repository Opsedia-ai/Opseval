import { useState, type FormEvent } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { LogIn } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { useSession } from "../lib/session";
import { Button, Callout, CoralButton } from "../components/ui";

/** Why an OIDC sign-in came back refused (the server redirects here with ?error=CODE). */
const OIDC_ERRORS: Record<string, string> = {
  NO_ACCOUNT: "No Opseval account is linked to that sign-in (sign-up is off). Ask an admin to link it to your account: they can find your sign-in in the audit trail.",
  OIDC_FAILED: "The identity provider's answer did not check out. Try again.",
  OIDC_NO_FLOW: "That sign-in was not started from this browser, or took too long. Try again.",
  OIDC_OFF: "Single sign-on is not configured on this server.",
};

/** Sign in (ADR-0050). Accounts mode only: in shared-token mode there is nothing to sign in to. */
export default function Login() {
  const { session, reload } = useSession();
  const [params] = useSearchParams();
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string>(() => {
    const code = params.get("error");
    return code ? OIDC_ERRORS[code] ?? `Sign-in refused (${code}).` : "";
  });
  const [busy, setBusy] = useState(false);

  if (session && (session.mode === "token" || session.user)) return <Navigate to="/" replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.login(email, password);
      reload();
      nav("/", { replace: true });
    } catch (err) {
      const ex = err as ApiError;
      setError(ex.code === "TOO_MANY_ATTEMPTS" ? "Too many failed sign-ins. Wait a little and try again." : ex.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-ink-deep px-4">
      <form onSubmit={(e) => void submit(e)} className="w-full max-w-sm space-y-5 rounded-3xl bg-white p-8 shadow-xl" aria-label="Sign in">
        <div>
          <div className="font-display text-2xl font-extrabold tracking-tight text-ink">Sign in to opseval</div>
          {session?.project && <div className="mt-1 font-mono text-xs text-muted-ink">{session.project} · {session.task}</div>}
        </div>
        {error && <Callout>{error}</Callout>}
        <label className="block text-sm font-semibold text-ink">Email
          <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)}
            className="mt-1 w-full rounded-xl border border-line px-3 py-2 font-normal" />
        </label>
        <label className="block text-sm font-semibold text-ink">Password
          <input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded-xl border border-line px-3 py-2 font-normal" />
        </label>
        <CoralButton type="submit" disabled={busy} className="w-full"><LogIn size={16} />Sign in</CoralButton>
        {session?.oidc && (
          // A full navigation, not a fetch: the identity provider takes over the page.
          <Button variant="secondary" className="w-full" type="button" onClick={() => window.location.assign("/api/v1/auth/oidc/start")}>
            Sign in with single sign-on
          </Button>
        )}
      </form>
    </div>
  );
}
