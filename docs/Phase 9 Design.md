# Phase 9 Design — DSGVO hardening, explained

Phase 9 makes AGB-Check fit for real client contracts. Until now it has only seen public
AGB (AGB-DE, shop terms). A client's AGB can name people: a managing director, a contact
person with an e-mail address, a sole trader's register number, an IBAN for payments. Under
the DSGVO (GDPR) that is personal data, and the moment the app processes it, four duties
apply that the earlier phases never had to meet:

1. **Data minimisation (Art. 5(1)(c)).** Send a third party only what it needs. Gemini
   needs the clause's legal meaning, not the name of the person in it → **masking (9.A)**.
2. **Right to erasure (Art. 17) and storage limitation (Art. 5(1)(e)).** Delete on request,
   and do not keep data longer than needed → **deletion (9.B)** and **retention (9.C)**.
3. **Processors in the EU, under contract (Art. 28, Chapter V).** Every service that sees
   the data needs a data processing agreement (AVV), and keeping it in the EU avoids
   third-country transfers → **EU object storage (9.D)** and the **checklist (9.F)**.
4. **Access control (Art. 32).** Only the lawyer who uploaded a contract may see it →
   **login and owners (9.E)**.

The phase gate says the same in testable form: no unmasked personal data in Langfuse or
in any LLM request; a deleted document leaves nothing behind that only it used; every store
confirmed EU-hosted; per-user access enforced.

This document explains how each part works and why it is built this way, with the options
that were not taken. `docs/Phase 9 API.md` is the exact contract.

## Where personal data lives today

Before deciding how to protect data, list where it is. A clause's text (and so any name in
it) ends up in all of these:

| Place | What | Leaves our servers? |
| --- | --- | --- |
| Object storage / `stores/files` | the uploaded PDF or text | no (EU storage, 9.D) |
| `documents`, `uploads` | filename (can contain a name) | no |
| `clause_texts` | every clause, by content hash | no |
| `run_events` | event payloads quoting clauses | no |
| `decisions`, `decision_events`, `feedback` | the lawyer's notes and comments, names | no |
| `verdicts.reason_de`, `summary_texts` | generated text quoting the clause | no |
| Gemini requests | clause, statute texts, chat history | **yes** |
| Jina requests | clause (embedding, topic) | **yes** |
| Langfuse traces, scores, datasets | clause, reasoning, comments | **yes** |
| LangGraph checkpoints | the state of an interrupted clause | no |

Two kinds of protection follow from this table. What **leaves** the server is masked
(9.A). What **stays** is owned by someone (9.E) and deleted on request or on schedule
(9.B, 9.C), and its storage is EU-hosted (9.D).

## Masking (9.A)

### What it does

Before text leaves the server, personal data in it is replaced by placeholders:

```
Ansprechpartner ist Anna Müller (anna.mueller@example.de), IBAN DE89 3704 0044 0532 0130 00.
→ Ansprechpartner ist <PERSON_1> (<EMAIL_1>), IBAN <IBAN_1>.
```

The model judges the clause just as well: whether a liability exclusion is void under
§ 309 Nr. 7 BGB does not depend on whose name is in it. The placeholders are numbered per
text, so "Anna Müller … Frau Müller" can become `<PERSON_1> … <PERSON_2>` (the detector
does not know they are one person) but the same exact string twice is one placeholder.

Detection uses **Presidio**, Microsoft's open-source PII framework, with a **German spaCy
model** for names and addresses (named-entity recognition, NER), plus **regular
expressions** for the things with a fixed shape: IBANs (with the checksum), phone numbers,
e-mail addresses and register numbers (HRA/HRB, for sole traders); Wave 1 added tax
numbers, dates of birth and personal URLs (see "How masking works" below). Regexes are
exact and cheap; NER finds what has no fixed shape but makes mistakes in both directions.

### Where it sits: one boundary, inside the outbound functions

The key design question is *where* masking happens. Three options:

1. **At every call site** (the explain step, the chat, the summary each mask their prompt).
   Simple, but every new feature is a new place to forget. Phase 8 alone added two LLM
   features; Phase 9's gate must hold for features not written yet.
2. **In a network proxy** in front of all providers. Catches everything, but the proxy
   only sees bytes, cannot tell a clause from a statute, and adds an operational component.
3. **Inside the few functions that talk to providers** (`core.llm.decide`,
   `core.llm.complete`, the Jina functions, the Langfuse client). **Chosen.** There are
   only five such functions, every feature already goes through them, and a new feature
   gets masking without doing anything.

So `core.masking` holds one process-wide `MaskingBoundary`, and each of those functions
calls `boundary().outbound(text)` on the client text just before sending. Langfuse is
covered by its SDK's own hook: `Langfuse(mask=boundary().langfuse_mask)` runs on every
observation's input, output and metadata before the SDK sends it. Two Langfuse calls are
not covered by that hook, because they are plain API calls rather than observations: score
comments (`create_score`) and dataset items (`create_dataset_item`). Those two mask
explicitly. The API doc's table lists every path.

Why a *process-wide* boundary instead of passing a masker into each function? Because
`core.llm` is called from the agent graph, the eval harness, the review service and the
Phase 8 features. Threading a parameter through all of them is exactly the "every call
site" option again. The cost of a global is that tests must reset it; they do
(`configure(None)` in a fixture).

### How we prove nothing slips through: `MaskedText`

A test that greps the outgoing payload for "Anna Müller" only finds the names you thought
of. A stronger check is structural: **the masker returns a `MaskedText`, a subclass of
`str`**. It goes anywhere a string goes, so no provider call changes shape. But Python
returns a plain `str` from every string operation (`+`, `format`, slicing), so anything
built *after* masking is unmasked by type. A strict fake client in the tests calls
`require_masked(payload)` and refuses any plain `str` while masking is on. If a future
change assembles a prompt after the boundary, the strict client fails the test, whatever
the prompt says.

The gate adds the content check too: a seeded PII corpus (names, addresses, IBANs…) is
run through the whole review with recording fakes, and zero hits may appear in any
captured LLM payload or fake-Langfuse trace.

### Reversible, but only for display

The model's answer can quote placeholders ("Klausel 3 nennt <PERSON_1> als Haftenden…").
`MaskedText.placeholders` maps each placeholder back, and `unmask(answer)` restores the
answer before the lawyer sees it. The map lives in memory for one call and is never
stored, logged or traced, because a stored map would just be the personal data again.

Alternatives considered:

- **Irreversible redaction** (`[REDACTED]`): simpler, but the explain and chat answers
  would show `[REDACTED]` to a lawyer who can see the clause anyway.
- **Keyed pseudonyms** (`<PERSON_a91f>` from an HMAC of the name, the same in every call):
  links one person across clauses and traces, which helps the model with cross-references
  but also makes traces linkable to a person. Per-text numbering is the more private
  default; pseudonyms can be added if the eval shows the model needs them.
- **A local model** (no provider at all): the strongest protection, but far below Gemini's
  quality on German legal text today, and it would replace the whole Phase 3–4 agent.

### Cache identity stays on the unmasked text

Verdicts, embeddings and retrievals are cached by `content_hash(clause text)`. That hash is
computed on the original text, before masking, so turning masking on does not invalidate
the cache, and two clauses that differ only in a name keep different cache keys (correct:
their masked texts might differ too). The hash itself never leaves the server.

### What can go wrong, and how the plan catches it

- **False negatives** (a name not detected): data leaks. Mitigated by the regexes for the
  fixed-shape data, the PII corpus test, and the Langfuse hook as a second layer.
- **False positives** (a legal term taken for a name: "Verwender", "Kunde"): the clause
  loses meaning and the verdict may change. Mitigated by `privacy.masking.allow_list` and
  the gate's eval: verdicts on the AGB-DE dev slice must not change (run only with
  explicit approval, ≤ 5 Gemini calls in the live check).
