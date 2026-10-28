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
  key_invalid: "the audio key is not valid",
  extra_missing: "the voice extra is not installed",
  too_large: "too large to play as one call",
};

/** How long a download's blob: URL is kept before it is revoked (the browser has the file by then). */
export const DOWNLOAD_URL_TTL_MS = 60_000;

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

/**
 * Save a blob: URL as a file. A URL made only for the download is revoked after
 * DOWNLOAD_URL_TTL_MS, or when the component goes, whichever comes first (revoking it at
 * once can cancel the download in some browsers; never revoking it keeps decrypted audio
 * reachable for the page's lifetime).
 */
function useDownloads() {
  const pending = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  useEffect(() => () => {
    for (const [u, timer] of pending.current) { clearTimeout(timer); URL.revokeObjectURL(u); }
    pending.current.clear();
  }, []);
  return (u: string, name: string, owned: boolean) => {
    const a = document.createElement("a");
    a.href = u;
    a.download = name;
    a.click();
    if (!owned) return; // a URL a player still uses is revoked with the player
    pending.current.set(u, setTimeout(() => { pending.current.delete(u); URL.revokeObjectURL(u); }, DOWNLOAD_URL_TTL_MS));
  };
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

const asApiError = (e: unknown) => (e instanceof ApiError ? e : new ApiError(0, "AUDIO", String(e)));

/** The bytes of an undecodable agent turn, as the agent sent them: offered, never played. */
function RawBytes({ where, audio }: { where: Where; audio: AudioTurn }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const download = useDownloads();
  const get = async () => {
    setBusy(true);
    setError(null);
    try {
      const blob = await fetchAudio(where.run, where.test, where.index, audio.seq, { raw: true });
      download(URL.createObjectURL(blob), `${where.test}-attempt${where.index}-turn${audio.seq}.raw`, true);
    } catch (e) {
      setError(asApiError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <span data-audio-state="undecodable" className="inline-flex flex-wrap items-center gap-2 text-xs">
      <Pill tone="mute" dashed>{SAID.undecodable}</Pill>
      {audio.note && <span className="opacity-80">{audio.note}</span>}
      <button type="button" onClick={get} disabled={busy} className="rounded-full border border-current px-2 py-0.5 font-semibold" aria-label={`download the bytes of turn ${audio.seq}`}>
        {busy ? "loading…" : "Download the bytes as sent"}
      </button>
      {error && <Failed error={error} />}
    </span>
  );
}

/** The ▶ control of one spoken turn. */
export function TurnAudio({ where, audio }: { where: Where; audio: AudioTurn }) {
  const [url, setUrl] = useBlobUrl();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  if (audio.state === "undecodable") return <RawBytes where={where} audio={audio} />;
  if (audio.state !== "available") return <Unplayable state={audio.state} note={audio.note} />;
  const play = async () => {
    setBusy(true);
    setError(null);
    try {
      setUrl(URL.createObjectURL(await fetchAudio(where.run, where.test, where.index, audio.seq)));
    } catch (e) {
      setError(asApiError(e));
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
  const download = useDownloads();
  const name = `${where.test}-attempt${where.index}-call.wav`;
  const run = async (then: (u: string) => void) => {
    setBusy(true);
    setError(null);
    try {
      then(URL.createObjectURL(await fetchAudio(where.run, where.test, where.index, "call")));
    } catch (e) {
      setError(asApiError(e));
    } finally {
      setBusy(false);
    }
  };
  // The call already loaded for the player is saved as it is: never fetched (and decrypted) twice.
  const save = () => (url ? download(url, name, false) : run((u) => download(u, name, true)));
  return (
    <div data-call-state={audio.callState} className="rounded-2xl border border-line bg-white px-4 py-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <b>Recorded call</b>
        <span className="text-muted-ink">{audio.turns.length} spoken turn(s)</span>
        {audio.callState === "available" ? (
          <>
            {!url && <Button type="button" onClick={() => run(setUrl)} disabled={busy}>▶ Play whole call</Button>}
            <Button type="button" variant="ghost" onClick={save} disabled={busy}>Download WAV</Button>
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
