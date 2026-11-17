# Review events (AG-UI over SSE)

The contract between `app/review` (emits), `app/api` (streams) and `web/` (consumes). Events
are `ag_ui.core` models serialised with `model_dump(by_alias=True, exclude_none=True)`, so
fields are camelCase. Every event carries `metadata.schemaVersion` (= `EVENT_SCHEMA_VERSION`
in `app/domain.py`, currently 1). Each is stored in `run_events` before it is sent.

## Transport

`GET /api/v1/reviews/{id}/events`, `text/event-stream`. Each stored event is one SSE message:

```
id: <seq>
event: <type>
data: <json>
```

On reconnect the client sends `Last-Event-ID: <seq>`. The server sends a fresh
`STATE_SNAPSHOT` **without an `id:` line** (it is not stored), then every stored event with a
higher `seq` exactly once, then live events. A `: ping` comment goes out every 15 s. The
stream closes after `RUN_FINISHED` or `RUN_ERROR`. A finished review replays from the store
and closes.

## The state the events maintain

```jsonc
{
  "review": Review,                      // app.domain.Review, camelCase not applied: field names as in Python
  "clauses": {
    "<position>": {
      "status": "queued" | "checking" | "done" | "failed" | "out_of_scope" | "awaiting_decision",
      "error": string | null,
      "verdict": ClauseVerdict | null    // app.domain.ClauseVerdict
    }
  }
}
```

`verdict` is the model's stored verdict whenever the clause has one: always in `done` and
`awaiting_decision`, possibly in `checking`, never in `queued`, `failed` or `out_of_scope`.
Decisions are not part of this state; the client has them from the findings and the
decision response (docs/Phase 7 API.md). Their effect on the counts is: every
`replace /review/counts` carries `decided`, `open_void` and `open_review` (see
`app.domain.ReviewCounts`).

### Clause and review status (Phase 7)

```
clause:  queued → checking → done | failed | awaiting_decision
         queued → done                 (cache hit)
         queued → awaiting_decision    (cache hit of an `uncertain` verdict)
         awaiting_decision → done      (the lawyer decides)
         failed → queued               (retry)
review:  queued → running → finished | awaiting_decisions | failed
         awaiting_decisions → finished (the decision that settles the last awaiting clause,
                                        when no run is going)
         finished | awaiting_decisions | failed → running   (retry of a failed clause)
```

A clause is `awaiting_decision` when its verdict is `uncertain`: the graph interrupts after
`self_check` (Phase 7 plan, 7.B), or a cached `uncertain` verdict is found. A run ends when
every in-scope clause is `done`, `failed` or `awaiting_decision`; the review becomes
`awaiting_decisions` if any clause waits, else `finished`. `finished`, `awaiting_decisions`
and `failed` are the idle statuses (`app.domain.IDLE_REVIEW_STATUSES`): no run is going and
the event log ends in a terminal event.

`snapshot` and `delta` values hold domain models dumped with `model_dump(mode="json")`, so the
field names inside them are Python's snake_case. Only the AG-UI envelope is camelCase.

## Events, in the order a review produces them

| Type | When | Payload |
| --- | --- | --- |
| `RUN_STARTED` | review starts or resumes | `threadId` = document_id, `runId` = review_id |
| `STATE_SNAPSHOT` | right after `RUN_STARTED`, and on every reconnect | `snapshot` = the full state above |
| `STEP_STARTED` | a clause starts being judged (after the cache check, before any limiter wait inside the judge) | `stepName` = `clause:<position>` |
| `STATE_DELTA` | a clause changes status (queued→checking, →done, →failed, →awaiting_decision), or a decision changes the counts | `delta` = JSON Patch ops: `replace /clauses/<position>` with the clause entry, plus `replace /review/counts` |
| `CUSTOM` `interrupt` | right after the `STATE_DELTA` that makes a clause `awaiting_decision` | `value` = `{position, verdict, confidence, question}` (`app.domain.Interrupt`) |
| `STEP_FINISHED` | the clause is done, failed or awaiting a decision | `stepName` = `clause:<position>` |
| `CUSTOM` `progress` | after each clause, after a decision during a run, and when the limiter starts or stops making clauses wait | `value` = `{counts, cost_usd, budget_left, waiting_reason}` |
| `RUN_ERROR` | the review itself cannot continue (not a clause failure) | `message`, `code`: `review_failed` or `agent_version_changed` |
| `RUN_FINISHED` | every in-scope clause is done, failed or awaiting a decision | `result` = `{counts, cost_usd}` |

