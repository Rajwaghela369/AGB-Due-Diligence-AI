# Study note: Human in the Loop (Phase 7)

This note explains how the human-in-the-loop (HITL) part of AGB-Check works, from the graph
that pauses to the metric that measures the lawyer's agreement. It is based on the code and
docs on branch `stage`. Where I could not confirm something, I say so. References are
`file:line`.

## 1. Why HITL here

The goal of Phase 7 (`docs/Phase 7-10 Plan.md`, "Phase 7 · Human in the loop") is that the
result of a review is the lawyer's judgement, recorded and defensible, not the model's
output.

Three things follow from that:

- The model's verdict is a draft. It is stored (the `verdicts` row) and never changed by a
  decision. The lawyer's decision is a separate row that points at it.
- A verdict the model is unsure about (`uncertain`) must not be reported as if it were
  settled. It waits.
- Every decision must be provable later: who, what, when, against which `agent_version`.

## 2. Interrupts and checkpoints

### What LangGraph gives us

A LangGraph graph runs node by node. With a checkpointer it saves its state after a run, keyed
by a thread id. Two pieces matter:

- `interrupt(value)` inside a node stops the run. The caller gets `value` back (under
  `__interrupt__`) and the state stays in the checkpointer.
- `Command(resume=x)` continues the same thread. The `interrupt()` call returns `x`, and the
  node carries on from there.

In `app/agent/graph.py` the node is `await_decision` (lines 384-398). It calls
`interrupt(summarise(state))`, so the waiting clause reports the model's verdict in the same
shape a finished clause does. On resume it writes the lawyer's `{"verdict", "basis"}` into the
draft. A dismissal has `verdict=None` and keeps the draft. The node "runs twice: once to
interrupt, once when resumed" (its own docstring). The routing is in `after_check`
(lines 377-382): only when the graph has a checkpointer and the verdict is `uncertain`, go to
`await_decision`. Otherwise it goes to `explain` or `finish` as before. Without a checkpointer
the node is not even added (lines 515-521), so offline evals behave as they always did.

After resume the same router (`explain_or_finish`) runs. A decided `potentially_void` goes
through `explain`, now on the lawyer's §. Everything else goes straight to `finish`.

### The thread id

One graph thread per clause per review: `{review_id}:{position}`
(`app/review/runner.py:207`, `thread_id()`). The runner passes it only to a judge that has
`interrupts` set (`runner.py:749-752`). Without a thread id the judge makes up a random one
(`app/agent/run.py:209`), which is fine because nobody will resume it.

Two details in `run.py`:

- Checkpoints are written once per invocation (`durability="exit"`, lines 210 and 309), not
  after every node.
- A thread that ran to the end is deleted (`forget`, lines 182-187, called at 229 and 312).
  Only waiting clauses keep a checkpoint. Failure to delete is only logged.

`judge.resume` (lines 295-320) first calls `compiled.get_state(config)`. If `waiting.next` is
empty, the thread was never interrupted, was already resumed, or was deleted, and it returns
`None`. That `None` is how the caller learns "there is no graph here".

### PostgresSaver vs InMemorySaver

`app/review/checkpoint.py` offers both:

- `postgres_checkpointer()` (lines 53-80) is for production. It needs its own `psycopg_pool`
  pool because LangGraph's saver wants autocommit, dict rows and no prepared statements, which
  the app store's connections are not (module docstring, lines 8-11). It also checks
  connections before use because Neon drops idle ones (line 70).
- `memory_checkpointer()` (line 48) is for tests and the e2e app. A "restart" in a test
  reuses the same saver object.

Both use a serializer that allows `core.retrieval.Retrieved` (line 27), the one non-builtin
type in the graph state.

### Why a checkpoint survives a restart

The lawyer may decide hours later, after the server restarted. With the Postgres saver the
paused graph state is in a table, not in process memory. A new process builds a new graph,
points it at the same thread id, and `Command(resume=...)` continues it. The test
`_interrupt_then_restart_and_resume` (`tests/test_interrupt.py:224`) does exactly this: it
shuts the first service down, builds a new judge and service on the second saver, and checks
`restarted.judged == []` (resumed, not judged again). The Postgres variant (line 301) is
skipped unless `TEST_DATABASE_URL` is set. I did not run it.

### Alternatives considered

The plan and code mention these. The code comment for the second is explicit. The rest of
the trade-off reading below is my summary of why, not a quote from the docs.

