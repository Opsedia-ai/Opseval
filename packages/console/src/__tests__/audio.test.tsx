// Listening back to a voice attempt (ADR-0053, V0d): the server says what each turn's audio
// is; the console plays it from a blob: URL fetched with the token, revokes that URL, and
// says in words why a turn cannot be played, never drawing a broken player.
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CallAudio, DOWNLOAD_URL_TTL_MS, TurnAudio } from "../components/AudioPlayer";
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
  fetchMock = vi.fn(async () => new Response("RIFF", { status: 200, headers: { "content-type": "audio/wav" } }));
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
      ["missing", "audio file missing"],
      ["key_invalid", "the audio key is not valid"],
      ["extra_missing", "the voice extra is not installed"],
      ["too_large", "too large to play as one call"],
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

  it("downloads the call already playing without fetching it again", async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const { container, unmount } = render(<CallAudio where={where} audio={call("available")} />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "▶ Play whole call" })));
    await waitFor(() => expect(container.querySelector("audio")).not.toBeNull());
    expect((fetchMock.mock.calls[0] as unknown as [string])[0]).toBe("/api/v1/runs/run-1/attempts/v-001/0/audio/call");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Download WAV" })));
    expect(click).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1); // the loaded blob, not a second decrypt
    expect(created).toEqual(["blob:test/0"]);
    expect(revoked).toEqual([]); // still the player's
    unmount();
    expect(revoked).toEqual(["blob:test/0"]);
    click.mockRestore();
  });

  it("a download-only URL is revoked after a delay, not at once, or on unmount", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
      const first = render(<CallAudio where={where} audio={call("available")} />);
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Download WAV" })));
      await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
      expect(revoked).toEqual([]); // revoking at once can cancel the download
      await act(async () => { vi.advanceTimersByTime(DOWNLOAD_URL_TTL_MS); });
      expect(revoked).toEqual(["blob:test/0"]);
      first.unmount();
      const second = render(<CallAudio where={where} audio={call("available")} />);
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Download WAV" })));
      await waitFor(() => expect(click).toHaveBeenCalledTimes(2));
      second.unmount();
      expect(revoked).toEqual(["blob:test/0", "blob:test/1"]); // gone with the page
      click.mockRestore();
    } finally {
      vi.useRealTimers();
    }
  });

  it("an unplayable call is said in words, with no buttons", () => {
    const { container } = render(<CallAudio where={where} audio={call("pruned", "audio pruned on 2026-10-01")} />);
    expect(container.querySelector("button")).toBeNull();
    expect(container.textContent).toContain("audio pruned on 2026-10-01");
    expect(container.querySelector("[data-call-state]")?.getAttribute("data-call-state")).toBe("pruned");
  });
});

describe("undecodable agent audio", () => {
  it("is never a player; its bytes can be downloaded as sent", async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const { container } = render(<TurnAudio where={where} audio={turn("undecodable", "not mu-law")} />);
    expect(container.querySelector("audio")).toBeNull();
    expect(container.textContent).toContain("undecodable audio");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "download the bytes of turn 3" })));
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect((fetchMock.mock.calls[0] as unknown as [string])[0]).toBe("/api/v1/runs/run-1/attempts/v-001/0/audio/3?raw=true");
    click.mockRestore();
  });

  it("is the agent's failure in the transcript, not a spoken turn", () => {
    const e: TranscriptEntry = { seq: 4, kind: "undecodable_audio", who: "agent", text: null, phase: null, latencyMs: null, detail: "not mu-law", audio: turn("undecodable") };
    const { container } = render(<Entry e={e} where={where} />);
    const box = container.querySelector("[data-kind=undecodable_audio]");
    expect(box?.getAttribute("data-tone")).toBe("fail");
    expect(box?.textContent).toContain("undecodable audio from the agent: not mu-law");
    expect(container.querySelector("[data-state=unheard]")).toBeNull();
  });
});

describe("audio errors share the API's error path", () => {
  it("falls back to the detail string when there is no message", async () => {
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ detail: "no run 'x'" }), { status: 404 }));
    const { container } = render(<TurnAudio where={where} audio={turn("available")} />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "play turn 3" })));
    await waitFor(() => expect(container.textContent).toContain("HTTP_404"));
    expect(container.textContent).toContain("no run 'x'");
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
