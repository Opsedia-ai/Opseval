# SPDX-License-Identifier: Apache-2.0
"""The versioned catalogue in Postgres (ADR-0047), the way Langfuse versions prompts.

- **Push** a catalogue folder (``profile.yaml``, ``suites/``, ``scenarios/``, and the
  ``datasets/`` CSV and JSONL files scenarios expand over, ADR-0060): every file goes
  through the loader's own checks first, and one problem refuses the whole push. A file that
  changed becomes a new numbered version; an unchanged one adds nothing.
- **Label** versions (``production``, ``staging``, anything). Pushing with a label moves it to
  every pushed item's current version, and takes it off items the folder no longer has, so a
  label always names one consistent catalogue. Every push does the same with ``latest``, which
  only Opseval sets: it is always the catalogue as last pushed, so a scenario deleted from the
  folder never comes back with it.
- **Tasks** (ADR-0049). A project holds tasks, and each task its own catalogue: one folder is
  one task, so every function that reads or writes items takes a :class:`Task`, never a
  project. Forgetting the filter would leak one task's scenarios into another's run.
- **Pull** a label back out as a folder, which ``opseval run`` and ``opseval validate`` take
  exactly as they take any other: the loader, its checks and its fail-closed rules are the same
  code, so the database adds storage and history, never a second way to read a scenario.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from sqlalchemy import Select, and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opseval.catalogue import KnownScorers, load_catalogue, load_profile, suite_files
from opseval.catalogue_files import StoredFile, write_label
from opseval.datasets import DatasetError, Limits, parse_dataset
from opseval.db.schema import (
    ITEM_NAME_MAX,
    ITEM_PATH_MAX,
    MODALITIES,
    TASK_KINDS,
    CatalogueItem,
    CatalogueLabel,
    CatalogueVersion,
    Organization,
    Project,
    Task,
)
from opseval.errors import ConfigError
from opseval.models.scenario import DATASET_FOLDER, DATASET_SUFFIXES, Suite

LATEST = "latest"
"""The catalogue as last pushed. Every push moves it, nobody else can, so it is never stale."""
DEFAULT_LABEL = "production"
"""What a pull or a run fetches when it names no label, as in Langfuse."""
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,35}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DEFAULT_ORG = "default"
DEFAULT_TASK = "default"
"""The task every project has (the migration made one per project), and the one each command
uses without ``--task``, so everything written for ADR-0047 keeps working."""
DEFAULT_KIND, DEFAULT_MODALITY = "chat", "text"
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
class TaskSummary:
    slug: str
    name: str
    kind: str
    modality: str
    region: str | None
    archived_at: datetime | None
    profile: str | None
    """The linked profile item's name; ``None`` before the first push."""
    default_suite: str | None
    items: int


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


def _files(root: Path, *, max_dataset_bytes: int) -> list[tuple[str, Path]]:
    profile = root / "profile.yaml"
    if not profile.is_file():
        raise ConfigError(
            f"{root}: no profile.yaml; a catalogue folder holds profile.yaml, suites/ and "
            "scenarios/ (as `opseval init` writes it)"
        )
    found = [("profile", profile)]
    found += [("suite", p) for p in sorted((root / "suites").glob("*.yaml"))]
    found += [("scenario", p) for p in sorted((root / "scenarios").rglob("*.yaml"))]
    data = root / DATASET_FOLDER
    found += [
        ("dataset", p)
        for p in sorted(data.rglob("*"))
        if p.suffix in DATASET_SUFFIXES and (p.is_file() or p.is_symlink())
    ]
    for kind, path in found:
        if not path.resolve().is_relative_to(root):
            raise ConfigError(f"{path}: a link to a file outside the catalogue folder")
        if kind == "dataset":
            if not path.resolve().is_relative_to(data.resolve()):
                raise ConfigError(f"{path}: a link to a file outside {DATASET_FOLDER}/")
            if _size(path) > max_dataset_bytes:
                raise ConfigError(
                    f"{path}: larger than {max_dataset_bytes} bytes (OPSEVAL_MAX_DATASET_BYTES)"
                )
        elif _size(path) > MAX_FILE_BYTES:
            raise ConfigError(f"{path}: larger than {MAX_FILE_BYTES // 1024} KB")
    return found


