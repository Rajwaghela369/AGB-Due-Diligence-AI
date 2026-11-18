# Study note: privacy and DSGVO in AGB-Check

This note explains how the Phase 9 privacy work is built and why. It is for a developer who knows Python and web APIs but not DSGVO. Line numbers refer to branch `stage` after Phase 9. Things I could not confirm are marked **(unsure)**. This is an engineering note, not legal advice.

Sources: `docs/Phase 9 Design.md` (reasoning), `docs/Phase 9 API.md` (contract), `docs/DSGVO Processor Checklist.md`, and the review reports in `agb-check-wt/reports/` (`p9-masking-review.md`, `p9-deletion-review.md`, `p9-storage-review.md`).

## 1. Why it matters here

AGB-Check reads standard terms that a law firm's client hands over. Those documents name people: a managing director, a contact with an e-mail address, a sole trader with a register number, an IBAN. That is personal data of third parties, held under a lawyer's professional secrecy. Four duties follow:

- **Art. 5(1)(c), data minimisation.** Send a processor only what it needs. Gemini needs the legal meaning of a clause, not the guarantor's name. This is masking.
- **Art. 5(1)(e) and Art. 17, storage limitation and erasure.** Do not keep data longer than needed; delete on request. This is deletion and retention.
- **Art. 25, data protection by design.** Build the protection into the structure, not into each feature. This is why masking sits in one boundary.
- **Art. 32, security.** Encryption and access control. This is storage, login and owner checks.

**§ 43e BRAO** adds that a lawyer may give client secrets to a service provider only as far as necessary, must choose the provider carefully, and must bind it to confidentiality in text form. The checklist (section 3) summarised the statute through a fetch tool and tells you to read it yourself. I repeat that.

The phase gate in testable form: no unmasked personal data in Langfuse or any LLM request; a deleted document leaves nothing behind that only it used; every store EU-hosted; per-user access enforced.

Keep this in mind: in the shipped `configs/app.yaml`, masking (line 148), retention (166), auth (176) and S3 storage (`backend: local`, line 130) are all **off**. The code exists and is tested. It is not active until someone turns it on.

## 2. Masking

### 2.1 What it does

Before text leaves the server, personal data becomes placeholders like `<PERSON_1>`. The model judges the clause equally well, because whether a liability exclusion is void does not depend on whose name is in it.

A real example. The fixture `tests/fixtures/pii/corpus.json` (id `guarantor`) was run through `PresidioMasker(allow_regex_only=True)` offline; this is the actual output:

```
Before (stored, shown to the lawyer):
  Für die Verbindlichkeiten des Bestellers bürgt Frau Lieselotte Brennecke, geb. Hartmann,
  geboren am 14.07.1961, wohnhaft Kastanienallee 3a, 04109 Leipzig, Steuer-ID 47 036 892 815.

After (what Gemini, Jina and Langfuse see):
  Für die Verbindlichkeiten des Bestellers bürgt Frau <PERSON_1>, geb. <PERSON_2>,
  geboren am <DATE_OF_BIRTH_1>, wohnhaft <ADDRESS_1>, Steuer-ID <TAX_ID_1>.

Map, in memory only: <PERSON_1> = Lieselotte Brennecke, <PERSON_2> = Hartmann,
  <DATE_OF_BIRTH_1> = 14.07.1961, <ADDRESS_1> = Kastanienallee 3a, 04109 Leipzig,
  <TAX_ID_1> = 47 036 892 815
```

Statute citations such as "§ 309 Nr. 7 BGB" are marked protected and left alone (`_protected`, `packages/core/masking.py:635`).

### 2.2 Detection

Two layers:

1. **German regexes** for things with a shape: IBAN with its checksum (`_iban_valid`, `masking.py:431`), phone, e-mail, tax IDs, HRA/HRB numbers, dates of birth (only after "geb." or "geboren am"), streets and postcodes. Names have no shape, so regexes find them by context: after "Frau", "Geschäftsführer:", "Kontoinhaber:", "geb.", or above an address (`_persons`, `masking.py:477`).
2. **Presidio with a German spaCy model** (NER) finds names with no context (`PresidioMasker.detect`, `masking.py:785`).

