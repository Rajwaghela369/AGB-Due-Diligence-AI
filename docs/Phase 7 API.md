# Phase 7 API — decisions, audit trail, agreement

The one source for the Phase 7 Wave 1 API (decisions-api) and UI (decisions-ui) builders.
Models are in `app/domain.py` and `app/api/schemas.py` and generated into
`web/src/api/types.gen.ts` (`make web-gen`); events are in `docs/Events.md`. All paths are
under `/api/v1`. Every error body is `ErrorBody {code, message}`; `message` is German and
shown as is.

## The reviewer (who decides)

There is no login before Phase 9. The UI asks for the reviewer's name once, keeps it in
`localStorage`, and sends it on every decision:

```
X-Reviewer: <encodeURIComponent(name)>
```

- Percent-encoded UTF-8, because header values are Latin-1 only („Müller“ → `M%C3%BCller`).
- The API decodes and trims it (`app.api.deps.reviewer`). Absent or blank → `name = null`
  (shown as „Unbekannt“). Invalid encoding, more than 100 characters, or a control
  character → **422 `invalid_reviewer`**.
- It is always recorded with `authenticated = false`; the audit view marks such entries
  „nicht angemeldet“. Phase 9 sets the name and `authenticated = true` from the OIDC token
  and ignores the header.
- CORS allows the header (`app/api/main.py`).

## Finding status

`Finding.finding_status` is derived from `Finding.decision` when the finding is built:

| decision | `finding_status` |
| --- | --- |
| none | `open` |
| `accept` | `accepted` |
| `override` | `overridden` |
| `dismiss` | `dismissed` |

Only clauses with a verdict (`status` `done` or `awaiting_decision`) can be decided.
`awaiting_decision` clauses are always `open` and always `uncertain`.

## Progress

`ReviewCounts` (on every `Review`) gains:

| field | meaning |
| --- | --- |
| `awaiting_decision` | clauses whose run waits for the lawyer |
| `decided` | clauses with a verdict and a decision (any action) |
| `open_void` | `potentially_void` verdicts without a decision |
| `open_review` | `uncertain` verdicts without a decision |

The verdict classes (`potentially_void`, `uncertain`, `valid`) count the model's verdict of
every clause that has one. `ReviewOut` (from `POST /reviews`, `GET /reviews/{id}`, retry,
and the decision response) gains, derived from the counts:

| field | value |
| --- | --- |
| `decidable` | `potentially_void + uncertain + valid` |
| `progress` | `decided / decidable`, `0.0` when `decidable` is 0 |
| `can_complete` | `status ∈ {finished, awaiting_decisions}` and `open_void == 0` and `open_review == 0` |

The home list shows progress from `DocumentListItem.latest_review.counts` (same formula; no
new field there). Progress updates without a reload: during a run from `replace
/review/counts` in the stream; after a decision from `DecisionOut.review`.

## Routes

### `PUT /reviews/{review_id}/clauses/{position}/decision` (changed)

Record, change or replace the decision on one finding. Idempotent per (review, position)
for the current decision; every call adds one history row.

- **Headers:** `X-Reviewer` (optional, see above).
- **Request:** `DecisionIn`

  ```jsonc
  {
    "action": "accept" | "override" | "dismiss",
    "corrected_verdict": "valid" | "potentially_void" | "uncertain" | null, // override only
    "corrected_basis": ["bgb-309-7-a"],  // override only; [] keeps the model's §
    "note": ""                           // ≤ 2000 characters, any action
  }
  ```

- **Response 200:** `DecisionOut`

  ```jsonc
  {
    "decision": Decision,  // the stored decision (= finding.decision)
    "finding": Finding,    // the clause now: status `done` (never `checking`), finding_status
    "review": ReviewOut    // new counts, progress, can_complete
  }
  ```

  `Decision` now also carries `model_verdict`, `model_basis` (the model's verdict when the
  decision was made), and `reason_de` / `reason_unavailable` (the reasoning written for the
  lawyer's verdict when the decision resumed an awaiting clause to `potentially_void`; the
  UI shows it in place of the model's reasoning for that finding).

- **Synchronous.** On an `awaiting_decision` clause decided `potentially_void` the request
  includes one model call (the explanation). A model failure is not an error: the decision
  is stored with `reason_unavailable = true` and the UI offers nothing more (no regenerate).
- **Side effects, in order:** resume (if awaiting) → decision + history row in one
  transaction → feedback scores (accept 👍 on the clause span; override 👎 `verdict_wrong`,
  plus `basis_wrong` when `corrected_basis` is given and differs; dismiss a `dismissed`
  score) → counts → events (`docs/Events.md`, "A decision") → Langfuse
  `lawyer_agreement` score on the review's trace (the review's own `kept / (kept +
  overridden)` over its current decisions, score id stable per review so it updates; not
  written while that is undefined) → on override, the corrections dataset item when the
  switch allows it (7.E). Langfuse failures never fail the request.
