# AGB-Check

AGB-Check reviews German consumer terms and conditions. It splits a document into
clauses, retrieves relevant BGB provisions, produces grounded findings, and lets a
lawyer accept, override, or dismiss uncertain results.

The repository contains an evaluation-ready MVP. It is not legal advice and has not
been validated as a production legal service.

## Architecture

```text
web/                  React review workspace
app/api/              FastAPI API, SSE events, authentication boundary
app/ingest/           PDF/DOCX/text validation, parsing, and clause splitting
app/review/           Review lifecycle, caching, events, decisions, exports
app/agent/            LangGraph workflow and AGB/AVV judges
packages/core/        Provider clients, retrieval, masking, settings, shared contracts
pipelines/            Offline legal-index and evaluation-data pipelines
evals/                Baselines, agent evaluation, and scoring
configs/              Runtime and pipeline configuration
data/gold/            Committed evaluation and splitter fixtures
reports/              Recorded evaluation outputs
```

The main review path is:

```text
upload → normalize → split into clauses → route topic → retrieve legal text
       → structured decision → grounding check → explanation → lawyer decision/export
```

The LangGraph workflow is implemented in `app/agent/graph.py`. It uses a typed Gemini
decision for AGB topic routing, Jina for legal-query embeddings, Qdrant for retrieval,
and Gemini for the structured legal decision and German explanation. Review-start topic
results are cached and reused by the graph, so a clause is not routed twice.

AVV reviews use the same review infrastructure with a separate DSGVO norm set and
multi-label Art. 28 obligation router.

## Services

The local Docker stack contains:

- FastAPI API on `http://localhost:8000`
- Gotenberg on `http://localhost:3000` for DOCX-to-PDF conversion

The frontend runs through Vite, normally on `http://localhost:5173`.

The application connects to configured external services for Neon PostgreSQL, Neon
S3-compatible storage, Qdrant, Gemini, Jina, and Langfuse. MinIO is not required by the
current Compose configuration.

## Setup

Requirements: Python 3.12, `uv`, Docker Compose, Node.js/Corepack, and credentials for
the configured external services.

```bash
cp .env.example .env
make install
```

Set credentials in the untracked `.env`. Keep secrets out of YAML, source files, and
`.env.example`. The S3 bucket is configured in `configs/app.yaml`; credentials use the
standard `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` variables. `AWS_ENDPOINT_URL_S3`
and `AWS_REGION` override the YAML endpoint and region for deployments such as Neon Object
Storage.

## Run locally

Start the API and Gotenberg:

```bash
make api-up
```

In another terminal, start the frontend:

```bash
cd web
pnpm install
pnpm dev
```

Open `http://localhost:5173`.

The API health endpoints are:

```text
GET /api/v1/health   liveness
GET /api/v1/ready    dependency readiness
```

## Authentication

Authentication is opt-in. The backend validates OIDC bearer tokens and uses the token
subject as the document owner. The frontend contains an optional Neon Managed Better
Auth integration.

Leave authentication disabled for the default local MVP. To test Neon Auth, configure
the following in `.env` with values from the Neon Console:

```env
VITE_NEON_AUTH_ENABLED=true
AGB_AUTH_ENABLED=true
VITE_NEON_AUTH_URL=https://<branch>.neonauth.<region>.aws.neon.tech/neondb/auth
NEON_AUTH_ISSUER=<issuer>
NEON_AUTH_AUDIENCE=<audience>
NEON_AUTH_JWKS_URI=<jwks-url>
```

Enable `http://localhost:5173` as a trusted origin in Neon Auth. Neon owns its
`neon_auth` schema; this application does not duplicate or migrate those tables.

## Tests and checks

```bash
make lint          # Python lint, formatting, and import boundaries
make test          # Python unit and contract tests
make web-check     # frontend generation, lint, typecheck, tests, and build
make e2e           # browser workflow against the fake-model test API
make check         # complete local CI-style check
```

Live evaluation commands make external model calls and require explicit quota planning:

```bash
make eval-smoke
make eval
```

## Offline pipelines

```bash
make kb             # build and validate the BGB legal index
make eval-data      # prepare clause data and answer keys
make eval-data-dry  # validate evaluation data without writing stores
make recheck        # score retrieval on real clause text
```

## Current limitations

- Model quality is tracked through evaluation reports; findings require lawyer review.
- Gemini/Jina credentials and external stores are required for live reviews.
- The default configuration is an MVP configuration, not a production deployment.
- Retention, masking, authentication, and storage settings must be reviewed before
  processing real client documents.
- Generated frontend bundles include a large PDF worker chunk; this is a build warning,
  not a functional failure.

Detailed design, API contracts, evaluation results, and phase history are maintained in
`docs/`.
