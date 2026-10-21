# SPDX-License-Identifier: Apache-2.0
"""Server mode on Postgres (ADR-0047): the schema, the database URL rule, and the versioned
catalogue, pushed from a folder and pulled back as one.

Most tests need a database (``OPSEVAL_TEST_DATABASE_URL``, see ``conftest.pg_url``). The URL
and project-reference rules need none and always run.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from opseval.catalogue import load_catalogue, load_profile
from opseval.cli import app
from opseval.config import Settings, check_database_url_allowed, config_digest
from opseval.db import migrate
from opseval.db.catalogue_store import (
    LATEST,
    ensure_project,
    get_project,
    history,
    list_items,
    materialize,
    parse_project,
    push,
    read_folder,
    resolve,
    set_label,
)
from opseval.db.engine import async_url, make_engine, sessions
from opseval.db.schema import Base, CatalogueItem, CatalogueVersion
from opseval.demo import catalogue_dir
from opseval.demo.refund_agent import run_agent
from opseval.errors import AllowedHostError, ConfigError
from opseval.runner import RunOptions, launch
from opseval.scoring import INVARIANTS, REGISTRY

runner = CliRunner()
PROJECT = "acme/support-bot"


def folder(tmp_path: Path, name: str = "cat") -> Path:
    """A writable copy of the demo catalogue."""
    dest = tmp_path / name
    shutil.copytree(catalogue_dir(), dest)
    return dest


def read(path: Path):  # type: ignore[no-untyped-def]
    return read_folder(path, known_scorers=REGISTRY.keys(), known_invariants=set(INVARIANTS))


@pytest.fixture
async def engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(Settings(_env_file=None, allowed_hosts="127.0.0.1", database_url=pg_url))
    await migrate.upgrade(engine)
    try:
        yield engine
    finally:
        await engine.dispose()


# --- no database needed -----------------------------------------------------------------


class TestTheDatabaseUrlRule:
    def test_loopback_without_tls_is_fine(self) -> None:
        check_database_url_allowed("postgresql://u:p@127.0.0.1:5432/db", {"127.0.0.1"})

    def test_the_host_must_be_allowlisted(self) -> None:
        with pytest.raises(AllowedHostError, match="not in OPSEVAL_ALLOWED_HOSTS"):
            check_database_url_allowed("postgresql://u:p@db.example.com/db", set())

    @pytest.mark.parametrize("query", ["", "?ssl=prefer", "?sslmode=allow", "?ssl=disable"])
    def test_a_remote_host_needs_tls(self, query: str) -> None:
        with pytest.raises(AllowedHostError, match="must use TLS"):
            check_database_url_allowed(
                f"postgresql://u:p@db.example.com/db{query}", {"db.example.com"}
            )

    @pytest.mark.parametrize("query", ["?ssl=require", "?sslmode=verify-full"])
    def test_tls_modes_that_guarantee_encryption(self, query: str) -> None:
        check_database_url_allowed(f"postgresql://u:p@db.example.com/db{query}", {"db.example.com"})

    def test_other_schemes_are_refused_without_echoing_the_password(self) -> None:
        with pytest.raises(AllowedHostError) as caught:
            check_database_url_allowed("mysql://u:hunter2@127.0.0.1/db", {"127.0.0.1"})
        assert "hunter2" not in str(caught.value)

    def test_the_driver_and_sslmode_are_normalised(self) -> None:
        assert async_url("postgresql://u:p@h:5432/db?sslmode=require") == (
            "postgresql+asyncpg://u:p@h:5432/db?ssl=require"
        )

    def test_the_url_is_a_secret(self) -> None:
        digest = config_digest(Settings(_env_file=None, database_url="postgresql://u:pw@h/db"))
        assert digest["database_url"] != "postgresql://u:pw@h/db"
        assert "pw" not in str(digest["database_url"])

    def test_no_url_is_a_configuration_error(self) -> None:
        with pytest.raises(ConfigError, match="OPSEVAL_DATABASE_URL"):
            make_engine(Settings(_env_file=None))


class TestProjectReferences:
    def test_org_slash_project(self) -> None:
        assert parse_project("acme/support-bot") == ("acme", "support-bot")

    def test_a_bare_project_is_in_the_default_organisation(self) -> None:
        assert parse_project("bot") == ("default", "bot")

    @pytest.mark.parametrize("ref", ["Acme/bot", "acme/", "a/b/c", "acme/bot_1", ""])
    def test_bad_references(self, ref: str) -> None:
        with pytest.raises(ConfigError):
            parse_project(ref)


class TestReadingAFolder:
    """The folder is checked before anything touches the database."""

    def test_the_demo_catalogue(self, tmp_path: Path) -> None:
        files = read(folder(tmp_path))
        kinds = sorted((f.kind, f.name) for f in files)
        assert ("profile", "refund-agent") in kinds and ("suite", "smoke") in kinds
        assert {f.path for f in files if f.kind == "scenario"} >= {
            "scenarios/rf-001-happy-path.yaml",
            "scenarios/llm/rf-101-gulf-persona.yaml",
        }

    def test_one_problem_refuses_everything(self, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        (cat / "scenarios" / "bad.yaml").write_text("id: bad\nname: x\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="nothing was pushed"):
            read(cat)

    def test_a_scenario_is_checked_with_its_suites_defaults(self, tmp_path: Path) -> None:
        # Valid only with the suite's default stop_at: checked through the suite, it passes.
        cat = folder(tmp_path)
        smoke = cat / "suites" / "smoke.yaml"
        smoke.write_text(
            smoke.read_text("utf-8") + "defaults:\n  execution: {repeats: 2}\n", encoding="utf-8"
        )
        assert read(cat)

    def test_an_alias_is_refused_even_in_a_valid_scenario(self, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        (cat / "scenarios" / "anchored.yaml").write_text(
            "id: anchored\n"
            "name: anchored\n"
            "goal: &goal Get a refund to the original card.\n"
            "persona: {role: customer, language: en}\n"
            "conversation:\n"
            "  scripted_turns: [*goal]\n"
            "  stop_at: supervisor_review\n"
            "  checkpoint_policy: {proposal_approval: {action: approve}}\n"
            "scorers: [{id: output.terminal_state}]\n"
            "execution: {repeats: 1}\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="anchors and aliases are not allowed"):
            read(cat)

    def test_an_alias_bomb_never_expands(self, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        bomb = "a: &a [1, 1]\nb: &b [*a, *a]\nc: &c [*b, *b]\n"
        (cat / "scenarios" / "bomb.yaml").write_text(bomb, encoding="utf-8")
        with pytest.raises(ConfigError):
            read(cat)

    def test_a_huge_file_is_refused(self, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        (cat / "scenarios" / "big.yaml").write_text("x" * 300_000, encoding="utf-8")
        with pytest.raises(ConfigError, match="larger than"):
            read(cat)

    def test_a_link_out_of_the_folder_is_refused(self, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        outside = tmp_path / "outside.yaml"
        outside.write_text("id: x\n", encoding="utf-8")
        (cat / "scenarios" / "linked.yaml").symlink_to(outside)
        with pytest.raises(ConfigError, match="outside the catalogue"):
            read(cat)

    def test_a_folder_without_a_profile(self, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        (cat / "profile.yaml").unlink()
        with pytest.raises(ConfigError, match="no profile.yaml"):
            read(cat)


# --- the database -----------------------------------------------------------------------


class TestSchema:
    async def test_upgrade_reaches_head(self, engine: AsyncEngine) -> None:
        assert await migrate.current(engine) == migrate.head() == "0001"

    async def test_the_migrations_create_exactly_the_models(self, engine: AsyncEngine) -> None:
        def diff(conn):  # type: ignore[no-untyped-def]
            context = MigrationContext.configure(
                conn, opts={"version_table": migrate.VERSION_TABLE, "compare_type": True}
            )
            return compare_metadata(context, Base.metadata)

        async with engine.connect() as conn:
            assert await conn.run_sync(diff) == []

    async def test_upgrading_twice_changes_nothing(self, engine: AsyncEngine) -> None:
        await migrate.upgrade(engine)
        assert await migrate.current(engine) == "0001"


class TestTheCatalogue:
    async def test_push_versions_labels_and_pull(self, engine: AsyncEngine, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        async with sessions(engine)() as s, s.begin():
            project = await ensure_project(s, PROJECT)
            first = await push(s, project, read(cat), label="staging", message="first")
        assert len(first.added) == 7 and not first.unchanged

        # An unchanged folder adds nothing; an edited file becomes v2; a removed one loses
        # the label, so `staging` still names exactly what was pushed.
        path = cat / "scenarios" / "rf-001-happy-path.yaml"
        path.write_text(path.read_text("utf-8").replace("name:", "name: (v2)", 1), "utf-8")
        (cat / "scenarios" / "rf-003-egyptian-arabic.yaml").unlink()
        async with sessions(engine)() as s, s.begin():
            project = await get_project(s, PROJECT)
            second = await push(s, project, read(cat), label="staging", message="edit")
        assert second.added == [("scenario", "rf-001", 2)]
        assert ("scenario", "rf-003") in second.unlabelled

        async with sessions(engine)() as s, s.begin():
            project = await get_project(s, PROJECT)
            for kind, name in (
                ("profile", "refund-agent"),
                ("suite", "smoke"),
                ("scenario", "rf-002"),
            ):
                await set_label(s, project, kind, name, 1, "production")
            await set_label(s, project, "scenario", "rf-001", 1, "production")
            versions = await history(s, project, "scenario", "rf-001")
            items = {(i.kind, i.name): i for i in await list_items(s, project)}
        assert [(v.version, v.labels) for v in versions] == [
            (2, ["staging", LATEST]),
            (1, ["production"]),
        ]
        assert versions[1].message == "first"
        assert items[("scenario", "rf-003")].labels == {}
        assert items[("scenario", "rf-001")].labels == {"production": 1, "staging": 2}

        out = tmp_path / "pulled"
        async with sessions(engine)() as s, s.begin():
            await materialize(s, await get_project(s, PROJECT), "production", out)
        written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.yaml"))
        assert written == [
            "profile.yaml",
            "scenarios/rf-001-happy-path.yaml",
            "scenarios/rf-002-reject-then-approve.yaml",
            "suites/smoke.yaml",
        ]
        # v1 of rf-001, byte for byte, and the pulled folder loads like any other.
        assert "(v2)" not in (out / "scenarios" / "rf-001-happy-path.yaml").read_text("utf-8")
        profile = load_profile(out / "profile.yaml")
        loaded, _ = load_catalogue(
            out / "suites" / "smoke.yaml",
            profile,
            known_scorers=REGISTRY.keys(),
            known_invariants=set(INVARIANTS),
        )
        assert [s.id for s in loaded.scenarios] == ["rf-001", "rf-002"] and not loaded.problems

    async def test_latest_is_the_newest_version(self, engine: AsyncEngine, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        async with sessions(engine)() as s, s.begin():
            project = await ensure_project(s, PROJECT)
            await push(s, project, read(cat))
            chosen = await resolve(s, project, LATEST)
            assert len(chosen) == 7
            assert await resolve(s, project, "production") == []

    async def test_labels_are_checked(self, engine: AsyncEngine, tmp_path: Path) -> None:
        async with sessions(engine)() as s, s.begin():
            project = await ensure_project(s, PROJECT)
            await push(s, project, read(folder(tmp_path)))
            with pytest.raises(ConfigError, match="kept by Opseval"):
                await set_label(s, project, "suite", "smoke", 1, LATEST)
            with pytest.raises(ConfigError, match="lowercase"):
                await set_label(s, project, "suite", "smoke", 1, "Prod!")
            with pytest.raises(ConfigError, match="no version 9"):
                await set_label(s, project, "suite", "smoke", 9, "production")
            with pytest.raises(ConfigError, match="no suite 'nope'"):
                await set_label(s, project, "suite", "nope", 1, "production")

    async def test_pull_never_mixes_with_other_files(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "stale.yaml").write_text("id: stale\n", encoding="utf-8")
        async with sessions(engine)() as s, s.begin():
            project = await ensure_project(s, PROJECT)
            await push(s, project, read(folder(tmp_path)), label="production")
            with pytest.raises(ConfigError, match="not an empty folder"):
                await materialize(s, project, "production", out)
            with pytest.raises(ConfigError, match="carries the label"):
                await materialize(s, project, "nothing-here", tmp_path / "x")

    async def test_a_stored_path_cannot_leave_the_folder(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        # Rows written by something other than push (a restore, a bug) are checked again.
        async with sessions(engine)() as s, s.begin():
            project = await ensure_project(s, PROJECT)
            await push(s, project, read(folder(tmp_path)), label="production")
            await s.execute(
                text("UPDATE catalogue_versions SET path = '../escape.yaml' WHERE version = 1")
            )
            with pytest.raises(ConfigError, match="not a catalogue file path"):
                await materialize(s, project, "production", tmp_path / "out")
        assert not (tmp_path / "escape.yaml").exists()

    async def test_projects_are_separate(self, engine: AsyncEngine, tmp_path: Path) -> None:
        async with sessions(engine)() as s, s.begin():
            a = await ensure_project(s, "acme/a")
            b = await ensure_project(s, "acme/b")
            assert (await ensure_project(s, "acme/a")).id == a.id
            await push(s, a, read(folder(tmp_path)), label="production")
            assert await list_items(s, b) == []
            assert len(await list_items(s, a)) == 7

    async def test_concurrent_pushes_never_share_a_version_number(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        import asyncio

        cat = folder(tmp_path)
        async with sessions(engine)() as s, s.begin():
            await ensure_project(s, PROJECT)
        versions = []
        for n in range(3):
            path = cat / "scenarios" / "rf-001-happy-path.yaml"
            copy = folder(tmp_path, f"cat{n}")
            p = copy / "scenarios" / "rf-001-happy-path.yaml"
            p.write_text(path.read_text("utf-8").replace("name:", f"name: ({n})", 1), "utf-8")
            versions.append(read(copy))

        async def one(files):  # type: ignore[no-untyped-def]
            async with sessions(engine)() as s, s.begin():
                await push(s, await get_project(s, PROJECT), files)

        await asyncio.gather(*(one(f) for f in versions))
        async with sessions(engine)() as s:
            numbers = list(
                await s.scalars(
                    select(CatalogueVersion.version)
                    .join(CatalogueItem, CatalogueItem.id == CatalogueVersion.item_id)
                    .where(CatalogueItem.name == "rf-001")
                    .order_by(CatalogueVersion.version)
                )
            )
            count = await s.scalar(select(func.count()).select_from(CatalogueItem))
        assert numbers == [1, 2, 3] and count == 7


class TestCommandLine:
    @pytest.fixture
    def env(self, pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        monkeypatch.chdir(tmp_path)  # no stray .env
        monkeypatch.setenv("OPSEVAL_DATABASE_URL", pg_url)
        monkeypatch.setenv("OPSEVAL_ALLOWED_HOSTS", "127.0.0.1")
        return pg_url

    def test_the_whole_loop(self, env: str, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        status = runner.invoke(app, ["db", "status"])
        assert status.exit_code == 3 and "opseval db upgrade" in status.output
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        assert runner.invoke(app, ["db", "status"]).exit_code == 0
        missing = runner.invoke(app, ["catalogue", "list", "--project", PROJECT])
        assert missing.exit_code == 3 and "opseval db init" in missing.output
        assert runner.invoke(app, ["db", "init", "--project", PROJECT]).exit_code == 0
        pushed = runner.invoke(
            app, ["catalogue", "push", str(cat), "--project", PROJECT, "--label", "production"]
        )
        assert pushed.exit_code == 0, pushed.output
        assert "7 new version(s)" in pushed.output
        listed = runner.invoke(app, ["catalogue", "list", "--project", PROJECT])
        assert "production=v1" in listed.output
        hist = runner.invoke(app, ["catalogue", "history", "suite", "smoke", "--project", PROJECT])
        assert "[production, latest]" in hist.output
        moved = runner.invoke(
            app, ["catalogue", "label", "suite", "smoke", "1", "staging", "--project", PROJECT]
        )
        assert moved.exit_code == 0
        out = tmp_path / "pulled"
        pulled = runner.invoke(app, ["catalogue", "pull", str(out), "--project", PROJECT])
        assert pulled.exit_code == 0 and (out / "suites" / "smoke.yaml").exists()

    def test_a_bad_folder_stores_nothing(self, env: str, tmp_path: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        assert runner.invoke(app, ["db", "init", "--project", PROJECT]).exit_code == 0
        cat = folder(tmp_path)
        (cat / "scenarios" / "bad.yaml").write_text("id: bad\n", encoding="utf-8")
        res = runner.invoke(app, ["catalogue", "push", str(cat), "--project", PROJECT])
        assert res.exit_code == 3 and "nothing was pushed" in res.output
        listed = runner.invoke(app, ["catalogue", "list", "--project", PROJECT])
        assert "empty" in listed.output

    def test_a_database_host_off_the_allowlist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPSEVAL_DATABASE_URL", "postgresql://u:secret@db.example.com/db")
        monkeypatch.setenv("OPSEVAL_ALLOWED_HOSTS", "127.0.0.1")
        res = runner.invoke(app, ["db", "upgrade"])
        assert res.exit_code == 3 and "not in OPSEVAL_ALLOWED_HOSTS" in res.output
        assert "secret" not in res.output

    def test_an_unreachable_database_is_exit_3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPSEVAL_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/db")
        monkeypatch.setenv("OPSEVAL_ALLOWED_HOSTS", "127.0.0.1")
        res = runner.invoke(app, ["db", "status"])
        assert res.exit_code == 3 and "cannot reach the database" in res.output

    def test_without_the_server_extra(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        real = builtins.__import__

        def no_sqlalchemy(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name.startswith("opseval.db.engine") or (
                name == "opseval.db" and args and args[2] and "engine" in args[2]
            ):
                raise ImportError("No module named 'sqlalchemy'")
            return real(name, *args, **kwargs)

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(builtins, "__import__", no_sqlalchemy)
        res = runner.invoke(app, ["db", "status"])
        assert res.exit_code == 3 and "opseval[server]" in res.output


async def test_a_pulled_label_runs_like_any_folder(
    engine: AsyncEngine, tmp_path: Path, settings: Settings
) -> None:
    async with sessions(engine)() as s, s.begin():
        project = await ensure_project(s, PROJECT)
        await push(s, project, read(folder(tmp_path)), label="production")
        out = tmp_path / "production"
        await materialize(s, project, "production", out)
    async with run_agent() as url:
        result = await launch(
            out / "suites" / "smoke.yaml",
            RunOptions(target=url, profile=out / "profile.yaml", repeats=1),
            settings,
        )
    assert result.exit_code == 0, result.gate.summary
