# SPDX-License-Identifier: Apache-2.0
"""The versioned catalogue in Postgres (ADR-0047), the way Langfuse versions prompts.

- **Push** a catalogue folder (``profile.yaml``, ``suites/``, ``scenarios/``): every file goes
  through the loader's own checks first, and one problem refuses the whole push. A file that
  changed becomes a new numbered version; an unchanged one adds nothing.
- **Label** versions (``production``, ``staging``, anything). Pushing with a label moves it to
  every pushed item's current version, and takes it off items the folder no longer has, so a
  label always names one consistent catalogue. Every push does the same with ``latest``, which
  only Opseval sets: it is always the catalogue as last pushed, so a scenario deleted from the
  folder never comes back with it.
- **Pull** a label back out as a folder, which ``opseval run`` and ``opseval validate`` take
  exactly as they take any other: the loader, its checks and its fail-closed rules are the same
  code, so the database adds storage and history, never a second way to read a scenario.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import ValidationError
from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from opseval.catalogue import load_catalogue, load_profile, suite_files
from opseval.db.schema import (
    CatalogueItem,
    CatalogueLabel,
    CatalogueVersion,
    Organization,
    Project,
)
from opseval.errors import ConfigError
from opseval.models.scenario import Suite

LATEST = "latest"
"""The catalogue as last pushed. Every push moves it, nobody else can, so it is never stale."""
DEFAULT_LABEL = "production"
"""What a pull or a run fetches when it names no label, as in Langfuse."""
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,35}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DEFAULT_ORG = "default"
MAX_FILE_BYTES = 256 * 1024
"""A scenario is a page of YAML; anything near this is a mistake or an attack."""


@dataclass(frozen=True)
class CatalogueFile:
    """One file of a catalogue folder, checked and ready to store."""

    kind: str
    name: str
    path: str
    source: str
    content: dict[str, Any]

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(f"{self.path}\0{self.source}".encode()).hexdigest()


@dataclass
class PushReport:
    added: list[tuple[str, str, int]] = field(default_factory=list)
    """(kind, name, version) of each new version."""
    unchanged: list[tuple[str, str, int]] = field(default_factory=list)
    label: str | None = None
    unlabelled: list[tuple[str, str]] = field(default_factory=list)
    """Items that lost the label because the pushed folder no longer has them."""


@dataclass(frozen=True)
class ItemSummary:
    kind: str
    name: str
    latest: int
    path: str
    labels: dict[str, int]


@dataclass(frozen=True)
class VersionInfo:
    version: int
    path: str
    created_at: datetime
    created_by: str | None
    message: str | None
    labels: list[str]


# --- reading a folder -------------------------------------------------------------------


def check_label(label: str) -> str:
    """A label a person may set. ``latest`` is Opseval's own (every push moves it)."""
    if label == LATEST:
        raise ConfigError("'latest' is kept by Opseval itself (the last push); pick another")
    if not LABEL_RE.match(label):
        raise ConfigError(
            f"label {label!r}: use lowercase letters, digits, '.', '_' or '-' (at most 36)"
        )
    return label


def _refuse_aliases(text: str, where: str) -> None:
    """YAML anchors and aliases are refused: a few hundred bytes of nested aliases expand to
    gigabytes once stored, while ``validate`` sees only the small file."""
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                raise ConfigError(
                    f"{where}: YAML anchors and aliases are not allowed in a catalogue file"
                )
    except yaml.YAMLError as exc:
        raise ConfigError(f"{where}: invalid YAML: {exc}") from exc


def _files(root: Path) -> list[tuple[str, Path]]:
    profile = root / "profile.yaml"
    if not profile.is_file():
        raise ConfigError(
            f"{root}: no profile.yaml; a catalogue folder holds profile.yaml, suites/ and "
            "scenarios/ (as `opseval init` writes it)"
        )
    found = [("profile", profile)]
    found += [("suite", p) for p in sorted((root / "suites").glob("*.yaml"))]
    found += [("scenario", p) for p in sorted((root / "scenarios").rglob("*.yaml"))]
    for _kind, path in found:
        if not path.resolve().is_relative_to(root):
            raise ConfigError(f"{path}: a link to a file outside the catalogue folder")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ConfigError(f"{path}: larger than {MAX_FILE_BYTES // 1024} KB")
    return found


