.DEFAULT_GOAL := help
.PHONY: test help

help:
	@echo "rig commands:"
	@echo "  make test  - Run test suite"

test:
	pytest tests/
