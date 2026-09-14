.DEFAULT_GOAL := help
.PHONY: test check-quality install-hooks help

help:
	@echo "rig commands:"
	@echo "  make test           - Run test suite"
	@echo "  make check-quality  - Run all quality gates in series"
	@echo "  make install-hooks  - Configure git pre-commit hook"

install-hooks:
	@git config core.hooksPath .githooks
	@chmod +x .githooks/pre-commit
	@echo "✅ Pre-commit hook configured via core.hooksPath=.githooks"

test:
	uv run pytest tests/

check-quality:
	python3 scripts/check_anti_tamper.py
	python3 scripts/check_cycles.py
	uv run ruff check --config ruff.toml --ignore-noqa src/
	uv run ruff check --config ruff.toml .
	uv run ruff format --config ruff.toml --check .
	npx jscpd src/ --threshold 0 --min-tokens 40 --min-lines 5 --format python
	python3 scripts/check_density.py
	python3 scripts/check_lines.py
	uv run lint-imports
	uv run deptry .
	uv run --isolated --python 3.11 python -c "import rig"
	uv run pytest --cov=src/rig --cov-fail-under=80 tests/
