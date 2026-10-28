# SPDX-License-Identifier: Apache-2.0
"""Fail when a commit in a pull request is not signed off (DCO, ADR-0065).

    python3 scripts/check_dco.py BASE HEAD     # checks every commit in BASE..HEAD

Only the pull request's own commits are checked (``git log BASE..HEAD``), never the history
before it. A commit passes when one of its ``Signed-off-by:`` trailers carries its author's
email (compared case-insensitively; the name may be written differently). Skipped:

- merge commits GitHub made (committer ``noreply@github.com``: "Update branch", the merge
  of a pull request). A merge made by hand is checked like any commit.
- nothing else. Dependabot signs off as ``support@github.com`` while authoring as its
  ``users.noreply.github.com`` address, so that pairing is accepted by name (BOT_SIGNOFFS);
  a Dependabot commit without the sign-off still fails.

Exit codes: 0 every commit signed off, 1 some are not, 2 the range could not be read (a bad
ref, not a git repository, or no commits at all: an empty range would pass on nothing).
Standard library only, so CI runs it without installing anything.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 - runs git with a fixed argument list
import sys
from dataclasses import dataclass, field

GITHUB_COMMITTER = "noreply@github.com"
# Bots whose sign-off address differs from their author address: author email -> accepted.
BOT_SIGNOFFS: dict[str, frozenset[str]] = {
    "49699333+dependabot[bot]@users.noreply.github.com": frozenset({"support@github.com"}),
}
_FIELD, _RECORD = "\x1f", "\x1e"
_FORMAT = _FIELD.join(
    [
        "%H",
        "%P",
        "%an",
        "%ae",
        "%ce",
        "%s",
        "%(trailers:key=Signed-off-by,valueonly,separator=%x1d)",
    ]
)
_EMAIL = re.compile(r"<([^<>]+)>\s*$")


class RangeError(Exception):
    """The commit range could not be read."""


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    committer_email: str
    subject: str
    signoffs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_github_merge(self) -> bool:
        return len(self.parents) > 1 and self.committer_email.lower() == GITHUB_COMMITTER


def signoff_email(value: str) -> str | None:
    """The email of one ``Signed-off-by`` value (``Name <email>``), lower-cased."""
    found = _EMAIL.search(value.strip())
    return found.group(1).strip().lower() if found else None


def read_commits(base: str, head: str, cwd: str | None = None) -> list[Commit]:
    """Every commit in ``base..head``, newest first."""
    for ref in (base, head):
        if not ref or ref.startswith("-"):
            raise RangeError(f"not a commit: {ref!r}")
    git = shutil.which("git")
    if git is None:
        raise RangeError("git is not installed")
    try:
        # git, fixed arguments; the refs are checked above and passed before `--`.
        out = subprocess.run(  # noqa: S603  # nosec B603
            [git, "log", f"--format={_FORMAT}{_RECORD}", f"{base}..{head}", "--"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError as exc:
        raise RangeError(f"could not run git: {exc}") from exc
    if out.returncode != 0:
        raise RangeError(out.stderr.strip() or f"git log exited {out.returncode}")
    commits = []
    for record in out.stdout.split(_RECORD):
        record = record.strip("\n")
        if not record:
            continue
        sha, parents, name, email, committer, subject, trailers = record.split(_FIELD)
        commits.append(
            Commit(
                sha=sha,
                parents=tuple(parents.split()),
                author_name=name,
                author_email=email,
                committer_email=committer,
                subject=subject,
                signoffs=tuple(t.strip() for t in trailers.split("\x1d") if t.strip()),
            )
        )
    return commits


def problem(commit: Commit) -> str | None:
    """Why this commit fails the DCO check, or None when it passes or is skipped."""
    if commit.is_github_merge:
        return None
    author = commit.author_email.lower()
    accepted = {author} | BOT_SIGNOFFS.get(author, frozenset())
    emails = {e for e in map(signoff_email, commit.signoffs) if e}
    if emails & accepted:
        return None
    who = f"{commit.author_name} <{commit.author_email}>"
    if not commit.signoffs:
        return f"{commit.sha[:12]} {commit.subject!r}: no Signed-off-by line (author {who})"
    return (
        f"{commit.sha[:12]} {commit.subject!r}: signed off by {', '.join(commit.signoffs)}, "
        f"not by its author {who}"
    )


def main(argv: list[str] | None = None, cwd: str | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: check_dco.py BASE HEAD", file=sys.stderr)
        return 2
    base, head = args
    try:
        commits = read_commits(base, head, cwd)
    except RangeError as exc:
        print(f"DCO: cannot read {base}..{head}: {exc}", file=sys.stderr)
        return 2
    if not commits:
        print(f"DCO: no commits in {base}..{head}; nothing to check is not a pass", file=sys.stderr)
        return 2
    problems = [p for p in map(problem, commits) if p]
    skipped = sum(c.is_github_merge for c in commits)
    if problems:
        print(
            f"DCO: {len(problems)} of {len(commits)} commits are not signed off:", file=sys.stderr
        )
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(
            "Sign off with `git commit -s` (an earlier commit: `git rebase --signoff "
            f"{base}`, then force-push the branch). See CONTRIBUTING.md, DCO.",
            file=sys.stderr,
        )
        return 1
    note = f" ({skipped} GitHub merge commit(s) skipped)" if skipped else ""
    print(f"DCO: all {len(commits)} commits in {base}..{head} are signed off{note}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
