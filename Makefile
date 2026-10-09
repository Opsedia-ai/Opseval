.PHONY: install lint typecheck test check audit demo schemas status reference docs console-types console console-check

# Licences allowed for runtime dependencies of the core (see CONTRIBUTING.md).
ALLOWED_LICENCES := MIT;BSD;Apache;ISC;PSF;Python Software Foundation;MPL 2.0;MPL-2.0

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run lint-imports
	uv run bandit -c pyproject.toml -r packages/opseval/src packages/pytest-opseval/src -q

typecheck:
	uv run mypy

test:
	uv run pytest --cov=opseval --cov=pytest_opseval --cov-report=term-missing --cov-fail-under=85

check: lint typecheck test schemas status reference docs

# Needs network: known vulnerabilities and licences of the runtime dependencies in uv.lock.
audit:
	@mkdir -p build/audit
	uv export --frozen --package pytest-opseval --no-dev --no-emit-workspace --no-hashes \
		--format requirements-txt -q -o build/audit/requirements.txt
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
