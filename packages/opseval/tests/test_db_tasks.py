# SPDX-License-Identifier: Apache-2.0
"""Tasks inside a project (ADR-0049): the migration that moves every catalogue into its
project's ``default`` task, each task's own catalogue, the task row lock, archiving and the
``--task`` option on every catalogue command.

Every test needs a database (``OPSEVAL_TEST_DATABASE_URL``, see ``conftest.pg_url``).
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from typer.testing import CliRunner

from opseval.cli import app
from opseval.config import Settings
from opseval.db import migrate
from opseval.db.catalogue_store import (
    DEFAULT_TASK,
    archive_task,
    ensure_project,
    ensure_task,
    get_task,
    history,
    list_items,
    list_tasks,
    materialize,
    push,
    read_folder,
    resolve,
    set_label,
    update_task,
)
from opseval.db.engine import make_engine, sessions
from opseval.db.schema import CatalogueItem, Task
from opseval.demo import catalogue_dir
from opseval.errors import ConfigError
from opseval.scoring import INVARIANTS, REGISTRY

runner = CliRunner()
PROJECT = "acme/support-bot"


def folder(tmp_path: Path, name: str = "cat") -> Path:
    dest = tmp_path / name
    shutil.copytree(catalogue_dir(), dest)
    return dest


def read(path: Path):  # type: ignore[no-untyped-def]
    return read_folder(path, known_scorers=REGISTRY, known_invariants=set(INVARIANTS))


def _engine(pg_url: str) -> AsyncEngine:
    return make_engine(Settings(_env_file=None, allowed_hosts="127.0.0.1", database_url=pg_url))


@pytest.fixture
async def engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = _engine(pg_url)
    await migrate.upgrade(engine)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _voice(s: AsyncSession) -> Task:
    await ensure_project(s, PROJECT)
    task, _ = await ensure_task(s, PROJECT, "voice", kind="voice", modality="audio_stream")
    return task


# --- the migration ----------------------------------------------------------------------


async def _seed_0001(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Three projects at revision 0001: one with a whole catalogue (one profile), one with two
    profiles, one empty. Rows written as 0001 knew them, with SQL, not the current models."""
    ids = {k: uuid.uuid4() for k in ("org", "one", "two", "empty")}
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organizations (id, slug, name) VALUES (:id, 'acme', 'acme')"),
            {"id": ids["org"]},
        )
        for slug in ("one", "two", "empty"):
            await conn.execute(
                text(
                    "INSERT INTO projects (id, organization_id, slug, name) "
                    "VALUES (:id, :org, :slug, :slug)"
                ),
                {"id": ids[slug], "org": ids["org"], "slug": slug},
            )
        items = [
            ("one", "profile", "refund-agent"),
            ("one", "suite", "smoke"),
            ("one", "scenario", "rf-001"),
            ("two", "profile", "staging"),
            ("two", "profile", "production"),
            ("two", "scenario", "rf-001"),  # the same name as in project one: allowed by 0001
        ]
        for project, kind, name in items:
            item = uuid.uuid4()
            ids[f"{project}:{kind}:{name}"] = item
            await conn.execute(
                text(
                    "INSERT INTO catalogue_items (id, project_id, kind, name) "
                    "VALUES (:id, :project, :kind, :name)"
                ),
                {"id": item, "project": ids[project], "kind": kind, "name": name},
            )
            version = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO catalogue_versions (id, item_id, version, path, source, "
                    "content, content_hash) VALUES (:id, :item, 1, 'profile.yaml', 'x: 1', "
                    "'{}'::jsonb, 'h')"
                ),
                {"id": version, "item": item},
            )
            await conn.execute(
                text(
                    "INSERT INTO catalogue_labels (item_id, label, version_id) "
                    "VALUES (:item, 'production', :version)"
                ),
                {"item": item, "version": version},
            )
    return ids


async def _constraints(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass)"),
            {"t": table},
        )
        return {str(r[0]) for r in rows}


def _downgrade(conn: Connection, revision: str) -> None:
    command.downgrade(migrate.config(conn), revision)