1. **No checkpoint; re-run explain only.** Store the decision, and for a void decision call
   the explain step alone. Simple, no new tables. Downside: the graph's paused state and
   its draft are lost, so the decision is not truly a continuation. The code does keep this
   path, for cached verdicts (see decision #1).
2. **`interrupt_after=["self_check"]`.** LangGraph can stop after a named node. It was
   rejected (`graph.py:31-32`) because the stop is conditional (only `uncertain` waits) and
   because the node is also where the lawyer's answer comes back in.
3. **Review-level waiting.** Mark the whole review as waiting and run nothing per clause.
   The plan chose per-clause waiting instead: the worker is free for other clauses, and the
   review ends `awaiting_decisions` only when nothing else is left to do. I did not find a
   document that spells out the trade-off beyond this; treat it as my reading.

### Decision #1: both

`docs/Phase 7-10 Plan.md`, "Decisions taken", item 1: "both. The graph interrupts on live
runs, and cached `uncertain` verdicts wait at review level as `awaiting_decision`."

The reason is the verdict cache. A cached `uncertain` verdict was judged by an earlier
review, so there is no live graph for this review to resume. `docs/Events.md` says: "There is
no graph to resume, and none is created." Resuming it means: record the decision and, if the
decided verdict is `potentially_void`, run explain only. In `service._resume_decided` that is
the fallback after `judge.resume(...)` returns `None` (`service.py:595-624`). The test
`test_a_cached_uncertain_verdict_waits_without_a_judge_call_and_explains_only`
(`tests/test_interrupt.py:315`) asserts there is no checkpoint for a cache hit
(`_checkpoint(...) is None`) and exactly one explain call.

## 3. A worked example

Review of a document with two clauses: clause 0 is unclear, clause 1 is clearly valid. The
tests build this as `_review_one(store, judge)` with texts `(UNSURE, CLEAR)`.

**Step 1: clause 0 comes back `uncertain`.** The graph runs `route`, `retrieve`, `judge`,
`self_check`. `after_check` sees `uncertain` and goes to `await_decision`, which calls
`interrupt(...)`. `compiled.invoke` returns a state containing `__interrupt__`, and
`judge_clause` uses `state["__interrupt__"][0].value` as the result (`run.py:224-225`). The
runner stores the model's verdict. `settled_status` (`runner.py:212-214`) returns
`awaiting_decision` for an undecided `uncertain` verdict.

**Step 2: the event sequence.** From `docs/Events.md` and asserted in
`tests/test_interrupt.py`:

```
RUN_STARTED
STATE_SNAPSHOT
STEP_STARTED  clause:0
STATE_DELTA   clause 0 -> checking
STATE_DELTA   clause 0 -> awaiting_decision
CUSTOM        interrupt
STEP_FINISHED clause:0
CUSTOM        progress
STEP_STARTED  clause:1
STATE_DELTA   clause 1 -> checking
STATE_DELTA   clause 1 -> done
STEP_FINISHED clause:1
CUSTOM        progress
RUN_FINISHED
```

The second delta is a JSON Patch (docs/Events.md):

```json
{"op": "replace", "path": "/clauses/0",
 "value": {"status": "awaiting_decision", "error": null, "verdict": { "...": "the model's ClauseVerdict" }}}
```

The `interrupt` event value is `app.domain.Interrupt` (`domain.py:366-372`):

```json
{"position": 0, "verdict": "uncertain", "confidence": 0.4, "question": "..."}
```

(0.4 is the confidence in the test. `question` has a default constant `INTERRUPT_QUESTION`;
I did not read its text.)

**Step 3: the review ends `awaiting_decisions`.** `_try_finish` (`runner.py:658-675`) sets
the status first, then emits `RUN_FINISHED`. If any clause is `awaiting_decision` the status is
`awaiting_decisions`, else `finished`. `RUN_FINISHED.result.counts.awaiting_decision` is 1.
Only clause 0 has a checkpoint. Clause 1's thread was deleted.

**Step 4: the lawyer overrides.** The UI sends (`docs/Phase 7 API.md`):

```
PUT /api/v1/reviews/{review_id}/clauses/0/decision
X-Reviewer: Anna%20M%C3%BCller

{"action": "override", "corrected_verdict": "potentially_void",
 "corrected_basis": ["bgb-309-7-a"], "note": ""}
```

`corrected_basis` takes chunk ids. The id above is the example used in the API doc.

**Step 5: `ReviewService.decide` runs** (`service.py:312-414`):

1. Take a per-clause lock, then the service lock. Check the clause is `done` or
   `awaiting_decision` (else 409), find the model's verdict, validate the body (lines
   322-346).
