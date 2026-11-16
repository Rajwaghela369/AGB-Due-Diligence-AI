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
      "status": "queued" | "checking" | "done" | "failed" | "out_of_scope",
      "error": string | null,
      "verdict": ClauseVerdict | null    // app.domain.ClauseVerdict
    }
  }
}
```

`snapshot` and `delta` values hold domain models dumped with `model_dump(mode="json")`, so the
field names inside them are Python's snake_case. Only the AG-UI envelope is camelCase.

## Events, in the order a review produces them

| Type | When | Payload |
| --- | --- | --- |
| `RUN_STARTED` | review starts or resumes | `threadId` = document_id, `runId` = review_id |
| `STATE_SNAPSHOT` | right after `RUN_STARTED`, and on every reconnect | `snapshot` = the full state above |
| `STEP_STARTED` | a clause starts being judged (after the cache check, before any limiter wait inside the judge) | `stepName` = `clause:<position>` |
| `STATE_DELTA` | a clause changes status (queued→checking, →done, →failed) | `delta` = JSON Patch ops: `replace /clauses/<position>` with the clause entry, plus `replace /review/counts` |
| `STEP_FINISHED` | the clause is done or failed | `stepName` = `clause:<position>` |
| `CUSTOM` `progress` | after each clause, and when the limiter starts or stops making clauses wait | `value` = `{counts, cost_usd, budget_left, waiting_reason}` |
| `RUN_ERROR` | the review itself cannot continue (not a clause failure) | `message`, `code`: `review_failed` or `agent_version_changed` |
| `RUN_FINISHED` | every in-scope clause is done or failed | `result` = `{counts, cost_usd}` |

Cache hits found when the run opens are settled together: one `STATE_DELTA` with a
`replace /clauses/<position>` per clause plus the counts, then one `CUSTOM` `progress` (so a
fully cached review costs a constant number of store writes). A hit found later (a verdict
saved meanwhile by another review) emits its own `STATE_DELTA`. Either way a hit is →done and counts in `counts.cache_hits`, without a
`STEP_STARTED`/`STEP_FINISHED` pair. A retried clause (`POST …/clauses/{pos}/retry`) on a
finished review emits `RUN_STARTED` and `STATE_SNAPSHOT`, then its own clause events and
`RUN_FINISHED` again, all under the same review_id with increasing `seq`.

The stream closes at `RUN_FINISHED`. After retrying a clause of a finished review, the
client reopens the stream with its `Last-Event-ID` to receive the retry's events.
