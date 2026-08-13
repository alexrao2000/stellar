.PHONY: sync reauth dev install-scheduler import-takeout migrate-supabase keepalive help

VENV := .venv/bin
PYTHON := $(VENV)/python

help:
	@echo "Targets:"
	@echo "  make sync                    Pull Google Tasks and store wake/weight data"
	@echo "  make reauth                  Force Google re-consent, then sync"
	@echo "  make import-takeout FILE=... Backfill full history from a Google Takeout export"
	@echo "  make migrate-supabase        Copy local SQLite data into Supabase"
	@echo "  make dev                     Run the app: check-in, calendar, charts, data"
	@echo "  make keepalive               Ping Supabase so the free project stays active"
	@echo "  make install-scheduler       Install local launchd sync job (optional)"

sync:
	$(PYTHON) -m src.sync

reauth:
	rm -f token.json
	$(PYTHON) -m src.sync

import-takeout:
	$(PYTHON) -m src.takeout_import $(FILE)

migrate-supabase:
	$(PYTHON) -m src.migrate_to_supabase

keepalive:
	$(PYTHON) -m src.keepalive

# The key stays in this process; pages fetch same-origin, so nothing ships to
# the browser. Read-only.
dev:
	$(PYTHON) -m uvicorn src.server:app --port 8000 --reload \
	  --reload-dir src --reload-dir scripts/templates --reload-include '*.html'

install-scheduler:
	bash scheduler/install.sh
