.PHONY: help install lint format typecheck test test-unit test-integration guard db-up db-down migrate seed smoke catalog api ui evals docker-up docker-down clean

help:
	@echo "install           install the package with dev extras"
	@echo "lint / format     ruff check / ruff format"
	@echo "typecheck         mypy"
	@echo "test              full pytest suite with coverage"
	@echo "guard             the sql_guard hostile-query suite (security regression net)"
	@echo "db-up / db-down   start / stop just Postgres"
	@echo "migrate           apply pending SQL migrations from db/migrations/"
	@echo "seed              load the Olist dataset into Postgres"
	@echo "smoke             assert the read-only role really is read-only"
	@echo "catalog           regenerate docs/metrics-catalog.md from the YAML definitions"
	@echo "api / ui          run the API / the Streamlit interface locally"
	@echo "evals             run the evaluation suite and write a report"
	@echo "docker-up/-down   the whole stack"

install:
	pip install -e ".[dev]"

lint:
	ruff check .

format:
	ruff format .

typecheck:
	mypy

test:
	pytest -v --cov=src/analyst_agent

test-unit:
	pytest tests/unit -v

test-integration:
	pytest tests/integration -v -m integration

guard:
	pytest tests/unit/test_sql_guard.py -v

db-up:
	docker compose up -d db

db-down:
	docker compose stop db

migrate:
	python scripts/migrate.py

migrate-status:
	python scripts/migrate.py --status

seed:
	python scripts/seed_db.py

smoke:
	python scripts/smoke.py

catalog:
	python scripts/generate_metrics_catalog.py

api:
	uvicorn analyst_agent.api.main:app --reload --port 8000

ui:
	streamlit run src/analyst_agent/ui/streamlit_app.py

evals:
	python -m evals.runner --all --report evals/reports/

docker-up:
	docker compose up

docker-down:
	docker compose down -v

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
