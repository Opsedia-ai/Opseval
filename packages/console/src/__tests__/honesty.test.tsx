// The console's share of the one rule: a harness problem never looks like an agent failure,
// an agent failure never looks like a pass, and what was not measured is never drawn as 0.
// The components state what a colour means (data-tone), and these tests assert exactly that:
// a restyle may change classes, never meanings.
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Count, GatePill, ResultBar } from "../components/ui";
import { Entry } from "../components/Transcript";
import { ProgressLine } from "../components/Progress";
import StopEditor, { NEVER, budget, stopFields } from "../components/StopEditor";
import { CancelRun, StoppedNotice } from "../components/RunControl";
import { Cell } from "../components/ScenarioTrend";
import { Warnings } from "../components/Warnings";
import { Amount, Cost, Tokens, UsageLine } from "../components/UsageFigures";
import { MeasuredValue, RatioValue, StackedBars, count, usd } from "../components/Charts";
import { MemoryRouter } from "react-router-dom";
import type { Gate, Measured, Ratio, RunProgress, TranscriptEntry, TrendCell, Usage, UsageBucket, UsageSeries } from "../lib/types";

const tone = (el: Element | null | undefined) => el?.getAttribute("data-tone");

describe("the gate pill", () => {
  const pill = (gate: Gate | null) => render(<GatePill gate={gate} />).container.querySelector("span");

  it("means exactly one thing per gate state", () => {
    const expected: [Gate | null, string, string][] = [
      ["safe", "pass", "safe to release"],
      ["gaps", "warn", "passed with gaps"],
      ["blocked", "fail", "blocked"],
      ["not_judged", "mute", "not judged · blocks"],
      ["harness_error", "mute", "harness error"],
      [null, "mute", "in progress"],
    ];
    for (const [gate, meaning, text] of expected) {
      const el = pill(gate);
      expect([gate, tone(el), el?.textContent]).toEqual([gate, meaning, text]);
    }
  });

  it("only a safe run is drawn as a pass", () => {
    const passes = (["safe", "gaps", "blocked", "not_judged", "harness_error", null] as const).filter(
      (g) => tone(pill(g)) === "pass",
    );
    expect(passes).toEqual(["safe"]);
  });

  it("a harness error is never drawn as, or like, an agent failure", () => {
    expect(tone(pill("harness_error"))).not.toBe("fail");
    expect(tone(pill("harness_error"))).not.toBe("warn"); // warn shares the failure's fill
  });
});

describe("unmeasured values", () => {
  it("are never drawn as 0", () => {
    for (const n of [null, undefined]) {
      const { container } = render(<Count n={n} />);
      expect(container.textContent).toBe("—");
      expect(tone(container.querySelector("span"))).toBe("mute");
    }
  });

  it("a real 0 is still 0", () => {
    render(<Count n={0} />);
    expect(screen.getByText("0")).toBeTruthy();
  });

  it("a run with any count unmeasured gets no result bar, never a guessed one", () => {
    for (const [p, f, u] of [[null, null, null], [5, null, 0], [5, 0, null], [null, 2, 0]] as const) {
      const { container } = render(<ResultBar passed={p} failed={f} unjudged={u} />);
      expect(container.textContent).toBe("no results");
    }
  });
});

describe("the transcript", () => {
  const entry = (kind: TranscriptEntry["kind"]): TranscriptEntry => ({
    seq: 3, kind, who: null, text: null, phase: null, latencyMs: null, detail: '{"code": 500}',
  });

  it("never shows an agent error as an ordinary turn", () => {
    const err = render(<Entry e={entry("error")} />).container.firstElementChild;
    const note = render(<Entry e={entry("notification")} />).container.firstElementChild;
    expect(err?.getAttribute("data-kind")).toBe("error");
    expect(tone(err)).toBe("fail");
    expect(tone(note)).toBe("mute"); // drawn differently from the error
    expect(err?.textContent).toMatch(/error from the agent/);
  });

  it("keeps an Arabic turn right to left", () => {
    render(<Entry e={{ ...entry("message"), who: "customer", text: "عايز أرجع الفلوس", detail: null }} />);
    expect(screen.getByText("عايز أرجع الفلوس").getAttribute("dir")).toBe("auto");
  });
});

