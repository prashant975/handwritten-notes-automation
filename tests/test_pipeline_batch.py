from pathlib import Path

from src import pipeline


def test_one_failed_batch_item_does_not_stop_later_items(monkeypatch):
    calls = []

    def fake_run(path, **_kwargs):
        calls.append(Path(path).name)
        if Path(path).name == "bad.pdf":
            raise RuntimeError("temporary failure")
        return f"result:{Path(path).name}"

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run)

    results = pipeline.run_batch(
        [Path("bad.pdf"), Path("good.pdf")],
        subject="physics",
        language="English",
        mode="summary",
    )

    assert calls == ["bad.pdf", "good.pdf"]
    assert results[0][1] is None
    assert results[0][2] == "temporary failure"
    assert results[1][1] == "result:good.pdf"
    assert results[1][2] is None
