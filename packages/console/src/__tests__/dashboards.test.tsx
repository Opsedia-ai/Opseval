// ADR-0066 D2: the Latency, Quality, Voice and Usage dashboards and the price table. Every
// number is the server's; these tests hold the console to drawing a null as a gap, never as 0,
// and to saying "not measured" where nothing was measured.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentType } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GapLines, PercentileChart, PercentileStats, RateValue } from "../components/Charts";
import { CostSplitLine, RowCost } from "../components/UsageFigures";
import type { DashboardOptions, DashboardScope, LatencyDashboard, Percentiles, Prices, QualityDashboard, Rate, UsageBucket, UsageDashboard, Usage, VoiceDashboard } from "../lib/types";

const buckets: UsageBucket[] = [
  { key: "2026-03-02", label: "Mar 2", start: "2026-03-02T00:00:00Z", runs: 1 },
  { key: "2026-03-03", label: "Mar 3", start: "2026-03-03T00:00:00Z", runs: 1 },
  { key: "2026-03-04", label: "Mar 4", start: "2026-03-04T00:00:00Z", runs: 1 },
];
const none = (unmeasured = 0): Percentiles => ({ n: 0, unmeasured, p50: null, p95: null, p99: null, mean: null });
const some = (p50: number, n = 3): Percentiles => ({ n, unmeasured: 0, p50, p95: p50 * 2, p99: p50 * 3, mean: p50 });
const rate = (value: number | null, passed: number, judged: number, notJudged: number): Rate => ({ value, passed, judged, notJudged });
const options: DashboardOptions = { suites: ["smoke"], tasks: [], languages: ["ar", "en"], dialects: [], serverOnly: ["task"], notRecorded: ["label"] };
const scope: DashboardScope = { runs: 3, attempts: 5, unreadableRuns: [], inFlightExcluded: true };
const filters = { suite: null, model: null, role: null, since: null, until: null, bucket: "day" as const, task: null, language: null, dialect: null };

describe("lines over time", () => {
  it("a null is a gap, never a point at 0", () => {
    const c = render(<GapLines title="t" buckets={buckets} fmt={(v) => `${v}`} lines={[{ name: "a", values: [10, null, 30] }]} />).container;
    const points = [...c.querySelectorAll("circle[data-line]")];
    expect(points.map((p) => p.getAttribute("data-value"))).toEqual(["10", "30"]);
    // The two measured points are not joined across the gap: no segment of two or more points.
    expect(c.querySelectorAll("path[data-line]").length).toBe(0);
    expect(c.querySelector('[data-bucket="2026-03-03"] [data-gap="true"]')?.textContent).toBe("no data");
    expect(c.textContent).not.toMatch(/\b0 ms/);
  });

  it("a real 0 is drawn as 0", () => {
    const c = render(<GapLines title="t" buckets={buckets} fmt={(v) => `${v}`} lines={[{ name: "a", values: [0, 0, null] }]} />).container;
    expect([...c.querySelectorAll("circle[data-line]")].map((p) => p.getAttribute("data-value"))).toEqual(["0", "0"]);
    expect(c.querySelectorAll("path[data-line]").length).toBe(1);
  });

  it("a bucket whose values were not measured says so", () => {
    const c = render(<PercentileChart title="t" buckets={buckets} points={[some(100), none(2), none()]} />).container;
    expect(c.querySelector('[data-bucket="2026-03-03"] [data-gap="true"]')?.textContent).toBe("not measured");
    expect(c.querySelector('[data-bucket="2026-03-04"] [data-gap="true"]')?.textContent).toBe("no data");
    expect(c.querySelectorAll('circle[data-line="p95"]').length).toBe(1);
  });
});

