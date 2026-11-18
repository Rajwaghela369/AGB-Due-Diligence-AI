# Phase 10 API — AVV checker

November 18, 2026. This is the frozen contract for the Phase 10 Wave 1 and Wave 2
builders. All additions are backward-compatible: omitted fields mean `agb` or an empty
AVV result.

## Document type and upload

`DocumentType = "agb" | "avv"`. The caller chooses it at upload; it cannot be changed on
a later review.

- Multipart `POST /api/v1/documents`: `file` plus optional text field `document_type`
  (default `agb`).
- JSON paste: `PasteIn {text, filename, document_type="agb"}`.
- `Document.document_type` stores the choice. `Review.document_type` is copied from the
  document when the review starts. `ReviewIn` deliberately has no document type.

The same bytes may be uploaded once as AGB and once as AVV. `source_hash` and the blob stay
shared, while `document_id_for(source_hash, owner_id, document_type)` includes the type for
AVV documents. Existing AGB ids remain unchanged.

## AVV profile

[`configs/profiles/avv.yaml`](../configs/profiles/avv.yaml) is the canonical profile. It
contains all obligations `a` through `h`, short German labels, the exact typed question
`Erfüllt die Klausel Art. 28 Abs. 3 lit. x?`, and collection alias `dsgvo_avv`.

`Obligation` is profile metadata (`code`, `label_de`, `question_de`, `legal_basis`). An AVV
clause may route to zero, one or several obligation codes. Each answer is an
`ObligationFinding {obligation, fulfilled, confidence, legal_basis, reason_de,
reason_unavailable}`. It is stored on `ClauseVerdict.obligation_findings` and copied to the
corresponding `Finding.obligation_findings`. Both lists are empty for AGB.

The ordinary `ClauseVerdict.verdict` remains available for shared review, decisions,
events and exports. Wave 1 maps the AVV result to it without changing the existing verdict
enum; the obligation findings are the authoritative AVV detail.

## Missing obligations

After every AVV clause has an obligation route, the service compares the union of mentioned
letters with the profile's `a`–`h`. Every absent letter produces one
`MissingObligation {review_id, obligation, reason_de, finding_status="open"}`. Thus a
document that never mentions lit. c has a document-level lit. c finding even if every
present clause is otherwise acceptable.

`GET /api/v1/reviews/{id}/findings` returns:

```json
{
  "review": "Review",
  "findings": ["Finding"],
  "missing_obligations": ["MissingObligation"]
}
```

AGB reviews always return an empty `missing_obligations`. Wave 1 does not extend the Phase 7
decision model to document-level findings; that would require a stable finding key in place
of the existing clause `position` and is outside Phase 10.

## Persistence

Migration `009_phase10_avv.sql` adds `document_type` to uploads, documents and reviews;
the review's obligation set; multi-label routes on review clauses; JSON obligation findings
on cached verdicts; and `obligation_routes`, keyed by `(content_hash, model, profile)`.
Deletion treats obligation routes like topics and other content-addressed caches.

## Events

The event schema version is 2. The review snapshot adds `missing_obligations`; AVV runs
replace that list once document-level checking completes. See `docs/Events.md`. AGB event
sequences and payload fields otherwise stay unchanged.

## Exact Wave 1 signatures

```python
# app.domain
DocumentType = Literal["agb", "avv"]
ObligationCode = Literal["a", "b", "c", "d", "e", "f", "g", "h"]
class Obligation(BaseModel):
    code: ObligationCode
    label_de: str
    question_de: str
    legal_basis: str
class ObligationFinding(BaseModel):
    obligation: ObligationCode
    fulfilled: bool
    confidence: float
    legal_basis: list[str]
    reason_de: str
    reason_unavailable: bool
class MissingObligation(BaseModel):
    review_id: str
    obligation: Obligation
    reason_de: str
    finding_status: FindingStatus

# app.agent.run.Judge (Wave 1 implements on the judge returned by make_judge)
def route_obligations(self, clause: ClauseSpan) -> list[ObligationCode]

# app.review.interfaces.ReviewService
def missing_obligations(self, review_id: str) -> list[MissingObligation]

# app.store.interfaces.CacheRepo
def get_obligations(
    self, content_hashes: list[str], model: str, profile: str
) -> dict[str, list[ObligationCode]]
def put_obligations(
    self, model: str, profile: str,
    obligations: dict[str, list[ObligationCode]],
) -> None

# app.store.interfaces.ReviewRepo
def set_clause_obligations(
    self, review_id: str, obligations: dict[int, list[ObligationCode]]
) -> None
def clause_obligations(self, review_id: str) -> dict[int, list[ObligationCode]]
```

The two protocol methods for routing and missing-obligation checking currently raise
`NotImplementedError`; that is the deliberate Wave 1 boundary.

## File ownership

### Wave 1 — avv-kb (Codex, 10.A through P1 + 10.B)

- `data/raw/dsgvo_avv/**`, `data/gold/avv/**` (new)
- `pipelines/p1_kb/**` only for the new DSGVO source/profile integration
- `pipelines/p2_eval/**` only for AVV labelled-set ingestion and held-out splitting
- `configs/kb.yaml`, only the `dsgvo_avv` source/alias block
- `configs/eval.yaml`, only an AVV dataset/profile block
- `tests/test_avv_kb.py`, `tests/test_avv_dataset.py` (new)
- `docs/Metrics.md`, only the AVV dataset description; no paid evaluation without approval

### Wave 1 — avv-agent (Claude, 10.C)

- `app/agent/avv.py` (new)
- `app/agent/run.py`, only implementation/wiring of `Judge.route_obligations`
- `app/review/avv.py` (new)
- `app/review/service.py`, only AVV profile dispatch and `missing_obligations`
- `app/review/runner.py`, only AVV obligation routing/judging and the final document check
- `app/store/{memory,postgres}.py`, only the four obligation route methods if W0 needs repair
- `configs/profiles/avv.yaml`, only corrections discovered while implementing the profile
- `tests/test_avv_agent.py` (new)

### Wave 2 — avv-ui (Codex, 10.D)

- `web/src/features/upload/**`, document-type selector only
- `web/src/features/workspace/**`, AVV obligation filters/cards and missing findings only
- `web/src/api/client.ts`, only multipart `document_type` transport if needed
- `web/src/**/*.test.ts{,x}` and `web/e2e/**`, AVV UI coverage only

Everything else is read-only to the builders, including this contract, `app/domain.py`,
`app/store/interfaces.py`, `app/review/interfaces.py`, `app/api/schemas.py`, migrations,
generated API files and `docs/Events.md`. The orchestrator regenerates API types after a
contract change.
