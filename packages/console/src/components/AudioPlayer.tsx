// Listening back to a voice attempt (ADR-0053, V0d). The server decides what each turn's
// audio is (available, pruned, hashes only, no key here, …); this only says it. A turn that
// cannot be played is said in words, never drawn as a player that fails.
import { useEffect, useRef, useState } from "react";
import { ApiError, fetchAudio } from "../lib/api";
import type { AttemptAudio, AudioTurn } from "../lib/types";
import { Button, Mono, Pill } from "./ui";

export type Where = { run: string; test: string; index: number };

const SAID: Record<AudioTurn["state"], string> = {
  available: "audio",
  pruned: "audio pruned",
  hashes_only: "only hashes kept",
  undecodable: "undecodable audio",
  missing: "audio file missing",
  key_missing: "no key to decrypt",
  key_mismatch: "encrypted with another key",
};

/** A blob: URL for the audio, made on demand and revoked when replaced or unmounted. */
function useBlobUrl() {
  const [url, setUrl] = useState<string | null>(null);
  const current = useRef<string | null>(null);
  const set = (next: string | null) => {
    if (current.current) URL.revokeObjectURL(current.current);
    current.current = next;
    setUrl(next);
  };
  useEffect(() => () => { if (current.current) URL.revokeObjectURL(current.current); }, []);
  return [url, set] as const;
}

function Unplayable({ state, note }: { state: AudioTurn["state"]; note: string | null }) {
  return (
    <span data-audio-state={state} className="inline-flex flex-wrap items-center gap-2 text-xs">
      <Pill tone="mute" dashed>{SAID[state]}</Pill>
      {note && <span className="opacity-80">{note}</span>}
    </span>
  );
}

function Failed({ error }: { error: ApiError }) {
  return <span data-audio-state="error" className="text-xs"><Mono>{error.code}</Mono> {error.message}</span>;
}

/** The ▶ control of one spoken turn. */
export function TurnAudio({ where, audio }: { where: Where; audio: AudioTurn }) {
  const [url, setUrl] = useBlobUrl();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  if (audio.state !== "available") return <Unplayable state={audio.state} note={audio.note} />;
  const play = async () => {
    setBusy(true);
    setError(null);
    try {
      setUrl(URL.createObjectURL(await fetchAudio(where.run, where.test, where.index, audio.seq)));
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(0, "AUDIO", String(e)));
    } finally {
      setBusy(false);
    }
  };
  return (
    <span data-audio-state="available" className="inline-flex flex-wrap items-center gap-2 text-xs">
      {url
        ? <audio controls autoPlay src={url} className="h-8 max-w-full" aria-label={`turn ${audio.seq}, ${audio.speaker}`} />
        : <button type="button" onClick={play} disabled={busy} className="rounded-full border border-current px-2 py-0.5 font-semibold" aria-label={`play turn ${audio.seq}`}>
            ▶ {busy ? "loading…" : `${(audio.durationMs / 1000).toFixed(1)} s`}
          </button>}
      {audio.encrypted && <span className="opacity-70">encrypted at rest</span>}
      {error && <Failed error={error} />}
    </span>
  );
}

/** "Play whole call" and "Download WAV": both sides on the attempt's clock, caller left, agent right. */
export function CallAudio({ where, audio }: { where: Where; audio: AttemptAudio }) {
  const [url, setUrl] = useBlobUrl();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const get = async () => URL.createObjectURL(await fetchAudio(where.run, where.test, where.index, "call"));
  const run = async (then: (u: string) => void) => {
    setBusy(true);
    setError(null);
    try {
      then(await get());
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(0, "AUDIO", String(e)));
    } finally {
      setBusy(false);
    }
  };
  const download = (u: string) => {
    const a = document.createElement("a");
    a.href = u;
    a.download = `${where.test}-attempt${where.index}-call.wav`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(u), 0);
  };
  return (
    <div data-call-state={audio.callState} className="rounded-2xl border border-line bg-white px-4 py-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <b>Recorded call</b>
        <span className="text-muted-ink">{audio.turns.length} spoken turn(s)</span>
        {audio.callState === "available" ? (
          <>
            {!url && <Button type="button" onClick={() => run(setUrl)} disabled={busy}>▶ Play whole call</Button>}
            <Button type="button" variant="ghost" onClick={() => run(download)} disabled={busy}>Download WAV</Button>
          </>
        ) : <Unplayable state={audio.callState} note={audio.callNote} />}
      </div>
      {url && <audio controls autoPlay src={url} className="mt-2 w-full" aria-label="the whole call" />}
      {audio.callState === "available" && <p className="mt-1 text-xs text-muted-ink">Stereo: the caller on the left, the agent on the right, each turn at its time in the call.</p>}
      {audio.skipped.length > 0 && <p className="mt-1 text-xs">Undecodable agent audio is silent in the call: turn(s) <Mono>{audio.skipped.join(", ")}</Mono>.</p>}
      {error && <p className="mt-1"><Failed error={error} /></p>}
    </div>
  );
}