def read_folder(
    root: Path, *, known_scorers: Iterable[str], known_invariants: set[str]
) -> list[CatalogueFile]:
    """Every file of a catalogue folder, through exactly the checks ``opseval validate``
    applies. Any problem anywhere refuses the whole folder: a push is all or nothing."""
    root = root.resolve()
    if not root.is_dir():
        raise ConfigError(f"{root}: not a folder")
    found = _files(root)
    profile = load_profile(root / "profile.yaml")
    known = list(known_scorers)
    problems: list[str] = []

    def check(path: Path) -> set[str]:
        """Load ``path`` as the loader would; return the scenario files it covered."""
        try:
            cat, _ = load_catalogue(
                path, profile, known_scorers=known, known_invariants=known_invariants
            )
        except ConfigError as exc:
            problems.append(str(exc))
            return set()
        problems.extend(str(p) for p in cat.problems)
        return set(cat.sources.values()) | {p.path for p in cat.problems}

    # A scenario a suite includes is checked through that suite, with its defaults and
    # overrides applied, exactly as it runs; one no suite includes is checked on its own.
    stored = {str(path.resolve()) for _kind, path in found}
    covered: set[str] = set()
    for kind, path in found:
        if kind != "suite":
            continue
        check(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            selected = {str(f) for f in suite_files(path, Suite.model_validate(raw))}
        except ValidationError:
            continue  # the loader reported it above
        # A file the suite runs must be a file the push stores, or the pulled suite would run
        # fewer scenarios than validate saw, and nothing would say so.
        missing = sorted(selected - stored)
        if missing:
            listed = ", ".join(Path(m).relative_to(root).as_posix() for m in missing)
            problems.append(
                f"{path.relative_to(root).as_posix()}: includes files a push does not store "
                f"({listed}); keep scenarios under scenarios/ with a .yaml name"
            )
        covered |= selected
    for kind, path in found:
        if kind == "scenario" and str(path.resolve()) not in covered:
            check(path)
    if problems:
        listed = "\n".join(f"  - {p}" for p in problems)
        raise ConfigError(f"the catalogue has problems, so nothing was pushed:\n{listed}")

    out: list[CatalogueFile] = []
    seen: dict[tuple[str, str], str] = {}
    for kind, path in found:
        # Where the file sits in the folder, not where a link points: that is where the
        # suite's globs look for it, and where a pull must put it back.
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        _refuse_aliases(text, rel)
        data = yaml.safe_load(text)
        if not isinstance(data, dict):  # pragma: no cover - the loader refused it already
            raise ConfigError(f"{rel}: must contain a mapping")
        if kind == "scenario":
            name = str(data["id"])
        elif kind == "suite":
            try:
                name = Suite.model_validate(data).name
            except ValidationError as exc:  # pragma: no cover - the loader refused it already
                raise ConfigError(f"{rel}: invalid suite: {exc}") from exc
        else:
            name = profile.name
        if (kind, name) in seen:
            raise ConfigError(f"{rel}: {kind} {name!r} is also defined in {seen[(kind, name)]}")
        seen[(kind, name)] = rel
        # JSON can hold every value the loader accepted; dates become ISO strings.
        content = json.loads(json.dumps(data, default=str))
        out.append(CatalogueFile(kind=kind, name=name, path=rel, source=text, content=content))
    return out


# --- projects ---------------------------------------------------------------------------


def parse_project(ref: str) -> tuple[str, str]:
    """``org/project``, or just ``project`` in the ``default`` organisation."""
    org, _, project = ref.rpartition("/")
    org = org or DEFAULT_ORG
    for slug in (org, project):
        if not SLUG_RE.match(slug):
            raise ConfigError(
                f"project {ref!r}: use org/project, each lowercase letters, digits and '-'"
            )
    return org, project


async def ensure_project(session: AsyncSession, ref: str, *, name: str | None = None) -> Project:
    """Create the organisation and project if they do not exist (headless initialisation)."""
    org_slug, project_slug = parse_project(ref)
    org = await session.scalar(select(Organization).where(Organization.slug == org_slug))
    if org is None:
        org = Organization(slug=org_slug, name=org_slug)
        session.add(org)
        await session.flush()
    project = await session.scalar(
        select(Project).where(Project.organization_id == org.id, Project.slug == project_slug)
    )
    if project is None:
        project = Project(organization_id=org.id, slug=project_slug, name=name or project_slug)
        session.add(project)
        await session.flush()
    return project


async def get_project(session: AsyncSession, ref: str) -> Project:
    org_slug, project_slug = parse_project(ref)
    project = await session.scalar(
        select(Project)
        .join(Organization, Organization.id == Project.organization_id)
        .where(Organization.slug == org_slug, Project.slug == project_slug)
    )
    if project is None:
        raise ConfigError(
            f"no project {org_slug}/{project_slug}; create it with "
            f"`opseval db init --project {org_slug}/{project_slug}`"
        )
    return project


# --- writing ----------------------------------------------------------------------------


async def _lock(session: AsyncSession, project: Project, *, write: bool) -> None:
    """Serialise catalogue changes per project, and let reads see one whole push.

    A push (or a label move) holds the project row ``FOR UPDATE`` until it commits; a pull
    holds it ``FOR SHARE``. So a pull waits for a push in progress, then no push can start
    until the pull has read everything: it never mixes two pushes into a catalogue nobody
    pushed, which a series of reads under READ COMMITTED otherwise could.
    """
    clause = select(Project.id).where(Project.id == project.id)
    await session.execute(clause.with_for_update(read=not write))


async def _latest_versions(
    session: AsyncSession, item_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, CatalogueVersion]:
    """The newest version of each item, in one query."""
    ids = list(item_ids)
    if not ids:
        return {}
    newest = (
        select(CatalogueVersion.item_id, func.max(CatalogueVersion.version).label("version"))
        .where(CatalogueVersion.item_id.in_(ids))
        .group_by(CatalogueVersion.item_id)
        .subquery()
    )
    rows = await session.scalars(
        select(CatalogueVersion).join(
            newest,
            and_(
                CatalogueVersion.item_id == newest.c.item_id,
                CatalogueVersion.version == newest.c.version,
            ),
        )
    )
    return {v.item_id: v for v in rows}


async def _set_label(
    session: AsyncSession, item_id: uuid.UUID, label: str, version_id: uuid.UUID
) -> None:
    stmt = insert(CatalogueLabel).values(item_id=item_id, label=label, version_id=version_id)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[CatalogueLabel.item_id, CatalogueLabel.label],
            set_={"version_id": version_id, "updated_at": func.now()},
        )
    )


