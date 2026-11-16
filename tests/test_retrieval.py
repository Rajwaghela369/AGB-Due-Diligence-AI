# --- the integration seam between the graph and the filter ---
#
# The agent passes topic="" and relies on the policy. Each side was correct on
# its own: the graph's tests inject a fake retriever, and the filter's tests
# never knew what topic the graph sends. Together they returned nothing.

from core.retrieval import build_filter


def test_empty_topic_and_empty_policy_searches_every_chunk():
    policy = {"top_k": 10, "always_include_topics": [], "always_include_paragraphs": []}

    built = build_filter("", policy)

    # No `should` at all: `must kind=chunk` alone matches all 105 chunks.
    # A `should` here would have to match, and topics == "" matches nothing.
    assert not built.should
    assert [(c.key, c.match.value) for c in built.must] == [("kind", "chunk")]


def test_empty_topic_still_honours_forced_paragraphs():
    policy = {"always_include_topics": [], "always_include_paragraphs": [309]}

    built = build_filter("", policy)

    assert [(c.key, c.match.value) for c in built.should] == [("paragraph", "§ 309")]


def test_a_real_topic_still_narrows():
    # The Phase 2 behaviour must survive: topic plus general.
    built = build_filter("liability", {"always_include_topics": ["general"]})

    assert sorted(c.match.value for c in built.should) == ["general", "liability"]


def test_the_committed_v1_policy_matches_real_chunks():
    # Guards the seam against config drift: whatever configs/app.yaml says,
    # the agent's own call must be able to reach the index.
    import json

    from core.config import load_yaml
    from core.paths import CONFIGS, PROCESSED

    policy = load_yaml(CONFIGS / "app.yaml")["retrieval"]
    built = build_filter("", policy)

    chunks = [
        json.loads(line) for line in (PROCESSED / "bgb_chunks.jsonl").read_text().splitlines()
    ]
    assert chunks, "no chunks on disk to check against"

    if not built.should:
        return  # unfiltered: every chunk is reachable
    wanted_topics = {c.match.value for c in built.should if c.key == "topics"}
    wanted_paras = {c.match.value for c in built.should if c.key == "paragraph"}
    reachable = [
        chunk
        for chunk in chunks
        if (set(chunk.get("topics", [])) & wanted_topics) or chunk.get("paragraph") in wanted_paras
    ]
    assert reachable, f"the committed policy reaches no chunk: {wanted_topics} / {wanted_paras}"
