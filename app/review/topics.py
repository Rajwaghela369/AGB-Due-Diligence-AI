"""Tag a review's clauses with the router's topic when the review starts.

Not at ingest: ingest stays offline and deterministic, and a parse is reused by
every review of the same bytes. The topic decides scope, so a clause whose
topic is outside the review's scope is `out_of_scope` and never reaches the
judge (Phase 7 plan, carry-over). Topics are cached by (content_hash, router
model), so a clause is classified once however many documents contain it.

A router failure never fails a review: the clauses it could not tag keep no
topic and are judged, which is what happened before topics existed.
"""

from collections.abc import Callable

from core.logging import get_logger

from app.domain import ClauseSpan
from app.store.interfaces import CacheRepo

log = get_logger("review.topics")

# texts -> one topic per text ("liability", "warranty" or "other"), e.g. a partial of
# core.routing.classify_topics. Injected, so tests never call Jina.
Router = Callable[[list[str]], list[str]]


def tag_topics(
    clauses: list[ClauseSpan], *, router: Router, model: str, caches: CacheRepo
) -> dict[int, str]:
    """Topic per position for every clause ingest kept in scope; cache first, then one
    batched router call for the rest. Positions the router could not tag are absent."""
    wanted = [clause for clause in clauses if clause.in_scope]
    hashes = list(dict.fromkeys(clause.content_hash for clause in wanted))
    try:
        known = caches.get_topics(hashes, model)
    except Exception:  # noqa: BLE001 - a cache read failing only costs router calls
        log.warning("topic cache read failed", exc_info=True)
        known = {}
    missing = [h for h in hashes if h not in known]
    if missing:
        texts = {clause.content_hash: clause.text for clause in wanted}
        try:
            found = router([texts[h] for h in missing])
            if len(found) != len(missing):
                raise RuntimeError(f"router returned {len(found)} topics for {len(missing)}")
            fresh = dict(zip(missing, found, strict=True))
        except Exception:  # noqa: BLE001 - untagged clauses are judged, as before topics
            log.warning("could not tag %d clauses; judging them all", len(missing), exc_info=True)
            fresh = {}
        if fresh:
            try:
                caches.put_topics(model, fresh)
            except Exception:  # noqa: BLE001 - the review still uses them
                log.warning("could not cache %d topics", len(fresh), exc_info=True)
        known = {**known, **fresh}
    return {
        clause.position: known[clause.content_hash]
        for clause in wanted
        if clause.content_hash in known
    }