describe("progress", () => {
  const p: RunProgress = {
    stage: "running", scenariosPlanned: 3, scenariosDone: 2, attemptsRecorded: 7, current: "rf-003",
    scenarios: [], cancelRequested: false,
  };

  it("says how far a run has got, and nothing about how it did", () => {
    const { container } = render(<ProgressLine p={p} />);
    expect(container.textContent).toMatch(/2\/3 scenarios · 7 attempt/);
    expect(container.textContent).toMatch(/rf-003/);
    expect(container.textContent).not.toMatch(/pass|fail|safe/i);
    expect(container.querySelector("[data-tone]")).toBeNull(); // no verdict-coloured pill
  });

  it("names the finishing stage", () => {
    render(<ProgressLine p={{ ...p, stage: "finishing", current: null, scenariosDone: 3 }} />);
    expect(screen.getByText(/writing the gate, reports and sinks/)).toBeTruthy();
  });

  it("says a run is cancelling once asked, and only then", () => {
    expect(render(<ProgressLine p={p} />).container.querySelector('[data-state="cancelling"]')).toBeNull();
    const asked = render(<ProgressLine p={{ ...p, cancelRequested: true }} />).container;
    expect(asked.querySelector('[data-state="cancelling"]')?.textContent).toBe("cancelling");
  });
});

describe("stopping early (ADR-0035)", () => {
  it("a stopped run says so, and never as a pass", () => {
    const { container } = render(<StoppedNotice stopped={{ by: "cancelled", after: "rf-001", detail: "cancelled by request" }} />);
    const notice = container.querySelector('[data-state="stopped-early"]');
    expect(notice?.textContent).toMatch(/Stopped early: cancelled by request/);
    expect(notice?.textContent).toMatch(/skipped, not passed/);
    expect(notice?.querySelector('[data-tone="pass"]')).toBeNull();
  });

  it("a run that was not stopped shows nothing", () => {
    expect(render(<StoppedNotice stopped={null} />).container.textContent).toBe("");
  });

  it("once a cancel is requested, the button gives way to what will happen", () => {
    render(<CancelRun id="r" requested />);
    expect(screen.queryByRole("button", { name: /cancel run/i })).toBeNull();
    expect(screen.getByText(/reported as cancelled, never as passed/)).toBeTruthy();
  });

  it("every stop choice names what it falls back to", () => {
    const base = { ...NEVER, stopOnCriticalFailure: true, maxConsecutiveFailures: 3 };
    render(<StopEditor value={{}} onChange={() => {}} base={base} baseLabel="suite" />);
    const critical = screen.getByRole("combobox", { name: /critical or blocker/i }) as HTMLSelectElement;
    expect(critical.selectedOptions[0].textContent).toBe("stop (suite)");
    const failFast = screen.getByRole("combobox", { name: /first failure/i }) as HTMLSelectElement;
    expect(failFast.selectedOptions[0].textContent).toBe("keep going (suite)");
    expect((screen.getByLabelText("Failures in a row") as HTMLInputElement).placeholder).toBe("3 (suite)");
  });

  it("a time budget is whole minutes, and never a 0 the server would refuse", () => {
    expect([budget("30"), budget("1.4"), budget("0.005"), budget("0"), budget(""), budget("-5")])
      .toEqual([1800, 60, null, null, null, null]);
  });

  it("only what was set is sent: everything else keeps the suite's value", () => {
    expect(stopFields({ failFast: false, stopOnRateLimit: null, maxConsecutiveFailures: undefined, maxWallClockSeconds: 600 }))
      .toEqual({ failFast: false, maxWallClockSeconds: 600 });
  });
});

describe("the per-scenario trend (FR-RPT-06)", () => {
  const cell = (status: TrendCell["status"], flaky = false): Element | null =>
    render(<MemoryRouter><Cell cell={{ run: "r1", status, passRate: status === null ? null : 1, flaky }} /></MemoryRouter>).container.firstElementChild;

  it("draws each verdict as what it means", () => {
    const pill = (el: Element | null) => el?.querySelector("[data-tone]") ?? null;
    expect(tone(pill(cell("passed")))).toBe("pass");
    expect(tone(pill(cell("passed", true)))).toBe("warn"); // a flaky pass is a gap, not a pass
    expect(tone(pill(cell("failed")))).toBe("fail");
  });

  it("never draws what was not judged as a failure", () => {
    for (const status of ["inconclusive", "skipped_precondition", "blocked"] as const) {
      const pill = cell(status)?.querySelector("[data-tone]");
      expect([status, tone(pill), pill?.getAttribute("data-dashed")]).toEqual([status, "mute", "true"]);
    }
  });

  it("a scenario that was not in a run is an absence, not a result", () => {
    const el = cell(null);
    expect(el?.getAttribute("data-state")).toBe("absent");
    expect(el?.querySelector("[data-tone]")).toBeNull();
  });
});

