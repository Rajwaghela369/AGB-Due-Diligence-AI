from core.routing import classify_topic


class Response:
    def raise_for_status(self):
        pass

    def json(self):
        return {
            "data": [
                {
                    "predictions": [
                        {"label": "liability", "score": 0.2},
                        {"label": "warranty", "score": 0.8},
                    ]
                }
            ]
        }


def test_classify_topic_uses_highest_prediction(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "core.routing.load_settings",
        lambda: type("Settings", (), {"jina_api_key": "key"})(),
    )
    monkeypatch.setattr(
        "core.routing.requests.post",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Response(),
    )

    assert classify_topic("Mängelansprüche verjähren.") == "warranty"
    assert calls[0][1]["json"]["labels"] == ["liability", "warranty", "other"]