Overlapping hits keep the longest, so an IBAN beats the digits inside it (`_resolve`, `masking.py:673`).

### 2.3 One boundary, inside the outbound functions

The design weighs masking at every call site, in a network proxy, or inside the few functions that talk to providers. It chose the last: every feature already goes through them, so a new feature gets masking for free. The wiring:

- `decide`: `outbound` then `require_masked` (`packages/core/llm.py:234-235`). `complete`: `llm.py:267-269`. `complete_stream`: `llm.py:524-525`.
- Langfuse: `Langfuse(mask=boundary().langfuse_mask)` (`packages/core/langfuse_client.py:22-27`). The SDK calls the hook on every observation's input, output and metadata. Score comments and dataset items are not observations, so they mask explicitly (`trace_text`, `masking.py:1122`).

`MaskedText` (`masking.py:100`) is a `str` subclass. Any string operation (`+`, `format`, slicing) returns a plain `str`, so text built after masking is unmasked by type. Strict fake clients call `require_masked` (`masking.py:1073`) and refuse any plain `str`. That catches a future change that assembles a prompt after the boundary, whatever the prompt says.

### 2.4 Scoped placeholders, and unmasking for display

A **scope** (`MaskingScope`, `masking.py:832`; opened by `scope()`, `masking.py:1000`) is one owner's one unit of work: one clause, one summary, one chat message. Inside it, numbering is shared, and a value found once is masked wherever it recurs, including other case or a genitive "s" (`_recurrences`, `masking.py:934`). The scope ends with its `with` block. Nothing outlives it. The judge opens one per clause, primed with clause text and heading (`app/agent/run.py:204-215`).

The model's answer may quote placeholders. `MaskedText.display` (`masking.py:130`) restores them inside `core.llm.complete`, so callers never see a placeholder and never hold the map. The map is never stored, logged or traced, because a stored map would be the personal data again. The answer is stored unmasked in our own database, like the clause. Only what leaves the server is masked. The cache key `content_hash` is computed on the unmasked text and never leaves the server.

### 2.5 Failing closed

If the masker raises, nothing is sent. `outbound` wraps any exception in `MaskingError` (`masking.py:1046`). `decide` and `complete` return a failed result without calling the provider; `complete_stream` returns a failed completion (`llm.py:526-527`). If the Langfuse hook fails, the SDK records its own "fully masked" fallback. A score comment that cannot be masked is dropped. A missing spaCy model raises `MaskerUnavailable` (`masking.py:762`), which stops the API at start.

### 2.6 The regex-only trade-off

`privacy.masking.allow_regex_only: true` accepts the regex layer alone (default `false`, `configs/app.yaml:153`). Labelled names and numbers are caught; a name with no context ("Die Ware holt Gundula Quast ab") is not. The default refuses to start, because a silent regex-only run leaks exactly the names nobody labelled. `pyproject.toml` lists `presidio-analyzer` and `spacy`, but the model `de_core_news_sm` is a separate download. Whether the Docker image contains it: **(unsure)**. All tests I ran were regex-only.

### 2.7 The leaks the review found, and how the redesign closed them

The first version kept one process-wide memory of every value it had seen (`_known`, up to 4,096 entries), so a name found once was masked everywhere later, including in a model answer traced alone. This memory caused three of the four High findings.

**Leak 1: the memory was lossy (finding 2).** Langfuse output masking depended on recall. The memory was lost on eviction, restart, or when another process wrote the trace. Probe: mask "Kontoinhaber: Lieselotte Brennecke", then 2,100 other names; then "Lieselotte Brennecke haftet persönlich." reached Langfuse unmasked. It leaked because the lone string had no context for the regexes and the memory had forgotten the name. *Closed by* removing the memory: the scope of the request that produced the answer knows the name, however much traffic ran between. Test: `test_probe_6_…` (`tests/test_masking_probes.py:214`).

