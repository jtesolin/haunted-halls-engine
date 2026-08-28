VENV=.venv
PYTHON=$(VENV)/bin/python
PIP=$(VENV)/bin/pip

.PHONY: help venv install install-dev start dev test lint typecheck db-upgrade db-current db-history clean

help:
	@echo "Targets: venv, install, install-dev, start, dev, test, lint, typecheck, db-upgrade, db-current, db-history, clean"

venv:
	$(PYTHON) -m venv .venv
	$(PIP) install --upgrade pip

install:
	$(PIP) install -r requirements.txt

install-dev:
	$(PIP) install -r requirements-dev.txt

start:
	$(PYTHON) -m uvicorn app.main:app --reload --port 8000

dev:
	$(PYTHON) -m fastapi dev app/main.py

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m pyright

typecheck:
	$(PYTHON) -m pyright

db-upgrade:
	$(VENV)/bin/alembic upgrade head

db-current:
	$(VENV)/bin/alembic current

db-history:
	$(VENV)/bin/alembic history

clean:
	rm -rf .pytest_cache .venv
