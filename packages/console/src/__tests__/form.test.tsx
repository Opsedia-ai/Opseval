// The guided scenario form (ADR-0048) decides nothing: its choices come from the server, the
// YAML it saves is the server's text byte for byte, and Save is only ever enabled by the
// loader's verdict on exactly what is in the form now.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import type { Draft, Drafted, ScenarioForm as Form } from "../lib/types";
import ScenarioForm, { parseParam } from "../pages/ScenarioForm";

vi.mock("../lib/api", async (original) => {
  const real = await original<typeof import("../lib/api")>();
  return { ...real, api: { ...real.api, scenarioForm: vi.fn(), draftScenario: vi.fn(), importScenarios: vi.fn(), scenarioById: vi.fn() } };
});

const DRAFT: Draft = {
  id: "question-001", name: "Answers", goal: "Get an answer.", criticality: "normal", tags: [], role: "customer", language: "en", dialect: null,
  typingNoise: 0, mode: "scripted", scriptedTurns: ["Hi"], openingMessage: null, flow: "", maxTurns: 30, stopAt: "auto", refusal: false,
  expectedOutcome: null, mustCall: [], mustNotCall: [], policy: [], injection: null, useDefaultChecks: true, repeats: 3, family: "uncategorised",
  fileName: null, answers: [], checks: [],
};
const chatbot = (over: Partial<Form> = {}): Form => ({
  profile: { name: "chatbot", phases: [], checkpoints: [], tools: [], traces: false, toolCallsComplete: false },
  languages: [{ code: "en", dialects: [], stable: true }],
  checks: [{ id: "conversation.turn_budget", area: "flow", reads: "frames", question: "Within budget?", params: [{ name: "budget", kind: "integer", required: false, description: "Turns allowed.", choices: null, minimum: 1, maximum: null, default: null }] }],
  templates: [{ id: "question", title: "A question", description: "Scripted.", draft: DRAFT }],
  defaultChecks: ["conversation.no_errors", "conversation.agent_replied", "output.terminal_state"],
  readOnly: false,
  ...over,
});
const verdict = (ok: boolean, yaml = "# from the server\nid: question-001\n"): Drafted => ({
  yaml, family: "uncategorised", fileName: "question-001.yaml", defaultChecks: ["conversation.no_errors"],
  check: { ok, id: ok ? "question-001" : null, title: ok ? "Answers" : null, problems: ok ? [] : [{ path: "(new)", field: "id", message: "bad id" }], warnings: [] },
});

function show() {
  return render(
    <MemoryRouter initialEntries={["/scenarios/new"]}>
      <Routes>
        <Route path="/scenarios/new" element={<ScenarioForm />} />
        <Route path="/scenarios/edit" element={<p>the editor</p>} />
      </Routes>
    </MemoryRouter>,
  );
}
const save = () => screen.getByRole("button", { name: /save/i }) as HTMLButtonElement;

beforeEach(() => { vi.mocked(api.draftScenario).mockReset(); vi.mocked(api.importScenarios).mockReset(); vi.mocked(api.scenarioById).mockReset(); });

describe("typed settings", () => {
  it("are decoded, never judged", () => {
    expect(parseParam("integer", "4")).toEqual({ value: 4 });
    expect(parseParam("integer", "4.5").error).toBeDefined();
    expect(parseParam("number", "")).toEqual({}); // left out: the check's own default applies
    expect(parseParam("boolean", "false")).toEqual({ value: false });
    expect(parseParam("text_list", "a, b\nc")).toEqual({ value: ["a", "b", "c"] });
    expect(parseParam("integer_list", "1000, 1001")).toEqual({ value: [1000, 1001] });
    expect(parseParam("json", '"refund"')).toEqual({ value: "refund" });
    expect(parseParam("json", "null")).toEqual({ value: null });
    // Unquoted text is not silently taken as a string: it is left out and said so.
    expect(parseParam("json", "refund")).toEqual({ error: expect.any(String) as string });
  });
});

describe("the form", () => {
  it("offers only what the profile declares", async () => {
    vi.mocked(api.scenarioForm).mockResolvedValue(chatbot());
    vi.mocked(api.draftScenario).mockResolvedValue(verdict(true));
    show();
    await screen.findByText(/the profile declares no tools/i);
    expect(screen.queryByText(/at each checkpoint/i)).toBeNull();
    expect(screen.queryByRole("option", { name: /irreversible/i })).toBeNull();
  });

  it("enables Save only on the loader's ok, and saves the server's YAML byte for byte", async () => {
    vi.mocked(api.scenarioForm).mockResolvedValue(chatbot());
    vi.mocked(api.draftScenario).mockResolvedValue(verdict(false));
    show();
    await screen.findByText("bad id");
    expect(save().disabled).toBe(true);

    const yaml = "# yaml-language-server: x\nid: question-002\n";
    vi.mocked(api.draftScenario).mockResolvedValue(verdict(true, yaml));
    vi.mocked(api.importScenarios).mockResolvedValue({ imported: ["question-001"] });
    vi.mocked(api.scenarioById).mockResolvedValue({ file: "f/question-001.yaml", id: "question-001", text: yaml, etag: "e" });
    fireEvent.change(screen.getByLabelText("Id"), { target: { value: "question-002" } });
    expect(save().disabled).toBe(true); // the old verdict judged other text
    await waitFor(() => expect(save().disabled).toBe(false));
    expect(screen.getByLabelText("Generated YAML").textContent).toBe(yaml);
    expect(vi.mocked(api.draftScenario).mock.lastCall?.[0].id).toBe("question-002");

    fireEvent.click(save());
    await screen.findByText("the editor");
    expect(api.importScenarios).toHaveBeenCalledWith("uncategorised", [{ name: "question-001.yaml", text: yaml }]);
    expect(api.scenarioById).toHaveBeenCalledWith("question-001");
  });

  it("never saves into a read-only catalogue", async () => {
    vi.mocked(api.scenarioForm).mockResolvedValue(chatbot({ readOnly: true }));
    vi.mocked(api.draftScenario).mockResolvedValue(verdict(true));
    show();
    await screen.findByText(/read-only/i);
    await screen.findByText("valid");
    expect(save().disabled).toBe(true);
  });

  it("starts from the defaults when choosing checks by hand, with each check's settings", async () => {
    vi.mocked(api.scenarioForm).mockResolvedValue(chatbot());
    vi.mocked(api.draftScenario).mockResolvedValue(verdict(true));
    show();
    const defaults = await screen.findByLabelText(/the default checks/i);
    await screen.findByText("valid");
    fireEvent.click(defaults);
    fireEvent.click(screen.getByLabelText("conversation.turn_budget"));
    fireEvent.change(screen.getByLabelText("budget"), { target: { value: "4" } });
    await waitFor(() => {
      const sent = vi.mocked(api.draftScenario).mock.lastCall?.[0];
      expect(sent?.useDefaultChecks).toBe(false);
      // The server's default pack for this draft, then the one picked by hand.
      expect(sent?.checks?.map((c) => c.id)).toEqual(["conversation.no_errors", "conversation.turn_budget"]);
      expect(sent?.checks?.[1].params).toEqual({ budget: 4 });
    });
  });
});
