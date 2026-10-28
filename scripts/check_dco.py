# SPDX-License-Identifier: Apache-2.0
"""Fail when a commit in a pull request is not signed off (DCO, ADR-0065).

    python3 scripts/check_dco.py BASE HEAD     # checks every commit in BASE..HEAD

Only the pull request's own commits are checked (``git log BASE..HEAD``), never the history
before it; CI passes the two parents of the pull request's merge ref, so commits of `main`
merged into the branch are never in the range. A commit passes when one of its
``Signed-off-by:`` trailers carries its author's email (compared case-insensitively; the name
may be written differently). Skipped:

- a merge commit with no changes of its own: its combined diff against its parents is empty
  **and** its tree is the one git's own merge of those parents produces (``git merge-tree``),
  so it carries nothing to certify ("Update branch", a clean ``git merge main``). Who
  committed it does not matter: a committer address is trivially spoofed. A merge that
  changes anything (a conflict resolution, an extra edit, a side's change dropped) is checked
  like any commit, and so is a merge of more than two parents. When either test cannot be
  run, the merge is checked: fail closed.
- nothing else. Dependabot signs off as ``support@github.com`` while authoring as its
  ``users.noreply.github.com`` address, so that pairing is accepted by name (BOT_SIGNOFFS);
  a Dependabot commit without the sign-off still fails.

git runs with ``log.showSignature`` and colour forced off and a fixed format, so a
contributor's configuration cannot change what is parsed. Records are NUL-separated (a commit
message cannot hold a NUL) and must have exactly the expected fields, so a control character
in a subject or a name is an unreadable range, never a forged record.

Exit codes: 0 every commit signed off, 1 some are not, 2 the range could not be read (a bad
ref, not a git repository, an unparsable record, or no commits at all: an empty range would
pass on nothing). Standard library only, so CI runs it without installing anything.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 - runs git with a fixed argument list
import sys
from dataclasses import dataclass, field

# Bots whose sign-off address differs from their author address: author email -> accepted.
BOT_SIGNOFFS: dict[str, frozenset[str]] = {
    "49699333+dependabot[bot]@users.noreply.github.com": frozenset({"support@github.com"}),
}
# Settings that would change git's output whatever the format asks for.
GIT_CONFIG = ("-c", "log.showSignature=false", "-c", "color.ui=never")
_FIELD, _TRAILER = "\x1f", "\x1d"
_FIELDS = ("%H", "%P", "%an", "%ae", "%s", "%(trailers:key=Signed-off-by,valueonly,separator=%x1d)")
_FORMAT = _FIELD.join(_FIELDS)
_EMAIL = re.compile(r"<([^<>]+)>\s*$")
_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


class RangeError(Exception):
    """The commit range could not be read."""


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    subject: str
    signoffs: tuple[str, ...] = field(default_factory=tuple)
    # Set by read_commits for a merge whose combined diff is empty and whose tree is git's own
    # merge of its parents; never true for an ordinary commit.
    clean_merge: bool = False


def signoff_email(value: str) -> str | None:
    """The email of one ``Signed-off-by`` value (``Name <email>``), lower-cased."""
    found = _EMAIL.search(value.strip())
    return found.group(1).strip().lower() if found else None


def _git(git: str, args: list[str], cwd: str | None) -> subprocess.CompletedProcess[str]:
    try:
        # git, fixed arguments; refs are checked by the caller and never start with `-`.
        return subprocess.run(  # noqa: S603  # nosec B603
            [git, *GIT_CONFIG, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except (OSError, ValueError) as exc:
        raise RangeError(f"could not run git: {exc}") from exc


def is_clean_merge(git: str, sha: str, parents: tuple[str, ...], cwd: str | None) -> bool:
    """True only when the merge ``sha`` carries no change of its own; False when unsure."""
    if len(parents) != 2:
        return False
    diff = _git(git, ["diff-tree", "--cc", "--no-commit-id", "-p", "--no-color", sha], cwd)
    if diff.returncode != 0 or diff.stdout.strip():
        return False
    # An empty combined diff still hides a merge that took one parent's version of a file
    # the other side changed (it drops that change), so the tree must be git's own merge.
    merged = _git(git, ["merge-tree", "--write-tree", *parents], cwd)
    tree = _git(git, ["rev-parse", "--verify", "--quiet", f"{sha}^{{tree}}"], cwd)
    if merged.returncode != 0 or tree.returncode != 0:
        return False  # a conflict, or a git without merge-tree --write-tree (before 2.38)
    lines = merged.stdout.splitlines()
    return bool(lines) and lines[0].strip() == tree.stdout.strip()


def _parse(record: str) -> tuple[str, tuple[str, ...], str, str, str, tuple[str, ...]]:
    parts = record.split(_FIELD)
    if len(parts) != len(_FIELDS):
        raise ValueError(f"expected {len(_FIELDS)} fields, got {len(parts)}")
    sha, parents, name, email, subject, trailers = parts
    if not _SHA.fullmatch(sha) or not all(_SHA.fullmatch(p) for p in parents.split()):
        raise ValueError(f"not a commit id: {sha!r}")
    signoffs = tuple(t.strip() for t in trailers.split(_TRAILER) if t.strip())
    return sha, tuple(parents.split()), name, email, subject, signoffs


def read_commits(base: str, head: str, cwd: str | None = None) -> list[Commit]:
    """Every commit in ``base..head``, newest first."""
    for ref in (base, head):
        if not ref or ref.startswith("-"):
            raise RangeError(f"not a commit: {ref!r}")
    git = shutil.which("git")
    if git is None:
        raise RangeError("git is not installed")
    out = _git(
        git, ["log", "--no-color", "-z", f"--format={_FORMAT}", f"{base}..{head}", "--"], cwd
    )
    if out.returncode != 0:
        raise RangeError(out.stderr.strip() or f"git log exited {out.returncode}")
    commits = []
    for record in out.stdout.split("\0"):
        record = record.strip("\n")
        if not record:
            continue
        try:
            sha, parents, name, email, subject, signoffs = _parse(record)
        except ValueError as exc:
            raise RangeError(f"an unreadable commit record ({exc}): {record[:80]!r}") from exc
        commits.append(
            Commit(
                sha=sha,
                parents=parents,
                author_name=name,
                author_email=email,
                subject=subject,
                signoffs=signoffs,
                clean_merge=len(parents) > 1 and is_clean_merge(git, sha, parents, cwd),
            )
        )
    return commits


def problem(commit: Commit) -> str | None:
    """Why this commit fails the DCO check, or None when it passes or is skipped."""
    if commit.clean_merge:
        return None
    author = commit.author_email.lower()
    accepted = {author} | BOT_SIGNOFFS.get(author, frozenset())
    emails = {e for e in map(signoff_email, commit.signoffs) if e}
    if emails & accepted:
        return None
    who = f"{commit.author_name} <{commit.author_email}>"
    merge = " (a merge with changes of its own)" if len(commit.parents) > 1 else ""
    if not commit.signoffs:
        return f"{commit.sha[:12]} {commit.subject!r}{merge}: no Signed-off-by line (author {who})"
    return (
        f"{commit.sha[:12]} {commit.subject!r}{merge}: signed off by "
        f"{', '.join(commit.signoffs)}, not by its author {who}"
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
    skipped = sum(c.clean_merge for c in commits)
    if problems:
        print(
            f"DCO: {len(problems)} of {len(commits)} commits are not signed off:", file=sys.stderr
        )
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(
            "Sign off with `git commit -s` (an earlier commit: `git rebase --signoff "
            f"{base}`, then force-push the branch; a merge: `git merge --signoff`). "
            "See CONTRIBUTING.md, DCO.",
            file=sys.stderr,
        )
        return 1
    note = f" ({skipped} merge commit(s) with no changes of their own skipped)" if skipped else ""
    print(f"DCO: all {len(commits)} commits in {base}..{head} are signed off{note}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
