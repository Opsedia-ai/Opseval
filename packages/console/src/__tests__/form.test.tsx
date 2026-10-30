// The guided scenario form (ADR-0048) decides nothing: its choices come from the server, the
// YAML it saves is the server's text byte for byte, and Save is only ever enabled by the
// loader's verdict on exactly what is in the form now.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../lib/api";
import type { Draft, Drafted, ScenarioForm as Form } from "../lib/types";
import ScenarioForm, { parseParam } from "../pages/ScenarioForm";

vi.mock("../lib/api", async (original) => {
  const real = await original<typeof import("../lib/api")>();
  return { ...real, api: { ...real.api, scenarioForm: vi.fn(), draftScenario: vi.fn(), saveNewScenario: vi.fn() } };
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
  stopAt: [{ value: "auto", label: "When the goal is reached", help: null }],
  answerActions: [
    { value: "approve", label: "approves", modes: ["scripted", "simulated"] },
    { value: "reject", label: "rejects", modes: ["scripted", "simulated"] },
    { value: "script", label: "decides from what is proposed", modes: ["simulated"] },
  ],
  defaultChecks: ["conversation.no_errors", "conversation.agent_replied", "output.terminal_state"],
  readOnly: false,
  ...over,
});
const verdict = (ok: boolean, yaml = "# from the server\nid: question-001\n"): Drafted => ({
  yaml, family: "uncategorised", fileName: "question-001.yaml", defaultChecks: ["conversation.no_errors"],
  check: { ok, id: ok ? "question-001" : null, title: ok ? "Answers" : null, problems: ok ? [] : [{ path: "(new)", field: "id", message: "bad id" }], warnings: [], expanded: [] },
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

beforeEach(() => { vi.mocked(api.draftScenario).mockReset(); vi.mocked(api.saveNewScenario).mockReset(); });

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
    vi.mocked(api.saveNewScenario).mockResolvedValue("f/question-001.yaml");
    fireEvent.change(screen.getByLabelText("Id"), { target: { value: "question-002" } });
    expect(save().disabled).toBe(true); // the old verdict judged other text
    await waitFor(() => expect(save().disabled).toBe(false));
    expect(screen.getByLabelText("Generated YAML").textContent).toBe(yaml);
    expect(vi.mocked(api.draftScenario).mock.lastCall?.[0].id).toBe("question-002");

    fireEvent.click(save());
    await screen.findByText("the editor");
    expect(api.saveNewScenario).toHaveBeenCalledWith("uncategorised", "question-001.yaml", yaml);
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

describe("what the form cannot send", () => {
  it("keeps a cleared number as typed and waits, instead of snapping it to 1", async () => {
    vi.mocked(api.scenarioForm).mockResolvedValue(chatbot());
    vi.mocked(api.draftScenario).mockResolvedValue(verdict(true));
    show();
    await screen.findByText("valid");
    const repeats = screen.getByLabelText("Repeats") as HTMLInputElement;
    fireEvent.change(repeats, { target: { value: "" } });
    expect(repeats.value).toBe("");
    expect(save().disabled).toBe(true);
    expect(screen.getByText(/not read as typed: repeats/i)).toBeTruthy();
    fireEvent.change(repeats, { target: { value: "5" } });
    await waitFor(() => expect(vi.mocked(api.draftScenario).mock.lastCall?.[0].repeats).toBe(5));
    await waitFor(() => expect(save().disabled).toBe(false));
  });

  it("names the fields of a request the API refused", async () => {
    vi.mocked(api.scenarioForm).mockResolvedValue(chatbot());
    vi.mocked(api.draftScenario).mockRejectedValue(
      new ApiError(422, "INVALID_REQUEST", "role: too long", [{ path: "request", field: "role", message: "String should have at most 300 characters" }]),
    );
    show();
    await screen.findByText(/cannot be sent as they are/i);
    expect(screen.getByText("role")).toBeTruthy();
    expect(screen.getByText(/at most 300 characters/)).toBeTruthy();
    expect(save().disabled).toBe(true);
  });

  it("offers only the answers the server says work for this kind of customer", async () => {
    const withCheckpoint = chatbot({ profile: { ...chatbot().profile, checkpoints: [{ name: "approval", phase: null, commitGate: false }] } });
    vi.mocked(api.scenarioForm).mockResolvedValue(withCheckpoint);
    vi.mocked(api.draftScenario).mockResolvedValue(verdict(true));
    show();
    const select = await screen.findByLabelText("Answer at approval");
    const offered = () => [...(select as HTMLSelectElement).options].map((o) => o.value);
    expect(offered()).toEqual(["", "approve", "reject"]); // scripted: no LLM to decide
    fireEvent.click(screen.getByLabelText(/a simulated customer/i));
    await waitFor(() => expect(offered()).toEqual(["", "approve", "reject", "script"]));
  });
});

describe("saving a new file", () => {
  it("asks the server where it went, and says so if it cannot find it", async () => {
    const { api: real } = await vi.importActual<typeof import("../lib/api")>("../lib/api");
    const fetched: string[] = [];
    const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }));
    vi.stubGlobal("fetch", (url: string) => {
      fetched.push(url);
      return url.endsWith("/scenarios/import") ? reply({ imported: ["x-001"], problems: [] }) : reply({ file: "flat/x-001.yaml", id: "x-001", text: "", etag: "e" });
    });
    expect(await real.saveNewScenario("flat", "x-001.yaml", "id: x-001")).toBe("flat/x-001.yaml");
    expect(fetched).toEqual(["/api/v1/scenarios/import", "/api/v1/scenarios/x-001/source"]);
    vi.stubGlobal("fetch", (url: string) => (url.endsWith("/scenarios/import") ? reply({ imported: ["x-001"], problems: [] }) : reply({ code: "UNKNOWN_SCENARIO" }, 404)));
    await expect(real.saveNewScenario("flat", "x-001.yaml", "id: x-001")).rejects.toMatchObject({ code: "SAVED_NOT_LISTED" });
    vi.unstubAllGlobals();
  });
});
