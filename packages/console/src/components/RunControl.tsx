import { useState } from "react";
import { Square } from "lucide-react";
import { api, ApiError } from "../lib/api";
import type { Stopped } from "../lib/types";
import { Button, Callout } from "./ui";

/** Why a finished run ended early. A stop is never a pass: what it left out is skipped. */
export function StoppedNotice({ stopped }: { stopped: Stopped | null | undefined }) {
  if (!stopped) return null;
  return (
    <div data-state="stopped-early">
      <Callout tone="warn"><b>Stopped early:</b> {stopped.detail}. Scenarios it did not run, or did not finish, are listed below as skipped, not passed.</Callout>
    </div>);
}

/** Ask a running run to stop before its next attempt (ADR-0035). */
export function CancelRun({ id, requested, onDone }: { id: string; requested: boolean; onDone?: () => void }) {
  const [state, setState] = useState<"idle" | "asking" | "failed">("idle");
  const [error, setError] = useState("");
  const cancel = async () => {
    setState("asking");
    try { await api.cancelRun(id); setState("idle"); onDone?.(); } catch (e) { setError((e as ApiError).message); setState("failed"); }
  };
  if (requested) return <Callout><b>Cancelling.</b> The run stops before its next attempt. What it does not run is reported as cancelled, never as passed.</Callout>;
  return (
    <div className="flex flex-wrap items-center gap-3">
      <Button onClick={() => void cancel()} disabled={state === "asking"}><Square size={14} />Cancel run</Button>
      <span className="text-sm text-muted-ink">Stops before the next attempt; scenarios it does not finish are reported as cancelled.</span>
      {state === "failed" && <span className="text-sm text-deep-coral">{error}</span>}
    </div>);
}
