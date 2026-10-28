# SPDX-License-Identifier: Apache-2.0
"""Recorded audio at rest and listening back (ADR-0053, V0d): AES-256-GCM per file, the
`opseval audio` commands, the console's audio routes, audio retention and the report."""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import subprocess
import sys
import wave
from array import array
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from opseval.audio import to_wav
from opseval.cli import app as cli
from opseval.config import Settings
from opseval.errors import ConfigError
from opseval.models.profile import FrameKind
from opseval.models.record import (
    FrameRecord,
    RunRecord,
    TerminalState,
    VoiceEventRecord,
    VoiceTimeline,
)
from opseval.reporting import audio_note
from opseval.retention import AUDIO_MARKER, apply_audio, plan_audio
from opseval.server import create_app
from opseval.storage import FileStore
from opseval.voice import AudioRecorder, check_audio_privacy, crypto
from opseval.voice.crypto import AudioKey, AudioUnavailable, FilePlace, key_id, parse_key
from opseval.voice.playback import (
    PRUNED_MARKER,
    barged,
    find,
    read_bytes,
    schedule,
    turn_wav,
    turns,
    whole_call,
)

RATE = 8000
KEY_BYTES = bytes(range(32))
KEY = base64.b64encode(KEY_BYTES).decode()
OTHER = bytes(range(100, 132)).hex()
NOW = datetime.now(UTC)
TOKEN = "t0ken"


