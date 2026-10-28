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

/** One request with the token, or the ApiError that says why not: shared by JSON and binary answers. */
async function request(path: string, init?: RequestInit): Promise<Response> {
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
  return res;
}

async function call<R>(path: string, init?: RequestInit): Promise<R> {
  return (await request(path, init)).json() as Promise<R>;
}

/** A binary answer (recorded audio) as a Blob, with the same token and error handling as `call`. */
export async function callBlob(path: string): Promise<Blob> {
  return (await request(path)).blob();
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
  scenarioTrend: (suite: string) =>
    call<T.ScenarioTrend>(`/trend/scenarios${suite ? `?suite=${encodeURIComponent(suite)}` : ""}`),
  usage: () => call<T.RunUsage[]>("/usage"),
  scenarios: () => call<T.Catalogue>("/scenarios"),
  persona: (id: string) => call<T.Persona>(`/scenarios/${encodeURIComponent(id)}/persona`),
  scenarioById: (id: string) => call<T.ScenarioSource>(`/scenarios/${encodeURIComponent(id)}/source`),
  scenarioForm: () => call<T.ScenarioForm>("/scenarios/form"),
  draftScenario: (body: T.DraftBody) => post<T.Drafted>("/scenarios/draft", body),
  scenarioFile: (file: string) => call<T.ScenarioSource>(`/scenarios/source?file=${encodeURIComponent(file)}`),
  saveScenarioFile: (file: string, text: string, etag: string) =>
    call<T.ScenarioSource>(`/scenarios/source?file=${encodeURIComponent(file)}`, { method: "PUT", body: JSON.stringify({ text, etag }) }),
  validateScenario: (body: { text: string; file?: string; family?: string; fileName?: string }) => post<T.ScenarioCheck>("/scenarios/validate", body),
  suites: () => call<T.Suite[]>("/suites"),
  checks: () => call<T.Check[]>("/checks"),
  gate: () => call<T.GateCatalog>("/gate"),
  resolveGate: (body: T.GateBody) => post<T.GateView>("/gate/resolve", body),
  readiness: () => call<T.Readiness[]>("/readiness"),
  config: () => call<T.SettingRow[]>("/config"),
  rehearse: (scenarioId: string, transcript: T.Turn[]) => post<T.Rehearsal>("/simulator/rehearse", { scenarioId, transcript }),
  startRun: (body: T.LaunchBody) => post<T.Launched>("/runs", body),
  cancelRun: (id: string) => post<T.Cancelled>(`/runs/${encodeURIComponent(id)}/cancel`, {}),
  saveSuite: (body: T.SuiteBody) => call<T.Suite>("/suites", { method: "PUT", body: JSON.stringify(body) }),
  importScenarios: (family: string, files: { name: string; text: string }[]) =>
    post<{ imported: string[] }>("/scenarios/import", { family, files }),
  saveNewScenario,
};

/**
 * Write one new scenario file (the never-overwrite import) and return where the server put
 * it: a flat catalogue has no scenarios/ folder, so the path is never assumed. Shared by the
 * form and the YAML editor, so the rule after a save lives in one place.
 */
async function saveNewScenario(family: string, name: string, text: string): Promise<string> {
  const out = await post<{ imported: string[] }>("/scenarios/import", { family, files: [{ name, text }] });
  const id = out.imported[0];
  if (out.imported.length !== 1 || !id) throw new ApiError(422, "NOT_IMPORTED", "nothing was written; check the folder and file name");
  try {
    return (await call<T.ScenarioSource>(`/scenarios/${encodeURIComponent(id)}/source`)).file;
  } catch {
    // Written, but not found by its id: say so rather than invite a second save.
    throw new ApiError(0, "SAVED_NOT_LISTED", `saved as ${family}/${name}, but the catalogue does not list ${id}; open it from Test cases`);
  }
}

/**
 * A voice attempt's recorded audio as a Blob (ADR-0053): a turn by its seq, or the whole
 * call. An <audio src> cannot carry the token, so the bytes are fetched here, with it, and
 * played from a blob: URL that the component revokes. The server decrypts in memory and
 * answers no-store; an unplayable turn is an ApiError with its code (AUDIO_PRUNED, …).
 * `raw`: an undecodable agent turn's bytes, as the agent sent them.
 */
export function fetchAudio(run: string, test: string, index: number, which: number | "call", opts: { mono?: boolean; raw?: boolean } = {}): Promise<Blob> {
  const query = opts.raw ? "?raw=true" : opts.mono ? "?mono=true" : "";
  return callBlob(`/runs/${encodeURIComponent(run)}/attempts/${encodeURIComponent(test)}/${index}/audio/${which}${query}`);
}
