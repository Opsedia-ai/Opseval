// The console's share of the one rule: a harness problem never looks like an agent failure,
// an agent failure never looks like a pass, and what was not measured is never drawn as 0.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Count, GatePill, ResultBar } from "../components/ui";
import { Entry } from "../components/Transcript";
import { ProgressLine } from "../components/Progress";
import type { RunProgress } from "../lib/types";

afterEach(cleanup);

describe("the gate pill", () => {
  // The outermost pill element of one render, read from that render's own container.
  const pill = (gate: Parameters<typeof GatePill>[0]["gate"]) =>
    render(<GatePill gate={gate} />).container.querySelector("span") as HTMLElement;

  it("draws only 'safe' green", () => {
    expect(pill("safe").textContent).toBe("safe to release");
    expect(pill("safe").className).toMatch(/bg-teal-tint/);
  });

  it("never draws a gap, an unjudged run or a harness error green", () => {
    for (const gate of ["gaps", "not_judged", "harness_error", null] as const) {
      const el = pill(gate);
      expect(el.className, String(gate)).not.toMatch(/bg-teal-tint/);
      cleanup();
    }
  });

  it("draws a harness error unlike an agent failure", () => {
    const harness = pill("harness_error");
    expect(harness.textContent).toBe("harness error");
    expect(harness.className).not.toMatch(/deep-coral/); // the colour of an agent failure
    cleanup();
    expect(pill("blocked").className).toMatch(/deep-coral/);
  });
});

describe("unmeasured values", () => {
  it("are never drawn as 0", () => {
    render(<Count n={null} />);
    expect(screen.queryByText("0")).toBeNull();
    expect(screen.getByText("—")).toBeTruthy();
  });

  it("a real 0 is still 0", () => {
    render(<Count n={0} />);
    expect(screen.getByText("0")).toBeTruthy();
  });

  it("a run with no counts gets no result bar", () => {
    render(<ResultBar passed={null} failed={null} unjudged={null} />);
    expect(screen.getByText("no results")).toBeTruthy();
  });
});

describe("the transcript", () => {
  it("never shows an agent error as an ordinary turn", () => {
    render(<Entry e={{ seq: 3, kind: "error", who: null, text: null, phase: null, latencyMs: null, detail: '{"code": 500}' }} />);
    expect(screen.getByText(/error from the agent/)).toBeTruthy();
  });

  it("keeps an Arabic turn right to left", () => {
    render(<Entry e={{ seq: 1, kind: "message", who: "customer", text: "عايز أرجع الفلوس", phase: null, latencyMs: null, detail: null }} />);
    expect(screen.getByText("عايز أرجع الفلوس").getAttribute("dir")).toBe("auto");
    expect(screen.getByText(/customer/)).toBeTruthy();
  });
});

describe("progress", () => {
  const p: RunProgress = {
    stage: "running", scenariosPlanned: 3, scenariosDone: 2, attemptsRecorded: 7, current: "rf-003",
    scenarios: [],
  };

  it("says how far a run has got, and nothing about how it did", () => {
    render(<ProgressLine p={p} />);
    expect(screen.getByText(/2\/3 scenarios · 7 attempt/)).toBeTruthy();
    expect(screen.getByText("rf-003")).toBeTruthy();
    expect(screen.queryByText(/pass|fail|safe/i)).toBeNull();
  });

  it("names the finishing stage", () => {
    render(<ProgressLine p={{ ...p, stage: "finishing", current: null, scenariosDone: 3 }} />);
    expect(screen.getByText(/writing the gate, reports and sinks/)).toBeTruthy();
  });
});