describe("figures", () => {
  it("percentiles: measured, not measured, or nothing", () => {
    expect(render(<PercentileStats p={some(100, 4)} />).container.textContent).toBe("p50 100 ms · p95 200 ms · p99 300 ms(n = 4)");
    expect(render(<PercentileStats p={none(3)} />).container.textContent).toBe("not measured (3)");
    expect(render(<PercentileStats p={none()} />).container.textContent).toBe("no data");
  });

  it("a pass rate with nothing judged is never 0%", () => {
    const c = render(<RateValue r={rate(null, 0, 0, 2)} />).container;
    expect(c.textContent).toBe("nothing judged (2 not judged)");
    expect(c.textContent).not.toContain("0%");
    expect(render(<RateValue r={rate(0.5, 1, 2, 1)} />).container.textContent).toBe("50%(1 of 2 judged, 1 not judged)");
  });

  it("a run's cost with unpriced calls is the known part and the count, never a total", () => {
    const c = render(<CostSplitLine c={{ knownUsd: 0.5, pricedCalls: 3, unpricedCalls: 2, totalUsd: null, recorded: true }} />).container;
    expect(c.textContent).toBe("Cost ≥ $0.5000 known (3 calls) · 2 calls with no price");
    expect(render(<CostSplitLine c={{ knownUsd: null, pricedCalls: 0, unpricedCalls: 2, totalUsd: null, recorded: true }} />).container.textContent).toContain("no price");
    expect(render(<CostSplitLine c={{ knownUsd: 0.5, pricedCalls: 3, unpricedCalls: 0, totalUsd: 0.5, recorded: true }} />).container.textContent).toBe("Cost $0.5000");
    const row = { costUsd: null, costKnownUsd: 0.01, unpricedCalls: 1 } as Usage;
    expect(render(<RowCost u={row} />).container.textContent).toBe("≥ $0.010 · 1 call with no price");
  });
});

// --- the pages, on a mocked API ---------------------------------------------------------------

const latency: LatencyDashboard = {
  filters, options, scope, rule: "nearest-rank", buckets,
  turnLatency: { group: "agent", points: [some(100), none(1), none()], total: some(100) },
  voiceTurnLatency: { group: "agent", points: [none(), none(), none()], total: none() },
  timeToFirstAudio: { group: "agent", points: [none(), none(), none()], total: none() },
  llmLatency: [],
};
const quality: QualityDashboard = {
  filters, options, scope, buckets,
  passRate: [rate(1, 1, 1, 0), rate(null, 0, 0, 1), rate(null, 0, 0, 0)],
  passRateTotal: rate(1, 1, 1, 1),
  gateHistory: [], flaky: [], checks: [{ scorerId: "goal", axis: "output", passRate: rate(null, 0, 0, 1), meanScore: null }],
  matrix: { runs: [], scenarios: [] }, judgeAgreement: null, judgeAgreementNote: "not measured: no store reads calibrations yet",
};
const voice: VoiceDashboard = {
  filters, options, scope, voiceAttempts: 0, rule: "nearest-rank",
  metrics: [{ name: "voice.dead_air_ms", unit: "ms", total: none(2), degraded: 0, byLanguage: [] }],
};
const usage: UsageDashboard = {
  filters, options, scope, buckets, attempts: [2, 0, 3],
  audioMinutes: [{ value: null, measured: 0, of: 0, complete: true }, { value: null, measured: 0, of: 1, complete: false }, { value: 1.5, measured: 1, of: 1, complete: true }],
  byTask: [{ project: null, task: null, runs: 3, attempts: 5, voiceAttempts: 0, audioMinutes: { value: null, measured: 0, of: 0, complete: true } }],
};

async function page(path: string, body: unknown, Page: ComponentType) {
  vi.resetModules();
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })));
  return render(<MemoryRouter initialEntries={[path]}><Page /></MemoryRouter>);
}

