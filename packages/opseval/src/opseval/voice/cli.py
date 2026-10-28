# SPDX-License-Identifier: Apache-2.0
"""``opseval audio``: list, export and listen back to a voice attempt (ADR-0053, V0d).

Exit codes: 0 done · 3 anything that stopped it (no such run or attempt, audio pruned or
hashes only, no key or the wrong key, a file that does not authenticate). Listening back
never judges anything, so it never exits 1 or 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Optional

import typer

from opseval.config import Settings
from opseval.errors import ConfigError
from opseval.models.record import RunRecord
from opseval.storage import FileStore
from opseval.voice.crypto import AudioKey, key_id, load_key, new_key, parse_key
from opseval.voice.playback import find, read_bytes, turns, whole_call

EXIT_HARNESS = 3  # verdicts.EXIT_HARNESS, without importing the scoring stack

audio_app = typer.Typer(
    no_args_is_help=True,
    help="Listen back to a voice run: list its recorded turns, export a turn or the whole "
    "call as WAV (decrypted with OPSEVAL_AUDIO_ENCRYPTION_KEY).",
)


def _fail(message: str) -> typer.Exit:
    typer.secho(f"error: {message}", fg="red", err=True)
    return typer.Exit(EXIT_HARNESS)


def _records(run_dir: Path, test: str | None, attempt: int | None) -> list[RunRecord]:
    if not (run_dir / "manifest.json").is_file():
        raise _fail(f"{run_dir} is not a run folder (no manifest.json)")
    store = FileStore(run_dir.parent)
    try:
        if test is not None and attempt is not None:
            one = store.load_record(run_dir.name, test, attempt)
            if one is None:
                raise _fail(f"no record of {test} attempt {attempt} in {run_dir}")
            return [one]
        found = store.load_records(run_dir.name)
    except (OSError, ValueError, ConfigError) as exc:
        raise _fail(f"cannot read the records in {run_dir}: {exc}") from exc
    return [
        r
        for r in found
        if (test is None or r.test_id == test) and (attempt is None or r.attempt_index == attempt)
    ]


def _key() -> AudioKey | None:
    secret = Settings().audio_encryption_key
    try:
        return load_key(secret.get_secret_value() if secret is not None else None)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


def _seconds(ms: float) -> str:
    return f"{ms / 1000:.3f}s"


@audio_app.command("list")
def list_audio(
    run_dir: Annotated[Path, typer.Argument(help="The run folder (reports/<run id>).")],
    test: Annotated[Optional[str], typer.Option("--test", help="One scenario.")] = None,  # noqa: UP045
    attempt: Annotated[Optional[int], typer.Option("--attempt", min=0)] = None,  # noqa: UP045
) -> None:
    """Every recorded turn: speaker, start and end on the attempt's clock, duration, and
    whether it can be played (encrypted, plain, pruned, hashes only, undecodable)."""
    records = _records(run_dir, test, attempt)
    shown = 0
    for record in records:
        found = turns(run_dir, record)
        if not found:
            continue
        shown += 1
        typer.echo(f"{record.test_id}  attempt {record.attempt_index}")
        typer.echo("  seq  speaker  start      end        duration  audio")
        for turn in found:
            if turn.state == "available":
                what = f"encrypted (key {turn.ref.key_id})" if turn.encrypted else "plain WAV"
            else:
                what = turn.note or turn.state
            typer.echo(
                f"  {turn.seq:<4} {turn.ref.speaker:<8} {_seconds(turn.ref.start_ms):<10} "
                f"{_seconds(turn.ref.end_ms):<10} {_seconds(turn.duration_ms):<9} {what}"
            )
    if not shown:
        typer.echo(f"No recorded audio in {run_dir}" + (f" for {test}" if test else "") + ".")


@audio_app.command("export")
def export(
    run_dir: Annotated[Path, typer.Argument(help="The run folder (reports/<run id>).")],
    test: Annotated[str, typer.Option("--test", help="The scenario id.")],
    out: Annotated[Path, typer.Option("--out", help="Where to write the WAV.")],
    attempt: Annotated[int, typer.Option("--attempt", min=0, help="Attempt number.")] = 0,
    turn: Annotated[
        Optional[int],  # noqa: UP045
        typer.Option("--turn", help="One turn, by its seq (`opseval audio list`)."),
    ] = None,
    speaker: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option("--speaker", help="caller or agent: only that side of the call."),
    ] = None,
    mono: Annotated[bool, typer.Option("--mono", help="Mix both sides into one channel.")] = False,
    raw: Annotated[
        bool,
        typer.Option(
            "--raw", help="With --turn: an undecodable turn's bytes as the agent sent them."
        ),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Overwrite --out.")] = False,
) -> None:
    """Decrypt and write a turn, or the whole call, as a WAV you can play.

    Without --turn the whole call is written on the attempt's own clock: every turn starts
    at its real time, with silence between. Stereo by default (left: the caller, right: the
    agent); --mono mixes both, summed and clipped where they overlap; --speaker keeps one
    side (mono). The decrypted audio goes only to --out.
    """
    if speaker not in (None, "caller", "agent"):
        raise _fail("--speaker is caller or agent")
    if out.exists() and not force:
        raise _fail(f"{out} exists; pass --force to overwrite it")
    (record,) = _records(run_dir, test, attempt)
    key = _key()
    try:
        if turn is not None:
            found = find(run_dir, record, turn)
            if speaker is not None and found.ref.speaker != speaker:
                raise _fail(f"turn {turn} is the {found.ref.speaker}'s, not the {speaker}'s")
            data = read_bytes(run_dir, record, found, key, raw=raw)
            what = f"turn {turn} ({found.ref.speaker}, {_seconds(found.duration_ms)})"
        else:
            side: Literal["caller", "agent"] | None = speaker  # type: ignore[assignment]
            call = whole_call(run_dir, record, key, stereo=not mono and side is None, only=side)
            data = call.wav
            layout = "stereo: left caller, right agent" if call.channels == 2 else "mono"
            what = f"the whole call ({layout}, {call.rate} Hz)"
            if call.skipped:
                typer.secho(
                    "note: undecodable agent audio left as silence in turn(s) "
                    + ", ".join(map(str, call.skipped))
                    + " (export each with --turn N --raw)",
                    fg="yellow",
                    err=True,
                )
    except ConfigError as exc:  # AudioUnavailable included
        code = getattr(exc, "code", "AUDIO")
        raise _fail(f"{code}: {exc}") from exc
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    typer.echo(f"Wrote {what} of {record.test_id} attempt {record.attempt_index} to {out}.")


@audio_app.command("keygen")
def keygen() -> None:
    """Print a new OPSEVAL_AUDIO_ENCRYPTION_KEY (32 random bytes, base64). Keep it with
    your other secrets: without it, recorded audio cannot be played back."""
    key = new_key()
    typer.echo(key)
    typer.echo(
        f"# key id {key_id(parse_key(key))}; set OPSEVAL_AUDIO_ENCRYPTION_KEY to the line above",
        err=True,
    )


__all__ = ["audio_app"]