def _unreadable(path: Path, exc: OSError) -> ConfigError:
    why = "a link to nothing" if path.is_symlink() and not path.exists() else exc.strerror
    return ConfigError(f"{path}: cannot be read ({why}); fix or remove it, nothing was pushed")


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:  # a dangling link, or no permission: named, never a traceback
        raise _unreadable(path, exc) from exc


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _unreadable(path, exc) from exc


def _check_lengths(files: list[CatalogueFile]) -> None:
    """Every item's name and path fit their columns, checked for every kind before the
    transaction: a long dataset path or suite name is refused naming the file, never a
    database error halfway through a push."""
    for f in files:
        if len(f.name) > ITEM_NAME_MAX:
            raise ConfigError(
                f"{f.path}: {f.kind} name is {len(f.name)} characters, over the {ITEM_NAME_MAX} "
                "the catalogue stores; shorten it"
            )
        if len(f.path) > ITEM_PATH_MAX:
            raise ConfigError(
                f"{f.path[:80]}…: path is {len(f.path)} characters, over the {ITEM_PATH_MAX} the "
                "catalogue stores; shorten it"
            )


def _dataset_file(root: Path, path: Path) -> CatalogueFile:
    """A dataset as it is stored: its text exactly (line endings too, so a pulled file has the
    digest the pushed one had), named by its path, which is how scenarios name it."""
    rel = path.relative_to(root).as_posix()
    raw = _read_bytes(path)
    try:
        columns, rows = parse_dataset(rel, raw)
    except DatasetError as exc:
        raise ConfigError(str(exc)) from exc
    content = {"format": path.suffix[1:], "columns": list(columns), "rows": len(rows)}
    return CatalogueFile(
        kind="dataset", name=rel, path=rel, source=raw.decode("utf-8"), content=content
    )


def read_folder(
    root: Path,
    *,
    known_scorers: KnownScorers,
    known_invariants: set[str],
    limits: Limits | None = None,
) -> list[CatalogueFile]:
    """Every file of a catalogue folder, through exactly the checks ``opseval validate``
    applies. Any problem anywhere refuses the whole folder: a push is all or nothing.
    ``limits`` (default: from settings) caps dataset files and expansions (ADR-0060)."""
    root = root.resolve()
    if not root.is_dir():
        raise ConfigError(f"{root}: not a folder")
    limits = limits or Limits.from_env()
    found = _files(root, max_dataset_bytes=limits.max_dataset_bytes)
    profile = load_profile(root / "profile.yaml")
    # The registry stays a mapping, so each scorer's params are checked too (ADR-0043).
    known = known_scorers if isinstance(known_scorers, Mapping) else list(known_scorers)
    problems: list[str] = []

    def check(path: Path) -> set[str]:
        """Load ``path`` as the loader would; return the scenario files it covered."""
        try:
            cat, _ = load_catalogue(
                path,
                profile,
                known_scorers=known,
                known_invariants=known_invariants,
                limits=limits,
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
        if kind == "dataset":
            out.append(_dataset_file(root, path))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - the loader read it a moment ago
            raise _unreadable(path, exc) from exc
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
    _check_lengths(out)
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
    """Create the organisation and project if they do not exist (headless initialisation).
    A new project comes with its ``default`` task, as the migration gave every old one."""
    org_slug, project_slug = parse_project(ref)
    # INSERT … ON CONFLICT DO NOTHING, then read: two `db init` at once both succeed, the
    # second finding what the first made, instead of failing on the unique key.
    await session.execute(
        insert(Organization)
        .values(id=uuid.uuid4(), slug=org_slug, name=org_slug)
        .on_conflict_do_nothing(index_elements=[Organization.slug])
    )
    org = await session.scalar(select(Organization).where(Organization.slug == org_slug))
    if org is None:  # pragma: no cover - inserted or found above
        raise ConfigError(f"organisation {org_slug!r} could not be created")
    made = await session.scalar(
        insert(Project)
        .values(
            id=uuid.uuid4(), organization_id=org.id, slug=project_slug, name=name or project_slug
        )
        .on_conflict_do_nothing(index_elements=[Project.organization_id, Project.slug])
        .returning(Project.id)
    )
    project = await session.scalar(
        select(Project).where(Project.organization_id == org.id, Project.slug == project_slug)
    )
    if project is None:  # pragma: no cover - inserted or found above
        raise ConfigError(f"project {org_slug}/{project_slug} could not be created")
    if made is not None:
        await _insert_task(
            session, project.id, DEFAULT_TASK, DEFAULT_TASK, DEFAULT_KIND, DEFAULT_MODALITY, None
        )
    return project


async def _insert_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    slug: str,
    name: str,
    kind: str,
    modality: str,
    region: str | None,
) -> bool:
    """Insert a task unless one with ``slug`` is there; whether this call made it."""
    made = await session.scalar(
        insert(Task)
        .values(
            id=uuid.uuid4(),
            project_id=project_id,
            slug=slug,
            name=name,
            kind=kind,
            modality=modality,
            region=region,
        )
        .on_conflict_do_nothing(index_elements=[Task.project_id, Task.slug])
        .returning(Task.id)
    )
    return made is not None


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


