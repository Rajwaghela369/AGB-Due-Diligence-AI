# Single entry point: CI runs exactly these targets, so local and CI never diverge.
.PHONY: help install smoke kb eval-data eval-data-dry recheck baseline baseline-held-out \
	baseline-smoke eval eval-held-out eval-smoke seed-prompts lint format test web web-gen \
	web-check check clean api api-up api-down

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

baseline:  ## Phase 3 one-shot baseline, kept as history; not part of the agent workflow
	$(PY) python -m evals.run_baseline --split dev

baseline-held-out:  ## Phase 3 baseline on held-out data, kept as history
	$(PY) python -m evals.run_baseline --split held_out --slice all

baseline-smoke:  ## 5 clauses, to prove the harness works without spending much
	$(PY) python -m evals.run_baseline --split dev --slice first:5

eval:  ## Agent on the stratified 70-clause dev slice, for continuous evaluation
	$(PY) python -m evals.run_agent --split dev

eval-held-out:  ## Agent on held-out data; only to confirm a configuration worth keeping
	$(PY) python -m evals.run_agent --split held_out --slice all

eval-smoke:  ## Phase 4 gate: 5 clauses through the agent, end to end
	$(PY) python -m evals.run_agent --split dev --slice first:5

seed-prompts:  ## Seed the agent prompts into Langfuse under the `evaluation` label
	$(PY) python scripts/seed_prompts.py

lint:  ## Lint, format check, and component boundary contracts
	$(PY) ruff check .
	$(PY) ruff format --check .
	$(PY) lint-imports --config .importlinter

format:  ## Apply formatting
	$(PY) ruff format .
	$(PY) ruff check --fix .

test:  ## Unit tests (pipelines logic only; the agent is measured by evals)
	$(PY) pytest -q

web:  ## Run the Vite development server
	cd web && COREPACK_ENABLE_DOWNLOAD_PROMPT=0 pnpm dev

web-gen:  ## Regenerate the committed frontend API contract
	$(PY) python scripts/export_api_schema.py
	cd web && COREPACK_ENABLE_DOWNLOAD_PROMPT=0 pnpm gen:api

web-check:  ## Lint, test, typecheck, build, and verify generated frontend files
	$(PY) python scripts/export_api_schema.py --check
	cd web && COREPACK_ENABLE_DOWNLOAD_PROMPT=0 pnpm check

check: lint test web-check  ## What CI runs

api:  ## Run the API locally with reload, against Gotenberg from compose
	docker compose up -d gotenberg
	GOTENBERG_URL=http://127.0.0.1:3000 $(PY) uvicorn app.api.main:build_app --factory --reload --host 127.0.0.1 --port 8000

api-up:  ## Build and start the API and Gotenberg with compose
	docker compose up -d --build

api-down:  ## Stop the compose stack (the stored PDFs in the files volume are kept)
	docker compose down

clean:  ## Remove build artifacts, keep raw data and gold files
	rm -rf .pytest_cache .ruff_cache **/__pycache__
