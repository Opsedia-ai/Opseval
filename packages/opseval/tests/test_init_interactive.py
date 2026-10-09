# SPDX-License-Identifier: Apache-2.0
"""`opseval init` asks what it needs in a terminal, takes flags anywhere, never prompts in CI,
and always leaves a catalogue that validates (FR-DX-02)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opseval.catalogue import load_profile
from opseval.cli import app

runner = CliRunner()


def init(*args: str, input: str | None = None) -> tuple[int, str]:
    res = runner.invoke(app, ["init", *args], input=input)
    return res.exit_code, res.output


class TestAsking:
    def test_the_answers_go_where_they_are_used(self, tmp_path: Path) -> None:
        dest = tmp_path / "evals"
        answers = "openai-chat\nhttps://bot.acme.example/v1\nAcme bot\ngpt-4o\n"
        code, out = init(str(dest), "--interactive", input=answers)
        assert code == 0, out
        profile = load_profile(dest / "profile.yaml")
        assert (profile.name, profile.sut_model) == ("Acme bot", "gpt-4o")
        env = (dest / ".env.example").read_text("utf-8")
        assert "OPSEVAL_ALLOWED_HOSTS=bot.acme.example\n" in env
        assert "--target https://bot.acme.example/v1" in (dest / "README.md").read_text("utf-8")
        assert "Valid: 5 scenario(s), 0 problem(s)." in out
        assert "--target https://bot.acme.example/v1" in out

    def test_accepting_every_default_keeps_the_placeholders(self, tmp_path: Path) -> None:
        dest = tmp_path / "evals"
        code, out = init(str(dest), "--interactive", input="\n\n\n\n")
        assert code == 0, out
        profile = load_profile(dest / "profile.yaml")
        assert profile.name == "my-agent" and profile.sut_model is None
        assert "OPSEVAL_ALLOWED_HOSTS=your-agent.example.com" in (dest / ".env.example").read_text()

    def test_flags_answer_without_asking(self, tmp_path: Path) -> None:
        dest = tmp_path / "evals"
        code, out = init(
            str(dest), "--no-interactive", "--target", "https://a.example/v1", "--name", "A"
        )
        assert code == 0, out
        assert "?" not in out.split("created")[0], "nothing was asked"
        assert load_profile(dest / "profile.yaml").name == "A"

    def test_without_a_terminal_it_never_prompts(self, tmp_path: Path) -> None:
        """CliRunner's stdin is not a terminal, as in CI: no prompt, no hang."""
        code, out = init(str(tmp_path / "evals"))
        assert code == 0, out
        assert "Which kind of agent" not in out


class TestRefusing:
    @pytest.mark.parametrize("target", ["ftp://bot.example", "bot.example", "https://"])
    def test_a_target_that_is_not_a_url_is_refused_before_writing(
        self, tmp_path: Path, target: str
    ) -> None:
        dest = tmp_path / "evals"
        code, out = init(str(dest), "--no-interactive", "--target", target)
        assert code == 3 and "not a http or https URL" in out
        assert not dest.exists()

    def test_an_answer_cannot_change_the_yaml_around_it(self, tmp_path: Path) -> None:
        """A colon is fine (quoted); a line break that could add keys is refused."""
        ok = tmp_path / "ok"
        assert init(str(ok), "--no-interactive", "--name", "Acme: bot")[0] == 0
        assert load_profile(ok / "profile.yaml").name == "Acme: bot"
        bad = tmp_path / "bad"
        code, out = init(str(bad), "--no-interactive", "--name", "x\ncommit_gate: none")
        assert code == 3 and not bad.exists()

    def test_the_refund_demo_takes_no_answers(self, tmp_path: Path) -> None:
        code, out = init(
            str(tmp_path / "d"), "--template", "refund-demo", "--target", "https://a.example"
        )
        assert code == 3 and "openai-chat template only" in out