class TestTheMigration:
    async def test_existing_catalogues_move_into_a_default_task(self, pg_url: str) -> None:
        engine = _engine(pg_url)
        try:
            await migrate.upgrade(engine, "0001")
            ids = await _seed_0001(engine)
            assert "uq_catalogue_items_project_id" in await _constraints(engine, "catalogue_items")
            await migrate.upgrade(engine)
            assert await migrate.current(engine) == "0002"

            async with sessions(engine)() as s:
                tasks = {t.project_id: t for t in await s.scalars(select(Task))}
                items = list(await s.scalars(select(CatalogueItem)))
                labels = await s.scalar(text("SELECT count(*) FROM catalogue_labels"))
            # One default task per project, the empty one included; nothing guessed.
            assert set(tasks) == {ids["one"], ids["two"], ids["empty"]}
            assert {(t.slug, t.kind, t.modality) for t in tasks.values()} == {
                (DEFAULT_TASK, "chat", "text")
            }
            # Every item is in its own project's default task, its versions and labels kept.
            assert len(items) == 6 and labels == 6
            assert all(i.task_id == tasks[i.project_id].id for i in items)
            # The profile is linked only where there was exactly one to choose.
            assert tasks[ids["one"]].profile_item_id == ids["one:profile:refund-agent"]
            assert tasks[ids["two"]].profile_item_id is None
            assert tasks[ids["empty"]].profile_item_id is None

            constraints = await _constraints(engine, "catalogue_items")
            assert "uq_catalogue_items_task_id" in constraints
            assert "uq_catalogue_items_project_id" not in constraints
            assert "fk_catalogue_items_task_id_tasks" in constraints

            # Upgrading again changes nothing.
            await migrate.upgrade(engine)
            async with sessions(engine)() as s:
                assert await s.scalar(select(func.count()).select_from(Task)) == 3

            # And the migrated catalogue works like any other: a project's commands find it.
            async with sessions(engine)() as s, s.begin():
                task = await get_task(s, "acme/one")
                assert {i.name for i in await list_items(s, task)} == {
                    "refund-agent",
                    "smoke",
                    "rf-001",
                }
        finally:
            await engine.dispose()

    async def test_it_reverses_while_only_default_tasks_exist(self, pg_url: str) -> None:
        engine = _engine(pg_url)
        try:
            await migrate.upgrade(engine, "0001")
            await _seed_0001(engine)
            await migrate.upgrade(engine)
            async with engine.begin() as conn:
                await conn.run_sync(_downgrade, "0001")
            assert await migrate.current(engine) == "0001"
            assert "uq_catalogue_items_project_id" in await _constraints(engine, "catalogue_items")
            async with engine.connect() as conn:
                assert await conn.scalar(text("SELECT count(*) FROM catalogue_items")) == 6

            # A second task makes it irreversible: the old key cannot hold two tasks' items.
            await migrate.upgrade(engine)
            async with sessions(engine)() as s, s.begin():
                await ensure_task(s, "acme/one", "voice", kind="voice")
            with pytest.raises(RuntimeError, match="besides 'default'"):
                async with engine.begin() as conn:
                    await conn.run_sync(_downgrade, "0001")
            assert await migrate.current(engine) == "0002"
        finally:
            await engine.dispose()


# --- the schema -------------------------------------------------------------------------


