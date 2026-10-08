import type * as T from "./types";

/** An API error carries the server's machine code, so the UI can say what to do about it. */
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public detail?: unknown) {
    super(message);
  }
}

const TOKEN_KEY = "opseval.token";
/** Only needed when the server was started with --token. Per-browser, never sent anywhere
 *  but this same-origin API. */
export const getToken = (): string => {
  try { return localStorage.getItem(TOKEN_KEY) ?? ""; } catch { return ""; }
};
export const setToken = (v: string): void => {
  try { v.trim() ? localStorage.setItem(TOKEN_KEY, v.trim()) : localStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ }
};

async function call<R>(path: string, init?: RequestInit): Promise<R> {
  let res: Response;
  const token = getToken();
  try {
    res = await fetch(`/api/v1${path}`, {
      ...init,
      headers: {
        "content-type": "application/json",
        ...(token ? { authorization: `Bearer ${token}` } : {}),
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError(0, "OFFLINE", "Cannot reach the Opseval API. Start it with: opseval serve");
  }
  if (res.status === 401) throw new ApiError(401, "UNAUTHORIZED", "This server needs an access token. Add it in Settings.");
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
  compare: (baseline: string, candidate: string) =>
    call<T.Comparison>(`/compare?baseline=${encodeURIComponent(baseline)}&candidate=${encodeURIComponent(candidate)}`),
  trend: () => call<T.TrendPoint[]>("/trend"),
  usage: () => call<T.RunUsage[]>("/usage"),
  scenarios: () => call<T.Catalogue>("/scenarios"),
  persona: (id: string) => call<T.Persona>(`/scenarios/${encodeURIComponent(id)}/persona`),
  suites: () => call<T.Suite[]>("/suites"),
  checks: () => call<T.Check[]>("/checks"),
  readiness: () => call<T.Readiness[]>("/readiness"),
  config: () => call<T.SettingRow[]>("/config"),
  rehearse: (scenarioId: string, transcript: T.Turn[]) => post<T.Rehearsal>("/simulator/rehearse", { scenarioId, transcript }),
  startRun: (body: T.LaunchBody) => post<{ accepted: boolean; message: string; allowCommit: boolean }>("/runs", body),
  saveSuite: (body: T.SuiteBody) => call<T.Suite>("/suites", { method: "PUT", body: JSON.stringify(body) }),
  importScenarios: (family: string, files: { name: string; text: string }[]) =>
    post<{ imported: string[] }>("/scenarios/import", { family, files }),
};
