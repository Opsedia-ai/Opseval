// The console's names for the API's types. The types themselves are GENERATED from
// packages/opseval/src/opseval/server/schemas.py into ./api.gen.ts (scripts/console_types.py);
// the test suite fails if they drift. Add a name here; never a field.
// The browser never recomputes aggregates (ADR-0029). null means "not measured", never 0.
import type * as G from "./api.gen";

export type Area = G.Axis;
/** What a run means for the release; decided by the server (views.gate_state), never here. */
export type Gate = NonNullable<G.RunSummary["gate"]>;
export type RunState = G.RunSummary["state"];

export type RunSummary = G.RunSummary;
export type RunProgress = G.RunProgress;
export type Score = G.ScoreOut;
export type Attempt = G.AttemptOut;
export type TestResult = G.TestOut;
export type Usage = G.UsageOut;
export type AttemptRecord = G.AttemptRecordOut;
export type TranscriptEntry = G.TranscriptEntry;
export type Sink = G.SinkOut;
export type RunDetail = G.RunDetail;
export type Scenario = G.ScenarioOut;
export type Problem = G.Problem;
export type Catalogue = G.CatalogueOut;

export type GatePreset = G.GateOut["preset"];
export type GateSetting = G.GateSettingOut;
export type GateCatalog = G.GateCatalogOut;
/** A suite's gate, as the server resolved it (ADR-0032). */
export type GateView = G.GateOut;
/** The editor always holds both fields, though the API lets a request leave them out. */
export type GateBody = Required<G.GateIn>;

export type Suite = G.SuiteOut;
export type Readiness = G.ReadinessItem;
export type Check = G.CheckOut;
export type RunUsage = G.RunUsage;
export type TrendPoint = G.TrendPoint;
export type SettingRow = G.SettingRow;
export type Health = G.Health;
export type Change = G.Change;
export type AreaDelta = G.AreaDelta;
export type Comparison = G.Comparison;
export type Persona = G.PersonaOut;
export type Turn = G.Turn;
export type Rehearsal = G.RehearseOut;
export type LaunchBody = G.LaunchIn;
export type Launched = G.LaunchOut;
export type SuiteBody = G.SuiteIn;