def tone(value: int, ms: int) -> bytes:
    """PCM16 of a constant sample: easy to find again, sample-exactly, in a mix."""
    samples = array("h", [value] * (RATE * ms // 1000))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


Speech = tuple[Literal["caller", "agent"], int, int, int]  # speaker, start ms, ms, value


def make_run(
    root: Path,
    spoken: list[Speech],
    *,
    key: str | None = KEY,
    mode: Literal["files", "hashes"] = "files",
    run_id: str = "run-1",
    test_id: str = "v-001",
    days_old: float = 0,
    barge_at: float | None = None,
    barge_detail: str | None = None,
    ended: bool = True,
    undecodable: tuple[float, bytes] | None = None,
) -> tuple[Path, RunRecord]:
    """A finished voice run folder with one attempt, recorded by the real recorder."""
    store = FileStore(root)
    run_dir = store.run_dir(run_id)
    started = NOW - timedelta(days=days_old)
    store.write_manifest(run_id, {"run_id": run_id, "started_at": started.isoformat()})
    recorder = AudioRecorder(
        run_dir, test_id, 0, mode, key=AudioKey.from_text(key) if key else None, run_id=run_id
    )
    frames = []
    for seq, (speaker, start, ms, value) in enumerate(spoken):
        ref = recorder.keep(
            turn=seq + 1, speaker=speaker, pcm16=tone(value, ms), rate=RATE, start_ms=start
        )
        frames.append(
            FrameRecord(
                seq=seq,
                at=started,
                direction="sent" if speaker == "caller" else "received",
                kind=FrameKind.MESSAGE,
                payload={},
                text=f"turn {seq}",
                audio=ref,
            )
        )
    if undecodable is not None:  # agent audio that could not be decoded, as the bridge keeps it
        at, data = undecodable
        ref = recorder.keep_raw(
            turn=len(frames) + 1, data=data, codec="mulaw", rate=RATE, start_ms=at
        )
        frames.append(
            FrameRecord(
                seq=len(frames),
                at=started,
                direction="received",
                kind=FrameKind.ERROR,
                payload={"code": "undecodable_audio", "detail": "not mu-law"},
                audio=ref,
            )
        )
    events = (
        ()
        if barge_at is None
        else (
            VoiceEventRecord(at_ms=barge_at, kind="barge_in", speaker="agent", detail=barge_detail),
        )
    )
    record = RunRecord(
        run_id=run_id,
        test_id=test_id,
        attempt_index=0,
        seed=1,
        target="ws://127.0.0.1:1",
        started_at=started,
        ended_at=started,
        terminal_state=TerminalState.GOAL_REACHED,
        frames=tuple(frames),
        voice=VoiceTimeline(codec="pcm16", rate=RATE, events=events),
    )
    store.save_record(record)
    if ended:
        (run_dir / "result.json").write_text("{}")
    return run_dir, record


def channels(wav: bytes) -> tuple[int, int, list[int]]:
    with wave.open(io.BytesIO(wav)) as w:
        n, rate, data = w.getnchannels(), w.getframerate(), w.readframes(w.getnframes())
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    return n, rate, list(samples)


CALL: list[Speech] = [("caller", 100, 200, 1000), ("agent", 500, 300, -2000)]


# --- the key -------------------------------------------------------------------------------


class TestKey:
    def test_hex_standard_and_url_safe_base64_are_the_same_key(self) -> None:
        url = base64.urlsafe_b64encode(KEY_BYTES).decode()
        assert parse_key(KEY) == parse_key(KEY_BYTES.hex()) == parse_key(url) == KEY_BYTES
        assert parse_key(f"  {KEY}\n") == KEY_BYTES

    def test_unpadded_url_safe_base64_is_accepted(self) -> None:
        import secrets

        made = secrets.token_urlsafe(32)  # 43 characters, no padding
        assert "=" not in made and len(parse_key(made)) == 32
        unpadded = base64.urlsafe_b64encode(KEY_BYTES).decode().rstrip("=")
        assert parse_key(unpadded) == KEY_BYTES
        assert parse_key(KEY.rstrip("=")) == KEY_BYTES

    def test_the_error_names_the_accepted_forms_and_what_was_found(self) -> None:
        with pytest.raises(ConfigError) as short:
            parse_key(base64.urlsafe_b64encode(bytes(range(16))).decode().rstrip("="))
        assert "decodes to 16 bytes, not 32" in str(short.value)
        for form in ("64 hex characters", "URL-safe", "padding", "token_urlsafe(32)"):
            assert form in str(short.value)
        with pytest.raises(ConfigError, match="neither 64 hex characters nor valid base64"):
            parse_key("not a key!")

    @pytest.mark.parametrize(
        "bad",
        [
            "short",
            base64.b64encode(bytes(range(16))).decode(),  # 16 bytes: AES-128, not 256
            bytes(range(33)).hex(),
            "k" * 32,  # a passphrase, not 32 random bytes
            "a" * 64,  # hex, but one byte repeated
            base64.b64encode(b"\x00" * 32).decode(),
            "é" * 44,
        ],
    )
    def test_a_short_weak_or_malformed_key_is_refused(self, bad: str) -> None:
        with pytest.raises(ConfigError, match="OPSEVAL_AUDIO_ENCRYPTION_KEY"):
            parse_key(bad)

    def test_the_key_id_is_a_fingerprint_never_the_key(self) -> None:
        kid = key_id(KEY_BYTES)
        assert len(kid) == 16 and kid == AudioKey(KEY_BYTES).key_id
        assert kid not in KEY_BYTES.hex() and kid != key_id(bytes.fromhex(OTHER))

    def test_keygen_prints_a_key_the_loader_accepts(self) -> None:
        out = CliRunner().invoke(cli, ["audio", "keygen"])
        assert out.exit_code == 0
        assert len(parse_key(out.stdout.strip().splitlines()[0])) == 32

    def test_the_key_is_a_secret_in_the_config_digest(self) -> None:
        from opseval.config import SECRET_FIELDS, config_digest

        assert "audio_encryption_key" in SECRET_FIELDS
        assert KEY not in json.dumps(config_digest(Settings(audio_encryption_key=KEY)))


# --- sealing --------------------------------------------------------------------------------


PLACE = FilePlace("run-1", "v-001", 0, 3, "agent")


class TestSealing:
    def test_round_trip_with_a_fresh_nonce_each_time(self) -> None:
        key = AudioKey(KEY_BYTES)
        a, b = key.seal(b"RIFF audio", PLACE), key.seal(b"RIFF audio", PLACE)
        assert a != b and b"RIFF" not in a
        assert a.startswith(crypto.MAGIC) and a[8] == crypto.VERSION
        assert crypto.file_key_id(a) == key.key_id
        assert key.open(a, PLACE) == key.open(b, PLACE) == b"RIFF audio"

    @pytest.mark.parametrize(
        "where",
        [
            FilePlace("run-2", "v-001", 0, 3, "agent"),
            FilePlace("run-1", "v-002", 0, 3, "agent"),
            FilePlace("run-1", "v-001", 1, 3, "agent"),
            FilePlace("run-1", "v-001", 0, 4, "agent"),
            FilePlace("run-1", "v-001", 0, 3, "caller"),
        ],
    )
    def test_a_file_moved_anywhere_else_does_not_open(self, where: FilePlace) -> None:
        key = AudioKey(KEY_BYTES)
        with pytest.raises(AudioUnavailable) as err:
            key.open(key.seal(b"x" * 100, PLACE), where)
        assert err.value.code == "AUDIO_TAMPERED"

    @pytest.mark.parametrize("at", [9, 20, 30, 40, -1])  # key id, nonce, body, tag
    def test_any_changed_byte_is_refused(self, at: int) -> None:
        key = AudioKey(KEY_BYTES)
        blob = bytearray(key.seal(b"x" * 100, PLACE))
        blob[at] ^= 1
        with pytest.raises(AudioUnavailable) as err:
            key.open(bytes(blob), PLACE)
        assert err.value.code in ("AUDIO_TAMPERED", "AUDIO_KEY_MISMATCH")

    def test_the_wrong_key_is_named_by_both_ids(self) -> None:
        sealed = AudioKey(KEY_BYTES).seal(b"x", PLACE)
        other = AudioKey.from_text(OTHER)
        with pytest.raises(AudioUnavailable) as err:
            other.open(sealed, PLACE)
        assert err.value.code == "AUDIO_KEY_MISMATCH"
        assert key_id(KEY_BYTES) in str(err.value) and other.key_id in str(err.value)

    def test_not_ours_or_a_newer_format_is_refused(self) -> None:
        key = AudioKey(KEY_BYTES)
        with pytest.raises(AudioUnavailable, match="not an Opseval"):
            key.open(b"RIFF....WAVE" * 10, PLACE)
        sealed = bytearray(key.seal(b"x", PLACE))
        sealed[8] = 2
        with pytest.raises(AudioUnavailable, match="version 2"):
            key.open(bytes(sealed), PLACE)


class TestTheExtraIsOptional:
    def test_the_core_never_imports_cryptography(self) -> None:
        code = "import sys, opseval.cli, opseval.server; print('cryptography' in sys.modules)"
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "False"

    def test_without_the_extra_a_key_is_a_config_error_naming_the_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None)
        with pytest.raises(ConfigError, match=r"pip install 'opseval\[voice\]'"):
            AudioKey(KEY_BYTES)
        with pytest.raises(ConfigError, match=r"opseval\[voice\]"):
            check_audio_privacy(Settings(audio_encryption_key=KEY, dotenv=False))
        # Hashes keep no file, so they need no extra (the key is still checked).
        assert (
            check_audio_privacy(
                Settings(record_audio="hashes", audio_encryption_key=KEY, dotenv=False)
            ).mode
            == "hashes"
        )


# --- recording ------------------------------------------------------------------------------


class TestRecording:
    def test_with_the_key_every_file_is_sealed(self, tmp_path: Path) -> None:
        plan = check_audio_privacy(Settings(audio_encryption_key=KEY, dotenv=False))
        assert plan.mode == "files" and plan.key is not None
        rec = AudioRecorder(tmp_path / "run-1", "v-001", 0, plan.mode, key=plan.key)
        ref = rec.keep(turn=1, speaker="caller", pcm16=tone(5, 40), rate=RATE, start_ms=0)
        raw = rec.keep_raw(turn=2, data=b"\xff" * 99, codec="mulaw", rate=RATE, start_ms=50)
        assert ref.path == "audio/v-001/attempt0/001-caller.wav.enc"
        assert raw.path == "audio/v-001/attempt0/002-agent.raw.enc"
        assert (ref.encryption, ref.key_id) == ("aes-256-gcm", plan.key.key_id)
        assert raw.encryption == "aes-256-gcm"
        for path in (tmp_path / "run-1" / "audio").rglob("*.enc"):
            body = path.read_bytes()
            assert (
                body.startswith(crypto.MAGIC) and b"RIFF" not in body and b"\xff" * 99 not in body
            )

    def test_hashes_claim_no_encryption_and_write_nothing(self, tmp_path: Path) -> None:
        key = AudioKey(KEY_BYTES)
        rec = AudioRecorder(tmp_path, "v-001", 0, "hashes", key=key)
        ref = rec.keep(turn=1, speaker="agent", pcm16=tone(5, 40), rate=RATE, start_ms=0)
        assert ref.hashes_only and ref.encryption == "none" and ref.key_id is None
        assert not (tmp_path / "audio").exists()

    def test_the_demos_synthetic_voices_stay_plain_without_a_key(self, tmp_path: Path) -> None:
        plan = check_audio_privacy(
            Settings(audio_encryption_key=None, dotenv=False), synthetic=True
        )
        rec = AudioRecorder(tmp_path, "v", 0, plan.mode, key=plan.key)
        ref = rec.keep(turn=1, speaker="agent", pcm16=tone(5, 40), rate=RATE, start_ms=0)
        assert ref.path is not None and ref.path.endswith(".wav") and ref.encryption == "none"


# --- listening back -------------------------------------------------------------------------


class TestPlayback:
    def test_a_turn_decrypts_to_the_wav_that_was_recorded(self, tmp_path: Path) -> None:
        run_dir, record = make_run(tmp_path, CALL)
        assert [t.state for t in turns(run_dir, record)] == ["available", "available"]
        wav = turn_wav(run_dir, record, 1, AudioKey(KEY_BYTES))
        assert wav == to_wav(tone(-2000, 300), RATE)

    def test_the_whole_call_is_sample_exact_on_the_attempts_clock(self, tmp_path: Path) -> None:
        run_dir, record = make_run(tmp_path, CALL)
        call = whole_call(run_dir, record, AudioKey(KEY_BYTES))
        n, rate, samples = channels(call.wav)
        left, right = samples[0::2], samples[1::2]
        assert (n, rate, call.channels) == (2, RATE, 2)
        # caller 100 ms → sample 800 for 200 ms; agent 500 ms → sample 4000 for 300 ms
        assert len(left) == 6400
        assert left[799] == 0 and left[800] == 1000 and left[2399] == 1000 and left[2400] == 0
        assert right[3999] == 0 and right[4000] == -2000 and right[6399] == -2000
        assert not any(left[3000:]) and not any(right[:4000])

    def test_mono_mixes_and_clips_where_they_overlap(self, tmp_path: Path) -> None:
        spoken: list[Speech] = [("caller", 0, 400, 30000), ("agent", 200, 400, 30000)]
        run_dir, record = make_run(tmp_path, spoken, barge_at=200)
        call = whole_call(run_dir, record, AudioKey(KEY_BYTES), stereo=False)
        n, _, mono = channels(call.wav)
        assert n == 1 and len(mono) == 4800
        assert mono[1599] == 30000 and mono[1600] == 32767 and mono[3199] == 32767
        assert mono[3200] == 30000

    def test_one_side_alone_keeps_the_calls_timing(self, tmp_path: Path) -> None:
        run_dir, record = make_run(tmp_path, CALL)
        call = whole_call(run_dir, record, AudioKey(KEY_BYTES), only="agent")
        n, _, mono = channels(call.wav)
        assert n == 1 and mono[3999] == 0 and mono[4000] == -2000

    def test_audio_sent_faster_than_it_plays_is_laid_end_to_end(self, tmp_path: Path) -> None:
        # The demo and audio-http: the record's starts are closer than the speech is long.
        spoken: list[Speech] = [
            ("caller", 50, 1000, 1),
            ("agent", 80, 500, 2),
            ("caller", 400, 300, 3),
        ]
        _, record = make_run(tmp_path, spoken)
        found = turns(tmp_path / "run-1", record)
        assert schedule(record, found) == {0: 50, 1: 1050, 2: 1550}

    def test_a_recorded_barge_in_is_kept_as_an_overlap(self, tmp_path: Path) -> None:
        spoken: list[Speech] = [("caller", 0, 1000, 1), ("agent", 600, 500, 2)]
        _, record = make_run(tmp_path, spoken, barge_at=600)
        assert schedule(record, turns(tmp_path / "run-1", record)) == {0: 0, 1: 600}

    def test_a_swapped_file_is_refused_not_played_as_another_turn(self, tmp_path: Path) -> None:
        run_dir, record = make_run(tmp_path, CALL)
        folder = run_dir / "audio" / "v-001" / "attempt0"
        a, b = folder / "001-caller.wav.enc", folder / "002-agent.wav.enc"
        a_bytes, b_bytes = a.read_bytes(), b.read_bytes()
        a.write_bytes(b_bytes)
        b.write_bytes(a_bytes)
        for seq in (0, 1):
            with pytest.raises(AudioUnavailable) as err:
                turn_wav(run_dir, record, seq, AudioKey(KEY_BYTES))
            assert err.value.code == "AUDIO_TAMPERED"

    def test_a_plain_file_edited_on_disk_fails_its_hash(self, tmp_path: Path) -> None:
        run_dir, record = make_run(tmp_path, CALL, key=None)
        path = run_dir / "audio" / "v-001" / "attempt0" / "002-agent.wav"
        path.write_bytes(path.read_bytes()[:-2] + b"\x00\x00")
        with pytest.raises(AudioUnavailable, match="hash"):
            turn_wav(run_dir, record, 1, None)
        assert turn_wav(run_dir, record, 0, None)  # the other turn still plays

    def test_states_without_bytes_are_said_never_played(self, tmp_path: Path) -> None:
        run_dir, record = make_run(tmp_path, CALL, mode="hashes")
        assert {t.state for t in turns(run_dir, record)} == {"hashes_only"}
        with pytest.raises(AudioUnavailable) as err:
            whole_call(run_dir, record, AudioKey(KEY_BYTES))
        assert err.value.code == "AUDIO_HASHES_ONLY" and "only hashes were kept" in str(err.value)

    def test_no_key_and_missing_files_are_their_own_codes(self, tmp_path: Path) -> None:
        run_dir, record = make_run(tmp_path, CALL)
        with pytest.raises(AudioUnavailable) as err:
            turn_wav(run_dir, record, 0, None)
        assert err.value.code == "AUDIO_KEY_MISSING"
        (run_dir / "audio" / "v-001" / "attempt0" / "001-caller.wav.enc").unlink()
        assert turns(run_dir, record)[0].state == "missing"
        with pytest.raises(AudioUnavailable) as err:
            whole_call(run_dir, record, AudioKey(KEY_BYTES))
        assert err.value.code == "AUDIO_MISSING", "a call with a turn gone is refused"


# --- the command line -----------------------------------------------------------------------


def run_cli(args: list[str], key: str | None = KEY, monkeypatch: Any = None) -> Any:
    env = {"OPSEVAL_AUDIO_ENCRYPTION_KEY": key or ""}
    return CliRunner().invoke(cli, args, env=env)


class TestCli:
    def test_list_names_every_turn_and_its_state(self, tmp_path: Path) -> None:
        run_dir, _ = make_run(tmp_path, CALL)
        out = run_cli(["audio", "list", str(run_dir)])
        assert out.exit_code == 0, out.output
        assert (
            "v-001  attempt 0" in out.stdout
            and f"encrypted (key {key_id(KEY_BYTES)})" in out.stdout
        )
        assert "0.100s" in out.stdout and "0.300s" in out.stdout

    def test_export_one_turn_and_the_whole_call(self, tmp_path: Path) -> None:
        run_dir, _ = make_run(tmp_path, CALL)
        turn, call, mono = tmp_path / "t.wav", tmp_path / "call.wav", tmp_path / "mono.wav"
        base = ["audio", "export", str(run_dir), "--test", "v-001", "--attempt", "0"]
        assert run_cli([*base, "--turn", "1", "--out", str(turn)]).exit_code == 0
        assert turn.read_bytes() == to_wav(tone(-2000, 300), RATE)
        out = run_cli([*base, "--out", str(call)])
        assert out.exit_code == 0 and "stereo: left caller, right agent" in out.stdout
        assert channels(call.read_bytes())[0] == 2
        assert run_cli([*base, "--mono", "--out", str(mono)]).exit_code == 0
        n, _, samples = channels(mono.read_bytes())
        assert n == 1 and samples[800] == 1000 and samples[4000] == -2000
        again = run_cli([*base, "--out", str(call)])
        assert again.exit_code == 3 and "--force" in again.output

    def test_the_wrong_key_pruned_and_hashes_only_exit_3_and_say_why(self, tmp_path: Path) -> None:
        run_dir, _ = make_run(tmp_path, CALL)
        base = ["audio", "export", str(run_dir), "--test", "v-001", "--out", str(tmp_path / "x")]
        wrong = run_cli(base, key=OTHER)
        assert wrong.exit_code == 3 and "AUDIO_KEY_MISMATCH" in wrong.output
        assert key_id(KEY_BYTES) in wrong.output
        missing = run_cli(base, key=None)
        assert missing.exit_code == 3 and "AUDIO_KEY_MISSING" in missing.output
        apply_audio(
            FileStore(tmp_path),
            plan_audio(FileStore(tmp_path), older_than_days=1, now=NOW + timedelta(days=2)),
            older_than_days=1,
        )
        pruned = run_cli(base)
        assert pruned.exit_code == 3 and "audio pruned on" in pruned.output
        hashes_dir, _ = make_run(tmp_path, CALL, mode="hashes", run_id="run-2")
        hashes = run_cli(
            ["audio", "export", str(hashes_dir), "--test", "v-001", "--out", str(tmp_path / "y")]
        )
        assert hashes.exit_code == 3 and "only hashes were kept" in hashes.output
        assert not (tmp_path / "x").exists() and not (tmp_path / "y").exists()


# --- the console API ------------------------------------------------------------------------


def client(root: Path, key: str | None = KEY) -> TestClient:
    settings = Settings(reports_dir=str(root), audio_encryption_key=key, dotenv=False)
    return TestClient(
        create_app(settings=settings, token=TOKEN),
        base_url="http://127.0.0.1:8000",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


BASE = "/api/v1/runs/run-1/attempts/v-001/0"


class TestApi:
    def test_audio_needs_the_token(self, tmp_path: Path) -> None:
        make_run(tmp_path, CALL)
        bare = TestClient(
            create_app(settings=Settings(reports_dir=str(tmp_path)), token=TOKEN),
            base_url="http://127.0.0.1:8000",
        )
        for path in ("/audio", "/audio/0", "/audio/call"):
            assert bare.get(BASE + path).status_code == 401

    def test_a_turn_and_the_call_are_wav_never_cached(self, tmp_path: Path) -> None:
        run_dir, _ = make_run(tmp_path, CALL)
        before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
        api = client(tmp_path)
        one = api.get(BASE + "/audio/1")
        assert one.status_code == 200 and one.content == to_wav(tone(-2000, 300), RATE)
        call = api.get(BASE + "/audio/call")
        mono = api.get(BASE + "/audio/call?mono=true")
        for r in (one, call, mono):
            assert r.headers["content-type"] == "audio/wav"
            assert r.headers["cache-control"] == "no-store"
            assert r.headers["content-disposition"].startswith("inline;")
        assert channels(call.content)[0] == 2 and channels(mono.content)[0] == 1
        # Decrypted in memory only: nothing new on disk, and nothing on disk is plain audio.
        assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
        assert not any(
            b"RIFF" in p.read_bytes() for p in (run_dir / "audio").rglob("*") if p.is_file()
        )

    def test_the_attempt_says_what_each_turn_can_do(self, tmp_path: Path) -> None:
        make_run(tmp_path, CALL)
        detail = client(tmp_path).get(BASE).json()
        assert detail["audio"]["callState"] == "available" and detail["audio"]["skipped"] == []
        spoken = [e["audio"] for e in detail["entries"] if e["kind"] == "message"]
        assert [(a["seq"], a["speaker"], a["state"], a["encrypted"]) for a in spoken] == [
            (0, "caller", "available", True),
            (1, "agent", "available", True),
        ]
        assert spoken[1]["startMs"] == 500 and spoken[1]["durationMs"] == 300
        listing = client(tmp_path).get(BASE + "/audio").json()
        assert listing == detail["audio"]

    def test_without_the_key_the_server_says_so(self, tmp_path: Path) -> None:
        make_run(tmp_path, CALL)
        api = client(tmp_path, key=None)
        assert {e["audio"]["state"] for e in api.get(BASE).json()["entries"]} == {"key_missing"}
        r = api.get(BASE + "/audio/0")
        assert (r.status_code, r.json()["code"]) == (409, "AUDIO_KEY_MISSING")
        wrong = client(tmp_path, key=OTHER).get(BASE + "/audio/call")
        assert (wrong.status_code, wrong.json()["code"]) == (409, "AUDIO_KEY_MISMATCH")

    def test_pruned_hashes_tampered_and_text_attempts(self, tmp_path: Path) -> None:
        run_dir, _ = make_run(tmp_path, CALL)
        enc = run_dir / "audio" / "v-001" / "attempt0" / "002-agent.wav.enc"
        blob = bytearray(enc.read_bytes())
        blob[-1] ^= 1
        enc.write_bytes(bytes(blob))
        r = client(tmp_path).get(BASE + "/audio/1")
        assert (r.status_code, r.json()["code"]) == (422, "AUDIO_TAMPERED")
        store = FileStore(tmp_path)
        apply_audio(
            store,
            plan_audio(store, older_than_days=1, now=NOW + timedelta(days=2)),
            older_than_days=1,
        )
        r = client(tmp_path).get(BASE + "/audio/0")
        assert (r.status_code, r.json()["code"]) == (410, "AUDIO_PRUNED")
        assert client(tmp_path).get(BASE).json()["audio"]["callState"] == "pruned"
        make_run(tmp_path, CALL, mode="hashes", run_id="run-2")
        r = client(tmp_path).get("/api/v1/runs/run-2/attempts/v-001/0/audio/0")
        assert (r.status_code, r.json()["code"]) == (404, "AUDIO_HASHES_ONLY")
        make_run(tmp_path, [], run_id="run-3")
        assert client(tmp_path).get("/api/v1/runs/run-3/attempts/v-001/0").json()["audio"] is None
        r = client(tmp_path).get("/api/v1/runs/run-3/attempts/v-001/0/audio")
        assert (r.status_code, r.json()["code"]) == (404, "AUDIO_NOT_FOUND")

    def test_a_malformed_server_key_is_said_on_each_audio_request(self, tmp_path: Path) -> None:
        make_run(tmp_path, CALL)
        api = client(tmp_path, key="too-short")
        assert api.get("/api/v1/health").status_code == 200  # the console still starts
        r = api.get(BASE + "/audio/0")
        assert (r.status_code, r.json()["code"]) == (409, "AUDIO_KEY_INVALID")


# --- retention ------------------------------------------------------------------------------


class TestRetention:
    def test_the_marker_names_agree(self) -> None:
        assert AUDIO_MARKER == PRUNED_MARKER

    def test_old_audio_goes_before_the_run_and_reads_as_pruned(self, tmp_path: Path) -> None:
        old_dir, record = make_run(tmp_path, CALL, days_old=40)
        new_dir, _ = make_run(tmp_path, CALL, run_id="run-new", days_old=5)
        live_dir, _ = make_run(tmp_path, CALL, run_id="run-live", days_old=40, ended=False)
        store = FileStore(tmp_path)
        items = plan_audio(store, older_than_days=30)
        assert [(i.run_id, i.files) for i in items] == [("run-1", 2)]
        assert apply_audio(store, items, older_than_days=30).deleted == ["run-1"]
        assert [p.name for p in (old_dir / "audio").rglob("*") if p.is_file()] == [PRUNED_MARKER]
        assert store.load_record("run-1", "v-001", 0) == record  # the record is untouched
        found = turns(old_dir, record)
        assert {t.state for t in found} == {"pruned"}
        assert found[0].note == f"audio pruned on {datetime.now(UTC).date().isoformat()}"
        for kept in (new_dir, live_dir):
            assert len([p for p in (kept / "audio").rglob("*.enc")]) == 2

    def test_prune_is_a_dry_run_without_yes(self, tmp_path: Path) -> None:
        run_dir, _ = make_run(tmp_path, CALL, days_old=40)
        dry = CliRunner().invoke(cli, ["prune", "--reports-dir", str(tmp_path)])
        assert dry.exit_code == 0 and "audio   run-1" in dry.stdout
        assert "the audio of 1 run(s) would be deleted" in dry.stdout
        assert len(list((run_dir / "audio").rglob("*.enc"))) == 2
        done = CliRunner().invoke(cli, ["prune", "--reports-dir", str(tmp_path), "--yes"])
        assert done.exit_code == 0 and "the audio of 1 run(s)" in done.stdout
        assert not list((run_dir / "audio").rglob("*.enc")) and (run_dir / "records").is_dir()
        marker = json.loads((run_dir / "audio" / PRUNED_MARKER).read_text())
        assert marker["files"] == 2 and marker["older_than_days"] == 30

    def test_a_linked_audio_folder_is_never_followed(self, tmp_path: Path) -> None:
        run_dir, _ = make_run(tmp_path, [], days_old=40)
        outside = tmp_path.parent / f"{tmp_path.name}-outside"
        outside.mkdir()
        (outside / "keep.wav").write_bytes(b"x")
        (run_dir / "audio").symlink_to(outside)
        assert plan_audio(FileStore(tmp_path), older_than_days=30) == []
        assert (outside / "keep.wav").exists()


# --- the report -----------------------------------------------------------------------------


class TestReport:
    def test_a_voice_attempt_names_the_exact_export_command(self, tmp_path: Path) -> None:
        run_dir, record = make_run(tmp_path, CALL)
        note = audio_note(record, run_dir)
        assert note is not None and note.turns == 2 and note.state == "encrypted"
        assert note.command == (
            f"opseval audio export {run_dir} --test v-001 --attempt 0 --out v-001-attempt0.wav"
        )

    def test_hashes_and_pruned_have_no_command(self, tmp_path: Path) -> None:
        _, record = make_run(tmp_path, CALL, mode="hashes")
        note = audio_note(record, tmp_path / "run-1")
        assert note is not None and note.command is None and "only hashes" in note.state

    def test_a_text_attempt_says_nothing_about_audio(self, tmp_path: Path) -> None:
        _, record = make_run(tmp_path, [])
        assert audio_note(record, tmp_path / "run-1") is None


# --- end to end: listen back to a failed call -------------------------------------------------


def test_a_failed_demo_call_is_sealed_and_can_be_listened_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's case (D17): an agent failed on a call; play back exactly what it said.
    With a key, the demo's synthetic voices are sealed like a real run's."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPSEVAL_TELEMETRY", "false")
    monkeypatch.setenv("OPSEVAL_AUDIO_ENCRYPTION_KEY", KEY)
    ran = CliRunner().invoke(cli, ["demo", "--voice", "--repeats", "1", "--bug", "garbled"])
    assert ran.exit_code == 1, ran.output  # the agent's failure, unchanged by sealing
    (run_dir,) = (tmp_path / "reports").iterdir()
    files = [p for p in (run_dir / "audio").rglob("*") if p.is_file()]
    assert files and all(p.name.endswith(".enc") for p in files)
    assert all(p.read_bytes().startswith(crypto.MAGIC) for p in files)
    record = FileStore(tmp_path / "reports").load_record(run_dir.name, "vr-001", 0)
    assert record is not None
    agent = [t for t in turns(run_dir, record) if t.ref.speaker == "agent"]
    assert agent and all(t.state == "available" and t.encrypted for t in agent)
    # The unheard turn is in the transcript, so the console can offer to play it.
    unheard = [f for f in record.frames if f.audio is not None and f.direction == "received"]
    assert any(not f.text for f in unheard)
    out = tmp_path / "failed-call.wav"
    exported = CliRunner().invoke(
        cli, ["audio", "export", str(run_dir), "--test", "vr-001", "--out", str(out)]
    )
    assert exported.exit_code == 0, exported.output
    n, rate, samples = channels(out.read_bytes())
    assert (n, rate) == (2, RATE) and any(samples[1::2]), "the agent's side has sound"
    assert "opseval audio export" in (run_dir / "report.html").read_text()


# --- review of #87 ----------------------------------------------------------------------------


class TestUndecodableAudio:
    """Undecodable agent audio is the agent's error, never a turn someone spoke."""

    def test_it_is_its_own_entry_not_a_message(self, tmp_path: Path) -> None:
        from opseval.transcript import transcript

        _, record = make_run(tmp_path, CALL, undecodable=(900, b"\xff" * 80))
        entries = transcript(record)
        assert [(e.seq, e.kind) for e in entries] == [
            (0, "message"),
            (1, "message"),
            (2, "undecodable_audio"),
        ]
        bad = entries[2]
        assert bad.who == "agent" and bad.text is None and bad.detail == "not mu-law"

    def test_only_a_spoken_message_with_no_text_is_unheard(self) -> None:
        from opseval.transcript import transcript

        ref = AudioRecorder(None, "v", 0).keep(
            turn=1, speaker="agent", pcm16=tone(5, 40), rate=RATE, start_ms=0
        )
        now = datetime.now(UTC)
        rec = RunRecord(
            run_id="r",
            test_id="v",
            attempt_index=0,
            seed=1,
            target="ws://127.0.0.1:1",
            started_at=now,
            ended_at=now,
            terminal_state=TerminalState.GOAL_REACHED,
            frames=(
                FrameRecord(
                    seq=0,
                    at=now,
                    direction="received",
                    kind=FrameKind.MESSAGE,
                    payload={},
                    audio=ref,
                ),
                FrameRecord(
                    seq=1, at=now, direction="received", kind=FrameKind.ERROR, payload={}, audio=ref
                ),
            ),
        )
        kinds = [(e.seq, e.kind, e.text) for e in transcript(rec)]
        assert kinds == [(0, "message", None), (1, "undecodable_audio", None)]

    def test_the_console_offers_its_bytes_and_the_report_names_the_command(
        self, tmp_path: Path
    ) -> None:

        run_dir, record = make_run(tmp_path, CALL, undecodable=(900, b"\xff" * 80))
        api = client(tmp_path)
        detail = api.get(BASE).json()
        bad = [e for e in detail["entries"] if e["kind"] == "undecodable_audio"]
        assert len(bad) == 1 and bad[0]["who"] == "agent"
        assert bad[0]["audio"]["state"] == "undecodable" and bad[0]["audio"]["seq"] == 2
        assert not [e for e in detail["entries"] if e["kind"] == "message" and e["seq"] == 2]
        assert detail["audio"]["skipped"] == [2]
        raw = api.get(BASE + "/audio/2?raw=true")
        assert raw.status_code == 200 and raw.content == b"\xff" * 80
        assert raw.headers["content-type"] == "application/octet-stream"
        assert raw.headers["content-disposition"].startswith("attachment;")
        assert api.get(BASE + "/audio/2").json()["code"] == "AUDIO_UNDECODABLE"
        note = audio_note(record, run_dir)
        assert note is not None and note.raw == {
            2: f"opseval audio export {run_dir} --test v-001 --attempt 0 --turn 2 --raw "
            "--out v-001-attempt0-turn2.raw"
        }

    def test_the_report_shows_it_as_the_agents_failure_with_the_command(
        self, tmp_path: Path
    ) -> None:
        from opseval.models.results import (
            AttemptVerdict,
            Gate,
            SuiteResult,
            TestVerdict,
            VerdictStatus,
        )
        from opseval.models.scenario import Criticality
        from opseval.reporting import render_html

        run_dir, record = make_run(tmp_path, CALL, undecodable=(900, b"\xff" * 80))
        result = SuiteResult(
            opseval_version="0",
            run_id="run-1",
            suite="s",
            target="ws://x",
            profile="p",
            started_at=NOW,
            ended_at=NOW,
            tests=[
                TestVerdict(
                    test_id="v-001",
                    name="a call",
                    criticality=Criticality.CRITICAL,
                    status=VerdictStatus.FAILED,
                    pass_rate=0.0,
                    attempts=[
                        AttemptVerdict(
                            attempt_index=0,
                            passed=False,
                            terminal_state=TerminalState.GOAL_REACHED,
                            scores=[],
                            record_path="a0.json",
                        )
                    ],
                )
            ],
            gate=Gate(status="block"),
            exit_code=1,
        )
        html = render_html(result, [record], run_dir=run_dir)
        line = html[html.index("undecodable audio from the agent") :].split("</div>")[0]
        assert "not mu-law" in line and "--turn 2 --raw" in line
        assert html.count("spoken; nothing was heard") == 0


class TestSizeLimits:
    """A record's times and sizes are data: a call too large is refused before any file is
    read or any buffer allocated."""

    def test_a_start_hours_into_the_call_is_refused_before_anything_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.voice import playback

        spoken: list[Speech] = [("caller", 0, 100, 1), ("agent", 3 * 3600 * 1000, 100, 2)]
        run_dir, record = make_run(tmp_path, spoken)

        def never(*_a: Any, **_k: Any) -> bytes:
            raise AssertionError("a file was read for a call that is too large")

        monkeypatch.setattr(playback, "read_bytes", never)
        with pytest.raises(AudioUnavailable) as err:
            whole_call(run_dir, record, AudioKey(KEY_BYTES))
        assert err.value.code == "AUDIO_TOO_LARGE" and "--turn" in str(err.value)
        with pytest.raises(AudioUnavailable) as late:  # past the call's hours, too
            whole_call(
                run_dir,
                record.model_copy(
                    update={
                        "frames": (
                            record.frames[0],
                            record.frames[1].model_copy(
                                update={
                                    "audio": record.frames[1].audio.model_copy(  # type: ignore[union-attr]
                                        update={"start_ms": 9e12, "end_ms": 9e12 + 100}
                                    )
                                }
                            ),
                        )
                    }
                ),
                AudioKey(KEY_BYTES),
            )
        assert late.value.code == "AUDIO_TOO_LARGE"

    def test_the_call_budget_and_the_total_of_the_turns(self, tmp_path: Path) -> None:
        run_dir, record = make_run(tmp_path, CALL)
        whole = whole_call(run_dir, record, AudioKey(KEY_BYTES))
        assert len(whole.wav) == 44 + 6400 * 4
        with pytest.raises(AudioUnavailable) as err:
            whole_call(run_dir, record, AudioKey(KEY_BYTES), max_bytes=6400 * 4)
        assert err.value.code == "AUDIO_TOO_LARGE"
        refs = sum(f.audio.bytes for f in record.frames if f.audio is not None)
        with pytest.raises(AudioUnavailable, match="bytes of audio") as err:
            whole_call(run_dir, record, AudioKey(KEY_BYTES), max_bytes=refs - 1)
        # Mono is half the size of stereo: it fits where stereo does not.
        mono = whole_call(run_dir, record, AudioKey(KEY_BYTES), stereo=False, max_bytes=13_000)
        assert len(mono.wav) == 44 + 6400 * 2

    def test_a_file_larger_than_the_record_says_is_never_read(self, tmp_path: Path) -> None:
        run_dir, record = make_run(tmp_path, CALL)
        path = run_dir / "audio" / "v-001" / "attempt0" / "002-agent.wav.enc"
        path.write_bytes(path.read_bytes() + b"\x00")
        with pytest.raises(AudioUnavailable, match="larger than the record says") as err:
            turn_wav(run_dir, record, 1, AudioKey(KEY_BYTES))
        assert err.value.code == "AUDIO_TAMPERED"

    def test_the_console_says_too_large_and_answers_413(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.voice import playback

        run_dir, _ = make_run(tmp_path, CALL)
        monkeypatch.setattr(playback, "MAX_CALL_BYTES", 1000)
        api = client(tmp_path)
        audio = api.get(BASE).json()["audio"]
        assert audio["callState"] == "too_large" and "turn by turn" in audio["callNote"]
        r = api.get(BASE + "/audio/call")
        assert (r.status_code, r.json()["code"]) == (413, "AUDIO_TOO_LARGE")
        assert api.get(BASE + "/audio/1").status_code == 200  # each turn still plays


class TestServerKeyStates:
    def test_unset_malformed_and_no_extra_are_each_said(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_run(tmp_path, CALL)
        cases: list[tuple[str | None, str, str]] = [
            (None, "key_missing", "AUDIO_KEY_MISSING"),
            ("too-short", "key_invalid", "AUDIO_KEY_INVALID"),
        ]
        for key, state, code in cases:
            api = client(tmp_path, key=key)
            detail = api.get(BASE).json()
            assert {e["audio"]["state"] for e in detail["entries"]} == {state}
            assert detail["audio"]["callState"] == state
            for path in ("/audio/0", "/audio/call"):
                r = api.get(BASE + path)
                assert (r.status_code, r.json()["code"]) == (409, code)
        monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None)
        api = client(tmp_path)
        detail = api.get(BASE).json()
        assert detail["audio"]["callState"] == "extra_missing"
        assert "opseval[voice]" in detail["audio"]["callNote"]
        r = api.get(BASE + "/audio/0")
        assert (r.status_code, r.json()["code"]) == (409, "AUDIO_EXTRA_MISSING")

    def test_plain_files_play_whatever_the_key(self, tmp_path: Path) -> None:
        make_run(tmp_path, CALL, key=None)
        api = client(tmp_path, key="too-short")
        assert api.get(BASE).json()["audio"]["callState"] == "available"
        assert api.get(BASE + "/audio/0").status_code == 200


class TestBargeIn:
    def test_a_barge_in_after_the_last_sparse_chunk_keeps_its_overlap(self, tmp_path: Path) -> None:
        # VAD-gated chunks: 200 ms of audio, but speech "began" at 900 ms on the clock,
        # after the turn's last recorded sample (600 + 200). Containment would miss it.
        spoken: list[Speech] = [("caller", 0, 1000, 1), ("agent", 600, 200, 2)]
        _, record = make_run(tmp_path, spoken, barge_at=900)
        found = turns(tmp_path / "run-1", record)
        assert schedule(record, found) == {0: 0, 1: 600}

    def test_a_tagged_event_names_its_turn_whatever_its_time(self, tmp_path: Path) -> None:
        spoken: list[Speech] = [
            ("caller", 0, 1000, 1),
            ("agent", 600, 500, 2),
            ("caller", 1200, 1000, 3),
            ("agent", 1500, 500, 4),
        ]
        _, record = make_run(
            tmp_path,
            spoken,
            barge_at=99_000,
            barge_detail="agent speech began before the caller finished (turn 4)",
        )
        found = turns(tmp_path / "run-1", record)
        assert barged(record, found) == {3}
        when = schedule(record, found)
        assert when[1] == 1000 and when[3] == when[2] + 300  # turn 2 moved; turn 4 kept

    def test_an_untagged_event_after_the_caller_spoke_again_is_not_the_earlier_turn(
        self, tmp_path: Path
    ) -> None:
        spoken: list[Speech] = [
            ("caller", 0, 500, 1),
            ("agent", 600, 200, 2),
            ("caller", 900, 500, 3),
        ]
        _, record = make_run(tmp_path, spoken, barge_at=1000)
        assert barged(record, turns(tmp_path / "run-1", record)) == set()


class TestPrivateExport:
    base = ["audio", "export"]

    def args(self, run_dir: Path, out: Path, *more: str) -> list[str]:
        return [
            *self.base,
            str(run_dir),
            "--test",
            "v-001",
            "--turn",
            "1",
            "--out",
            str(out),
            *more,
        ]

    def test_the_file_is_the_owners_alone(self, tmp_path: Path) -> None:
        run_dir, _ = make_run(tmp_path, CALL)
        out = tmp_path / "t.wav"
        assert run_cli(self.args(run_dir, out)).exit_code == 0
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
        out.write_bytes(b"old")
        os.chmod(out, 0o644)
        refused = run_cli(self.args(run_dir, out))
        assert refused.exit_code == 3 and "--force" in refused.output and out.read_bytes() == b"old"
        assert run_cli(self.args(run_dir, out, "--force")).exit_code == 0
        assert out.read_bytes() == to_wav(tone(-2000, 300), RATE)
        assert stat.S_IMODE(out.stat().st_mode) == 0o600

    def test_a_link_is_refused_even_dangling_and_with_force(self, tmp_path: Path) -> None:
        run_dir, _ = make_run(tmp_path, CALL)
        target = tmp_path / "elsewhere.wav"
        link = tmp_path / "link.wav"
        link.symlink_to(target)  # dangling
        for more in ((), ("--force",)):
            out = run_cli(self.args(run_dir, link, *more))
            assert out.exit_code == 3 and "symbolic link" in out.output
        assert not target.exists()
        target.write_bytes(b"keep")
        out = run_cli(self.args(run_dir, link, "--force"))
        assert out.exit_code == 3 and target.read_bytes() == b"keep"

    def test_a_link_put_in_place_after_the_check_is_not_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.voice import cli as audio_cli

        target = tmp_path / "elsewhere.wav"
        link = tmp_path / "late.wav"
        checked = audio_cli._check_out

        def then_link(out: Path, *, force: bool) -> None:
            checked(out, force=force)
            if not link.is_symlink():
                link.symlink_to(target)

        monkeypatch.setattr(audio_cli, "_check_out", then_link)
        with pytest.raises(Exception) as err:
            audio_cli._write_private(link, b"secret", force=True)
        assert getattr(err.value, "exit_code", None) == 3
        assert not target.exists()


class TestPruneRace:
    def test_audio_pruned_after_it_was_listed_is_pruned_not_missing(self, tmp_path: Path) -> None:
        run_dir, record = make_run(tmp_path, CALL)
        listed = find(run_dir, record, 0)
        assert listed.state == "available"
        store = FileStore(tmp_path)
        apply_audio(
            store,
            plan_audio(store, older_than_days=1, now=NOW + timedelta(days=2)),
            older_than_days=1,
        )
        with pytest.raises(AudioUnavailable) as err:
            read_bytes(run_dir, record, listed, AudioKey(KEY_BYTES))
        assert err.value.code == "AUDIO_PRUNED" and "audio pruned on" in str(err.value)


class TestOneParser:
    def test_an_unreadable_marker_reads_as_pruned_in_player_and_report(
        self, tmp_path: Path
    ) -> None:
        run_dir, record = make_run(tmp_path, CALL)
        (run_dir / "audio" / PRUNED_MARKER).write_text("{not json")
        said = "audio pruned (marker unreadable)"
        assert {(t.state, t.note) for t in turns(run_dir, record)} == {("pruned", said)}
        note = audio_note(record, run_dir)
        assert note is not None and note.command is None and note.state.startswith(said)

    def test_a_dated_marker_reads_the_same_in_both(self, tmp_path: Path) -> None:
        run_dir, record = make_run(tmp_path, CALL)
        (run_dir / "audio" / PRUNED_MARKER).write_text('{"pruned_at": "2026-10-01T00:00:00+00:00"}')
        assert turns(run_dir, record)[0].note == "audio pruned on 2026-10-01"
        note = audio_note(record, run_dir)
        assert note is not None and note.state.startswith("audio pruned on 2026-10-01")


class TestReadOnce:
    def test_the_attempt_is_read_once_and_its_turns_listed_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.server import audio as audio_api
        from opseval.voice import playback

        make_run(tmp_path, CALL)
        api = client(tmp_path)
        loads: list[str] = []
        listed: list[int] = []
        real_load = FileStore.load_record
        real_turns = playback.turns

        def load(self: FileStore, *a: Any, **k: Any) -> Any:
            loads.append(a[1])
            return real_load(self, *a, **k)

        def count(*a: Any, **k: Any) -> Any:
            listed.append(1)
            return real_turns(*a, **k)

        monkeypatch.setattr(FileStore, "load_record", load)
        monkeypatch.setattr(audio_api, "turns", count)
        monkeypatch.setattr(playback, "turns", count)
        assert api.get(BASE).status_code == 200
        assert loads == ["v-001"] and len(listed) == 1