class TestTheTable:
    async def test_kind_and_modality_are_checked_by_the_database(self, engine: AsyncEngine) -> None:
        async with sessions(engine)() as s, s.begin():
            project = await ensure_project(s, PROJECT)
        for column, value in (("kind", "chatt"), ("modality", "audio")):
            values = {"kind": "chat", "modality": "text", column: value}
            with pytest.raises(IntegrityError, match=f"ck_tasks_{column}"):
                async with sessions(engine)() as s, s.begin():
                    s.add(Task(project_id=project.id, slug="x", name="x", **values))

    async def test_an_item_cannot_sit_in_another_projects_task(self, engine: AsyncEngine) -> None:
        async with sessions(engine)() as s, s.begin():
            other = await ensure_project(s, "acme/other")
            await ensure_project(s, PROJECT)
            task = await get_task(s, PROJECT)
        with pytest.raises(IntegrityError, match="fk_catalogue_items_task_id_tasks"):
            async with sessions(engine)() as s, s.begin():
                s.add(CatalogueItem(project_id=other.id, task_id=task.id, kind="suite", name="x"))

    async def test_a_new_project_has_its_default_task(self, engine: AsyncEngine) -> None:
        async with sessions(engine)() as s, s.begin():
            await ensure_project(s, PROJECT)
            await ensure_project(s, PROJECT)
            summaries = await list_tasks(s, PROJECT)
        assert [(t.slug, t.kind, t.modality, t.items) for t in summaries] == [
            (DEFAULT_TASK, "chat", "text", 0)
        ]


# --- creating and naming a task ---------------------------------------------------------


