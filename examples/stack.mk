# Standard local stack targets for stack-orchestrator.
# Include this file in your project's Makefile: `include stack.mk` (or inline it).

.PHONY: up down local-up local-down backend-up backend-down ui-up ui-down status stack-logs

STACK_PYTHON ?= python3
STACK_RUNNER ?= $(STACK_PYTHON) scripts/stack.py

up:
	@$(STACK_RUNNER) up --scope full

down:
	@$(STACK_RUNNER) down --scope full

local-up:
	@$(STACK_RUNNER) up --scope local

local-down:
	@$(STACK_RUNNER) down --scope local

backend-up:
	@$(STACK_RUNNER) up --scope backend

backend-down:
	@$(STACK_RUNNER) down --scope backend

ui-up:
	@$(STACK_RUNNER) up --scope ui

ui-down:
	@$(STACK_RUNNER) down --scope ui

status:
	@$(STACK_RUNNER) status

stack-logs:
	@tail -n 200 -F .local-run/logs/*.log
