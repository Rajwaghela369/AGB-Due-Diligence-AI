# Phase 9 API — masking, owners, deletion, retention, storage, login

The one source for the Phase 9 Wave 1 builders (masking, deletion, storage) and the Wave 2
builder (auth-scale). The reasoning behind each rule, with the alternatives, is in
`docs/Phase 9 Design.md`; this file is the contract. Models are in `app/domain.py` and
`app/api/schemas.py`, generated into `web/src/api/types.gen.ts` (`make web-gen`). All paths
are under `/api/v1`; every error body is `ErrorBody {code, message}` with a German message.

Until Wave 1 writes the bodies, `DELETE /documents/{id}` answers **501 `not_implemented`**
(`NotImplementedError` from `app.review.deletion`), and so does `GET /me` once
`auth.enabled` is turned on before Wave 2.

## Masking (9.A)

### The boundary

`core.masking` (in `packages/core`, so `core.llm` can use it) holds one process-wide
`MaskingBoundary`. `app.api.main.build_app` configures it from `privacy.masking`
(`masker_from_config`); the eval harness does the same. Masking is off (`masker=None`) until
9.A's gate.

```python
class MaskedText(str):            # what the masker returns; .placeholders, .unmask(text=None)
class Masker(Protocol):
    def mask(self, text: str) -> MaskedText
boundary().outbound(text) -> str  # MaskedText when enabled; `text` unchanged when off
boundary().require_masked(*values) -> None   # UnmaskedPayload for a plain str when enabled
boundary().langfuse_mask(*, data, **kwargs)  # Langfuse's `mask=` hook
configure(masker | None) -> MaskingBoundary
MaskingError(RuntimeError)        # the masker failed; nothing was sent
UnmaskedPayload(RuntimeError)     # a strict client got unmasked text
```

Placeholders are `<KIND_n>` with `KIND` one of `PERSON, ADDRESS, EMAIL, PHONE, IBAN,
REGISTER_NUMBER`, numbered from 1 per masked text in order of first appearance; the same
value twice in one text is one placeholder. `MaskedText.placeholders` maps placeholder →
original. The map is never stored, logged or traced.

### Where it sits (every outbound path)

| Path | What is masked | Where the call goes |
| --- | --- | --- |
| `core.llm.decide(text, …)` | `text` | Gemini Decisions API |
| `core.llm.complete(system, user, …)` | `user` | Gemini / Anthropic generation |
| `core.llm.complete_stream` (Phase 8 chat, if added there) | `user` | Gemini streaming |
| `core.embeddings.embed_texts(texts, …)` | each text, **after** its cache key `content_hash(text)` is computed | Jina embeddings |
| `core.routing.classify_topics(texts, …)` | each text | Jina classifier |
| `core.langfuse_client.client()` | everything set through `start_observation`/`update` | Langfuse, via `Langfuse(mask=boundary().langfuse_mask)` |
| `FeedbackService` score `comment`s | the comment | Langfuse `create_score` (not covered by `mask=`) |
| `Corrections` dataset items | `input`, `expected_output`, `metadata` strings | Langfuse `create_dataset_item` (not covered by `mask=`) |

Rules every caller keeps:

1. **Client text goes only into `text`/`user`.** System prompts are our own versioned
   templates and are not masked; never put a clause, a note or chat history into one.
2. **Mask the final string.** Build the whole user message, then let the function mask it.
   A `MaskedText` survives no string operation, so a prompt assembled from masked parts is
   a plain `str` and would be masked again (harmless) or refused by a strict client.
3. **A masker failure fails the call, not the run.** `outbound` raises `MaskingError`;
   `decide`/`complete` turn it into a failed result (`error="MaskingError: …"`) **without
   sending**, like any provider error. `NotImplementedError` (the Wave 0 stub) propagates.
4. **Cache keys stay on the unmasked text.** `content_hash` is computed before masking,
   so verdicts, embeddings and retrievals cached before 9.A are still found.