2. Release the service lock. Because the clause is `awaiting_decision` and has no earlier
   decision, call `_resume_decided` (lines 359-361). This is the model call, and it runs
   outside the service lock.
3. `_resume_decided` calls `judge.resume(clause, thread_id="{review_id}:0",
   verdict="potentially_void", basis=[...])`. That sends
   `Command(resume={"verdict": "potentially_void", "basis": [...]})`. `await_decision`
   returns, writes the lawyer's verdict into the draft and clears `reason_de`. The router
   sends it to `explain`, which writes the reason from the lawyer's §. The thread is then
   deleted.
4. The reason goes into `Decision.reason_de` (not into the shared verdict row). The cost of
   the extra tokens is computed from the resume's token delta (`service.py:587`,
   `run.py:318-319`).
5. Retake the service lock. `decisions.put` stores the decision and its history row in one
   transaction. `set_clause_status(..., "done")` flips the clause. Counts are recomputed.
   If the review is idle, its status becomes `finished` when nothing else awaits (lines
   389-393). Events are emitted (`_emit_decision`, lines 448-507).
6. Outside the lock: feedback scores, the agreement score, and the corrections dataset.

**Step 6: the events for the decision.** The review is idle (no run), so
`docs/Events.md` says the sequence is the same as for a retry on a finished review:

```
RUN_STARTED
STATE_SNAPSHOT        (state after the decision)
STATE_DELTA           replace /clauses/0 (awaiting_decision -> done), replace /review/counts
RUN_FINISHED          result.counts, result.cost_usd
```

`tests/test_decisions.py:126` asserts this four-event shape for each of three decisions.

**Step 7: the response** (`DecisionOut`): `{"decision": ..., "finding": ..., "review": ...}`.
The finding has `status: "done"` and `finding_status: "overridden"`. The decision carries
`model_verdict: "uncertain"`, `reason_de` set, and `reason_unavailable: false`.

If the explanation fails, the decision is still stored with `reason_unavailable: true`. It
is not an error (`service.py:568-585`, test at `tests/test_interrupt.py:423`).

Accept and dismiss, and an override to a non-void verdict, close the checkpoint without any
model call (`test_a_non_void_decision_closes_the_checkpoint_without_a_model_call`).

## 4. Decisions as state

### Three actions

- **accept**: the lawyer keeps the model's verdict.
- **override**: the lawyer changes the verdict, the §, or both. `corrected_basis: []` keeps
  the model's §.
- **dismiss**: the finding is not relevant. There is no verdict in force afterwards.

`app.domain.decided_verdict` (`domain.py:209-216`) gives the verdict and § in force after a
decision: the model's on accept, the corrected ones on override, `None` on dismiss.

### Validation (`service.py:429-446`)

| Status | Code | When |
| --- | --- | --- |
| 404 | `not_found` | unknown review or position |
| 409 | `not_decidable` | clause is `queued`, `checking`, `failed`, `out_of_scope`, or has no verdict |
| 422 | `invalid_decision` | override without `corrected_verdict`; `corrected_*` on accept or dismiss; unknown chunk id in `corrected_basis`; an override that changes neither verdict nor § |
| 422 | `invalid_request` | body fails validation (unknown action, note over 2000 characters) |
| 422 | `invalid_reviewer` | bad `X-Reviewer` |

A reordered § list is not a change (`set(...)` comparison, line 443;
`test_reordered_basis_is_not_an_override`).

### Current decision vs audit trail

Two tables, two jobs (`app/store/migrations/003_decisions_audit.sql`):

- `decisions` holds one current row per review and position. A new decision replaces it
  (upsert). This is idempotent: deciding the same thing twice leaves one row.
- `decision_events` is the history. One row per decision ever made, written in the same
  transaction as the upsert. A trigger refuses every update, delete and truncate (file
  header and trigger definitions), so the database itself enforces append-only.

Changing a decision three times gives three history rows and one current decision
(`tests/test_decisions.py:126`: `len(store.decisions.for_review(...)) == 1` and
`len(audit.json()["events"]) == 3`).

Each history row has `from_*` (what was in force before) and `to_*` (after), `previous_action`,
`decided_by`, `authenticated`, `agent_version`, `request_id` (`docs/Phase 7 API.md`).

### Who decided: X-Reviewer, "not signed in"