# --- tasks ------------------------------------------------------------------------------


def check_task_slug(slug: str) -> str:
    if not SLUG_RE.match(slug):
        raise ConfigError(f"task {slug!r}: use lowercase letters, digits and '-' (at most 64)")
    return slug


def check_kind(kind: str) -> str:
    if kind not in TASK_KINDS:
        raise ConfigError(f"task kind {kind!r}: use one of {', '.join(TASK_KINDS)}")
    return kind


def check_modality(modality: str) -> str:
    if modality not in MODALITIES:
        raise ConfigError(f"task modality {modality!r}: use one of {', '.join(MODALITIES)}")
    return modality


def _ref(project_ref: str) -> str:
    """``org/project`` as typed in a command, with the default organisation spelt out."""
    return "/".join(parse_project(project_ref))


async def ensure_task(
    session: AsyncSession,
    project_ref: str,
    slug: str,
    *,
    kind: str | None = None,
    modality: str | None = None,
    name: str | None = None,
    region: str | None = None,
) -> tuple[Task, bool]:
    """Create task ``slug`` in an existing project, unless it is there; ``(task, created)``.

    Asking for a kind or modality the existing task does not have is an error, not a silent
    no-op: the caller would believe the task is what they asked for. An archived task that is
    otherwise what was asked for is found, not an error (``archived_at`` tells the caller);
    nothing can be pushed to it until `task unarchive`."""
    check_task_slug(slug)
    for value, check in ((kind, check_kind), (modality, check_modality)):
        if value is not None:
            check(value)
    project = await get_project(session, project_ref)
    # `--region ""` means no region, as in `task update`; compared as stored.
    wanted_region = region or None
    created = await _insert_task(
        session,
        project.id,
        slug,
        name or slug,
        kind or DEFAULT_KIND,
        modality or DEFAULT_MODALITY,
        wanted_region,
    )
    task = await session.scalar(
        select(Task)
        .where(Task.project_id == project.id, Task.slug == slug)
        .execution_options(populate_existing=True)
    )
    if task is None:  # pragma: no cover - inserted or found above
        raise ConfigError(f"task {slug!r} could not be created in {_ref(project_ref)}")
    if created:
        return task, True
    asked: list[tuple[str, str | None, str | None]] = [
        (field, have, want)
        for field, have, want in (("kind", task.kind, kind), ("modality", task.modality, modality))
        if want is not None
    ]
    if region is not None:
        asked.append(("region", task.region, wanted_region))
    differs = [f"{field} is {have!r}, not {want!r}" for field, have, want in asked if want != have]
    if differs:
        raise ConfigError(
            f"task {slug!r} already exists in {_ref(project_ref)} and its "
            f"{'; '.join(differs)}; change it with "
            f"`opseval task update {slug} --project {_ref(project_ref)}`"
        )
    return task, False


