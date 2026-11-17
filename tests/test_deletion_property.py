"""Reference counting must be independent of document shape and deletion order."""

from hypothesis import given
from hypothesis import strategies as st

from app.domain import ClauseSpan, ClauseVerdict, Document
from app.store.memory import memory_store


@st.composite
def document_sets(draw):
    documents = draw(
        st.lists(
            st.sets(st.integers(min_value=0, max_value=7), min_size=1, max_size=6),
            min_size=1,
            max_size=7,
        )
    )
    order = draw(st.permutations(tuple(range(len(documents)))))
    return documents, order


@given(document_sets())
def test_random_shared_clauses_leave_exactly_the_still_referenced_rows(case) -> None:
    clause_sets, order = case
    store = memory_store()
    for document_number, values in enumerate(clause_sets):
        document_id = f"doc-{document_number}"
        clauses = [
            ClauseSpan(position=position, text=f"clause-{value}", content_hash=f"hash-{value}")
            for position, value in enumerate(sorted(values))
        ]
        store.documents.save(
            Document(
                document_id=document_id,
                filename=f"{document_id}.pdf",
                source_type="pdf",
                parser_version="p1",
                splitter_version="s1",
                text_hash=document_id,
            ),
            clauses,
            {clause.content_hash: clause.text for clause in clauses},
        )
        for clause in clauses:
            store.verdicts.save(
                ClauseVerdict(
                    content_hash=clause.content_hash,
                    agent_version="agent",
                    verdict="valid",
                    confidence=0.9,
                )
            )
            store.caches.put_embedding(clause.content_hash, "embed", [1.0])
            store.caches.put_retrieval(clause.content_hash, "index", "policy", [])
            store.caches.put_topics("router", {clause.content_hash: "other"})

    remaining = set(range(len(clause_sets)))
    for document_number in order:
        store.deletion.delete_document(f"doc-{document_number}", deleted_by=None, reason="user")
        remaining.remove(document_number)
        referenced = {f"hash-{value}" for index in remaining for value in clause_sets[index]}
        assert set(store.documents._texts) == referenced
        assert {key[0] for key in store.verdicts._rows} == referenced
        assert {key[0] for key in store.caches._embeddings} == referenced
        assert {key[0] for key in store.caches._retrievals} == referenced
        assert {key[0] for key in store.caches._topics} == referenced
