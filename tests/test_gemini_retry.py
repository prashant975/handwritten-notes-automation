"""Unit tests for the Gemini empty-response / transient-error retry logic.

Runs standalone (python tests/test_gemini_retry.py) or under pytest. All
network and sleeping is stubbed out, so the suite is instant and offline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pw_access
from src import ai_client
from src.ai_client import GeminiClient, GeminiError

TOKEN = "test-token"


def _ok_response(text="Generated notes.", response_id="ok-1"):
    return {
        "ok": True,
        "result": {
            "candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                            "finishReason": "STOP"}],
            "responseId": response_id,
        },
        "usage": {"tokens_in": 10, "tokens_out": 5},
    }


def _empty_response(response_id="empty-1"):
    # The exact production shape: finishReason STOP, text "".
    return {
        "ok": True,
        "result": {
            "candidates": [{"content": {"role": "model", "parts": [{"text": ""}]},
                            "finishReason": "STOP"}],
            "responseId": response_id,
        },
        "usage": {"tokens_in": 2685, "tokens_out": 2073},
    }


def _image_response():
    return {
        "ok": True,
        "result": {
            "candidates": [{"content": {"role": "model",
                                        "parts": [{"inline_data": {"mime_type": "image/png",
                                                                   "data": "aGk="}}]},
                            "finishReason": "STOP"}],
            "responseId": "img-1",
        },
        "usage": {},
    }


class _Harness:
    """Stub pw_access.gemini_generate with a scripted sequence of responses
    (or exceptions), and capture time.sleep delays."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.sleeps: list[float] = []

    def __enter__(self):
        self._orig_gen = pw_access.gemini_generate
        self._orig_sleep = ai_client.time.sleep

        def fake_generate(token, *, model, request, session=None, **kw):
            self.calls += 1
            item = self.script.pop(0) if self.script else self.script_exhausted()
            if isinstance(item, Exception):
                raise item
            return item

        pw_access.gemini_generate = fake_generate
        ai_client.time.sleep = lambda s: self.sleeps.append(s)
        return self

    def script_exhausted(self):
        raise AssertionError("gemini_generate called more times than scripted")

    def __exit__(self, *exc):
        pw_access.gemini_generate = self._orig_gen
        ai_client.time.sleep = self._orig_sleep
        return False


def test_successful_response_no_retry():
    with _Harness([_ok_response()]) as h:
        resp = GeminiClient(TOKEN).generate("prompt", [])
    assert resp.text == "Generated notes."
    assert h.calls == 1, f"expected 1 call, got {h.calls}"
    assert h.sleeps == [], "no backoff sleeps expected on first-try success"


def test_empty_then_success_retries_once():
    with _Harness([_empty_response(), _ok_response(response_id="ok-2")]) as h:
        resp = GeminiClient(TOKEN).generate("prompt", [])
    assert resp.text == "Generated notes."
    assert h.calls == 2, f"expected 2 calls (1 empty + 1 retry), got {h.calls}"
    assert h.sleeps == [1.0], f"expected one 1s backoff, got {h.sleeps}"


def test_all_empty_raises_clear_error_after_3_retries():
    with _Harness([_empty_response(f"e{i}") for i in range(4)]) as h:
        try:
            GeminiClient(TOKEN).generate("prompt", [])
            raise AssertionError("expected GeminiError")
        except GeminiError as e:
            assert "empty response after 3 retry attempts" in str(e), str(e)
    assert h.calls == 4, f"expected 4 calls (initial + 3 retries), got {h.calls}"
    assert h.sleeps == [1.0, 2.0, 4.0], f"expected exponential backoff, got {h.sleeps}"


def test_missing_candidates_counts_as_empty():
    no_candidates = {"ok": True, "result": {"responseId": "nc-1"}, "usage": {}}
    with _Harness([no_candidates, _ok_response()]) as h:
        resp = GeminiClient(TOKEN).generate("prompt", [])
    assert resp.text == "Generated notes."
    assert h.calls == 2


def test_auth_error_is_not_retried():
    err = pw_access.PWAccessError("vertex token error 401: invalid or expired token")
    with _Harness([err]) as h:
        try:
            GeminiClient(TOKEN).generate("prompt", [])
            raise AssertionError("expected GeminiError")
        except GeminiError as e:
            assert e.status_code == 401
    assert h.calls == 1, f"auth errors must not be retried, got {h.calls} calls"
    assert h.sleeps == []


def test_bad_request_is_not_retried():
    err = pw_access.PWAccessError("vertex gemini error 400: invalid request body")
    with _Harness([err]) as h:
        try:
            GeminiClient(TOKEN).generate("prompt", [])
            raise AssertionError("expected GeminiError")
        except GeminiError:
            pass
    assert h.calls == 1
    assert h.sleeps == []


def test_transient_429_is_retried_then_succeeds():
    err = pw_access.PWAccessError("vertex gemini error 429: rate limited")
    with _Harness([err, _ok_response()]) as h:
        resp = GeminiClient(TOKEN).generate("prompt", [])
    assert resp.text == "Generated notes."
    assert h.calls == 2
    assert h.sleeps == [1.0]


def test_image_response_is_not_treated_as_empty():
    with _Harness([_image_response()]) as h:
        data = GeminiClient(TOKEN).generate_image("redraw", Path("nonexistent.png"))
    assert data == b"hi"
    assert h.calls == 1, "image responses (no text) must not trigger empty-retries"


def test_image_empty_is_retried():
    # An image request whose response has neither text nor inline data IS empty.
    with _Harness([_empty_response(), _image_response()]) as h:
        data = GeminiClient(TOKEN).generate_image("redraw", Path("nonexistent.png"))
    assert data == b"hi"
    assert h.calls == 2


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL TESTS PASSED' if not failures else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)