async def get_task(session: AsyncSession, project_ref: str, slug: str = DEFAULT_TASK) -> Task:
    """A task by project and slug. One that does not exist is an error naming both, never an
    implicit create: a typo in ``--task`` must not start an empty catalogue."""
    check_task_slug(slug)
    project = await get_project(session, project_ref)
    task = await session.scalar(
        select(Task).where(Task.project_id == project.id, Task.slug == slug)
    )
    if task is None:
        ref = _ref(project_ref)
        raise ConfigError(
            f"no task {slug!r} in project {ref}; create it with "
            f"`opseval db init --project {ref} --task {slug}`"
        )
    return task


async def list_tasks(
    session: AsyncSession, project_ref: str, *, archived: bool = False
) -> list[TaskSummary]:
    """A project's tasks by slug; archived ones only when asked for."""
    project = await get_project(session, project_ref)
    query = select(Task).where(Task.project_id == project.id).order_by(Task.slug)
    if not archived:
        query = query.where(Task.archived_at.is_(None))
    tasks = list(await session.scalars(query))
    linked = {t.profile_item_id for t in tasks} | {t.default_suite_item_id for t in tasks}
    # The keys make every linked item one of its own task's, so the id is enough.
    names: dict[uuid.UUID | None, str] = {
        item_id: name
        for item_id, name in await session.execute(
            select(CatalogueItem.id, CatalogueItem.name).where(
                CatalogueItem.project_id == project.id,
                CatalogueItem.id.in_([i for i in linked if i]),
            )
        )
    }
    counts = {
        task_id: int(n)
        for task_id, n in await session.execute(
            select(CatalogueItem.task_id, func.count())
            .where(CatalogueItem.project_id == project.id)
            .group_by(CatalogueItem.task_id)
        )
    }
    return [
        TaskSummary(
            slug=t.slug,
            name=t.name,
            kind=t.kind,
            modality=t.modality,
            region=t.region,
            archived_at=t.archived_at,
            profile=names.get(t.profile_item_id),
            default_suite=names.get(t.default_suite_item_id),
            items=counts.get(t.id, 0),
        )
        for t in tasks
    ]


async def update_task(
    session: AsyncSession,
    task: Task,
    *,
    kind: str | None = None,
    modality: str | None = None,
    name: str | None = None,
    region: str | None = None,
    default_suite: str | None = None,
) -> Task:
    """Change what an operator may change: kind, modality, name, region, the default suite
    (a suite of this task, by name). An archived task can be changed too: this is the task's
    description, not its catalogue, which stays closed to pushes and label moves."""
    await session.execute(_lock_clause(task, write=True))
    if kind is not None:
        task.kind = check_kind(kind)
    if modality is not None:
        task.modality = check_modality(modality)
    if name is not None:
        task.name = name
    if region is not None:
        task.region = region or None
    if default_suite is not None:
        task.default_suite_item_id = (await _item(session, task, "suite", default_suite)).id
    await _flush_links(session, task)
    return task


async def archive_task(session: AsyncSession, task: Task, *, archived: bool = True) -> Task:
    """Hide a task, or bring it back. Nothing is deleted: its versions, labels and (later)
    runs stay, and it can still be listed and pulled. An archived task takes no push and no
    label move, so nothing changes under a task nobody is looking at."""
    await session.execute(_lock_clause(task, write=True))
    task.archived_at = datetime.now(UTC) if archived else None
    await session.flush()
    return task


