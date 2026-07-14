.PHONY: sync reauth dashboard install-scheduler probe-calendar import-takeout migrate-supabase help

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

dashboard:
	$(STREAMLIT) run dashboard/app.py

install-scheduler:
	bash scheduler/install.sh