**Leak 2: the memory crossed owners (finding 3).** One memory served all owners. Owner A's imprint "Ansprechpartnerin: Frau Kaufmann" stored `Kaufmann`. Owner B's clause "Ist der Kunde Kaufmann, … Gerichtsstand Hamburg" then went to Gemini as "Ist der Kunde <PERSON_1>", which deletes the decisive term (§ 38 ZPO). The verdict was cached under B's `content_hash`, shared by all owners, so the damage persisted. Surnames that are ordinary words (Sommer, Lange) behave the same. Masking also stopped being deterministic, so the "verdicts do not change" eval could not be reproduced. *Closed by* having no global state. Tests: probe 5 (`:162`) and probe 7 (`:229`).

**Leak 3: a cached verdict carried a name from one owner's heading (finding 8).** The explain prompt includes the heading, which belongs to one owner ("§ 9 Haftung von Herrn Dr. Klaus Brenner"). The model's reasoning could quote that name, the answer was unmasked and cached by clause text, and owner B with the same text and another heading was served A's name. *Closed by* `restore_from`: the scope knows which placeholders came from the clause text itself. A placeholder from a heading, a note, or one the model invented shows as `[unbekannt]` (`display`, `masking.py:130`; `_restorable`, `masking.py:980`; the judge passes `restore_from=[clause.text]`, `run.py:214`). The design chose "cache only owner-independent text" over keying the cache by owner, which would multiply model calls by the number of owners. Test: `tests/test_pii_leak.py:232`.