# --- writing ----------------------------------------------------------------------------


async def _lock(session: AsyncSession, task: Task, *, write: bool) -> None:
    """Serialise catalogue changes per task, and let reads see one whole push.

    A push, a label move or a task update holds the task row ``FOR NO KEY UPDATE`` until it
    commits; a pull holds it ``FOR SHARE``. The two conflict, and ``NO KEY`` leaves out only
    the key-share locks that inserting a row referencing the task takes, which a push does
    itself. So a pull waits for a push in progress, then no push can start until the
    pull has read everything: it never mixes two pushes into a catalogue nobody pushed, which
    a series of reads under READ COMMITTED otherwise could. Two tasks of one project never
    wait for each other.

    A write reads ``archived_at`` under the lock, so an archive that commits first is seen.
    """
    archived_at = await session.scalar(_lock_clause(task, write=write))
    if write and archived_at is not None:
        ref = await _task_project(session, task)
        raise ConfigError(
            f"task {task.slug!r} is archived; nothing in it can be pushed, labelled or "
            f"changed (`opseval task unarchive {task.slug} --project {ref}` brings it back)"
        )


def _lock_clause(task: Task, *, write: bool) -> Select[datetime | None]:
    """The task row lock (see :func:`_lock`), reading ``archived_at``."""
    clause = select(Task.archived_at).where(Task.id == task.id)
    return clause.with_for_update(key_share=True) if write else clause.with_for_update(read=True)


async def _task_project(session: AsyncSession, task: Task) -> str:
    """``org/project`` of a task, for a message the operator can paste."""
    row = (
        await session.execute(
            select(Organization.slug, Project.slug)
            .join(Project, Project.organization_id == Organization.id)
            .where(Project.id == task.project_id)
        )
    ).one()
    return f"{row[0]}/{row[1]}"


LINK_RULES = (
    "fk_tasks_profile_item_id_catalogue_items",
    "fk_tasks_default_suite_item_id_catalogue_items",
    "ck_tasks_profile_item_kind",
    "ck_tasks_default_suite_item_kind",
)
"""What the database calls the rule that a task links only its own items, of the right kind."""


async def _flush_links(session: AsyncSession, task: Task) -> None:
    """Write a task's changed links. The callers only ever pick the task's own items of the
    right kind; should one not, the database refuses it, and this names the rule."""
    slug = task.slug  # read now: a failed flush expires the row
    try:
        await session.flush()
    except IntegrityError as exc:
        rule = next((r for r in LINK_RULES if r in str(exc.orig)), None)
        if rule is None:
            raise
        raise ConfigError(
            f"task {slug!r} may link only its own profile and suite, of those kinds "
            f"(the database refused it: {rule})"
        ) from exc


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
    task: Task,
    files: list[CatalogueFile],
    *,
    label: str | None = None,
    message: str | None = None,
    actor: str | None = None,
) -> PushReport:
    """Store a checked folder as new versions of ``task``'s catalogue, in the caller's
    transaction, and link the folder's profile as the task's profile."""
    if label is not None:
        check_label(label)
    await _lock(session, task, write=True)
    items = {
        (i.kind, i.name): i
        for i in await session.scalars(
            select(CatalogueItem).where(CatalogueItem.task_id == task.id)
        )
    }
    latest = await _latest_versions(session, (i.id for i in items.values()))
    report = PushReport(label=label)
    current: dict[uuid.UUID, uuid.UUID] = {}
    for f in files:
        item = items.get((f.kind, f.name))
        if item is None:
            item = CatalogueItem(
                project_id=task.project_id, task_id=task.id, kind=f.kind, name=f.name
            )
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
    profiles = [items[(f.kind, f.name)] for f in files if f.kind == "profile"]
    if profiles:  # read_folder always yields exactly one
        task.profile_item_id = profiles[0].id
    await _flush_links(session, task)
    others = [i for i in items.values() if i.id not in current]
    await _move_label(session, LATEST, current, others)
    if label is not None:
        report.unlabelled = await _move_label(session, label, current, others)
    return report


