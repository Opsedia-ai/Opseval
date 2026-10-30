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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from typer.testing import CliRunner

from opseval.catalogue import load_catalogue, load_profile
from opseval.cli import app
from opseval.config import Settings, config_digest
from opseval.db import migrate
from opseval.db.catalogue_store import (
    LATEST,
    ensure_project,
    get_task,
    history,
    list_items,
    materialize,
    parse_project,
    push,
    read_folder,
    resolve,
    set_label,
)
from opseval.db.engine import checked_url, make_engine, sessions
from opseval.db.schema import Base, CatalogueItem, CatalogueVersion, Task
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
    return read_folder(path, known_scorers=REGISTRY, known_invariants=set(INVARIANTS))


def _without_persona(path: Path) -> str:
    """The scenario file with its ``persona`` block removed."""
    out, skipping = [], False
    for line in path.read_text("utf-8").splitlines(keepends=True):
        if line.startswith("persona:"):
            skipping = True
            continue
        if skipping and line.startswith((" ", "\t")):
            continue
        skipping = False
        out.append(line)
    return "".join(out)


async def default_task(s: AsyncSession, ref: str = PROJECT) -> Task:
    """The project's ``default`` task, creating the project (and so the task) if needed."""
    await ensure_project(s, ref)
    return await get_task(s, ref)


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
    """The host and TLS are checked as the driver will connect, not as a URL looks."""

    def test_loopback_without_tls_is_fine(self) -> None:
        url, tls = checked_url("postgresql://u:p@127.0.0.1:5432/db", {"127.0.0.1"})
        assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1" and tls is None

    def test_the_host_must_be_allowlisted(self) -> None:
        with pytest.raises(AllowedHostError, match="not in OPSEVAL_ALLOWED_HOSTS"):
            checked_url("postgresql://u:p@db.example.com/db?ssl=verify-full", set())

    @pytest.mark.parametrize("query", ["", "?ssl=require", "?ssl=prefer", "?sslmode=allow"])
    def test_a_remote_host_needs_verified_tls(self, query: str) -> None:
        with pytest.raises(AllowedHostError, match="must use verified TLS"):
            checked_url(f"postgresql://u:p@db.example.com/db{query}", {"db.example.com"})

    def test_verify_full_checks_the_certificate_and_the_name(self) -> None:
        import ssl

        url, tls = checked_url(
            "postgresql://u:p@db.example.com/db?sslmode=verify-full", {"db.example.com"}
        )
        assert isinstance(tls, ssl.SSLContext) and tls.check_hostname
        assert tls.verify_mode is ssl.CERT_REQUIRED and dict(url.query) == {}

    def test_a_private_ca_is_read_from_sslrootcert(self) -> None:
        import certifi

        _url, tls = checked_url(
            f"postgresql://u:p@db.example.com/db?ssl=verify-ca&sslrootcert={certifi.where()}",
            {"db.example.com"},
        )
        assert not isinstance(tls, str | None) and not tls.check_hostname

    def test_a_host_parameter_cannot_redirect_the_connection(self) -> None:
        # asyncpg reads every query key as a connection argument: ?host= would win over the
        # host the allowlist checked, and send the password there in plain text.
        with pytest.raises(AllowedHostError, match="not supported"):
            checked_url("postgresql://u:p@localhost/db?host=evil.example.com", {"localhost"})

    def test_a_url_two_parsers_read_differently_is_refused(self) -> None:
        with pytest.raises(AllowedHostError, match="ambiguous"):
            checked_url("postgresql://localhost:pw/@evil.example.com/db", {"localhost"})

    def test_other_schemes_are_refused_without_echoing_the_password(self) -> None:
        with pytest.raises(AllowedHostError) as caught:
            checked_url("mysql://u:hunter2@127.0.0.1/db", {"127.0.0.1"})
        assert "hunter2" not in str(caught.value)

    def test_the_url_is_a_secret(self) -> None:
        digest = config_digest(Settings(_env_file=None, database_url="postgresql://u:pw@h/db"))
        assert "pw" not in str(digest["database_url"])

    def test_no_url_is_a_configuration_error(self) -> None:
        with pytest.raises(ConfigError, match="OPSEVAL_DATABASE_URL"):
            make_engine(Settings(_env_file=None))

    def test_a_missing_ca_file_is_a_configuration_error(self) -> None:
        settings = Settings(
            _env_file=None,
            allowed_hosts="db.example.com",
            database_url="postgresql://u:p@db.example.com/db?ssl=verify-full&sslrootcert=/no/ca.pem",
        )
        with pytest.raises(ConfigError, match="sslrootcert"):
            make_engine(settings)


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
        # rf-002 without a persona is invalid on its own, and valid in a suite that supplies one.
        cat = folder(tmp_path)
        path = cat / "scenarios" / "rf-002-reject-then-approve.yaml"
        path.write_text(_without_persona(path), encoding="utf-8")
        with pytest.raises(ConfigError, match="persona"):
            read(cat)  # the suites include it without defaults: refused
        for suite in ("smoke.yaml", "full.yaml"):
            (cat / "suites" / suite).write_text(
                (cat / "suites" / suite).read_text("utf-8")
                + "defaults:\n  persona: {role: customer, language: en}\n",
                encoding="utf-8",
            )
        assert read(cat)

    def test_a_scenario_a_suite_filters_out_keeps_the_suites_defaults(self, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        path = cat / "scenarios" / "rf-002-reject-then-approve.yaml"
        path.write_text(_without_persona(path), encoding="utf-8")
        (cat / "suites" / "full.yaml").unlink()
        smoke = cat / "suites" / "smoke.yaml"
        smoke.write_text(
            smoke.read_text("utf-8")
            + "filters: [tag=happy-path]\ndefaults:\n  persona: {role: customer, language: en}\n",
            encoding="utf-8",
        )
        (cat / "scenarios" / "llm").rename(tmp_path / "llm-aside")
        assert read(cat)

    def test_a_file_a_suite_runs_must_be_a_file_the_push_stores(self, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        (cat / "extra").mkdir()
        shutil.move(
            cat / "scenarios" / "rf-003-egyptian-arabic.yaml", cat / "extra" / "rf-003.yaml"
        )
        smoke = cat / "suites" / "smoke.yaml"
        smoke.write_text(
            smoke.read_text("utf-8").replace(
                'include: ["scenarios/*.yaml"]', 'include: ["scenarios/*.yaml", "extra/*.yaml"]'
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="includes files a push does not store"):
            read(cat)

    def test_a_link_inside_the_folder_is_stored_where_it_sits(self, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        (cat / "shared").mkdir()
        target = cat / "shared" / "rf-002.yaml"
        shutil.move(cat / "scenarios" / "rf-002-reject-then-approve.yaml", target)
        (cat / "scenarios" / "rf-002-reject-then-approve.yaml").symlink_to(target)
        paths = {f.name: f.path for f in read(cat) if f.kind == "scenario"}
        assert paths["rf-002"] == "scenarios/rf-002-reject-then-approve.yaml"

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

    def test_an_alias_bomb_in_a_valid_scenario_never_expands(self, tmp_path: Path) -> None:
        # payload_equals' `equals` takes any value, so the loader itself accepts this file.
        cat = folder(tmp_path)
        (cat / "scenarios" / "bomb.yaml").write_text(
            "id: bomb\nname: bomb\ngoal: g\npersona: {role: customer}\n"
            "conversation:\n  scripted_turns: [hi]\n  stop_at: supervisor_review\n"
            "  checkpoint_policy: {proposal_approval: {action: approve}}\n"
            "scorers:\n  - id: output.payload_equals\n"
            "    params:\n      checkpoint: proposal_approval\n      path: x\n"
            "      equals: {a: &a [1, 1], b: &b [*a, *a], c: [*b, *b]}\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="anchors and aliases are not allowed"):
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

    def test_a_params_typo_refuses_the_push(self, tmp_path: Path) -> None:
        # The push checks params like `validate` does (ADR-0043), not only the schema.
        cat = folder(tmp_path)
        path = cat / "scenarios" / "rf-001-happy-path.yaml"
        path.write_text(
            path.read_text("utf-8").replace(
                "scorers:\n",
                "scorers:\n  - {id: conversation.turn_budget, params: {budjet: 1}}\n",
                1,
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="budjet"):
            read(cat)

    def test_a_folder_without_a_profile(self, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        (cat / "profile.yaml").unlink()
        with pytest.raises(ConfigError, match="no profile.yaml"):
            read(cat)


# --- the database -----------------------------------------------------------------------


class TestSchema:
    async def test_upgrade_reaches_head(self, engine: AsyncEngine) -> None:
        assert await migrate.current(engine) == migrate.head() == "0007"

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
        assert await migrate.current(engine) == "0007"

    async def test_two_upgrades_at_once_both_succeed(self, pg_url: str) -> None:
        import asyncio

        settings = Settings(_env_file=None, allowed_hosts="127.0.0.1", database_url=pg_url)
        engines = [make_engine(settings), make_engine(settings)]
        try:
            await asyncio.gather(*(migrate.upgrade(e) for e in engines))
            assert await migrate.current(engines[0]) == migrate.head()
        finally:
            for e in engines:
                await e.dispose()


class TestTheCatalogue:
    async def test_push_versions_labels_and_pull(self, engine: AsyncEngine, tmp_path: Path) -> None:
        cat = folder(tmp_path)
        async with sessions(engine)() as s, s.begin():
            project = await default_task(s)
            first = await push(s, project, read(cat), label="staging", message="first")
        assert len(first.added) == 7 and not first.unchanged

        # An unchanged folder adds nothing; an edited file becomes v2; a removed one loses
        # the label, so `staging` still names exactly what was pushed.
        path = cat / "scenarios" / "rf-001-happy-path.yaml"
        path.write_text(path.read_text("utf-8").replace("name:", "name: (v2)", 1), "utf-8")
        (cat / "scenarios" / "rf-003-egyptian-arabic.yaml").unlink()
        async with sessions(engine)() as s, s.begin():
            project = await get_task(s, PROJECT)
            second = await push(s, project, read(cat), label="staging", message="edit")
        assert second.added == [("scenario", "rf-001", 2)]
        assert ("scenario", "rf-003") in second.unlabelled

        async with sessions(engine)() as s, s.begin():
            project = await get_task(s, PROJECT)
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
        assert items[("scenario", "rf-001")].labels == {"latest": 2, "production": 1, "staging": 2}

        out = tmp_path / "pulled"
        async with sessions(engine)() as s, s.begin():
            await materialize(s, await get_task(s, PROJECT), "production", out)
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
            project = await default_task(s)
            await push(s, project, read(cat))
            chosen = await resolve(s, project, LATEST)
            assert len(chosen) == 7
            assert await resolve(s, project, "production") == []

    async def test_latest_is_the_last_push_never_a_deleted_item(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        cat = folder(tmp_path)
        async with sessions(engine)() as s, s.begin():
            project = await default_task(s)
            await push(s, project, read(cat))
        # Delete one scenario and rename another (same file, new id): neither the deleted one
        # nor the old name may come back under latest, and the pull must not collide.
        (cat / "scenarios" / "rf-003-egyptian-arabic.yaml").unlink()
        path = cat / "scenarios" / "rf-001-happy-path.yaml"
        path.write_text(path.read_text("utf-8").replace("id: rf-001", "id: rf-001b", 1), "utf-8")
        async with sessions(engine)() as s, s.begin():
            project = await get_task(s, PROJECT)
            await push(s, project, read(cat))
            names = {item.name for item, _v in await resolve(s, project, LATEST)}
            await materialize(s, project, LATEST, tmp_path / "latest")
        assert "rf-003" not in names and "rf-001" not in names and "rf-001b" in names

    async def test_a_pull_waits_for_a_push_in_progress(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        import asyncio

        cat = folder(tmp_path)
        async with sessions(engine)() as s, s.begin():
            project = await default_task(s)
            await push(s, project, read(cat), label="production")
        path = cat / "scenarios" / "rf-001-happy-path.yaml"
        path.write_text(path.read_text("utf-8").replace("name:", "name: (v2)", 1), "utf-8")
        edited = read(cat)

        async with sessions(engine)() as pusher:
            await pusher.begin()
            await push(pusher, await get_task(pusher, PROJECT), edited, label="production")

            async def pull() -> list[int]:
                async with sessions(engine)() as s, s.begin():
                    chosen = await resolve(s, await get_task(s, PROJECT), "production")
                    return [v.version for i, v in chosen if i.name == "rf-001"]

            reader = asyncio.create_task(pull())
            await asyncio.sleep(0.3)
            assert not reader.done()  # it waits rather than read half of the push
            await pusher.commit()
        assert await reader == [2]

    async def test_labels_are_checked(self, engine: AsyncEngine, tmp_path: Path) -> None:
        async with sessions(engine)() as s, s.begin():
            project = await default_task(s)
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
            project = await default_task(s)
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
            project = await default_task(s)
            await push(s, project, read(folder(tmp_path)), label="production")
            await s.execute(
                text("UPDATE catalogue_versions SET path = '../escape.yaml' WHERE version = 1")
            )
            with pytest.raises(ConfigError, match="not a catalogue file path"):
                await materialize(s, project, "production", tmp_path / "out")
        assert not (tmp_path / "escape.yaml").exists()

    async def test_projects_are_separate(self, engine: AsyncEngine, tmp_path: Path) -> None:
        async with sessions(engine)() as s, s.begin():
            a_project = await ensure_project(s, "acme/a")
            assert (await ensure_project(s, "acme/a")).id == a_project.id
            a, b = await default_task(s, "acme/a"), await default_task(s, "acme/b")
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
                await push(s, await get_task(s, PROJECT), files)

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

    def test_a_wrong_password_is_named_not_a_crash(self, env: str, tmp_path: Path) -> None:
        import os

        os.environ["OPSEVAL_DATABASE_URL"] = env.replace("opseval:opseval@", "opseval:wrong@")
        res = runner.invoke(app, ["db", "status"])
        assert res.exit_code == 3 and "the database refused" in res.output
        assert "wrong" not in res.output and "Traceback" not in res.output

    def test_a_file_error_is_not_called_a_database_error(self, env: str, tmp_path: Path) -> None:
        assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
        assert runner.invoke(app, ["db", "init", "--project", PROJECT]).exit_code == 0
        assert (
            runner.invoke(
                app,
                ["catalogue", "push", str(folder(tmp_path)), "--project", PROJECT, "-m", "x"],
            ).exit_code
            == 0
        )
        blocked = tmp_path / "blocked"
        blocked.write_text("a file, not a folder", encoding="utf-8")
        res = runner.invoke(
            app,
            ["catalogue", "pull", str(blocked / "out"), "--project", PROJECT, "--label", "latest"],
        )
        assert res.exit_code == 3 and "cannot reach the database" not in res.output


async def test_a_pulled_label_runs_like_any_folder(
    engine: AsyncEngine, tmp_path: Path, settings: Settings
) -> None:
    async with sessions(engine)() as s, s.begin():
        project = await default_task(s)
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


BASE_INSTALL = """
import sys
for name in ("sqlalchemy", "asyncpg", "alembic"):
    sys.modules[name] = None  # an install without the server extra
from typer.testing import CliRunner
from opseval.cli import app
runner = CliRunner()
assert runner.invoke(app, ["--version"]).exit_code == 0
for args in (
    ["db", "upgrade"], ["db", "status"], ["db", "init", "--project", "p"],
    ["catalogue", "push", ".", "--project", "p"], ["catalogue", "pull", "x", "--project", "p"],
    ["catalogue", "list", "--project", "p"],
    ["catalogue", "history", "suite", "smoke", "--project", "p"],
    ["catalogue", "label", "suite", "smoke", "1", "x", "--project", "p"],
    ["catalogue", "list", "--project", "p", "--task", "voice"],
    ["db", "init", "--project", "p", "--task", "voice", "--kind", "voice"],
    ["task", "list", "--project", "p"], ["task", "archive", "t", "--project", "p"],
    ["task", "unarchive", "t", "--project", "p"],
    ["task", "update", "t", "--project", "p", "--kind", "voice"],
):
    res = runner.invoke(app, args)
    assert res.exit_code == 3, (args, res.output)
    assert "opseval[server]" in res.output, (args, res.output)
print("ok")
"""


def test_without_the_server_extra_every_command_says_what_to_install(tmp_path: Path) -> None:
    """The whole CLI, local mode included, must import and run with no SQLAlchemy, asyncpg or
    Alembic; each server-mode command says what to install instead of crashing."""
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-c", BASE_INSTALL],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stdout + done.stderr
