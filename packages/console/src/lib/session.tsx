import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { ApiError, loadSession } from "./api";
import type { Capability, Session } from "./types";

interface SessionState { session?: Session; error?: ApiError; reload: () => void }

/** null outside a provider: a page rendered on its own (a test) is not gated by it. */
const SessionContext = createContext<SessionState | null>(null);

/** Asks the server once who the console is talking as (ADR-0050), before any page loads. */
export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session>();
  const [error, setError] = useState<ApiError>();
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    loadSession().then(
      (s) => { if (live) { setSession(s); setError(undefined); } },
      (e) => { if (live) setError(e as ApiError); },
    );
    return () => { live = false; };
  }, [tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return <SessionContext.Provider value={{ session, error, reload }}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  return useContext(SessionContext) ?? { reload: () => {} };
}

/** Whether the server says the caller may do this. Never worked out from a role here, and
 * never the only guard: the server refuses what is not allowed whatever the page shows. */
export function useCan(capability: Capability): boolean {
  const ctx = useContext(SessionContext);
  if (ctx === null) return true; // a page rendered outside the app (tests)
  return !!ctx.session?.capabilities.includes(capability);
}

/** A test or a story: render with a session already known. */
export function SessionFixture({ session, children }: { session: Session; children: ReactNode }) {
  return <SessionContext.Provider value={{ session, reload: () => {} }}>{children}</SessionContext.Provider>;
}