describe("scenario warnings (FR-AUT)", () => {
  it("are shown as warnings, never as errors", () => {
    const { container } = render(<Warnings items={[{ path: "x.yaml", field: "execution.repeats", message: "one attempt cannot show flakiness" }]} />);
    const panel = container.querySelector('[data-state="warnings"]');
    expect(panel?.textContent).toMatch(/1 warning\(s\)/);
    expect(panel?.textContent).toMatch(/one attempt cannot show flakiness/);
    expect(panel?.querySelector('[data-tone="fail"]')).toBeNull();
    expect(tone(panel?.querySelector("[data-tone]"))).toBe("warn");
  });

  it("no warnings, no panel", () => {
    expect(render(<Warnings items={[]} />).container.textContent).toBe("");
  });
});

describe("usage figures", () => {
  const speech: Usage = {
    role: "tts", model: "tts-1", calls: 2, inputTokens: null, outputTokens: null, costUsd: null,
    audioInSeconds: null, audioOutSeconds: 3.5, characters: 42,
    reasoningTokens: null, cachedInputTokens: null, failedCalls: 0, reported: null,
  };

  it("a speech row shows audio and characters, and never tokens as 0", () => {
    const { container } = render(<UsageLine u={speech} />);
    expect(container.textContent).toContain("3.5 s spoken");
    expect(container.textContent).toContain("42 characters");
    expect(container.textContent).not.toMatch(/\b0 tokens/);
    expect(container.textContent).not.toContain("tokens");
  });

  it("an unmeasured amount is drawn as not measured, never 0", () => {
    const { container } = render(<Amount v={null} />);
    expect(container.textContent).toBe("—");
    expect(tone(container.querySelector("span"))).toBe("mute");
  });

  it("a cost over unpriced models is a floor, named partial", () => {
    const partial = render(<Cost usd={0.75} complete={false} />).container;
    expect(partial.textContent).toContain("≥ $0.75");
    expect(partial.textContent).toContain("partial");
    const whole = render(<Cost usd={0.75} complete />).container;
    expect(whole.textContent).toBe("$0.75");
    expect(render(<Cost usd={null} complete={false} />).container.textContent).toBe("not measured");
  });
});

