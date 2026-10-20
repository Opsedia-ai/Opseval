# SPDX-License-Identifier: Apache-2.0
"""``opseval init``: write a starter catalogue that validates out of the box."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlparse

from opseval.errors import ConfigError

TEMPLATES = {
    "openai-chat": "An agent behind an OpenAI-compatible /chat/completions endpoint.",
    "refund-demo": "The WebSocket refund agent from `opseval demo`, as a worked example.",
}

REFUND_DEMO_README = """# Opseval catalogue: refund demo

A worked example with approval checkpoints, a commit gate and tool evidence.
Start the sample agent in one terminal:

```bash
python -m opseval.demo.refund_agent --port 8765            # add --bug skip-approval to break it
```

Then, in another terminal:

```bash
export OPSEVAL_ALLOWED_HOSTS=127.0.0.1
opseval validate suites/full.yaml --profile profile.yaml
opseval run suites/smoke.yaml --profile profile.yaml --target ws://127.0.0.1:8765
```
"""


PLACEHOLDER_URL = "https://your-agent.example.com/v1"
PLACEHOLDER_HOST = "your-agent.example.com"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:/-]{0,79}$")


@dataclass(frozen=True)
class Answers:
    """What ``opseval init`` asked (or was told by flags). ``None`` keeps the placeholder."""

    target: str | None = None
    name: str | None = None
    sut_model: str | None = None

    def check(self, template: str) -> None:
        """Refuse an answer the generated files could not hold, with what to do instead."""
        if self.target is not None:
            # urlparse silently drops tabs and line breaks, so it would pass a target that
            # then writes a new line (a new YAML key) into the files. Refuse them first.
            if any(c.isspace() or not c.isprintable() for c in self.target):
                raise ConfigError(
                    f"target {self.target!r} contains spaces, tabs or line breaks; give one URL"
                )
            url = urlparse(self.target)
            schemes = ("http", "https") if template == "openai-chat" else ("ws", "wss")
            if url.scheme not in schemes or not url.hostname:
                raise ConfigError(
                    f"target {self.target!r} is not a {' or '.join(schemes)} URL with a host; "
                    f"for example {PLACEHOLDER_URL if template == 'openai-chat' else 'wss://agent.example.com/ws'}"
                )
        for label, value in (("name", self.name), ("model", self.sut_model)):
            if value is not None and not NAME.match(value):
                raise ConfigError(
                    f"{label} {value!r} must start with a letter or digit and use only letters, "
                    "digits, spaces and . _ : / - (at most 80 characters)"
                )


def _fill(rel: Path, text: str, answers: Answers) -> str:
    """Put the answers where the template has placeholders. Values go into YAML as JSON
    strings, so no answer can change the structure of the file it lands in."""
    if answers.target is not None:
        host = urlparse(answers.target).hostname or PLACEHOLDER_HOST
        text = text.replace(PLACEHOLDER_URL, answers.target)
        if rel.name == ".env.example":
            text = text.replace(
                f"OPSEVAL_ALLOWED_HOSTS={PLACEHOLDER_HOST}", f"OPSEVAL_ALLOWED_HOSTS={host}"
            )
    if rel.name == "profile.yaml":
        if answers.name is not None:
            text = text.replace("\nname: my-agent\n", f"\nname: {json.dumps(answers.name)}\n", 1)
        if answers.sut_model is not None:
            text = text.replace(
                "# sut_model: gpt-4o", f"sut_model: {json.dumps(answers.sut_model)}", 1
            )
    return text


def _sources(template: str) -> list[tuple[Path, str]]:
    """(relative destination path, text) for every file of ``template``."""
    if template == "refund-demo":
        root = Path(str(files("opseval.demo") / "catalogue"))
        out = [(f.relative_to(root), f.read_text("utf-8")) for f in root.rglob("*.yaml")]
        return [*out, (Path("README.md"), REFUND_DEMO_README)]
    root = Path(str(files("opseval.scaffold") / template))
    out = []
    for f in root.rglob("*"):
        if f.is_file() and f.suffix != ".pyc":
            rel = f.relative_to(root)
            rel = rel.with_name(".env.example") if rel.name == "env.example" else rel
            out.append((rel, f.read_text("utf-8")))
    return out


def scaffold(
    dest: Path,
    template: str = "openai-chat",
    *,
    force: bool = False,
    answers: Answers | None = None,
) -> list[Path]:
    """Write the template into ``dest``. Refuses to overwrite files unless ``force``."""
    if template not in TEMPLATES:
        raise ConfigError(f"unknown template {template!r}; choose from {sorted(TEMPLATES)}")
    answers = answers or Answers()
    answers.check(template)
    planned = sorted((rel, _fill(rel, text, answers)) for rel, text in _sources(template))
    clashes = [dest / rel for rel, _ in planned if (dest / rel).exists()]
    if clashes and not force:
        names = ", ".join(str(c) for c in clashes[:5])
        raise ConfigError(f"refusing to overwrite existing files ({names}); use --force")
    written = []
    for rel, text in planned:
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_with_editor_hint(rel, text), encoding="utf-8")
        written.append(path)
    return written


def _with_editor_hint(rel: Path, text: str) -> str:
    """Prepend the yaml-language-server line so an editor validates as you type.

    Which schema a YAML file follows is only knowable from where it sits, so the
    mapping lives here rather than in the templates: a scaffolded file that pointed at
    the wrong schema would flag valid fields and accept invalid ones.
    """
    from opseval.schemas import editor_hint

    if rel.suffix not in (".yaml", ".yml") or text.lstrip().startswith("# yaml-language-server"):
        return text
    parts = rel.parts
    if "scenarios" in parts:
        name = "scenario"
    elif "suites" in parts:
        name = "suite"
    elif rel.name.startswith("profile"):
        name = "profile"
    elif rel.name.startswith("tenants"):
        name = "tenants"
    else:
        return text
    return f"{editor_hint(name)}\n{text}"
