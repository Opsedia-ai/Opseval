import type * as T from "./types";

/** An API error carries the server's machine code, so the UI can say what to do about it. */
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public detail?: unknown) {
    super(message);
  }
}

/**
 * The API answers only requests carrying the token `opseval serve` printed (ADR-0031). The
 * printed link carries it in the fragment (never sent to a server, never in logs); it is
 * kept for this browser and dropped from the address bar.
 */
const TOKEN_KEY = "opseval.token";
function adoptToken(): string | null {
  const m = window.location.hash.match(/(?:^#|&)token=([^&]+)/);
  try {
    if (m) {
      localStorage.setItem(TOKEN_KEY, decodeURIComponent(m[1]));
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return m ? decodeURIComponent(m[1]) : null; // storage blocked: this page view only
  }
}
const token = adoptToken();

async function call<R>(path: string, init?: RequestInit): Promise<R> {
  let res: Response;
  try {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (token) headers.authorization = `Bearer ${token}`;
    res = await fetch(`/api/v1${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "OFFLINE", "Cannot reach the Opseval API. Start it with: opseval serve");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = body.detail;
    throw new ApiError(res.status, body.code ?? `HTTP_${res.status}`, body.message ?? (typeof detail === "string" ? detail : `Request failed (${res.status})`), detail);
  }
  return res.json() as Promise<R>;
}
const post = <R,>(path: string, body: unknown) => call<R>(path, { method: "POST", body: JSON.stringify(body) });

export const api = {
  health: () => call<T.Health>("/health"),
  runs: () => call<T.RunSummary[]>("/runs"),
  run: (id: string) => call<T.RunDetail>(`/runs/${encodeURIComponent(id)}`),
  attempt: (run: string, test: string, index: number) =>
    call<T.AttemptRecord>(`/runs/${encodeURIComponent(run)}/attempts/${encodeURIComponent(test)}/${index}`),
  compare: (baseline: string, candidate: string) =>
    call<T.Comparison>(`/compare?baseline=${encodeURIComponent(baseline)}&candidate=${encodeURIComponent(candidate)}`),
  trend: () => call<T.TrendPoint[]>("/trend"),
  usage: () => call<T.RunUsage[]>("/usage"),
  scenarios: () => call<T.Catalogue>("/scenarios"),
  persona: (id: string) => call<T.Persona>(`/scenarios/${encodeURIComponent(id)}/persona`),
  suites: () => call<T.Suite[]>("/suites"),
  checks: () => call<T.Check[]>("/checks"),
  gate: () => call<T.GateCatalog>("/gate"),
  resolveGate: (body: T.GateBody) => post<T.GateView>("/gate/resolve", body),
  readiness: () => call<T.Readiness[]>("/readiness"),
  config: () => call<T.SettingRow[]>("/config"),
  rehearse: (scenarioId: string, transcript: T.Turn[]) => post<T.Rehearsal>("/simulator/rehearse", { scenarioId, transcript }),
  startRun: (body: T.LaunchBody) => post<T.Launched>("/runs", body),
  saveSuite: (body: T.SuiteBody) => call<T.Suite>("/suites", { method: "PUT", body: JSON.stringify(body) }),
  importScenarios: (family: string, files: { name: string; text: string }[]) =>
    post<{ imported: string[] }>("/scenarios/import", { family, files }),
};