describe("the D2 pages", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("Latency says not measured, and draws no 0", async () => {
    const { default: Latency } = await import("../pages/LatencyDashboard");
    const { container } = await page("/latency", latency, Latency);
    await screen.findAllByText("no data");
    expect(container.textContent).toContain("not measured");
    // Axis ticks may read 0; no mark of data does.
    expect(container.querySelector('circle[data-value="0"]')).toBeNull();
    expect(container.querySelector('[data-bucket="2026-03-03"] [data-gap="true"]')?.textContent).toBe("not measured");
  });

  it("Quality: a day with nothing judged is a gap and judge agreement is not measured", async () => {
    const { default: Quality } = await import("../pages/QualityDashboard");
    const { container } = await page("/quality", quality, Quality);
    await screen.findByText("Pass rate over time");
    expect(container.querySelector('[data-bucket="2026-03-03"] [data-gap="true"]')?.textContent).toBe("nothing judged");
    expect(container.textContent).toContain("not measured: no store reads calibrations yet");
    expect([...container.querySelectorAll("circle[data-line]")].map((c) => c.getAttribute("data-value"))).toEqual(["1"]);
    expect(container.textContent).toContain("nothing judged (1 not judged)");
  });

  it("Voice: an unmeasured metric is not measured, never 0", async () => {
    const { default: Voice } = await import("../pages/VoiceDashboard");
    const { container } = await page("/voice", voice, Voice);
    await screen.findByText("Dead air");
    expect(container.textContent).toContain("not measured (2)");
  });

  it("Usage: no speech is a gap, not 0 minutes", async () => {
    const { default: Usage } = await import("../pages/UsageDashboard");
    const { container } = await page("/usage-totals", usage, Usage);
    await screen.findByText("Audio minutes over time");
    const [attempts, audio] = [...container.querySelectorAll("figure")];
    // A real 0 attempts is drawn as 0; no speech is a gap.
    expect([...attempts.querySelectorAll("circle[data-line]")].map((c) => c.getAttribute("data-value"))).toEqual(["2", "0", "3"]);
    expect(audio.querySelectorAll("circle[data-line]").length).toBe(1);
    expect(audio.querySelector('[data-bucket="2026-03-02"] [data-gap="true"]')?.textContent).toBe("no speech");
    expect(audio.querySelector('[data-bucket="2026-03-03"] [data-gap="true"]')?.textContent).toBe("not measured");
    expect(container.textContent).toContain("no speech calls");
  });
});

describe("the price table", () => {
  afterEach(() => vi.unstubAllGlobals());
  const prices = (canEdit: boolean): Prices => ({
    builtin: [{ id: null, match: "gpt-4o", matchKind: "regex", unit: "tokens", effectiveFrom: "2024-10-02", effectiveAt: null, project: false, input: 2.5, output: 10, reasoning: null, cachedInput: 1.25, audioIn: null, audioOut: null, characters: null, source: "s", createdBy: null }],
    project: [], canEdit, editNote: canEdit ? null : "adding a price needs server mode (a database): the built-in table is read-only here",
  });

  it("offers the form only when the server says this caller may add a row", async () => {
    const { default: Prices } = await import("../pages/Prices");
    const read = await page("/prices", prices(false), Prices);
    await screen.findByTestId("read-only");
    expect(screen.queryByRole("form", { name: "Add a price" })).toBeNull();
    expect(read.container.textContent).toContain("needs server mode");
    read.unmount();
    await page("/prices", prices(true), Prices);
    expect(await screen.findByRole("form", { name: "Add a price" })).toBeTruthy();
  });

  it("a model with no price resolves to no price, never $0", async () => {
    vi.resetModules();
    const fetchMock = vi.fn(async (url: string) =>
      new Response(JSON.stringify(url.includes("/resolve") ? { model: "mine", at: "2026-03-02T00:00:00Z", price: null, source: null } : prices(false)), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const { default: Prices } = await import("../pages/Prices");
    render(<MemoryRouter><Prices /></MemoryRouter>);
    await screen.findByTestId("read-only");
    fireEvent.change(screen.getAllByRole("textbox")[0], { target: { value: "mine" } });
    fireEvent.submit(screen.getByRole("form", { name: "Resolve a price" }));
    await waitFor(() => expect(screen.getByTestId("resolved").textContent).toBe("no price"));
    expect(screen.getByTestId("resolved").textContent).not.toContain("$0");
  });
});
