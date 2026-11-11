# Single entry point: CI runs exactly these targets, so local and CI never diverge.
.PHONY: help install smoke kb eval-data eval-data-dry recheck baseline baseline-held-out \
	baseline-smoke eval lint format test check clean

PY := uv run

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t24

install:  ## Create the venv and install all dependency groups
	uv sync --all-extras

smoke:  ## Phase 0 gate: prove every external dependency works
	$(PY) python scripts/smoke.py

kb:  ## P1 KB-INGEST: run the Prefect flow that builds and validates the index
	$(PY) python -m pipelines.p1_kb.flow

eval-data:  ## P2 EVAL-PREP: build the clause table and the Langfuse dataset
	$(PY) python -m pipelines.p2_eval.flow

eval-data-dry:  ## P2 EVAL-PREP, asserting everything but writing to no store
	$(PY) python -m pipelines.p2_eval.flow --dry-run --skip-recheck

recheck:  ## Re-score retrieval on real clause text (needs Qdrant + the embedding API)
	$(PY) python -m pipelines.p2_eval.flow --recheck-only

baseline:  ## Phase 3 baseline on the dev half - where prompt iteration belongs
	$(PY) python -m evals.run_baseline --split dev

baseline-held-out:  ## Phase 3 gate number. One run per phase; 8 void clauses only
	$(PY) python -m evals.run_baseline --split held_out

baseline-smoke:  ## 40 dev clauses, to prove the harness works without spending much
	$(PY) python -m evals.run_baseline --split dev --limit 40

eval:  ## Run the eval suite against the current agent
	@echo "Phase 3 - not implemented yet"

lint:  ## Lint, format check, and component boundary contracts
	$(PY) ruff check .
	$(PY) ruff format --check .
	$(PY) lint-imports --config .importlinter

format:  ## Apply formatting
	$(PY) ruff format .
	$(PY) ruff check --fix .

test:  ## Unit tests (pipelines logic only; the agent is measured by evals)
	$(PY) pytest -q

check: lint test  ## What CI runs

clean:  ## Remove build artifacts, keep raw data and gold files
	rm -rf .pytest_cache .ruff_cache **/__pycache__
