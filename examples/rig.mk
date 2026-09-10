# Standard local dev targets for rig.
# Include this file in your project's Makefile: `include rig.mk` (or inline it).

.PHONY: up down local-up local-down backend-up backend-down ui-up ui-down status logs

RIG ?= rig

up:
	@$(RIG) up --scope full

down:
	@$(RIG) down --scope full

local-up:
	@$(RIG) up --scope local

local-down:
	@$(RIG) down --scope local

backend-up:
	@$(RIG) up --scope backend

backend-down:
	@$(RIG) down --scope backend

ui-up:
	@$(RIG) up --scope ui

ui-down:
	@$(RIG) down --scope ui

status:
	@$(RIG) status

logs:
	@tail -n 200 -F .local-run/logs/*.log