There is no login before Phase 9. The UI asks for a name once, keeps it in `localStorage`
(`web/src/features/decisions/reviewer.ts`), and sends it percent-encoded in `X-Reviewer`
(header values are Latin-1 only, so "Müller" becomes `M%C3%BCller`). The API decodes and
trims it. Absent or blank means `name = null`, shown as "Unbekannt". Invalid encoding, over
100 characters, or a control character gives 422 `invalid_reviewer`.

`authenticated` is always `false` for now, and the audit view shows „nicht angemeldet“. This
is honest about what the name is: a claim, not proof. Phase 9 is meant to take the name from
the OIDC token and ignore the header.

### Finding status in the UI

`Finding.finding_status` is derived from the decision: none is `open`, accept is `accepted`,
override is `overridden`, dismiss is `dismissed`. A clause waiting on the lawyer has
`status: "awaiting_decision"`, and the UI shows it with its own "awaiting" tone
(`web/src/features/decisions/status.ts`, `decisionTone`).

The hotkeys are in `web/src/features/review/hotkeys.tsx:39-41`: **A** accept, **O** override,
**D** dismiss. They act on the selected finding, and are ignored while a dialog is open
(line 37-38). They only request the action (`requestDecision`). The panel with the optional
note and, for override, the verdict and § picker is
`web/src/features/decisions/decision-panel.tsx`. Card buttons file the same request as a
hotkey (`card-actions.tsx`).

Progress is `decided / decidable`. "Prüfung abschließen" (`can_complete`) needs
`open_void == 0` and `open_review == 0` and a status of `finished` or `awaiting_decisions`
(`docs/Phase 7 API.md`).

## 5. Feedback loops

Every decision is also a measurement. All of it is best effort: it must not fail the
decision.

### Langfuse scores per decision

`FeedbackService.record_decision` (`app/review/feedback.py:147-198`):

- **accept**: a thumbs-up score `feedback.clause_span` (value 1) on the clause span.
- **override**: a thumbs-down with reason `verdict_wrong`, plus `basis_wrong` when the corrected
  § differs from the model's. `REASON_STEP` (`domain.py:311-316`) maps `verdict_wrong` to the
  `judge` observation and `basis_wrong` to the `retrieval` observation, so the score lands on
  the step that produced the error. The score is `feedback.<step>` with value -1 (see
  `submit`, lines 69-87).
- **dismiss**: a score named `dismissed` (value 1) on the `clause_span` observation, plus
  removal of any earlier decision-derived feedback. Dismissal is "not relevant", not "the
  model was wrong".

Score ids are stable hashes, so a re-decision updates the same score instead of adding one.
Moving between actions deletes scores that no longer apply (lines 60-64, 178-179).

Observation ids come from `verdict.observations`, stored when the clause was judged
(`run.py:232-233`).

There is also a per-review score `lawyer_agreement` (`service.py:509-533`), attached to the
review's trace with a stable id. It is deleted again when there is nothing to measure.

### The agreement metric

Defined in `domain.kept_model_verdict` (`domain.py:219-224`) and the SQL view
`decision_agreement` (`003_decisions_audit.sql:63-80`):

```
agreement = kept / (kept + overridden)
```

- `kept`: accepted, or overridden to the same verdict (only the § changed).
- `overridden`: overridden to a different verdict.
- `dismissed`: counted separately, in neither side.

It is grouped by `agent_version` and by the model's verdict, and served at
`GET /metrics/agreement`. Re-deciding replaces the earlier decision, so nothing is
double-counted. A version with no decisions returns zeros and `rate: null`.

Note that an override of an `uncertain` verdict to `potentially_void` counts as
`overridden`, because the verdict changed. That is by the definition, and I flag it only
because it means resolved `uncertain` clauses always count against agreement.

### The corrections dataset

`app/review/corrections.py`. When a lawyer overrides, the clause becomes an item in the
Langfuse dataset `agb-lawyer-corrections`: the clause text, the model's verdict, the
corrected verdict and §, `agent_version`, and a review link. Rules:

- **Off by default.** `configs/app.yaml` has `corrections.enabled: false`, with the comment
  that contract text is not exported until Phase 9 masking exists.
- **Source allowlist.** `allowed_sources: [agb_de, public_shop]`. A document whose
  `source_type` is not listed is skipped (`corrections.py:61-66`).
- **Idempotent.** The item id is a hash of `(content_hash, review_id)` (line 23-25), so a
  second override updates the same item.
- **Archived on re-decision.** If the new decision is not an override but the previous one
  was, the item is set to `ARCHIVED` (lines 68-79).

