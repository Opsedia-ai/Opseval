// Listening back to a voice attempt (ADR-0053, V0d): the server says what each turn's audio
// is; the console plays it from a blob: URL fetched with the token, revokes that URL, and
// says in words why a turn cannot be played, never drawing a broken player.
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CallAudio, TurnAudio } from "../components/AudioPlayer";
import { Entry } from "../components/Transcript";
import type { AttemptAudio, AudioTurn, TranscriptEntry } from "../lib/types";

// The API module adopts the token when it is first imported, before any test runs.
vi.hoisted(() => localStorage.setItem("opseval.token", "tok"));

const where = { run: "run-1", test: "v-001", index: 0 };
const turn = (state: AudioTurn["state"], note: string | null = null): AudioTurn => ({
  seq: 3, speaker: "agent", state, startMs: 500, durationMs: 1500, encrypted: true, note,
});

let created: string[];
let revoked: string[];
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  created = [];
  revoked = [];
  fetchMock = vi.fn(async () => new Response(new Blob(["RIFF"]), { status: 200, headers: { "content-type": "audio/wav" } }));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("URL", Object.assign(Object.create(URL), {
    createObjectURL: () => { const u = `blob:test/${created.length}`; created.push(u); return u; },
    revokeObjectURL: (u: string) => { revoked.push(u); },
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe("a spoken turn's player", () => {
  it("fetches with the token, plays from a blob URL and revokes it when unmounted", async () => {
    const { container, unmount } = render(<TurnAudio where={where} audio={turn("available")} />);
    expect(container.querySelector("audio")).toBeNull(); // nothing fetched until asked
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "play turn 3" })));
    await waitFor(() => expect(container.querySelector("audio")).not.toBeNull());
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("/api/v1/runs/run-1/attempts/v-001/0/audio/3");
    expect((init.headers as Record<string, string>).authorization).toBe("Bearer tok");
    expect(container.querySelector("audio")?.getAttribute("src")).toBe("blob:test/0");
    expect(revoked).toEqual([]);
    unmount();
    expect(revoked).toEqual(["blob:test/0"]);
  });

  it("says why a turn cannot be played, and never draws a player for it", () => {
    for (const [state, words] of [
      ["pruned", "audio pruned"],
      ["hashes_only", "only hashes kept"],
      ["key_missing", "no key to decrypt"],
      ["key_mismatch", "encrypted with another key"],
      ["undecodable", "undecodable audio"],
      ["missing", "audio file missing"],
    ] as const) {
      const { container, unmount } = render(<TurnAudio where={where} audio={turn(state, `why: ${state}`)} />);
      expect(container.querySelector("audio")).toBeNull();
      expect(container.querySelector("button")).toBeNull();
      expect(container.querySelector("[data-audio-state]")?.getAttribute("data-audio-state")).toBe(state);
      expect(container.textContent).toContain(words);
      expect(container.textContent).toContain(`why: ${state}`);
      unmount();
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("an error from the server is said with its code, not as a silent player", async () => {
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ code: "AUDIO_TAMPERED", message: "does not authenticate" }), { status: 422 }));
    const { container } = render(<TurnAudio where={where} audio={turn("available")} />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "play turn 3" })));
    await waitFor(() => expect(container.textContent).toContain("AUDIO_TAMPERED"));
    expect(container.querySelector("audio")).toBeNull();
    expect(created).toEqual([]);
  });
});

describe("the whole call", () => {
  const call = (callState: AttemptAudio["callState"], callNote: string | null = null): AttemptAudio => ({
    turns: [turn("available")], callState, callNote, skipped: [],
  });

  it("plays and downloads the call; each blob URL is revoked", async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const { container, unmount } = render(<CallAudio where={where} audio={call("available")} />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "▶ Play whole call" })));
    await waitFor(() => expect(container.querySelector("audio")).not.toBeNull());
    expect((fetchMock.mock.calls[0] as unknown as [string])[0]).toBe("/api/v1/runs/run-1/attempts/v-001/0/audio/call");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Download WAV" })));
    await waitFor(() => expect(click).toHaveBeenCalled());
    await waitFor(() => expect(revoked).toContain("blob:test/1")); // the download's URL
    unmount();
    expect(revoked).toContain("blob:test/0"); // the player's URL
    click.mockRestore();
  });

  it("an unplayable call is said in words, with no buttons", () => {
    const { container } = render(<CallAudio where={where} audio={call("pruned", "audio pruned on 2026-10-01")} />);
    expect(container.querySelector("button")).toBeNull();
    expect(container.textContent).toContain("audio pruned on 2026-10-01");
    expect(container.querySelector("[data-call-state]")?.getAttribute("data-call-state")).toBe("pruned");
  });
});

describe("a spoken turn in the transcript", () => {
  it("is shown even when nothing was heard in it, with its player", () => {
    const e: TranscriptEntry = { seq: 3, kind: "message", who: "agent", text: null, phase: null, latencyMs: null, detail: null, audio: turn("available") };
    const { container } = render(<Entry e={e} where={where} />);
    expect(container.querySelector("[data-state=unheard]")).not.toBeNull();
    expect(screen.getByRole("button", { name: "play turn 3" })).toBeTruthy();
  });
});
