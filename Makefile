.PHONY: install test lint demo

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check . && uv run mypy core adapters agents

demo:
	@echo not implemented yet
