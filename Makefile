.PHONY: install test lint format check

install:
	python3 -m pip install --upgrade pip setuptools wheel
	python3 -m pip install -e ".[dev]"

test:
	python3 -m pytest

lint:
	python3 -m ruff check .
	python3 -m ruff format --check .

format:
	python3 -m ruff format .

check: lint test
