.PHONY: sync reauth dev dashboard install-scheduler probe-calendar import-takeout migrate-supabase keepalive restore-task help

VENV := .venv/bin
PYTHON := $(VENV)/python
STREAMLIT := $(VENV)/streamlit

help:
	@echo "Targets:"
	@echo "  make sync                    Pull Google Tasks and store wake/weight data"
	@echo "  make reauth                  Force Google re-consent, then sync"
	@echo "  make probe-calendar          Inspect how recurring tasks appear in Calendar"
	@echo "  make import-takeout FILE=... Backfill full history from a Google Takeout export"
	@echo "  make migrate-supabase        Copy local SQLite data into Supabase"
	@echo "  make dev                     Run the app locally (calendar + weight, reads Supabase)"
	@echo "  make keepalive               Ping Supabase so the free project stays active"
	@echo "  make restore-task ARGS=...   Un-delete / re-date a Google Task in place"
	@echo "  make dashboard               Launch Streamlit charts"
	@echo "  make install-scheduler       Install local launchd sync job (optional)"

sync:
	$(PYTHON) -m src.sync

reauth:
	rm -f token.json
	$(PYTHON) -m src.sync

probe-calendar:
	$(PYTHON) -m scripts.probe_calendar

import-takeout:
	$(PYTHON) -m src.takeout_import $(FILE)

migrate-supabase:
	$(PYTHON) -m src.migrate_to_supabase

keepalive:
	$(PYTHON) -m src.keepalive

# Dry run by default; add --apply to ARGS to actually write.
restore-task:
	$(PYTHON) -m scripts.restore_task $(ARGS)

# The key stays in this process; pages fetch same-origin, so nothing ships to
# the browser. Read-only.
dev:
	$(PYTHON) -m uvicorn src.server:app --reload --port 8000

dashboard:
	$(STREAMLIT) run dashboard/app.py

install-scheduler:
	bash scheduler/install.sh
