"""The e2e test app is the real API on fakes; prove its fakes behave as the browser test expects."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.testing import build_test_app

TEXT = (
    "1.1 Unsere Haftung ist ausgeschlossen.\n"
    "1.2 Die Gewährleistung richtet sich nach dem Gesetz.\n"
    "1.3 Die Lieferung erfolgt innerhalb von zwei Wochen."
)


def test_keyword_verdicts_cache_and_recorded_scores(monkeypatch):
    monkeypatch.setenv("AGB_TEST_JUDGE_DELAY", "0")
    with TestClient(build_test_app()) as client:
        document = client.post("/api/v1/documents", json={"text": TEXT, "filename": "t.txt"})
        document_id = document.json()["document"]["document_id"]
        source = client.get(f"/api/v1/documents/{document_id}/file")
        review_id = client.post("/api/v1/reviews", json={"document_id": document_id}).json()[
            "review"
        ]["review_id"]
        assert "event: RUN_FINISHED" in client.get(f"/api/v1/reviews/{review_id}/events").text
        findings = client.get(f"/api/v1/reviews/{review_id}/findings").json()["findings"]
        calls_after_review = client.get("/api/v1/__test__/judge-calls").json()["calls"]

        # Reading the stored review again must not judge anything.
        client.get(f"/api/v1/reviews/{review_id}/findings")
        calls_after_reload = client.get("/api/v1/__test__/judge-calls").json()["calls"]

        void = next(f for f in findings if f["verdict"]["verdict"] == "potentially_void")
        feedback = client.post(
            "/api/v1/feedback",
            json={
                "review_id": review_id,
                "position": void["position"],
                "vote": "down",
                "reasons": ["verdict_wrong"],
                "corrected_verdict": "valid",
            },
        )
        scores = client.get("/api/v1/__test__/scores").json()["scores"]

    assert source.text == TEXT
    assert sorted(f["verdict"]["verdict"] for f in findings) == [
        "potentially_void",
        "uncertain",
        "valid",
    ]
    assert calls_after_review == 3
    assert calls_after_reload == 3
    assert feedback.status_code == 200
    assert [s["name"] for s in scores] == ["feedback.judge"]
    assert scores[0]["observation_id"].startswith("judge-")
    assert scores[0]["value"] == -1