async def set_label(
    session: AsyncSession, task: Task, kind: str, name: str, version: int, label: str
) -> None:
    """Point ``label`` at one version of one item (promote ``staging`` to ``production``)."""
    check_label(label)
    await _lock(session, task, write=True)
    item = await _item(session, task, kind, name)
    target = await session.scalar(
        select(CatalogueVersion).where(
            CatalogueVersion.item_id == item.id, CatalogueVersion.version == version
        )
    )
    if target is None:
        raise ConfigError(f"{kind} {name!r} has no version {version}")
    await _set_label(session, item.id, label, target.id)


# --- reading ----------------------------------------------------------------------------


async def _item(session: AsyncSession, task: Task, kind: str, name: str) -> CatalogueItem:
    item = await session.scalar(
        select(CatalogueItem).where(
            CatalogueItem.task_id == task.id,
            CatalogueItem.kind == kind,
            CatalogueItem.name == name,
        )
    )
    if item is None:
        raise ConfigError(f"no {kind} {name!r} in task {task.slug!r}")
    return item


async def list_items(session: AsyncSession, task: Task) -> list[ItemSummary]:
    items = list(
        await session.scalars(
            select(CatalogueItem)
            .where(CatalogueItem.task_id == task.id)
            .order_by(CatalogueItem.kind, CatalogueItem.name)
        )
    )
    latest = await _latest_versions(session, (i.id for i in items))
    labels: dict[uuid.UUID, dict[str, int]] = {}
    rows = await session.execute(
        select(CatalogueLabel.item_id, CatalogueLabel.label, CatalogueVersion.version)
        .join(CatalogueVersion, CatalogueVersion.id == CatalogueLabel.version_id)
        .join(CatalogueItem, CatalogueItem.id == CatalogueLabel.item_id)
        .where(CatalogueItem.task_id == task.id)
        .order_by(CatalogueLabel.label)
    )
    for item_id, label, version in rows:
        labels.setdefault(item_id, {})[str(label)] = int(version)
    return [
        ItemSummary(i.kind, i.name, latest[i.id].version, latest[i.id].path, labels.get(i.id, {}))
        for i in items
        if i.id in latest
    ]


async def history(session: AsyncSession, task: Task, kind: str, name: str) -> list[VersionInfo]:
    item = await _item(session, task, kind, name)
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
    session: AsyncSession, task: Task, label: str
) -> list[tuple[CatalogueItem, CatalogueVersion]]:
    """Every item's version under ``label``, read as one push left it. Items without the label
    are left out, as Langfuse serves no prompt for a label it does not carry."""
    if label != LATEST:
        check_label(label)
    await _lock(session, task, write=False)
    rows = await session.execute(
        select(CatalogueItem, CatalogueVersion)
        .join(CatalogueLabel, CatalogueLabel.item_id == CatalogueItem.id)
        .join(CatalogueVersion, CatalogueVersion.id == CatalogueLabel.version_id)
        .where(CatalogueItem.task_id == task.id, CatalogueLabel.label == label)
        .order_by(CatalogueItem.kind, CatalogueItem.name)
    )
    return [(item, version) for item, version in rows]


async def materialize(
    session: AsyncSession, task: Task, label: str, dest: Path
) -> list[tuple[CatalogueItem, CatalogueVersion]]:
    """Write the catalogue under ``label`` to ``dest``, a folder that must be empty or new:
    a file left over from something else would run as if it were part of the label. The
    rules are :func:`opseval.catalogue_files.write_label`'s, shared with ``run --server``."""
    chosen = await resolve(session, task, label)
    write_label(
        dest,
        [
            StoredFile(item.kind, item.name, version.path, version.source)
            for item, version in chosen
        ],
        label,
        f"task {task.slug!r}",
    )
    return chosen
