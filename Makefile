.PHONY: install lint typecheck test check demo

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

test:
	uv run pytest --cov=opseval --cov-report=term-missing

check: lint typecheck test

demo:
	uv run opseval demo
