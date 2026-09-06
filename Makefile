.PHONY: install test lint demo demo-fast

SOURCE ?=
MAP ?=

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check . && uv run mypy core adapters agents

demo: install
	uv run python -u -m scripts.run_demo --source "$(SOURCE)" --map "$(MAP)"

demo-fast: install
	uv run python -u -m scripts.run_demo --fast --source "$(SOURCE)" --map "$(MAP)"