async def _move_label(
    session: AsyncSession,
    label: str,
    current: dict[uuid.UUID, uuid.UUID],
    others: list[CatalogueItem],
) -> list[tuple[str, str]]:
    """Point ``label`` at ``current`` (item → version) and take it off every other item."""
    for item_id, version_id in current.items():
        await _set_label(session, item_id, label, version_id)
    gone = [i.id for i in others]
    if not gone:
        return []
    removed = await session.scalars(
        delete(CatalogueLabel)
        .where(CatalogueLabel.item_id.in_(gone), CatalogueLabel.label == label)
        .returning(CatalogueLabel.item_id)
    )
    by_id = {i.id: i for i in others}
    return sorted((by_id[i].kind, by_id[i].name) for i in removed)


async def push(
    session: AsyncSession,
    project: Project,
    files: list[CatalogueFile],
    *,
    label: str | None = None,
    message: str | None = None,
    actor: str | None = None,
) -> PushReport:
    """Store a checked folder as new versions, in the caller's transaction."""
    if label is not None:
        check_label(label)
    await _lock(session, project, write=True)
    items = {
        (i.kind, i.name): i
        for i in await session.scalars(
            select(CatalogueItem).where(CatalogueItem.project_id == project.id)
        )
    }
    latest = await _latest_versions(session, (i.id for i in items.values()))
    report = PushReport(label=label)
    current: dict[uuid.UUID, uuid.UUID] = {}
    for f in files:
        item = items.get((f.kind, f.name))
        if item is None:
            item = CatalogueItem(project_id=project.id, kind=f.kind, name=f.name)
            session.add(item)
            await session.flush()
            items[(f.kind, f.name)] = item
        newest = latest.get(item.id)
        if newest is not None and newest.content_hash == f.content_hash:
            report.unchanged.append((f.kind, f.name, newest.version))
            current[item.id] = newest.id
            continue
        version = CatalogueVersion(
            item_id=item.id,
            version=(newest.version + 1) if newest else 1,
            path=f.path,
            source=f.source,
            content=f.content,
            content_hash=f.content_hash,
            message=message,
            created_by=actor,
        )
        session.add(version)
        await session.flush()
        report.added.append((f.kind, f.name, version.version))
        current[item.id] = version.id
    others = [i for i in items.values() if i.id not in current]
    await _move_label(session, LATEST, current, others)
    if label is not None:
        report.unlabelled = await _move_label(session, label, current, others)
    return report


async def set_label(
    session: AsyncSession, project: Project, kind: str, name: str, version: int, label: str
) -> None:
    """Point ``label`` at one version of one item (promote ``staging`` to ``production``)."""
    check_label(label)
    await _lock(session, project, write=True)
    item = await _item(session, project, kind, name)
    target = await session.scalar(
        select(CatalogueVersion).where(
            CatalogueVersion.item_id == item.id, CatalogueVersion.version == version
        )
    )
    if target is None:
        raise ConfigError(f"{kind} {name!r} has no version {version}")
    await _set_label(session, item.id, label, target.id)


# --- reading ----------------------------------------------------------------------------


async def _item(session: AsyncSession, project: Project, kind: str, name: str) -> CatalogueItem:
    item = await session.scalar(
        select(CatalogueItem).where(
            CatalogueItem.project_id == project.id,
            CatalogueItem.kind == kind,
            CatalogueItem.name == name,
        )
    )
    if item is None:
        raise ConfigError(f"no {kind} {name!r} in this project")
    return item