- **Masker failure**: nothing is sent. The call fails like a provider error, so one clause
  fails and the run continues. A missing spaCy model stops the API at start, unless
  `allow_regex_only: true` explicitly accepts the regex layer alone.

### How masking works (as built in Wave 1)

One clause, followed through one call. The judge's explain step sends the clause plus the
cited statute:

```
Before (what the lawyer uploaded, and what we store):
  Für die Verbindlichkeiten des Bestellers bürgt Frau Lieselotte Brennecke, geb. Hartmann,
  geboren am 14.07.1961, wohnhaft Kastanienallee 3a, 04109 Leipzig, Steuer-ID
  47 036 892 815. Die Haftung nach § 309 Nr. 7 BGB bleibt unberührt.

After (what Gemini, Jina and Langfuse see):
  Für die Verbindlichkeiten des Bestellers bürgt Frau <PERSON_1>, geb. <PERSON_2>,
  geboren am <DATE_OF_BIRTH_1>, wohnhaft <ADDRESS_1>, Steuer-ID
  <TAX_ID_1>. Die Haftung nach § 309 Nr. 7 BGB bleibt unberührt.

The map, in memory for this one call only:
  <PERSON_1> → Lieselotte Brennecke   <PERSON_2> → Hartmann   <DATE_OF_BIRTH_1> → 14.07.1961
  <ADDRESS_1> → Kastanienallee 3a, 04109 Leipzig   <TAX_ID_1> → 47 036 892 815

The model answers:        {"reason_de": "Die Bürgschaft von <PERSON_1> ist …"}
The lawyer reads:         Die Bürgschaft von Lieselotte Brennecke ist …
```

The steps inside `PresidioMasker.mask`:

1. **Protect.** Statute citations (`§ 309 Nr. 7 BGB`, `§§ 305 ff. BGB`, `Art. 28 Abs. 3
   lit. a DSGVO`, `BGBl. I S. 2909`) and placeholders already in the text are marked as
   protected; nothing that touches them is masked. The statute texts themselves contain no
   personal-data shapes, and a test runs every statute chunk through the masker and
   expects it back unchanged.