`make eval-corrections` (the `evaluate` function) runs the agent over the dataset and refuses
without `--yes`, because it makes paid Gemini calls.

## 6. Edge cases the reviews caught

These are all covered by tests in `tests/test_decisions.py`.

1. **A decision during a live run.** If the review is running, the service has a `ReviewRun`
   holding the counts in memory. Writing counts from the store would be overwritten by the
   run's stale ones. So `decide` calls `run.note_decision(stored, resume_cost)`
   (`service.py:377-378`). `ReviewRun.note_decision` (`runner.py:495-506`) records the
   decision, moves an `awaiting_decision` entry to `done`, adds the resume cost to the run's
   cost, and persists and returns the counts. Without that, the run would still end
   `awaiting_decisions` for a clause already decided. Test:
   `test_awaiting_decision_during_run_finishes_with_resume_cost`
   (`tests/test_decisions.py:323`) expects `finished`, `awaiting_decision == 0`, and
   `cost_usd == 0.25`.

2. **Deciding on a review that has no run.** The service uses the stored review status, not
   the existence of a run object, to decide if it is idle (`service.py:375-376`: `idle = ...
   in IDLE_REVIEW_STATUSES`, and `run` is used only when `not idle`). A stale registered run
   on an idle review is ignored (`test_idle_status_wins_over_a_stale_registered_run`, line
   390). A `queued` or `running` review with no run keeps its status, and only a
   `STATE_DELTA` and a `CUSTOM progress` are emitted, with no fake `RUN_STARTED`
   (`test_decision_without_a_run_never_ends_an_active_review`, line 377).

3. **Feedback never fails a decision.** `record_decision` is wrapped in `try/except Exception`
   and logged (`service.py:397-403`). The agreement score and the corrections upsert have
   their own `except`s (`service.py:521`, `532`; `corrections.py:77`, `106`). Test:
   `test_feedback_failure_after_lock_does_not_fail_decision` (line 409) raises `OSError` from
   feedback and still gets a stored decision.

4. **Langfuse calls outside the lock.** Scoring happens after the `with self._lock:` block has
   ended (`service.py:397` onward), and the resume's model call happens between two lock
   sections (lines 359-361). The tests assert `not service._lock._is_owned()` inside the
   resume stub (line 240) and inside the feedback stub (line 416). A slow network call
   under the service lock would block every other review.

5. **The restart repair for a clause that already has a decision.** If the process died
   after the decision was stored but before the clause status moved to `done`, the clause
   would stay `awaiting_decision` forever. `ReviewRun.open` checks, for each
   `awaiting_decision` entry, whether a decision exists, and if so treats it as `done`
   (`runner.py:424-432`, persisted at line 460). Test:
   `test_restart_repairs_awaiting_clause_that_already_has_a_decision` (line 460). A related
   guard: in `decide`, resume runs only if `previous is None` (`service.py:360`), so a stored
   decision is never resumed twice (`test_stored_decision_on_awaiting_clause_is_not_resumed_twice`,
   line 438).

Two more worth knowing: decisions on one clause are serialised by a per-`(review_id,
position)` lock (`service.py:322`), so a second decision on the same clause waits and is
then recorded as a normal re-decision. And `_resume_decided` never retries a failed resume as
explain-only, "so one decision never pays for two explanations" (docstring, lines 562-564).

## 7. Try it yourself

```
uv run pytest tests/test_interrupt.py tests/test_decisions.py -q
```

I did not run this for the note. What to look for:

- `test_uncertain_interrupts_and_a_restarted_service_resumes_the_checkpoint`: read
  `_interrupt_then_restart_and_resume` (line 224). Follow the event list, the checkpoint
  asserts (waiting clause has one, finished clause does not), then the "restart" and
  `judge.resumed == [f"{review.review_id}:0"]`.
- The Postgres variant is skipped without `TEST_DATABASE_URL`. A skip is expected.
- `test_a_cached_uncertain_verdict_waits_...`: compare its event list with the live one.
  There is one combined `STATE_DELTA`, no `STEP_STARTED`, and no checkpoint.
- `test_all_actions_validation_audit_header_and_idle_events`: three decisions, one current
  row, three audit rows, and the four-event idle sequence times three.
- The edge-case tests in section 6. Try changing `note_decision` or the repair in `open` and
  see which test fails. That is the quickest way to see why each exists.

The frontend has its own test file, `web/src/features/decisions/decisions.test.tsx`, which
presses `a`, `o` and `d`. I did not read it closely and did not run it.