async def list_items(session: AsyncSession, project: Project) -> list[ItemSummary]:
    items = list(
        await session.scalars(
            select(CatalogueItem)
            .where(CatalogueItem.project_id == project.id)
            .order_by(CatalogueItem.kind, CatalogueItem.name)
        )
    )
    latest = await _latest_versions(session, (i.id for i in items))
    labels: dict[uuid.UUID, dict[str, int]] = {}
    rows = await session.execute(
        select(CatalogueLabel.item_id, CatalogueLabel.label, CatalogueVersion.version)
        .join(CatalogueVersion, CatalogueVersion.id == CatalogueLabel.version_id)
        .join(CatalogueItem, CatalogueItem.id == CatalogueLabel.item_id)
        .where(CatalogueItem.project_id == project.id)
        .order_by(CatalogueLabel.label)
    )
    for item_id, label, version in rows:
        labels.setdefault(item_id, {})[str(label)] = int(version)
    return [
        ItemSummary(i.kind, i.name, latest[i.id].version, latest[i.id].path, labels.get(i.id, {}))
        for i in items
        if i.id in latest
    ]


async def history(
    session: AsyncSession, project: Project, kind: str, name: str
) -> list[VersionInfo]:
    item = await _item(session, project, kind, name)
    labels: dict[uuid.UUID, list[str]] = {}
    for label, version_id in await session.execute(
        select(CatalogueLabel.label, CatalogueLabel.version_id).where(
            CatalogueLabel.item_id == item.id
        )
    ):
        labels.setdefault(version_id, []).append(str(label))
    versions = await session.scalars(
        select(CatalogueVersion)
        .where(CatalogueVersion.item_id == item.id)
        .order_by(CatalogueVersion.version.desc())
    )
    return [
        VersionInfo(v.version, v.path, v.created_at, v.created_by, v.message, _ordered(labels, v))
        for v in versions
    ]


def _ordered(labels: dict[uuid.UUID, list[str]], version: CatalogueVersion) -> list[str]:
    names = labels.get(version.id, [])
    return sorted(n for n in names if n != LATEST) + ([LATEST] if LATEST in names else [])


async def resolve(
    session: AsyncSession, project: Project, label: str
) -> list[tuple[CatalogueItem, CatalogueVersion]]:
    """Every item's version under ``label``, read as one push left it. Items without the label
    are left out, as Langfuse serves no prompt for a label it does not carry."""
    if label != LATEST:
        check_label(label)
    await _lock(session, project, write=False)
    rows = await session.execute(
        select(CatalogueItem, CatalogueVersion)
        .join(CatalogueLabel, CatalogueLabel.item_id == CatalogueItem.id)
        .join(CatalogueVersion, CatalogueVersion.id == CatalogueLabel.version_id)
        .where(CatalogueItem.project_id == project.id, CatalogueLabel.label == label)
        .order_by(CatalogueItem.kind, CatalogueItem.name)
    )
    return [(item, version) for item, version in rows]


def _safe_target(dest: Path, path: str) -> Path:
    rel = PurePosixPath(path)
    allowed = rel == PurePosixPath("profile.yaml") or (
        len(rel.parts) > 1 and rel.parts[0] in ("suites", "scenarios")
    )
    if rel.is_absolute() or ".." in rel.parts or not allowed or rel.suffix != ".yaml":
        raise ConfigError(f"stored path {path!r} is not a catalogue file path; refusing to write")
    target = (dest / Path(*rel.parts)).resolve()
    if not target.is_relative_to(dest):  # pragma: no cover - the checks above rule it out
        raise ConfigError(f"stored path {path!r} leaves the folder; refusing to write")
    return target


async def materialize(
    session: AsyncSession, project: Project, label: str, dest: Path
) -> list[tuple[CatalogueItem, CatalogueVersion]]:
    """Write the catalogue under ``label`` to ``dest``, a folder that must be empty or new:
    a file left over from something else would run as if it were part of the label."""
    chosen = await resolve(session, project, label)
    if not chosen:
        raise ConfigError(f"nothing in this project carries the label {label!r}")
    dest = dest.resolve()
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise ConfigError(f"{dest}: not an empty folder; pull into a new or empty one")
    targets: dict[Path, str] = {}
    for item, version in chosen:
        target = _safe_target(dest, version.path)
        if target in targets:
            raise ConfigError(
                f"{version.path}: both {targets[target]} and {item.kind} {item.name!r} under "
                f"{label!r} write it; label only one of them"
            )
        targets[target] = f"{item.kind} {item.name!r}"
    for _item, version in chosen:
        target = _safe_target(dest, version.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(version.source, encoding="utf-8")
    return chosen
