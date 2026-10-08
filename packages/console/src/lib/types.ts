// Mirrors packages/opseval/src/opseval/server/schemas.py (camelCase on the wire).
// The browser never recomputes aggregates (ADR-0004). null means "not measured", never 0.
export type Area = "output" | "flow" | "tools" | "security" | "harness";
export type Gate = "judged" | "not_judged" | "blocked";

export interface RunSummary {
  id: string; suite: string; startedAt: string | null; state: "running" | "finished"; gate: Gate;
  passed: number; failed: number; unjudged: number; flaky: number;
}
export interface Score {
  scorerId: string; axis: Area; status: "passed" | "failed" | "infra_error";
  score: number | null; threshold: number; reason: string | null; degraded: boolean;
}
export interface Attempt {
  index: number; passed: boolean | null; terminalState: string; invalidReason: string | null; scores: Score[];
}
export interface TestResult {
  id: string; name: string; criticality: string;
  status: "passed" | "failed" | "inconclusive" | "skipped_precondition" | "blocked";
  reason: string | null; passRate: number | null; flaky: boolean; attempts: Attempt[];
}
export interface Usage {
  role: string; model: string; calls: number; inputTokens: number; outputTokens: number; costUsd: number | null;
}
export interface RunDetail {
  summary: RunSummary; target: string; profile: string; gateStatus: string; gateSummary: string;
  blocking: string[]; warnings: string[]; exitCode: number; tests: TestResult[]; usage: Usage[]; loadErrors: string[];
}
export interface Scenario {
  id: string; title: string; difficulty: "low" | "medium" | "high"; criticality: string; stopAt: string;
  repeats: number; tags: string[]; allowCommit: boolean; language: string; personaRole: string;
}
export interface Problem { path: string; field: string; message: string }
export interface Catalogue { scenarios: Scenario[]; problems: Problem[] }
export interface Suite { file: string; displayName: string; description: string; include: string[]; scenarioCount: number; repeats: number | null; stopAt: string | null }
export interface Readiness { id: string; label: string; ready: boolean; detail: string }
export interface Check { id: string; question: string; area: Area; kind: "deterministic" | "judge"; reads: "chat" | "trace" | "frames" }
export interface RunUsage { run: string; simulatorTokens: number | null; judgeTokens: number | null; costUsd: number | null }
export interface TrendPoint { run: string; label: string; passRate: number | null }
export interface SettingRow { key: string; value: string | null; redacted: boolean }
export interface Health { status: string; version: string; reportsDir: string; catalogue: string | null; profile: string | null }
export interface Change { testId: string; name: string; before: string; after: string }
export interface AreaDelta { area: Area; before: number | null; after: number | null }
export interface Comparison { baseline: string; candidate: string; regressed: Change[]; improved: Change[]; unjudged: Change[]; areas: AreaDelta[] }
export interface Persona {
  scenarioId: string; role: string; goal: string; flow: string; language: string; dialect: string | null;
  timezone: string | null; typingNoise: number; scripted: boolean; opening: string | null;
  simulatorModel: string | null; judgeModel: string | null;
}
export interface Turn { who: "customer" | "agent"; text: string }
export interface Rehearsal { turn: Turn; goalReached: boolean; model: string | null }
export interface LaunchBody { suite?: string; scenarios?: string[]; target?: string; repeats?: number; allowCommit?: boolean; failFast?: boolean }
export interface SuiteBody { file: string; displayName: string; description: string; include: string[]; repeats?: number; stopAt?: string }
