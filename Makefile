.PHONY: sync reauth dashboard install-scheduler help

VENV := .venv/bin
PYTHON := $(VENV)/python
STREAMLIT := $(VENV)/streamlit

help:
	@echo "Targets:"
	@echo "  make sync               Pull Google Tasks and store wake/weight data"
	@echo "  make reauth             Force Google re-consent, then sync"
	@echo "  make dashboard          Launch Streamlit charts"
	@echo "  make install-scheduler  Install hourly launchd sync job"

sync:
	$(PYTHON) -m src.sync

reauth:
	rm -f token.json
	$(PYTHON) -m src.sync

dashboard:
	$(STREAMLIT) run dashboard/app.py

install-scheduler:
	bash scheduler/install.sh