class TestNamingATask:
    async def test_a_missing_task_is_an_error_naming_project_and_slug(
        self, engine: AsyncEngine
    ) -> None:
        async with sessions(engine)() as s, s.begin():
            await ensure_project(s, PROJECT)
            with pytest.raises(ConfigError) as caught:
                await get_task(s, PROJECT, "voise")
            count = await s.scalar(select(func.count()).select_from(Task))
        message = str(caught.value)
        assert "voise" in message and PROJECT in message
        assert f"opseval db init --project {PROJECT} --task voise" in message
        assert count == 1  # never an implicit create

    async def test_a_bare_project_is_named_with_its_organisation(self, engine: AsyncEngine) -> None:
        async with sessions(engine)() as s, s.begin():
            await ensure_project(s, "bot")
            with pytest.raises(ConfigError, match="default/bot"):
                await get_task(s, "bot", "voice")

    async def test_ensure_task_is_idempotent_and_never_silently_differs(
        self, engine: AsyncEngine
    ) -> None:
        async with sessions(engine)() as s, s.begin():
            voice = await _voice(s)
            again, created = await ensure_task(s, PROJECT, "voice")
            assert again.id == voice.id and not created
            same, _ = await ensure_task(s, PROJECT, "voice", kind="voice")
            assert same.id == voice.id
            with pytest.raises(ConfigError, match="kind is 'voice', not 'chat'"):
                await ensure_task(s, PROJECT, "voice", kind="chat")
            with pytest.raises(ConfigError, match="task kind 'phone'"):
                await ensure_task(s, PROJECT, "x", kind="phone")
            with pytest.raises(ConfigError, match="task modality 'audio'"):
                await ensure_task(s, PROJECT, "x", modality="audio")
            with pytest.raises(ConfigError, match="lowercase"):
                await ensure_task(s, PROJECT, "Voice")
            with pytest.raises(ConfigError, match="no project"):
                await ensure_task(s, "acme/nope", "voice")

    async def test_update_changes_what_an_operator_may_change(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        async with sessions(engine)() as s, s.begin():
            await ensure_project(s, PROJECT)
            task = await get_task(s, PROJECT)
            await push(s, task, read(folder(tmp_path)))
            await update_task(
                s, task, kind="agentic", modality="text", region="eu", default_suite="smoke"
            )
            with pytest.raises(ConfigError, match="no suite 'nope' in task 'default'"):
                await update_task(s, task, default_suite="nope")
            with pytest.raises(ConfigError, match="task kind"):
                await update_task(s, task, kind="robot")
            (summary,) = await list_tasks(s, PROJECT)
        assert (summary.kind, summary.region, summary.default_suite) == ("agentic", "eu", "smoke")
        assert summary.profile == "refund-agent" and summary.items == 7


# --- one catalogue per task -------------------------------------------------------------


class TestOneCataloguePerTask:
    async def test_two_tasks_may_each_have_an_item_with_the_same_name(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        cat = folder(tmp_path)
        async with sessions(engine)() as s, s.begin():
            await ensure_project(s, PROJECT)
            chat, voice = await get_task(s, PROJECT), await _voice(s)
            await push(s, chat, read(cat), label="production")
            path = cat / "scenarios" / "rf-001-happy-path.yaml"
            path.write_text(path.read_text("utf-8").replace("name:", "name: (v)", 1), "utf-8")
            pushed = await push(s, voice, read(cat), label="staging")
            # The voice task's rf-001 is its own item at v1, not v2 of the chat task's.
            assert ("scenario", "rf-001", 1) in pushed.added and len(pushed.added) == 7
            await set_label(s, voice, "scenario", "rf-001", 1, "production")
            assert [v.labels for v in await history(s, chat, "scenario", "rf-001")] == [
                ["production", "latest"]
            ]
            assert [v.labels for v in await history(s, voice, "scenario", "rf-001")] == [
                ["production", "staging", "latest"]
            ]
            # A label means this task's catalogue: the chat task has no `staging`.
            assert await resolve(s, chat, "staging") == []
            assert len(await resolve(s, voice, "staging")) == 7
            out_chat, out_voice = tmp_path / "chat", tmp_path / "voice"
            await materialize(s, chat, "production", out_chat)
            await materialize(s, voice, "production", out_voice)
            summaries = {t.slug: t for t in await list_tasks(s, PROJECT)}
        rel = Path("scenarios") / "rf-001-happy-path.yaml"
        assert "(v)" not in (out_chat / rel).read_text("utf-8")
        assert "(v)" in (out_voice / rel).read_text("utf-8")
        assert summaries["voice"].profile == "refund-agent" and summaries["voice"].items == 7

    async def test_a_push_links_the_folders_profile(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        cat = folder(tmp_path)
        async with sessions(engine)() as s, s.begin():
            voice = await _voice(s)
            assert voice.profile_item_id is None
            await push(s, voice, read(cat))
            profile = cat / "profile.yaml"
            profile.write_text(
                profile.read_text("utf-8").replace("name: refund-agent", "name: renamed", 1),
                "utf-8",
            )
            await push(s, voice, read(cat))
            (summary,) = [t for t in await list_tasks(s, PROJECT) if t.slug == "voice"]
        assert summary.profile == "renamed"


# --- archiving --------------------------------------------------------------------------


class TestArchiving:
    async def test_an_archived_task_takes_no_writes_but_can_be_read(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        cat = folder(tmp_path)
        async with sessions(engine)() as s, s.begin():
            voice = await _voice(s)
            await push(s, voice, read(cat), label="production")
            await archive_task(s, voice)
            with pytest.raises(ConfigError, match="'voice' is archived"):
                await push(s, voice, read(cat))
            with pytest.raises(ConfigError, match="'voice' is archived"):
                await set_label(s, voice, "suite", "smoke", 1, "staging")
            # Reads still work: nothing is deleted, and a pull can bring the catalogue out.
            assert len(await list_items(s, voice)) == 7
            assert await history(s, voice, "suite", "smoke")
            await materialize(s, voice, "production", tmp_path / "out")
            assert [t.slug for t in await list_tasks(s, PROJECT)] == [DEFAULT_TASK]
            hidden = {t.slug: t for t in await list_tasks(s, PROJECT, archived=True)}
            assert hidden["voice"].archived_at is not None
            await archive_task(s, voice, archived=False)
            assert await push(s, voice, read(cat))

    async def test_an_archive_that_commits_first_stops_a_push(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        # The push read the task before the archive, but checks archived_at under its lock.
        files = read(folder(tmp_path))
        async with sessions(engine)() as s, s.begin():
            await _voice(s)
        async with sessions(engine)() as pusher:
            await pusher.begin()
            stale = await get_task(pusher, PROJECT, "voice")
            async with sessions(engine)() as s, s.begin():
                await archive_task(s, await get_task(s, PROJECT, "voice"))
            with pytest.raises(ConfigError, match="archived"):
                await push(pusher, stale, files)
            await pusher.rollback()


# --- the task row lock ------------------------------------------------------------------


class TestTheTaskLock:
    async def test_a_pull_waits_for_a_push_to_the_same_task_only(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        cat = folder(tmp_path)
        async with sessions(engine)() as s, s.begin():
            await ensure_project(s, PROJECT)
            await push(s, await get_task(s, PROJECT), read(cat), label="production")
            await push(s, await _voice(s), read(cat), label="production")
        path = cat / "scenarios" / "rf-001-happy-path.yaml"
        path.write_text(path.read_text("utf-8").replace("name:", "name: (v2)", 1), "utf-8")
        edited = read(cat)

        async def pull(slug: str) -> list[int]:
            async with sessions(engine)() as s, s.begin():
                chosen = await resolve(s, await get_task(s, PROJECT, slug), "production")
                return [v.version for i, v in chosen if i.name == "rf-001"]

        async with sessions(engine)() as pusher:
            await pusher.begin()
            await push(pusher, await get_task(pusher, PROJECT, "voice"), edited, label="production")
            same = asyncio.create_task(pull("voice"))
            # Another task of the project never waits for this push.
            assert await asyncio.wait_for(pull(DEFAULT_TASK), timeout=5) == [1]
            await asyncio.sleep(0.3)
            assert not same.done()  # it waits rather than read half of the push
            await pusher.commit()
        assert await same == [2]

    async def test_concurrent_pushes_to_one_task_never_share_a_version(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        async with sessions(engine)() as s, s.begin():
            await _voice(s)
        batches = []
        for n in range(3):
            copy = folder(tmp_path, f"cat{n}")
            p = copy / "scenarios" / "rf-001-happy-path.yaml"
            p.write_text(p.read_text("utf-8").replace("name:", f"name: ({n})", 1), "utf-8")
            batches.append(read(copy))

        async def one(files):  # type: ignore[no-untyped-def]
            async with sessions(engine)() as s, s.begin():
                await push(s, await get_task(s, PROJECT, "voice"), files)

        await asyncio.gather(*(one(f) for f in batches))
        async with sessions(engine)() as s, s.begin():
            versions = await history(s, await get_task(s, PROJECT, "voice"), "scenario", "rf-001")
        assert [v.version for v in versions] == [3, 2, 1]


# --- the command line -------------------------------------------------------------------


class TestCommandLine:
    @pytest.fixture
    def env(self, pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPSEVAL_DATABASE_URL", pg_url)
        monkeypatch.setenv("OPSEVAL_ALLOWED_HOSTS", "127.0.0.1")
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        return pg_url

    def test_every_catalogue_command_takes_a_task(self, env: str, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        made = runner.invoke(
            app,
            ["db", "init", "--project", PROJECT, "--task", "voice", "--kind", "voice"]
            + ["--modality", "audio_stream", "--region", "eu"],
        )
        assert made.exit_code == 0, made.output
        assert "Task voice (voice, audio_stream) created." in made.output
        again = runner.invoke(app, ["db", "init", "--project", PROJECT, "--task", "voice"])
        assert again.exit_code == 0 and "already exists" in again.output

        task = ["--project", PROJECT, "--task", "voice"]
        pushed = runner.invoke(app, ["catalogue", "push", str(cat), *task, "--label", "staging"])
        assert pushed.exit_code == 0 and "7 new version(s)" in pushed.output, pushed.output
        listed = runner.invoke(app, ["catalogue", "list", *task])
        assert "staging=v1" in listed.output
        # The default task is untouched by a push to another.
        empty = runner.invoke(app, ["catalogue", "list", "--project", PROJECT])
        assert empty.exit_code == 0 and "empty" in empty.output
        hist = runner.invoke(app, ["catalogue", "history", "suite", "smoke", *task])
        assert "[staging, latest]" in hist.output
        moved = runner.invoke(
            app, ["catalogue", "label", "suite", "smoke", "1", "production", *task]
        )
        assert moved.exit_code == 0, moved.output
        out = tmp_path / "pulled"
        pulled = runner.invoke(app, ["catalogue", "pull", str(out), *task])
        assert pulled.exit_code == 0 and (out / "suites" / "smoke.yaml").exists()

        tasks = runner.invoke(app, ["task", "list", "--project", PROJECT])
        assert tasks.exit_code == 0, tasks.output
        lines = {line.split()[0]: line for line in tasks.output.splitlines() if line.strip()}
        assert "chat" in lines["default"] and "0 item(s)" in lines["default"]
        assert "audio_stream" in lines["voice"] and "profile refund-agent" in lines["voice"]
        assert "region eu" in lines["voice"]

        updated = runner.invoke(
            app, ["task", "update", "voice", "--project", PROJECT, "--default-suite", "smoke"]
        )
        assert updated.exit_code == 0, updated.output
        assert (
            "default suite smoke"
            in runner.invoke(app, ["task", "list", "--project", PROJECT]).output
        )

    def test_without_task_the_default_is_used(self, env: str, tmp_path: Path) -> None:
        assert runner.invoke(app, ["db", "init", "--project", PROJECT]).exit_code == 0
        res = runner.invoke(app, ["catalogue", "push", str(folder(tmp_path)), "--project", PROJECT])
        assert res.exit_code == 0, res.output
        listed = runner.invoke(
            app, ["catalogue", "list", "--project", PROJECT, "--task", DEFAULT_TASK]
        )
        assert "latest=v1" in listed.output

    def test_an_unknown_task_is_exit_3_and_creates_nothing(self, env: str, tmp_path: Path) -> None:
        assert runner.invoke(app, ["db", "init", "--project", PROJECT]).exit_code == 0
        for args in (
            ["catalogue", "push", str(folder(tmp_path))],
            ["catalogue", "pull", str(tmp_path / "out")],
            ["catalogue", "list"],
            ["catalogue", "history", "suite", "smoke"],
            ["catalogue", "label", "suite", "smoke", "1", "production"],
            ["task", "archive", "voise"],
        ):
            extra = [] if args[0] == "task" else ["--task", "voise"]
            res = runner.invoke(app, [*args, "--project", PROJECT, *extra])
            assert res.exit_code == 3, (args, res.output)
            assert f"no task 'voise' in project {PROJECT}" in res.output, (args, res.output)
        tasks = runner.invoke(app, ["task", "list", "--project", PROJECT])
        assert "voise" not in tasks.output

    def test_bad_task_options(self, env: str) -> None:
        orphan = runner.invoke(app, ["db", "init", "--project", PROJECT, "--kind", "voice"])
        assert orphan.exit_code == 3 and "add --task" in orphan.output
        bad = runner.invoke(
            app, ["db", "init", "--project", PROJECT, "--task", "x", "--kind", "phone"]
        )
        assert bad.exit_code == 3 and "chat, agentic, voice, batch" in bad.output
        clash = runner.invoke(
            app, ["db", "init", "--project", PROJECT, "--task", "default", "--kind", "voice"]
        )
        assert clash.exit_code == 3 and "opseval task update default" in clash.output

    def test_archive_refuses_pushes_until_unarchived(self, env: str, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        assert runner.invoke(app, ["db", "init", "--project", PROJECT]).exit_code == 0
        task = ["--project", PROJECT, "--task", DEFAULT_TASK]
        assert (
            runner.invoke(app, ["task", "archive", DEFAULT_TASK, "--project", PROJECT]).exit_code
            == 0
        )
        refused = runner.invoke(app, ["catalogue", "push", str(cat), *task])
        assert refused.exit_code == 3 and "is archived" in refused.output
        assert runner.invoke(app, ["task", "list", "--project", PROJECT]).output.strip() == ""
        shown = runner.invoke(app, ["task", "list", "--project", PROJECT, "--all"])
        assert "archived" in shown.output
        assert (
            runner.invoke(app, ["task", "unarchive", DEFAULT_TASK, "--project", PROJECT]).exit_code
            == 0
        )
        assert runner.invoke(app, ["catalogue", "push", str(cat), *task]).exit_code == 0
