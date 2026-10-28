.PHONY: install hooks lint typecheck test check audit demo schemas status reference docs rulesets console-types console console-check db-up db-down

# Licences allowed for runtime dependencies of the core (see CONTRIBUTING.md).
ALLOWED_LICENCES := MIT;BSD;Apache;ISC;PSF;Python Software Foundation;MPL 2.0;MPL-2.0

install:
	uv sync

# The pre-push hook (ADR-0065): refuses a push to main, runs ruff and the schema check first.
# Sets core.hooksPath in this clone's .git/config only. Setting it switches off every other
# hook of the clone, so it refuses (unless FORCE=1) when core.hooksPath already points
# elsewhere or the hooks directory has hooks of its own (anything but the *.sample files).
hooks:
	@current=$$(git config --get core.hooksPath || true); \
	if [ -n "$$current" ] && [ "$$current" != scripts/githooks ]; then \
		echo "make hooks: core.hooksPath is already $$current; setting it would switch those hooks off." >&2; \
		if [ "$(FORCE)" != 1 ]; then echo "  Move them to scripts/githooks, or run: make hooks FORCE=1" >&2; exit 1; fi; \
	elif [ -z "$$current" ]; then \
		dir=$$(git rev-parse --git-path hooks) || exit 1; \
		own=$$(find "$$dir" -maxdepth 1 ! -type d ! -name '*.sample' 2>/dev/null | sort); \
		if [ -n "$$own" ]; then \
			echo "make hooks: $$dir has hooks that core.hooksPath would switch off:" >&2; \
			echo "$$own" | sed 's/^/  /' >&2; \
			if [ "$(FORCE)" != 1 ]; then echo "  Move them to scripts/githooks, or run: make hooks FORCE=1" >&2; exit 1; fi; \
		fi; \
	fi; \
	git config --local core.hooksPath scripts/githooks && \
	echo "pre-push hook installed (scripts/githooks/pre-push)"

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run lint-imports
	uv run bandit -c pyproject.toml -r packages/opseval/src packages/pytest-opseval/src -q

typecheck:
	uv run mypy

test:
	uv run pytest --cov=opseval --cov=pytest_opseval --cov-report=term-missing --cov-fail-under=85

check: lint typecheck test schemas status reference docs rulesets

# Needs network: known vulnerabilities and licences of the runtime dependencies in uv.lock.
audit:
	@mkdir -p build/audit
	uv export --frozen --package opseval --extra server --no-dev --no-emit-workspace --no-hashes \
		--format requirements-txt -q -o build/audit/requirements-core.txt
	uv export --frozen --package pytest-opseval --no-dev --no-emit-workspace --no-hashes \
		--format requirements-txt -q -o build/audit/requirements-pytest.txt
	sort -u build/audit/requirements-core.txt build/audit/requirements-pytest.txt | grep -v '^[[:space:]]*#' \
		> build/audit/requirements.txt
	uv run pip-audit --disable-pip --no-deps -r build/audit/requirements.txt
	uv venv -q --allow-existing build/audit/venv
	uv pip install -q --python build/audit/venv/bin/python -r build/audit/requirements.txt
	uv run pip-licenses --python build/audit/venv/bin/python --from=mixed \
		--partial-match --allow-only="$(ALLOWED_LICENCES)"

demo:
	uv run opseval demo

# The models are the source of truth; a drifted schema would make an editor accept a
# file the loader rejects.
schemas:
	uv run opseval schemas --check

# docs/STATUS.md: the completion percentages are computed from its table, never typed.
status:
	uv run python scripts/project_status.py --check

# docs/reference/ is generated from the scorer registry and config.Settings; a stale page fails.
reference:
	uv run python scripts/reference_docs.py --check

# .github/rulesets/main.json is a valid payload and requires exactly the checks ci.yml reports
# (ADR-0065). A dry run: nothing is sent to GitHub.
rulesets:
	bash scripts/apply_rulesets.sh --dry-run >/dev/null

# The documentation site, built strictly (a broken link or anchor fails). Not deployed before Gate 0.
# Never add --quiet: it hides the warnings that strict mode counts, so nothing would fail.
docs:
	uv run mkdocs build

# Regenerate packages/console/src/lib/api.gen.ts from the server's models. Drift is caught by
# the test suite (tests/test_console_types.py), so there is one check, not three.
console-types:
	uv run python scripts/console_types.py

# The web console (needs Node 22.22+ or 24.15+, as package.json `engines` says). `npm ci` once, then these.
console-check:
	cd packages/console && npm test && npm run build

console:
	cd packages/console && npm run dev

# Server mode (ADR-0047): a local Postgres for `opseval db` and the database tests, on
# 127.0.0.1 only. Without OPSEVAL_TEST_DATABASE_URL those tests skip locally (never in CI).
db-up:
	docker compose up -d --wait postgres
	@echo "export OPSEVAL_TEST_DATABASE_URL=postgresql://opseval:opseval@127.0.0.1:$${OPSEVAL_PG_PORT:-5433}/opseval"
	@echo "export OPSEVAL_DATABASE_URL=postgresql://opseval:opseval@127.0.0.1:$${OPSEVAL_PG_PORT:-5433}/opseval"

db-down:
	docker compose down
