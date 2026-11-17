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


def test_classify_topics_batches_and_keeps_order(monkeypatch):
    from core import routing

    calls = []

    class Batch:
        def __init__(self, inputs):
            self.inputs = inputs

        def raise_for_status(self):
            pass

        def json(self):
            # Returned out of order; `index` puts them back.
            items = [
                {
                    "index": i,
                    "predictions": [
                        {"label": "liability", "score": 0.9 if "Haftung" in text else 0.1},
                        {"label": "other", "score": 0.5},
                    ],
                }
                for i, text in enumerate(self.inputs)
            ]
            return {"data": list(reversed(items))}

    monkeypatch.setattr(
        "core.routing.load_settings",
        lambda: type("Settings", (), {"jina_api_key": "key"})(),
    )
    monkeypatch.setattr(routing, "BATCH_SIZE", 2)
    monkeypatch.setattr(
        "core.routing.requests.post",
        lambda *args, **kwargs: calls.append(kwargs["json"]["input"]) or Batch(calls[-1]),
    )

    texts = ["Haftung A", "Gerichtsstand", "Haftung B"]
    assert routing.classify_topics(texts) == ["liability", "other", "liability"]
    assert calls == [["Haftung A", "Gerichtsstand"], ["Haftung B"]]
    assert routing.classify_topics([]) == []