Cache hits found when the run opens are settled together: one `STATE_DELTA` with a
`replace /clauses/<position>` per clause plus the counts, then one `CUSTOM` `progress` (so a
fully cached review costs a constant number of store writes). A hit found later (a verdict
saved meanwhile by another review) emits its own `STATE_DELTA`. Either way a hit is →done and counts in `counts.cache_hits`, without a
`STEP_STARTED`/`STEP_FINISHED` pair. A retried clause (`POST …/clauses/{pos}/retry`) on a
finished review emits `RUN_STARTED` and `STATE_SNAPSHOT`, then its own clause events and
`RUN_FINISHED` again, all under the same review_id with increasing `seq`.

The stream closes at `RUN_FINISHED`. After retrying a clause of a finished review, the
client reopens the stream with its `Last-Event-ID` to receive the retry's events.

## Interrupts and decisions (Phase 7)

### A live clause interrupts

The judged verdict is `uncertain`. The graph interrupts after `self_check` with its state
checkpointed under thread `{review_id}:{position}` (Postgres checkpointer), and the clause's
events are:

```
STEP_STARTED  clause:<pos>
STATE_DELTA   replace /clauses/<pos> {status: "checking", …}, replace /review/counts
STATE_DELTA   replace /clauses/<pos> {status: "awaiting_decision", error: null, verdict}
              replace /review/counts
CUSTOM        interrupt {position: <pos>, verdict: "uncertain", confidence, question}
STEP_FINISHED clause:<pos>
CUSTOM        progress
```

The worker is free again; the run goes on with other clauses. When nothing is left the run
sets the review `awaiting_decisions` (status first, as for `finished`) and appends
`RUN_FINISHED`, whose `result.counts.awaiting_decision` says how many wait.

### A cached `uncertain` verdict

There is no graph to resume, and none is created. When the run opens, cached clauses are
settled together as before: the one `STATE_DELTA` carries `status: "awaiting_decision"` for
each cached `uncertain` verdict and `"done"` for the rest, then one `CUSTOM interrupt` per
awaiting clause (in position order), then the one `CUSTOM progress`. A cache hit found
later emits its own `STATE_DELTA`, then its `CUSTOM interrupt`. Either way it counts in
`counts.cache_hits`, without `STEP_STARTED`/`STEP_FINISHED`. Resuming it later means:
record the decision and, if the decided verdict is `potentially_void`, run the explain step
only (`judge.explain_only`-style, one model call).

### A decision

`PUT /reviews/{id}/clauses/{pos}/decision` is synchronous and appends its events once,
after everything else (the model call, if any, is made before any event):

1. If the clause is `awaiting_decision`, it is resumed: the live checkpoint with the decided
   verdict and § (`Command(resume=…)`), or explain-only for a cached verdict. The
   explanation runs only when the decided verdict is `potentially_void`; it writes
   `Decision.reason_de` (or `reason_unavailable=true`), never the shared verdict row. The
   clause becomes `done`. A dismissal or a non-void decision resumes without a model call.
2. The decision and its `decision_events` row are stored in one transaction; feedback is
   scored; the counts are recomputed with the decision.
3. Events, under the service lock:
   - **A run of the review is going:** `STATE_DELTA` (`replace /clauses/<pos>` only if
     the status changed, i.e. awaiting → done; always `replace /review/counts`), then
     `CUSTOM progress`. The run's own terminal event comes later, as usual.
   - **No run is going** (review idle): `RUN_STARTED`, `STATE_SNAPSHOT` (state after the
     decision), the same `STATE_DELTA`, then the terminal again: `RUN_FINISHED` with the
     new counts, or `RUN_ERROR` for a `failed` review. This is the sequence of an explain
     regeneration or a retry on a finished review, so a terminal is only ever followed by
     a `RUN_STARTED`. Before the terminal, the review's status is set: `finished` if no
     clause is still awaiting (else it stays `awaiting_decisions`); `failed` stays `failed`.

Decisions on one clause are serialised (a per-`(review_id, position)` lock in the service):
a second decision arriving while the first resumes the clause waits, then finds it `done`
and is recorded as an ordinary re-decision, without a resume. Every decision appends
events, even one that changes nothing in the state (re-deciding
accept → dismiss), so the log always shows that a decision happened. The resumed
explanation adds no `STEP_STARTED`/`STEP_FINISHED`: the request is synchronous and the
client already shows its own pending state. A client that is not streaming learns
everything from the response (`DecisionOut`); one whose stream closed reopens it with its
`Last-Event-ID` only if it wants other clients' changes.

### Restarts and reloads

A reload rebuilds from `GET …/findings` (statuses, verdicts and decisions are in the store)
or from the stream's `STATE_SNAPSHOT`; nothing about an interrupt lives only in memory. A
restart does not resume `awaiting_decisions` reviews (they have no run): their checkpoints
stay in Postgres until the decision. A `running` review is resumed as before; its
`awaiting_decision` clauses are not re-judged.