- **Errors:**

  | status | code | when |
  | --- | --- | --- |
  | 404 | `not_found` | unknown review, or no clause at `position` |
  | 409 | `not_decidable` | the clause has no verdict (`queued`, `checking`, `failed`, `out_of_scope`) |
  | 422 | `invalid_decision` | override without `corrected_verdict`; `corrected_*` on accept or dismiss; an override equal to the model's verdict **and** basis; an unknown chunk_id in `corrected_basis` |
  | 422 | `invalid_request` | body fails validation (unknown action, note too long) |
  | 422 | `invalid_reviewer` | bad `X-Reviewer` |
  | 503 | `service_unavailable` | shutting down |

  Service exceptions → codes: `KeyError` → 404, `NotDecidable` → 409, `InvalidDecision` →
  422 (`app/review/interfaces.py`), `RuntimeError` → 503.

### `GET /reviews/{review_id}/audit?position={n}` (new)

The decision history, oldest first; `position` (optional) narrows it to one finding (the
drawer's „Verlauf“).

- **Response 200:** `AuditOut {events: DecisionEvent[]}`

  ```jsonc
  {
    "event_id": 41,                 // increasing in write order
    "review_id": "…", "position": 3,
    "action": "override",
    "previous_action": "accept",    // null for the first decision
    "from_verdict": "potentially_void", "from_basis": ["bgb-309-7-a"],
    "to_verdict": "valid", "to_basis": ["bgb-309-7-a"],   // null / [] after a dismissal
    "note": "…",
    "decided_by": "Anna Müller",    // null: no name given
    "authenticated": false,
    "agent_version": "…",
    "request_id": "…",              // X-Request-Id of the PUT
    "at": "2026-11-17T10:02:03Z"
  }
  ```

  `from_*` is what was in force before (the previous decision's verdict and basis, or the
  model's for the first one); `to_*` what is in force after (`app.domain.decided_verdict`).
- **Errors:** 404 `not_found` (unknown review). An unknown position gives `[]`.
- Rows are never updated or deleted (a database trigger refuses it).

### `GET /metrics/agreement?agent_version={v}` (new)

Lawyer agreement for one agent version over the current decisions of every review it
judged (7.C). Without `agent_version`: the version the service judges with now.

- **Response 200:** `AgreementOut`

  ```jsonc
  {
    "agreement": {
      "agent_version": "…",
      "decided": 12, "kept": 8, "overridden": 3, "dismissed": 1,
      "rate": 0.727,                // kept / (kept + overridden); null when that is 0
      "by_verdict": {               // keyed by the model's verdict
        "potentially_void": {"decided": 5, "kept": 4, "overridden": 1, "dismissed": 0, "rate": 0.8},
        "uncertain": {…}, "valid": {…}
      },
      "last_decided_at": "…"        // null without decisions
    },
    "current": true                 // agent_version is the service's current one
  }
  ```

  `kept` = accepted, or overridden to the **same** verdict (only the § changed);
  `overridden` = overridden to a different verdict; `dismissed` is neither agreement nor
  disagreement. Re-deciding replaces the earlier decision, so nothing is double-counted.
  A version without decisions returns zeros and `rate: null` (200, not 404).
- **Errors:** none beyond the generic ones.

### Unchanged routes affected

- `GET /reviews/{id}/findings`: each `Finding` has `finding_status`; `status` may be
  `awaiting_decision`; `clause.topic` is the topic this review tagged.
- `GET /reviews/{id}`, `POST /reviews`, `POST …/retry`: `ReviewOut` progress fields;
  `review.status` may be `awaiting_decisions`.
- `POST /reviews` while the document's latest review is `awaiting_decisions` starts a new
  review (only `queued`/`running` reviews are reused); the UI should open the existing one.

## Service signatures (Wave 1 implements; `app/review/interfaces.py`)

```python
def decide(self, review_id: str, position: int, decision: DecisionIn, reviewer: Reviewer,
           *, request_id: str | None = None) -> Finding
def audit(self, review_id: str, position: int | None = None) -> list[DecisionEvent]
def agreement(self, agent_version: str | None = None) -> Agreement
```

Route handlers get the reviewer with `app.api.deps.reviewer(request)` and the request id
with `app.api.deps.request_id(request)`.