**Leak 4: the summary prompt (finding 1).** The summary sends the canonical JSON of every finding to Gemini, including `decided_by` (the reviewer's name) and the decision `note`. In JSON, a bare `"Raj Waghela"` has no "Herr" before it, newlines are the two characters `\n`, and an excerpt can be cut mid-name, so the detectors had no context. The probe showed Gemini receiving the reviewer's full name, a client's first name and a postcode. *Closed by* `_masked_prompt` (`app/review/summary.py:336`): it masks each string field on its own before serialising, and fields like `decided_by` become one `PERSON` placeholder via `mask_as` (`masking.py:897`). The summary scope uses `restore_from=texts` (`summary.py:293-295`), so the reviewer can never be restored into the cached recommendation. Test: `test_probe_3_…` (`test_masking_probes.py:130`).

The re-check found two Low items, both fixed in the last commit: a cut excerpt leaked a lone first name (R1; `_token` now indexes first names, `masking.py:906`), and an upper-case genitive such as "QUARCKS" was missed (R2; `casefold`, `masking.py:973`). Tests at `test_masking_probes.py:329` and `:339`.

Other fixes: false positives that change meaning ("Paul Hartmann AG" as a person) and statute citations swallowing a neighbouring phone number (`_outside`, `masking.py:641`, now keeps the part outside the citation). Lesson: false positives are bugs too, because they can change a legal verdict. Still open per the reviewer: OpenTelemetry exception events from code outside the wrapped observations are not masked. No exception message carries clause text today, but nothing enforces it.

## 3. Deletion

### 3.1 Reference counting

The app is content-addressed: a clause text, verdict, embedding and retrieval are stored once per `content_hash`, however many documents contain the clause. So deleting a document cannot simply delete clause rows. A shared row goes only when no remaining document references its hash:

```
Document A: h1, h2, h3     Document B: h2, h4
Delete A -> h1, h3 deleted. h2 kept ("kept_shared").
```

The count is taken inside the transaction after the document's own rows are gone (`_partition_hashes`, `app/store/deletion.py:1090`). The eval `clauses` table also counts as a reference. Blobs work the same way: documents with equal `source_hash` share one file, queued for deletion only if no other document uses it.

### 3.2 The cascade

`PostgresDeletionRepo.delete_document` (`deletion.py:663`) is one transaction: take the content lock and the document row lock; delete the document's rows and every review with its events, decisions, feedback, run events and summary paragraphs (`_delete_review_rows`, `deletion.py:1118`); delete unreferenced content rows (`_delete_content_rows`, `deletion.py:1139`); write a **tombstone** (who, when, why, row counts, no content); queue external work.

`decision_events` is append-only by trigger. Migration 006 lets DELETE through only inside a transaction that ran `set local agb.erasure = 'on'` (`deletion.py:42`, `:673`; `app/store/migrations/006_owners_deletion.sql:46`).

**LangGraph checkpoints** hold clause text of an interrupted clause. An outbox task `checkpoint` with key `"{review_id}:*"` deletes the threads by prefix through the judge's strict eraser (`app/agent/run.py:389-396`).

The review found two cascade bugs. The eraser was never attached to the real judge (tests injected a fake that had it), so every checkpoint task would have failed for ever. And summary paragraphs were never erased, because `summary_texts.review_id` was always NULL. Both are fixed: `put_summary_text` now gets the review id (`summary.py:194`), and `forget` is attached at `run.py:396`.

### 3.3 The outbox

A transaction cannot include an HTTP call to Langfuse. So it writes one `erasure_tasks` row per external thing (blob, trace, score, dataset item, checkpoint) and commits. A worker then does them (`_run`, `app/review/deletion.py:102`; `_erase`, `:130`). If the process crashes after the commit, the tasks remain and the nightly job retries. Each task is idempotent: a missing blob or a Langfuse 404 counts as done (`_is_missing`, `:156`). Deleting externals before the transaction would leave a document without its PDF after a crash; after it, without a record, a crash would leak the trace for ever.

The review found head-of-line blocking: failed tasks kept their old timestamp and stayed first in the queue, so after about 100 failures newer tasks were never reached. The fix is exponential backoff, 1, 2, 4 minutes up to a day (`erasure_backoff`, `deletion.py:68`), and a request runs its own tasks first (`app/review/deletion.py:58`).

### 3.4 Dry runs

`DELETE /documents/{id}?dry_run=true` calls `plan_document` and writes nothing (`app/review/deletion.py:49-50`). Ownership is checked first (`_check_owner`, `:88`), so a dry run cannot be used to probe whether another owner's document exists.

### 3.5 Concurrency and the locks

Three races, found in review (findings 4 to 6), all under Postgres READ COMMITTED:

- **Two deletions at once leak.** Delete A and B, which share clause `h`, in separate transactions: each sees the other's rows, both call `h` shared, both skip it. Nobody ever removes it, and a shared PDF stays for ever.
- **Deletion versus ingest loses data.** If a new document C that reuses `h` commits between the partition and the DELETE, C is left with clauses whose text and verdicts are gone.
- **Blob erasure did not re-check.** A same-bytes upload between commit and worker run lost its file.

The fix is one advisory lock, `CONTENT_LOCK` (`deletion.py:56`). Deletion, the orphan sweep and blob erasure take it exclusively (`deletion.py:669`, `:746`, `:837`). Saving a document takes it shared (`app/store/postgres.py:220`), so saves do not block each other. Deletions run one after another, and an ingest either committed before the counts were read or starts after. Each content DELETE also re-checks `not exists (… document_clauses …)` in the statement itself (`deletion.py:1144`). The blob task re-checks under the lock that no document names the key (`erase_blob`, `deletion.py:833`).

The memory repo has no concurrency, and the Postgres test is skipped without `TEST_DATABASE_URL`. I did not run it. So the locks rest on review by reading plus that test, not on the property test.

### 3.6 What the property test proves

`tests/test_deletion_property.py:193` builds, with Hypothesis, up to seven documents with overlapping clause sets, shared source files across two owners, a finished review each, a decision (accept, dismiss or override), feedback, a summary and live checkpoints. It deletes them in random order. After each deletion it asserts that:

- every content table holds exactly the hashes a remaining document references (`:215` onward);
- nothing of a deleted document or review remains, summaries and checkpoints included;
- blobs present are exactly those some remaining document names, whoever owns it;
- the erasure tasks equal an independently computed set of `(target, key)` pairs (`_expected`, `:167`), each run once and marked done.

It does not prove concurrency, the Postgres SQL and trigger, or that the keys match what Langfuse stores.

## 4. Retention

Retention is deletion on a schedule, run as a job (`python -m app.review.retention`) outside the API, because a timer in the API would run once per replica. `run_sweep` (`app/review/retention.py:66`) does:

1. Documents older than `files_days` since last upload: `delete_document`, reason `retention`.
2. Idle, finished reviews older than `reviews_days`: `delete_review`.
3. Traces older than `traces_days`: queued for erasure.
4. The orphan sweep: rows and blobs nothing references, ignoring anything under an hour old so it never races an upload (`ORPHAN_MIN_AGE`, `deletion.py:46`).
5. All due erasure tasks, page by page; tasks past `erasure_max_attempts` are logged.

Expiry uses the same code as a user's request, so retention cannot leave behind what a user deletion would not.

**Why the periods are an open legal decision.** Defaults are `enabled: false`, `files_days: null`, `reviews_days: null`, `traces_days: 30` (`configs/app.yaml:166-169`). How long a law firm must keep work papers is a professional-law and contractual question. The checklist names § 50 BRAO (six years) but marks it "to be verified by counsel". Keeping too long breaks Art. 5(1)(e); deleting too early may break a duty to keep files. The code can enforce any period but cannot pick one. Note also that the two periods start at different events: files from last upload, reviews from `finished_at`. Backups are separate: Neon's point-in-time window is not yet recorded.

## 5. Storage

`FileStore` is a small protocol (`put`, `get`, `exists`, `delete`, `presign`, `list_keys`) with two implementations.

- **`LocalFileStore`** (`app/store/files.py:19`) blocks path escapes (`_path`, `:24`) and writes via a temp file and `os.link`, so the first writer wins (`:43`). `presign` returns `None`, so the API proxies bytes.
- **`S3FileStore`** (`app/store/s3.py:83`) serves any S3-compatible service: MinIO in Compose for development and tests, an EU provider in production.

**EU providers by configuration.** The provider is only `endpoint_url`, `region` and credentials (`S3Settings`, `s3.py:47`). Hetzner, Scaleway and OVH are the candidates; none is chosen yet. The design prefers an EU *company* over an EU region of a US provider, because of US compulsion law (CLOUD Act). The checklist says no primary source was gathered for that, so treat it as an open legal question.

**Encryption.** Every `put` asks for server-side encryption (`s3.py:148`). `put` is a conditional write (`IfNoneMatch="*"`, `s3.py:147`), so blobs are immutable. On a conflict it accepts only if the stored bytes hash the same (`s3.py:153-156`); this came from review finding 2, where two concurrent uploads of one PDF gave a 500. Credentials come only from the environment (`file_store_from_config`, `s3.py:219`).

**Presigned URLs after owner checks.** A presigned URL is a time-limited link fetched straight from the bucket, usable by anyone holding it. The default `download: proxy` streams through the API, so the login check covers every download. With `redirect` the route answers 307 with `no-store` (`app/api/routes/documents.py:149-152`). The review's top storage finding was that the first version minted this URL before any owner check. Now the route calls `owned_document` first (`documents.py:141`), so another owner gets 404 and no `Location`. Expiry is capped at 3,600 seconds (`s3.py:39`), and the proxied response is `private, no-store` (`documents.py:162`).

**Idempotent delete.** `delete` calls `delete_object` directly (`s3.py:177`), because a HEAD on a missing key can return 403 on buckets without list rights, which made retried erasures fail for ever (finding 3).

**Migration.** `scripts/migrate_files_to_s3.py:24` copies each local blob, verifies size and sha256, checks the recorded encryption, skips existing keys (so it is resumable), skips `.tmp-*` crash leftovers, and never deletes the local copy. Switching `files.backend` is a separate last step.

Review findings 4 and 8 are fixed: `docker-compose.yml` now refuses to start MinIO unless `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` are set (`${VAR:?…}`, `docker-compose.yml:49-50`), `.env.example` leaves them blank, and the MinIO images are pinned to release tags (not yet to digests).

## 6. Access

### 6.1 OIDC token validation

An identity provider handles the password. The browser app gets an access token and sends `Authorization: Bearer …`. The API checks it without calling the provider per request. `OidcVerifier.verify` (`app/api/oidc.py:254`) does:

1. **Algorithm allow-list.** `alg` must be in the configured asymmetric list (`RS*`, `PS*`, `ES*`; `oidc.py:41`) and not in `_FORBIDDEN` (`none`, `HS256/384/512`; `oidc.py:42`). A config naming a forbidden algorithm is rejected at start (`oidc.py:79`).
2. Reject `crit` headers and unsigned tokens.
3. **Signature** against the issuer's cached JWKS. An unknown `kid` triggers one rate-limited refetch, so key rotation works but garbage tokens cannot make the API hammer the provider.
4. **Claims** (`validate_claims`, `oidc.py:327`): issuer, audience, `exp`, `nbf`, `iat`, non-empty `sub`, with 30 s leeway.

**Why `none` and `HS*` are refused.** A library that trusts the token's own `alg` header lets an attacker set `alg: none` and send no signature. Or set `alg: HS256` and "sign" with the issuer's public key as the HMAC secret, which anyone can download. Here the algorithm comes from configuration, and only public-key algorithms are allowed, so the public key can never act as a secret (docstring, `oidc.py:19-20`).

### 6.2 Owner checks: 404, not 403

Every document and review has an `owner_id`, the token's `sub`. Document ids are per owner, `document_id_for(source_hash, owner_id)`, so two lawyers uploading the same file get two documents. Otherwise B uploading A's file would get A's document with A's latest review.

Checks sit at the API boundary, not in each repository method. Every id-taking route loads through `owned_document` (`app/api/deps.py:142`) or `owned_review` (`:151`), which answer 404 `not_found` for an unknown id and another owner's id alike. A 403 would confirm that the id exists, and ids are hashes of file contents, so anyone could test whether a known file was uploaded. `principal` (`deps.py:91`) is the single caller dependency; with login on, `X-Reviewer` is ignored and the audit trail takes the name from the token (`deps.py:38-49`).

### 6.3 The access matrix test

`tests/test_access.py`: user A owns a document with review, decisions and feedback. User B calls every route taking one of A's ids and must get 404 `not_found` (`MATRIX`, `:93`; `test_another_user_gets_404_on_every_route`, `:129`). Other tests check nothing leaked (`:142`), the owner reaches every route (`:161`), and the same file by two users gives two documents (`:198`). `test_every_id_route_is_in_the_matrix` (`:251`) lists the real app's routes and fails when a new id-taking route is missing. That test replaces row-level security: a developer who adds a route and forgets the check is caught by a test.

### 6.4 The admin role

`auth.oidc.admin_role` may read and delete any owner's documents (`is_admin`, `deps.py:130`). Only an authenticated caller can hold it, read from the token's `roles_claim`. Deletion has its own copy of the rule (`_may_delete`, `app/review/deletion.py:94`). The tombstone records the admin as the deleter (`test_access.py:213`).

## 7. What is deliberately not built (MVP scope)

- **Multi-worker scaling.** The API runs one worker (`README.md:85`); runs and SSE streams live in process memory. The plan's `LISTEN/NOTIFY`, Postgres rate-limit counter and `FOR UPDATE SKIP LOCKED` queue are not built. This is a single-firm MVP and the work is unrelated to the privacy gate.
- **The web login.** The API validates tokens, and Keycloak runs under a Compose profile, but the browser app does not sign in yet (`Phase 9 API.md`, "Web login is not built yet"). Turn login on only for API clients until it is.
- **Row-level security.** Postgres could filter rows by a session setting so no query can forget the check. The design left it out for now: it is invisible in code, harder to test with in-memory fakes, and needs a per-request setting through the pool. The matrix test substitutes. RLS remains a later defence in depth, since repositories are owner-agnostic.

## 8. The open decisions (DSGVO checklist, section 6)

Legal and business calls, not engineering. The checklist lists eleven; the main ones:

1. **Switch-on and sign-off.** When do masking, retention, auth and EU storage go on? Is masking enough for § 203 StGB, or is client consent (§ 43e Abs. 5 BRAO) also needed?
2. **Retention periods** (section 4 above), and Neon's backup window.
3. **Gemini and non-EU access.** The Gemini terms read give no EU-only guarantee; Langfuse has non-EU sub-processors. Does that meet § 43e Abs. 4 BRAO and Art. 44 ff., or move to Vertex AI EU or self-hosted Langfuse?
4. **Object-storage provider** and whether client-side encryption is required.
5. **Confidentiality undertakings** (§ 43e Abs. 3, § 203 Abs. 4): none of the public DPAs read showed the required criminal-liability warning.
6. **Is a DPIA needed (Art. 35)?**
7. **Missing contract facts** for Jina/Elastic and Neon/Databricks.
8. **Legal basis and information duties** to third persons named in client documents (Art. 6, 14).
9. **Roles.** Is the operator the firm, or a separate processor?
10. **Breach procedure** across processors (only Qdrant gives a fixed 48 h).
11. **Corrections dataset** (`corrections.enabled: false`): may client text ever go to the Langfuse dataset?

The checklist is unreviewed by a lawyer, many cells say UNVERIFIED, and nothing is "confirmed" without a link to a signed AVV.

## 9. Try it yourself

All offline, no Gemini or other live service. I ran the first three together on `stage`: 118 passed in about five seconds.

```
uv run pytest -q tests/test_masking_probes.py
uv run pytest -q tests/test_deletion_property.py
uv run pytest -q tests/test_access.py
```

What to look for:

- **`test_masking_probes.py`.** Each block is one reviewer probe turned into assertions. Read `LEAKS` (line 31) for real German layouts, then probes 5, 6, 7 (global-memory bugs) and 3 (summary). Experiment: remove the first-name indexing in `_token` (`masking.py:906`) and watch `test_r1_…` fail.
- **`test_pii_leak.py`.** Runs the corpus through judge, chat and summary with strict fakes and recording Langfuse. Line 232 is the cross-owner heading case.
- **`test_deletion_property.py`.** Make `_shared_hashes` in the memory repo (`deletion.py:553`) return an empty set and watch Hypothesis shrink to a minimal failing case.
- **`test_access.py`.** Remove the `owned_document` call from one route (`documents.py:129`) and see the matrix fail. Add a dummy `{document_id}` route and see the coverage test fail.
- **`test_oidc.py`.** I did not read it. It presumably covers the `none` and `HS256` refusals **(unsure)**; check.
- **`test_deletion_postgres.py`, `test_s3_store.py`.** Need `TEST_DATABASE_URL` or `TEST_S3_ENDPOINT` and are skipped without them. The locks in 3.5 are only exercised there.

To see masking on your own text:

```
uv run python -c "from core.masking import PresidioMasker as P; m = P(allow_regex_only=True).mask('Ansprechpartnerin: Gundula Quast, Musterweg 4, 50667 Köln'); print(m, dict(m.placeholders))"
```

Then try an unlabelled name ("Die Ware holt Björn Gößling ab"). With regexes alone it passes through; with the spaCy model installed it should not **(unsure, not run with the model)**. That difference is the regex-only trade-off in one line.