describe("dashboards (ADR-0066)", () => {
  const m = (value: number | null, measured: number, of: number): Measured => ({ value, measured, of, complete: measured === of });
  const buckets: UsageBucket[] = [
    { key: "2026-03-01", label: "Mar 1", start: "2026-03-01T00:00:00Z", runs: 1 },
    { key: "2026-03-02", label: "Mar 2", start: "2026-03-02T00:00:00Z", runs: 1 },
    { key: "2026-03-03", label: "Mar 3", start: "2026-03-03T00:00:00Z", runs: 1 },
  ];
  const series: UsageSeries[] = [
    { group: "judge", points: [m(2, 3, 3), m(null, 0, 2), m(null, 0, 0)], total: m(2, 3, 5) },
    { group: "simulator", points: [m(1, 1, 2), m(null, 0, 1), m(null, 0, 0)], total: m(1, 1, 3) },
  ];
  const chart = () => render(<StackedBars title="t" buckets={buckets} series={series} order={["judge", "simulator"]} fmt={usd} />).container;

  it("a null is a gap, never a bar of 0", () => {
    const c = chart();
    const marks = [...c.querySelectorAll("rect[data-series]")];
    expect(marks.map((r) => r.getAttribute("data-value"))).toEqual(["2", "1"]);
    expect(marks.every((r) => Number(r.getAttribute("height")) > 0)).toBe(true);
    // Mar 2 had calls that reported nothing; Mar 3 had no calls at all.
    for (const [key, said] of [["2026-03-02", "not measured"], ["2026-03-03", "no data"]]) {
      const bucket = c.querySelector(`[data-bucket="${key}"]`);
      expect(bucket?.querySelector("rect[data-series]")).toBeNull();
      expect(bucket?.querySelector('[data-gap="true"]')?.textContent).toBe(said);
    }
  });

  it("a partial value is drawn and said as a floor", () => {
    const c = chart();
    const sim = c.querySelector('rect[data-series="simulator"]');
    expect(sim?.getAttribute("data-partial")).toBe("true");
    expect(sim?.querySelector("title")?.textContent).toBe("Mar 1 · simulator: ≥ $1.00 (1 of 2 calls)");
    expect(c.querySelector('rect[data-series="judge"]')?.getAttribute("data-partial")).toBeNull();
    expect(c.textContent).toContain("no calls");
    expect(c.textContent).toContain("not measured (0 of 2 calls reported)");
  });

  it("a measured figure: whole, partial, not measured, or nothing to count", () => {
    expect(render(<MeasuredValue m={m(1.5, 2, 2)} fmt={usd} />).container.textContent).toBe("$1.50");
    const partial = render(<MeasuredValue m={m(1.5, 2, 3)} fmt={usd} />).container;
    expect(partial.textContent).toBe("≥ $1.50(2 of 3 calls)");
    expect(partial.querySelector('[data-partial="true"]')).not.toBeNull();
    expect(render(<MeasuredValue m={m(null, 0, 4)} fmt={count} />).container.textContent).toBe("not measured");
    expect(render(<MeasuredValue m={m(null, 0, 0)} fmt={count} />).container.textContent).toBe("no calls");
    expect(render(<MeasuredValue m={m(0, 2, 2)} fmt={count} />).container.textContent).toBe("0");
  });

  it("a bucket missing a series is drawn partial as a whole, never as a complete bar", () => {
    const c = chart();
    // Mar 1: judge is whole, but simulator is partial → still a floor for the bucket.
    // Mar 2: both series had calls and neither was measured → not measured, not "no data".
    const mar2 = c.querySelector('[data-bucket="2026-03-02"]');
    expect(mar2?.getAttribute("data-partial")).toBe("true");
    expect(mar2?.querySelector('[data-gap="true"]')?.textContent).toBe("not measured");
    const mixed: UsageSeries[] = [
      { group: "judge", points: [m(2, 3, 3)], total: m(2, 3, 3) },
      { group: "tts", points: [m(null, 0, 1)], total: m(null, 0, 1) },
    ];
    const one = render(<StackedBars title="t" buckets={buckets.slice(0, 1)} series={mixed} order={["judge", "tts"]} fmt={usd} />).container;
    const bar = one.querySelector('rect[data-series="judge"]');
    expect(bar?.getAttribute("data-partial")).toBe("true");
    expect(bar?.querySelector("title")?.textContent).toBe("Mar 1 · judge: $2.00 · partial: tts not measured");
    expect(one.querySelector('[data-bucket="2026-03-01"]')?.getAttribute("data-partial")).toBe("true");
    expect(one.querySelector("tbody tr")?.textContent).toContain("(partial: tts not measured)");
    // A bucket with no calls in the other series stays whole.
    const whole = render(<StackedBars title="t" buckets={buckets.slice(0, 1)} series={[mixed[0], { ...mixed[1], points: [m(null, 0, 0)] }]} order={["judge", "tts"]} fmt={usd} />).container;
    expect(whole.querySelector('rect[data-series="judge"]')?.getAttribute("data-partial")).toBeNull();
  });

  it("a ratio is a floor when its numerator is, and not measured when null", () => {
    const r = (value: number | null, measured: number, of: number, complete: boolean): Ratio => ({ value, measured, of, complete });
    expect(render(<RatioValue r={r(0.5, 2, 2, true)} fmt={usd} unit="passed" />).container.textContent).toBe("$0.50");
    const floor = render(<RatioValue r={r(0.5, 1, 2, false)} fmt={usd} unit="passed" />).container;
    expect(floor.textContent).toBe("≥ $0.50(1 of 2 passed)");
    expect(floor.querySelector('[data-partial="true"]')).not.toBeNull();
    expect(render(<RatioValue r={r(220, 3, 4, true)} fmt={count} unit="attempts" />).container.textContent).toBe("220(3 of 4 attempts)");
    expect(render(<RatioValue r={r(null, 0, 2, true)} fmt={usd} unit="passed" />).container.textContent).toBe("not measured");
    expect(render(<RatioValue r={r(null, 0, 0, true)} fmt={usd} unit="passed" />).container.textContent).toBe("no passed");
  });

  it("a token total some model did not report is a floor on the usage page", () => {
    expect(render(<Tokens v={1200} complete />).container.textContent).toBe("1,200");
    const floor = render(<Tokens v={1200} complete={false} />).container;
    expect(floor.textContent).toBe("≥ 1,200");
    expect(floor.querySelector('[data-partial="true"]')).not.toBeNull();
    expect(render(<Tokens v={null} complete={false} />).container.textContent).toBe("—");
  });
});