5. **Unmask only for display, inside the same function.** `core.llm.complete` (and
   `complete_stream`, per chunk, holding back a partial `<KIND_` at a chunk's end) return
   the answer already restored with `masked.unmask(answer)`, so callers never see
   placeholders and never hold the map. The answer is stored unmasked, in our own
   database, like the clause itself. The Langfuse observation of that call records the
   masked prompt and the masked answer.
6. **Tests use strict fakes.** Every fake provider client calls
   `boundary().require_masked(payload)` on what it receives;
   `tests/test_phase9_contracts.py` shows one (`StrictClient`).

`privacy.masking.required: true` makes `build_app` refuse to start when masking is enabled
but the masker cannot load (Presidio or the spaCy model missing), instead of failing every
call.

## Owners and login (9.E)

### Owner model

- `Document.owner_id`, `Review.owner_id`: who owns the row. Default
  `DEFAULT_OWNER_ID = "local"`; `auth.default_owner` overrides it for new rows and for the
  migration.
- `Document.source_hash`: sha256 of the uploaded bytes; `None` on construction means
  "same as `document_id`".
- `document_id_for(source_hash, owner_id)`: the document id. The default owner keeps the
  plain source hash (every existing id and URL stays valid); any other owner gets
  `sha256(owner_id + "\0" + source_hash)`. Two owners uploading one file get two documents.
- `blob_key(document)`: `"{source_hash}.pdf"`, or `.txt` for pasted text. Documents of the
  same bytes share one blob.
- `Review.owner_id` is the document's owner in v1 (no sharing yet).
- `Principal {subject, issuer, name, roles, authenticated}`; `.owner_id` is the subject;
  `.reviewer()` gives the audit trail's `Reviewer` from the session.
- `default_principal(owner_id)`: the caller while login is off.
- Migration `006_owners_deletion.sql` adds `owner_id` and `source_hash` to `documents`,
  `owner_id` to `reviews` and `uploads`, and fills existing rows with the default owner
  (`apply_pending(default_owner=…)` sets `agb.default_owner` for each migration).
- `DocumentRepo.list(limit, *, owner_id=None)` filters by owner. Every other per-owner
  check is Wave 2.

### `GET /me`

- **Response 200:** `PrincipalOut {owner_id, name, roles, authenticated, auth_enabled}`.
  With login off: `{owner_id: auth.default_owner, name: null, roles: [], authenticated:
  false, auth_enabled: false}`.
- **401 `unauthenticated`** (Wave 2): login on and no valid bearer token.
- **501** until Wave 2 when `auth.enabled` is true (fails closed).

### Login rules for Wave 2

- The SPA signs in with the authorization code flow plus PKCE against `auth.oidc.issuer`
  as the public client `auth.oidc.client_id`, and sends `Authorization: Bearer <access
  token>` on every request (CORS adds `Authorization` to the allowed headers).
- The API validates the token's signature against the issuer's JWKS (discovered from
  `/.well-known/openid-configuration`, cached), `iss`, `aud == auth.oidc.audience`, `exp`
  and `nbf`; then builds the `Principal` from `sub`, `name` (`auth.oidc.name_claim`) and the
  roles at `auth.oidc.roles_claim` (a dotted path). Nothing provider-specific beyond that.
- `app.api.deps.principal(request)` is the one dependency every route uses.
  `X-Reviewer` is ignored when login is on; `Reviewer` comes from `principal.reviewer()`.
- **Another owner's row is 404, never 403**: a 403 would confirm that the id exists. The
  role `auth.oidc.admin_role` may read and delete any owner's documents.
- Every repository read and write that takes an id is checked against the owner (Wave 2
  decides between a `owner_id` parameter on each method and Postgres row-level security;
  see the design doc).

## Deletion (9.B)

### `DELETE /documents/{document_id}?dry_run=false`

- **Who:** the owner (or the admin role). With login off, anyone, as every route.
- **Response 200:** `DeletionOut {report: DeletionReport, pending_erasures}`.
  `pending_erasures` counts `report.erasures` without `done_at`: external deletions that
  failed during the request and will be retried by the nightly job. With `dry_run=true`
  nothing changes, `report.dry_run` is true, `report.tombstone` is null, and `erasures`
  lists what would be queued.
- **404 `not_found`:** unknown document, or another owner's.
- **409 `deletion_blocked`:** `DeletionBlockedBody {code, message, review_ids}`: a review
  of the document is `queued` or `running`. Idle reviews (`finished`,
  `awaiting_decisions`, `failed`) are deleted with the document.
- **501 `not_implemented`:** until Wave 1.

### What goes, in one transaction

`DeletionRepo.delete_document(document_id, deleted_by, reason)`; the counts by table go into
`DeletionReport.removed` with the `DELETION_TABLES` keys.

1. Refuse (`ValueError` → `DeletionBlocked` in the service) if a review is queued or running.
   In Postgres, lock the document row (`select … for update`) first, so a review cannot
   start in between.
2. **The document's own rows:** `uploads`, `document_clauses`, `documents`; every review of
   it with `review_clauses`, `run_events`, `decisions`, `decision_events` and `feedback`.
   `decision_events` is append-only; the transaction runs `set local agb.erasure = 'on'`,
   which the trigger (migration 006) lets through for DELETE only.
3. **Content-addressed rows, by reference count.** For each `content_hash` the document's
   clauses used, count references that remain after step 2: `document_clauses` of other
   documents, plus the evaluation `clauses` table if it exists. At zero, delete its
   `clause_texts`, `verdicts` (every agent version), `embeddings`, `retrievals` and `topics`
   rows; otherwise count it in `kept_shared`.
4. **Summary paragraphs:** `summary_texts` where `review_id` is one of the deleted reviews
   (the column is new in 006; see "Open points").
5. **Queue the erasures** (`erasure_tasks`, unique per pending `(target, key)`):
   - `blob`: `blob_key(document)`, only if no other document has the same `source_hash`;
   - `trace`: each deleted review's `trace_id`, and the `trace_id` of each deleted verdict;
   - `score`: each deleted feedback row's Langfuse score ids
     (`app.review.feedback._stable_id(feedback_id, step)` for each step in
     `Feedback.observations`): the scores sit on verdict traces that may be shared and stay;
   - `dataset_item`: `correction_id(content_hash, review_id)` for every decision of a
     deleted review that produced a lawyer correction;
   - `checkpoint`: `"{review_id}:{position}"` for every clause of a deleted review that
     was ever `awaiting_decision`.
6. **Append the tombstone** (`deletion_tombstones`): kind, subject id, owner, who
   (`Principal.owner_id`, or null for retention), authenticated, reason, the `removed`
   counts, and the time. No filename, no clause text, no note.

After the commit, `Service.delete_document` runs `process_erasures` once. Each task is
idempotent: `FileStore.delete` returns False for a missing key, and Langfuse's 404 counts
as done. A failure increments `attempts` and stays pending; after
`retention.erasure_max_attempts` it is still pending and reported by the sweep.

`delete_review` is the same without steps 1's document lock (the review must be idle), the
document's own rows, or step 3 (verdicts belong to documents, not reviews).

### The orphan sweep

`DeletionRepo.sweep_orphans()` removes content-addressed rows with no reference (a crash
before 9.B existed, or a clause text saved by an upload that never finished), and queues
blobs whose key no `documents.source_hash` names (the storage backend lists keys). It must
not race an upload: it ignores rows younger than one hour.

### What is not deleted

- **Statute data** (Qdrant, `bgb_chunks.jsonl`): no client data.
- **Tombstones**: kept, by design (they are the proof of deletion).
- **Database backups**: Neon keeps a point-in-time history (the branch's history
  retention window); a deleted row disappears from backups only when that window passes.
  `docs/DSGVO.md` (9.F) states the window.

## Retention (9.C)

`retention` in configs/app.yaml, read by `app.review.retention.retention_settings`:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | false | Off: the job still runs the orphan sweep and pending erasures, but expires nothing. |
| `files_days` | null | A document whose last upload is older is deleted through 9.B (reason `retention`). |
| `reviews_days` | null | An idle review that finished earlier is deleted through `delete_review`. |
| `traces_days` | 30 | Langfuse traces of reviews and verdicts created earlier are queued for erasure. |
| `erasure_max_attempts` | 10 | After this many failures a task is reported each run (still retried). |
| `batch` | 100 | Ids per data class per run. |

A period is a whole number of days ≥ 1, or null (keep). The job is
`python -m app.review.retention` (Wave 1 adds the `__main__`), run once a night outside
the API process; it builds the service like `build_app` and calls `Service.sweep(now)`,
which returns a `SweepReport` and logs it as JSON.

## Storage (9.D)

`app.store.interfaces.FileStore`:

```python
put(key, data) -> str              # no-op if present (S3: If-None-Match: *); SSE on every write
get(key) -> bytes                  # FileNotFoundError if absent
exists(key) -> bool
delete(key) -> bool                # True if it existed; idempotent
presign(key, expires_in) -> str | None   # None: cannot sign (local); the API proxies
```

- `files.backend: local | s3` chooses `LocalFileStore(root)` or
  `S3FileStore(S3Settings)` through `app.store.s3.file_store_from_config`.
- `files.s3`: `endpoint_url`, `region`, `bucket`, `prefix`, `server_side_encryption`
  (`AES256` or `aws:kms`), `presign_seconds`, `download: proxy | redirect`. Credentials
  only from the environment: `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`.
- `GET /documents/{id}/file` keeps its response for `download: proxy` (the default). With
  `redirect` it answers **307** to `presign(blob_key(document), presign_seconds)`, with
  `Cache-Control: no-store`; the immutable cache headers stay on the proxied response only.
- `scripts/migrate_files_to_s3.py` (Wave 1) copies every key from the local root to the
  bucket with `put`, verifies each by size and sha256, is resumable (skips keys that
  `exists`), and never deletes the local copy.
- Docker Compose gains a `minio` service (and a bucket-creating init step) for development
  and tests; tests against it are skipped without `TEST_S3_ENDPOINT`, like Postgres.

## Errors added

| Status | `code` | When |
| --- | --- | --- |
| 401 | `unauthenticated` | login on, no or invalid token (Wave 2) |
| 404 | `not_found` | also: another owner's document or review |
| 409 | `deletion_blocked` | a review of the document is queued or running |
| 501 | `not_implemented` | a Wave 1 or Wave 2 body not written yet |

## Open points

- **Summary paragraphs and `review_id`.** `CacheRepo.put_summary_text` (Phase 8) has no
  `review_id`. Migration 006 adds the nullable column; once Phase 8 Wave 1 is merged, the
  summary should pass it (a one-line contract change), so step 4 can find every paragraph.
  Until then the sweep deletes `summary_texts` rows with a null `review_id` older than
  `reviews_days`.
- **`uploads.owner_id`** is added and backfilled, but `DocumentRepo.record_upload` does not
  take an owner yet; Wave 2 adds the parameter.
