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
export type ScenarioSource = G.ScenarioSourceOut;
export type ScenarioCheck = G.ScenarioCheckOut;
export type Catalogue = G.CatalogueOut;

/** The guided scenario form (ADR-0048): what it may offer, decided by the server. */
export type ScenarioForm = G.FormOut;
export type FormCheck = G.FormCheck;
export type FormParam = G.FormParam;
export type FormTemplate = G.FormTemplate;
/** A filled-in form. The form holds every field, though the API lets a request leave them out. */
export type Draft = G.ScenarioDraftOut;
export type DraftBody = G.ScenarioDraftIn;
export type DraftCheck = G.DraftCheckOut;
/** The YAML a draft becomes and the loader's verdict on it; nothing is written. */
export type Drafted = G.DraftOut;

export type GatePreset = G.GateOut["preset"];
export type GateSetting = G.GateSettingOut;
export type GateCatalog = G.GateCatalogOut;
/** A suite's gate, as the server resolved it (ADR-0032). */
export type GateView = G.GateOut;
/** The editor always holds both fields, though the API lets a request leave them out. */
export type GateBody = Required<G.GateIn>;

export type Suite = G.SuiteOut;
/** A stop policy as edited: a null field falls back (to the suite's, or to "never"). ADR-0035. */
export type StopBody = G.StopIn;
/** A stop policy with every field resolved. */
export type StopView = G.StopOut;
export type Stopped = G.StoppedOut;
export type Cancelled = G.CancelOut;
export type Readiness = G.ReadinessItem;
export type Check = G.CheckOut;
export type RunUsage = G.RunUsage;
export type TrendPoint = G.TrendPoint;
/** Each scenario's verdict across runs (FR-RPT-06). A null status is "not in that run". */
export type ScenarioTrend = G.ScenarioTrend;
export type TrendCell = G.TrendCell;
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

/** The Cost and Tokens dashboards (ADR-0066). Every number is the server's; null is not measured. */
export type Measured = G.MeasuredOut;
export type Ratio = G.RatioOut;
export type UsageBucket = G.UsageBucket;
export type UsageSeries = G.UsageSeries;
export type UsageScope = G.UsageScopeOut;
export type UsageFilterOptions = G.UsageFilterOptions;
export type CostDashboard = G.CostDashboard;
export type TokensDashboard = G.TokensDashboard;
export type UsageBucketing = G.UsageFiltersOut["bucket"];
/** The dashboards' filters as the page holds them; "" is "any". */
export interface UsageQuery { suite: string; role: string; model: string; since: string; until: string; bucket: UsageBucketing }

/** The Latency, Quality, Voice and Usage dashboards (ADR-0066 D2). Percentiles are the server's
 * (nearest rank); a null is a gap. */
export type Percentiles = G.PercentilesOut;
export type PercentileSeries = G.PercentileSeriesOut;
export type Rate = G.RateOut;
export type DashboardOptions = G.DashboardOptionsOut;
export type DashboardScope = G.DashboardScopeOut;
export type LatencyDashboard = G.LatencyDashboard;
export type QualityDashboard = G.QualityDashboard;
export type VoiceDashboard = G.VoiceDashboard;
export type VoiceMetric = G.VoiceMetricOut;
export type UsageDashboard = G.UsageDashboard;
export type TaskUsage = G.TaskUsageOut;
export type CostSplit = G.CostSplitOut;
/** The D2 dashboards' filters as the page holds them; "" is "any". */
export interface DashQuery { suite: string; task: string; language: string; dialect: string; since: string; until: string; bucket: UsageBucketing }

/** Model prices (ADR-0066 D2): the built-in table and a project's own rows. */
export type Price = G.PriceOut;
export type Prices = G.PricesOut;
export type PriceBody = G.PriceIn;
export type PriceResolved = G.PriceResolveOut;

/** Recorded audio of a voice attempt (ADR-0053): what each turn can do is the server's call. */
export type AudioTurn = G.AudioTurnOut;
export type AttemptAudio = G.AttemptAudioOut;

/** Accounts and access (ADR-0050): who the console talks as and what they may do. The
 * capabilities are the server's; the console shows or hides by them and works out nothing. */
export type Session = G.SessionOut;
export type Capability = G.SessionOut["capabilities"][number];
export type ApiKey = G.KeyOut;
export type KeyBody = G.KeyIn;
export type KeyCreated = G.KeyCreatedOut;
export type KeyScope = G.KeyIn["scopes"][number];
export type Member = G.MemberOut;
export type MemberBody = G.MemberIn;
