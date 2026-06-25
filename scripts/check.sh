#!/usr/bin/env sh
set -eu

python -m ruff check .
python -m ruff format --check .
python -m mypy ops_agent evals
python -m pytest --cov=ops_agent --cov=evals --cov-report=term-missing --cov-fail-under=70

wheel_dir="${TMPDIR:-/tmp}/ops-agent-wheel"
rm -rf "$wheel_dir"
mkdir -p "$wheel_dir"
python -m pip wheel . --no-deps -w "$wheel_dir"
