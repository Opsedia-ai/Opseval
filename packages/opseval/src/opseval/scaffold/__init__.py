# SPDX-License-Identifier: Apache-2.0
"""``opseval init``: write a starter catalogue that validates out of the box."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

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


def scaffold(dest: Path, template: str = "openai-chat", *, force: bool = False) -> list[Path]:
    """Write the template into ``dest``. Refuses to overwrite files unless ``force``."""
    if template not in TEMPLATES:
        raise ConfigError(f"unknown template {template!r}; choose from {sorted(TEMPLATES)}")
    planned = sorted(_sources(template))
    clashes = [dest / rel for rel, _ in planned if (dest / rel).exists()]
    if clashes and not force:
        names = ", ".join(str(c) for c in clashes[:5])
        raise ConfigError(f"refusing to overwrite existing files ({names}); use --force")
    written = []
    for rel, text in planned:
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
