.PHONY: install lint typecheck test check audit demo schemas console console-check

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

check: lint typecheck test schemas

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

# The web console (needs Node 20+). `npm ci` once, then these.
console-check:
	cd packages/console && npm run build

console:
	cd packages/console && npm run dev