2. **Detect, by regex.** German patterns for what has a shape: e-mail; phone numbers
   (international, or national with a separator after the area code, or after "Tel."/"Fax");
   IBAN (mod-97 checksum, or after "IBAN"); USt-IdNr (`DE` + 9 digits) and Steuer-ID or
   Steuernummer after their label; HRA/HRB register numbers; dates only after "geb.",
   "geboren am" or "Geburtsdatum" (a contract date is not personal); street + house number
   (+ postcode and city), "Postfach …"; URLs only when the path or host is personal
   (`linkedin.com/in/…`, `/~name`, `/team/…`, `?email=`). Names have no shape, so the regex
   layer finds them by **context**: after Herr/Frau/Dr./RA, after a role or label
   ("Geschäftsführer:", "Inhaberin", "Kontakt:", "Verantwortlich i.S.d. § 18 MStV:",
   "z. Hd.", "Kontoinhaber:", "gez."), also when the name is on the next line; after
   "geb."; on a line directly above an address; under a signature date ("Berlin, den
   1.3.2026"); or when a common German first name is followed by a capitalised word
   ("Anna Kowalczyk"), unless a company form follows ("Paul Hartmann AG") or the run is
   title-case prose ("Notar Kosten Trägt Der Käufer").
3. **Detect, by NER (optional).** If the spaCy model (`de_core_news_sm`) is installed,
   Presidio's spaCy recogniser adds names with no context at all. Without the model the
   API refuses to start, unless `privacy.masking.allow_regex_only: true` says the
   deployment accepts the regex layer's gaps. The default is to refuse: a silent
   regex-only run would leak exactly the names nobody labelled.
4. **Spread, within a scope.** Masking runs inside a **scope**: one owner's unit of work
   (one clause judged, one summary, one chat message, one comment), primed with the texts
   it is about. Every value detected in the scope is masked wherever it recurs, also in
   another case or with a genitive "s" ("<PERSON_1>s Haftung"), and a detected name's
   surname on its own. This is what keeps traces clean: the model's answer "Lieselotte
   Brennecke haftet" has no "Frau" in front of it when the Langfuse hook sees it, but the
   scope of the request that produced it knows the name. The scope ends with the work;
   there is no process-wide memory, so another owner's documents can never change what
   this owner's clause looks like to the model.
5. **Resolve and number.** A hit that touches a protected citation keeps only its part
   outside it; overlapping hits keep the longest (an IBAN beats the phone number inside
   it); the rest become `<KIND_n>`, numbered per scope in order of first appearance, the
   same value always the same placeholder.

Where the way back happens: `core.llm.complete` restores the placeholders inside the
parsed JSON answer (so an address with a line break stays valid JSON), and
`core.llm.complete_stream` restores them chunk by chunk, holding back a tail like `<PERS`
until the next chunk shows whether it is a placeholder. The chat, the summary and the
reasoning therefore never show a placeholder, and no caller ever holds the map.

**Restored only what the cache may hold.** A clause's reasoning is cached by its text and
shared by every owner who uploads the same clause. The explain prompt also contains the
heading, which belongs to one owner ("§ 9 Haftung von Herrn Dr. Klaus Brenner"). So the
scope says which values may come back: only those in the clause text itself. A
placeholder from the heading, from a lawyer's note or name, or one the model invented,
reads `[unbekannt]`. We chose "cache only owner-independent text" over keying the cache
by owner: keying by owner would multiply every model call by the number of owners, for
names the reasoning does not need.

What it costs: the regex layer masks all 3,764 AGB-DE clauses in under a second; a
36-clause document takes a few milliseconds, and a 40-candidate retrieval trace about
10 ms (a review measured 684 ms when the first version still kept a global memory). Presidio itself is only imported when the
spaCy model is there (its import pulls in `transformers` and takes seconds).

What it does not catch on regexes alone: a name with no context and an uncommon first name
("Die Ware holt Gundula Quast ab"). That is the NER layer's job, which is why production
should install the model, and why the 9.A gate still asks for a live check.

## Owners and login (9.E)

### Who owns what

Every document and review gets an `owner_id`. Before login exists, and for every row from
before Phase 9, that is the configured default owner (`local`). With login it is the OIDC
subject (`sub`) of the signed-in user, held in a `Principal`.

There is a subtle problem the owner model has to fix. A document's id has been the sha256
of its bytes, so that uploading the same file twice reuses the parse. With two users, that
reuse is a leak: user B uploads the same AGB as user A, gets A's document back with
`reused: true` and **A's latest review**. So the document id becomes per owner:
`document_id_for(source_hash, owner_id)`. The default owner keeps the plain hash, which
keeps every existing id and URL valid. The cost is small: B's upload is parsed again
(milliseconds), but every clause verdict is still shared through the content-hash cache,
so B's review costs nothing extra in model calls.

The stored file is keyed by the bytes (`blob_key`: `{source_hash}.pdf`), so A's and B's
documents share one blob. That is why deletion reference-counts blobs (below).

### Login: OIDC, provider-agnostic (decision #2)

OpenID Connect is the standard way for an app to let an identity provider do the login.
The flow for a single-page app:

1. The browser is redirected to the provider (Keycloak in development) and the user logs
   in there; the app never sees the password.
2. The provider redirects back with a one-time code; the SPA exchanges it for tokens
   (the **authorization code flow with PKCE**, which protects the code without a client
   secret in the browser).
3. The SPA sends the **access token** as `Authorization: Bearer …` on each API request.
4. The API checks the token's signature with the provider's public keys (JWKS), and its
   issuer, audience and expiry. No call to the provider per request.

"Provider-agnostic" means the API reads only standard claims (`sub`, `iss`, `name`, a
configurable roles claim). Keycloak runs in Docker Compose for development and tests; the
production provider (a law firm's Entra ID, an EU-hosted Keycloak, …) is configuration.

Alternatives: **cookie sessions through a backend-for-frontend** keep tokens out of the
browser entirely (stronger against XSS) but add a server component and CSRF handling;
**a hosted auth SaaS** is quicker but adds a processor outside our control. Bearer tokens
with PKCE are the common, standard choice for an SPA plus API, and keep the API stateless.

### Enforcing access: 404, and checks in every repository method

Another owner's document answers **404, not 403**. A 403 would confirm that the id exists,
and document ids are hashes of file contents: a 403 would let anyone test whether some
known file has been uploaded.

Where to check ownership: in **every repository method** (the plan's choice), or with
**Postgres row-level security** (RLS: the database itself filters rows by a session
setting). RLS cannot be forgotten by a new query, but it is invisible in the code, harder
to test with the in-memory fakes, and needs a per-request database setting through the
connection pool. Wave 2 decides; the contract only requires that every read and write by
id is checked and that the memory fakes enforce the same.

## Deletion (9.B)

### Why deletion is hard here: shared rows

The app is content-addressed to save money: a clause text, its verdict, its embedding and
its retrieval are stored **once**, however many documents contain that clause. Standard AGB
share many clauses ("Es gilt deutsches Recht."). So "delete everything of this document"
cannot simply delete the clause rows: another document may still use them.

**Reference counting** solves it: a shared row is deleted only when no remaining document
references its content hash. Example:

```
Document A: clauses h1, h2, h3        Document B: clauses h2, h4
Delete A →  h1, h3: no other reference → deleted (text, verdicts, embeddings, …)
            h2: still used by B         → kept ("kept_shared")
```

The count is computed inside the deletion transaction, after the document's own rows are
gone, so it is always current. The evaluation `clauses` table also counts as a reference:
the eval set's verdicts must survive a user deleting a copy of a public AGB.

### One transaction, plus an outbox for what is not in the database

The database part is **one transaction**: either everything of the document is gone, or
nothing is (a crash halfway leaves no half-deleted document). But some things live outside
the database: the blob in object storage, Langfuse traces, scores and dataset items, and
LangGraph checkpoints. A transaction cannot include an HTTP call to Langfuse.

The **outbox pattern** bridges this: the transaction writes one `erasure_tasks` row per
external thing to delete, and commits. Then a worker carries the tasks out and marks each
done. If the process crashes after the commit, the tasks are still there and the nightly
job retries them. Every task is idempotent (deleting a missing blob or trace counts as
done), so a retry is harmless.

Alternatives: deleting externals *before* the transaction (a crash then leaves a document
whose PDF is gone), or *after* it without a record (a crash then leaks the trace forever).
The outbox is the standard way to make "database + external system" reliable.

### The audit trail keeps a tombstone

Phase 7 made `decision_events` append-only: a database trigger refuses every UPDATE and
DELETE, because the history is evidence. Erasure needs one exception: a decision's note
can contain personal data, and a deleted document's decisions must go. Migration 006 lets
DELETE through **only** inside a transaction that ran `set local agb.erasure = 'on'`,
which only the deletion code does; UPDATE and TRUNCATE stay refused for everyone.

What remains is a **tombstone**: who deleted which document (its id, a hash), when, why
(user, retention, orphan sweep) and how many rows went, but no content. Tombstones are
themselves append-only. They answer an auditor's "show me that you deleted it" without
keeping what was deleted.

### Other approaches

- **Soft delete** (a `deleted_at` flag): easy to undo, but the data is still there, which
  is not erasure under Art. 17.
- **`ON DELETE CASCADE` foreign keys only**: handles the document's own rows, but not the
  shared content-addressed rows (no foreign key from a shared row to "its" documents) and
  not anything outside the database.
- **Crypto-shredding** (encrypt each document's data with its own key, delete the key):
  elegant for backups, but shared rows would need shared keys, and it complicates every
  read. Worth revisiting for backups only.

### A nightly orphan sweep

Rows can become orphans outside a deletion: a crash between saving clause texts and the
document, or data from before 9.B. The nightly sweep removes content-addressed rows (and
blobs) that no document references, ignoring rows younger than an hour so it never races
an upload in progress.

### Backups

Deleting a row does not remove it from database backups. Neon keeps a point-in-time
history for a configured window; deleted data disappears from it when the window passes.
That is accepted practice under the DSGVO if the window is short and documented, which
9.F does in `docs/DSGVO.md`.

## Retention (9.C)

Storage limitation means data should not be kept "just in case". Retention is configured
per **data class**:

- **files**: a document and everything only it used, counted from its last upload;
- **reviews**: a finished review with its events, decisions and feedback, counted from
  when it finished (the document stays);
- **traces**: Langfuse traces, counted from creation (debugging needs them for weeks,
  not years; 30 days is the suggested default).

Expired data is deleted **through the same deletion as a user's request**, so retention
can never leave behind what a user deletion would not, and there is one code path to test.

The periods default to **off** (`enabled: false`, periods null): how long a law firm must
keep due-diligence work papers is a legal and contractual question (professional rules can
require years), not an engineering one, so the client sets them.

The job runs **outside the API process** (`python -m app.review.retention`, from cron, a
Compose service or a Kubernetes CronJob), not as a timer inside the API: with several API
replicas a timer would run once per replica, and a long sweep would hold a request worker.

## Storage (9.D)

### S3-compatible, any EU provider (decision #3)

Source files move from the local disk to **object storage** behind the existing
`FileStore` protocol, which gains `delete` (for 9.B) and `presign`. "S3-compatible" means
the Amazon S3 API, which nearly every provider speaks, so one implementation
(`S3FileStore`) serves **MinIO** in Docker Compose for development and tests, and
**Hetzner, Scaleway or OVH** (EU companies with EU regions) in production. Changing
provider is a change of `endpoint_url`, `region` and credentials.

Why object storage instead of the disk: a disk belongs to one machine (no second replica,
no managed durability, backups are our job), while a bucket is replicated by the provider
and reachable from any instance. Keeping the PDFs in Postgres (`bytea`) was the other
option: one store to back up, but it bloats the database and its backups, and Neon's
storage is the expensive kind.

Choosing an **EU company**, not just an EU region of a US provider, matters because a US
provider can be compelled under US law (CLOUD Act) regardless of region; this is the
reasoning behind many German firms' preference, and 9.F records it per processor.

### Details that matter

- **Server-side encryption** on every write (`AES256`, or the provider's KMS): data at rest
  is encrypted by the provider, which the AVV and Art. 32 expect.
- **Immutable blobs**: `put` is a conditional write (`If-None-Match: *`), so the first
  writer wins, exactly like the local store's link-based write.
- **Downloads: proxy or presigned URL.** A presigned URL is a time-limited link the browser
  fetches directly from the bucket: less load on the API, but the bucket must be reachable
  from the browser and the link works for anyone holding it until it expires. Proxying
  through the API keeps every download behind the login check. The default is `proxy`;
  `redirect` is a configuration switch.
- **Credentials only from the environment**, never in configs/app.yaml (which is in git).
- **Migration from disk** is a separate, resumable script that copies, verifies (size and
  sha256) and never deletes the local copy; switching `files.backend` is the last step.

## How the work is split

Wave 0 (this one) wrote the contracts: the types, protocols, stubs, migration 006, the
routes answering 501, these two documents, and the contract tests. Wave 1 builds three
slices in parallel, on disjoint files: **masking** (Claude: Presidio, the regexes, wiring
the boundary into `core.llm`, Jina and Langfuse), **deletion** (Codex: reference-counted
deletion, the outbox worker, retention and the nightly job), **storage** (Codex:
`S3FileStore`, MinIO in Compose, the migration script). Wave 2 adds **login and per-user
access** (OIDC, owner checks, the multi-worker changes) and the **AVV checklist**. The
exact file ownership is in the Wave 0 report (`agb-check-wt/reports/p9-w0.md`).
