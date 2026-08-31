VENV=.venv
PYTHON=$(VENV)/bin/python
PIP=$(VENV)/bin/pip

.PHONY: help venv install install-dev start dev test lint typecheck db-upgrade db-current db-history db-heads db-check clean

help:
	@echo "Local development:"
	@echo "  venv              Create and configure virtual environment"
	@echo "  install           Install production dependencies"
	@echo "  install-dev       Install development dependencies (includes linting, testing)"
	@echo "  dev               Run FastAPI development server with auto-reload"
	@echo "  start             Run FastAPI production-like server"
	@echo "  test              Run pytest test suite"
	@echo "  lint              Run ruff and pyright checks"
	@echo "  typecheck         Run pyright type checker"
	@echo ""
	@echo "Database (SQLite):"
	@echo "  db-upgrade        Apply all pending Alembic migrations to DATABASE_URL"
	@echo "                    Use after: creating/deleting a local SQLite database, or pulling new migrations"
	@echo "  db-current        Show the revision currently applied to DATABASE_URL"
	@echo "  db-history        Show all Alembic migration history"
	@echo "  db-heads          Show current Alembic migration head(s)"
	@echo "  db-check          Check if SQLAlchemy metadata requires a new migration"
	@echo ""
	@echo "Cleanup:"
	@echo "  clean             Remove .pytest_cache and .venv"

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

# Database migrations

db-upgrade:
	$(VENV)/bin/alembic upgrade head

db-current:
	$(VENV)/bin/alembic current

db-history:
	$(VENV)/bin/alembic history

db-heads:
	$(VENV)/bin/alembic heads

db-check:
	$(VENV)/bin/alembic check

clean:
	rm -rf .pytest_cache .venv
