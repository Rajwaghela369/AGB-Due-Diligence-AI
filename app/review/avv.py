"""Review-level AVV routing and missing-obligation checks.

An AVV review runs through the same runner, events and decisions as an AGB review. Two
things are its own:

- **Routing at start.** Where an AGB review tags topics, an AVV review routes every clause
  to the Art. 28(3) letters it addresses (`AvvJudge.route_many`, cached by content hash,
  router model and profile) and stores the routes on the review. The routes double as
  scope: a clause routed to no letter states no mandatory term, so it is stored with the
  topic `UNROUTED` and is `out_of_scope` (never judged), exactly as an off-topic AGB
  clause is. A clause the router could not route keeps no topic and is judged; the judge
  then routes it itself.
- **The document-level check.** A letter that no clause mentions is a finding of its own,
  `MissingObligation`, even if every present clause is fine. It needs the whole document,
  which is why it is here and not in the per-clause agent.
"""

from typing import Any

from core.logging import get_logger

from app.domain import ClauseSpan, MissingObligation, ObligationCode, Review
from app.store.interfaces import Store

log = get_logger("review.avv")

ROUTED = "avv"  # topic of a clause routed to at least one letter
UNROUTED = "other"  # topic of a clause routed to none: out of scope

# A review's missing obligations are final once its run has ended.
COMPLETE_STATUSES = ("finished", "awaiting_decisions")


def route_clauses(
    clauses: list[ClauseSpan], judge: Any
) -> tuple[dict[int, list[ObligationCode]], dict[int, str]]:
    """Routes and topics per position for the clauses ingest kept in scope.

    A router failure never fails the review: the clauses stay unrouted (no topic) and the
    judge routes each on its own when it judges it.
    """
    wanted = [clause for clause in clauses if clause.in_scope]
    try:
        routes = judge.route_many(wanted)
    except Exception:  # noqa: BLE001 - judged unrouted instead
        log.warning("could not route %d AVV clauses", len(wanted), exc_info=True)
        return {}, {}
    topics = {position: ROUTED if codes else UNROUTED for position, codes in routes.items()}
    return routes, topics


def mentioned(store: Store, review: Review, entries: dict[int, tuple] | None = None) -> set[str]:
    """Every letter some clause of the review addresses: its stored routes, plus the
    findings of clauses the judge had to route itself."""
    routes = store.reviews.clause_obligations(review.review_id)
    letters: set[str] = {code for codes in routes.values() for code in codes}
    for _status, _error, verdict in (entries or {}).values():
        if verdict is not None:
            letters.update(finding.obligation for finding in verdict.obligation_findings)
    return letters


def missing_obligations(review: Review, profile: Any, letters: set[str]) -> list[MissingObligation]:
    """One finding per letter of the review that no clause mentions, in profile order."""
    if review.document_type != "avv" or profile is None:
        return []
    wanted = set(review.obligations or profile.codes)
    return [
        MissingObligation(
            review_id=review.review_id,
            obligation=obligation,
            reason_de=(
                f"Keine Klausel des Vertrags regelt {obligation.legal_basis} "
                f"({obligation.label_de})."
            ),
        )
        for obligation in profile.obligations
        if obligation.code in wanted and obligation.code not in letters
    ]


def review_missing(
    store: Store, review: Review, profile: Any, entries: dict[int, tuple] | None = None
) -> list[MissingObligation]:
    """The check as the API and snapshots see it: empty until the run has ended."""
    if review.document_type != "avv" or review.status not in COMPLETE_STATUSES:
        return []
    return missing_obligations(review, profile, mentioned(store, review, entries))
