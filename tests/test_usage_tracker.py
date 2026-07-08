from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import usage_tracker


class FakeResponse:
    text = '{"ok": true}'

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True}


def test_webhook_appends_to_multiple_tracking_sheets(monkeypatch):
    calls = []

    def fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(usage_tracker.requests, "post", fake_post)
    secrets = {
        "usage_tracking_webhook_url": "https://script.google.com/macros/s/demo/exec",
        "usage_tracking_google_sheet_urls": [
            "https://docs.google.com/spreadsheets/d/old-sheet/edit?gid=854820019#gid=854820019",
            "https://docs.google.com/spreadsheets/d/new-sheet/edit?gid=1178654693#gid=1178654693",
        ],
        "usage_tracking_gids": ["854820019", "1178654693"],
    }

    result = usage_tracker._append_via_webhook(["2026-07-08", "app"], secrets)

    assert result == "google_sheet_webhook:2"
    assert [call["json"]["spreadsheetId"] for call in calls] == ["old-sheet", "new-sheet"]
    assert [call["json"]["sheetId"] for call in calls] == ["854820019", "1178654693"]
    assert calls[0]["json"]["eventId"] != calls[1]["json"]["eventId"]
