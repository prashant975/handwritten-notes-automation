"""Unit tests for the Gemini empty-response / transient-error retry logic.

Runs standalone (python tests/test_gemini_retry.py) or under pytest. All
network and sleeping is stubbed out, so the suite is instant and offline.
"""
from __future__ import annotations

import sys
import threading
import time
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
        self.gen_configs: list[dict] = []   # snapshot of generationConfig per call
        self.models: list[str] = []         # model used per call

    def __enter__(self):
        self._orig_gen = pw_access.gemini_generate
        self._orig_sleep = ai_client.time.sleep
        self._orig_uniform = ai_client.random.uniform
        self._orig_settings = ai_client._REQUEST_SETTINGS
        self._orig_gate = ai_client._REQUEST_GATE
        settings = ai_client.GeminiRequestSettings(
            max_concurrency=1, request_delay=0, max_attempts=7,
            initial_delay=2, max_delay=60, timeout=120,
        )
        ai_client._REQUEST_SETTINGS = settings
        ai_client._REQUEST_GATE = ai_client.GeminiRequestGate(settings)

        def fake_generate(token, *, model, request, session=None, **kw):
            self.calls += 1
            self.models.append(model)
            # The client mutates the body in place across empty-retries, so snapshot
            # the generationConfig as it was on THIS call.
            self.gen_configs.append(dict((request or {}).get("generationConfig") or {}))
            item = self.script.pop(0) if self.script else self.script_exhausted()
            if isinstance(item, Exception):
                raise item
            return item

        pw_access.gemini_generate = fake_generate
        ai_client.time.sleep = lambda s: self.sleeps.append(s)
        ai_client.random.uniform = lambda _a, _b: 0.0
        return self

    def script_exhausted(self):
        raise AssertionError("gemini_generate called more times than scripted")

    def __exit__(self, *exc):
        pw_access.gemini_generate = self._orig_gen
        ai_client.time.sleep = self._orig_sleep
        ai_client.random.uniform = self._orig_uniform
        ai_client._REQUEST_SETTINGS = self._orig_settings
        ai_client._REQUEST_GATE = self._orig_gate
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


def test_all_empty_raises_clear_error_after_4_retries():
    with _Harness([_empty_response(f"e{i}") for i in range(5)]) as h:
        try:
            GeminiClient(TOKEN).generate("prompt", [])
            raise AssertionError("expected GeminiError")
        except GeminiError as e:
            assert "empty response after 4 retry attempts" in str(e), str(e)
    assert h.calls == 5, f"expected 5 calls (initial + 4 retries), got {h.calls}"
    assert h.sleeps == [1.0, 2.0, 4.0, 8.0], f"expected exponential backoff, got {h.sleeps}"


def test_empty_text_retry_adapts_request():
    # Every text empty-retry must WIDEN maxOutputTokens and CLAMP thinking, so a
    # deck that deterministically empties out still converges instead of failing
    # every identical retry. This is what makes generation deck-agnostic.
    with _Harness([_empty_response(f"e{i}") for i in range(4)] + [_ok_response()]) as h:
        resp = GeminiClient(TOKEN).generate("prompt", [])
    assert resp.text == "Generated notes."
    assert h.calls == 5
    outs = [c.get("maxOutputTokens") for c in h.gen_configs]
    thinks = [(c.get("thinkingConfig") or {}).get("thinkingBudget") for c in h.gen_configs]
    # First call uses the generate() defaults; each retry relaxes further.
    assert outs == [16000, 24000, 32000, 32000, 32000], outs   # widened, capped at 32000
    assert thinks == [2048, 1024, 512, 256, 256], thinks        # halved, floored at 256


def test_empty_falls_back_to_next_model():
    # A model that is UP but keeps emptying out on this deck must not fail the run:
    # after its 4 retries are spent, generation switches to the next model, which
    # succeeds. This is what makes an equation-dense deck survive a Pro model that
    # exhausts its budget on internal thinking.
    script = [_empty_response(f"e{i}") for i in range(5)] + [_ok_response(response_id="ok-fb")]
    with _Harness(script) as h:
        resp = GeminiClient(TOKEN, model="gemini-3.1-pro-preview",
                            fallback_models=["gemini-3.6-flash"]).generate("prompt", [])
    assert resp.text == "Generated notes."
    assert resp.model == "gemini-3.6-flash", resp.model      # attributed to the model that answered
    assert h.calls == 6, f"expected 5 on primary + 1 on fallback, got {h.calls}"
    assert h.models == (["gemini-3.1-pro-preview"] * 5) + ["gemini-3.6-flash"], h.models
    assert h.sleeps == [1.0, 2.0, 4.0, 8.0], h.sleeps        # no extra sleep on the model switch


def test_empty_on_all_models_raises_naming_them():
    # Every model empties -> a single clear error that names the models tried.
    script = [_empty_response(f"e{i}") for i in range(10)]
    with _Harness(script) as h:
        try:
            GeminiClient(TOKEN, model="gemini-3.1-pro-preview",
                         fallback_models=["gemini-3.6-flash"]).generate("prompt", [])
            raise AssertionError("expected GeminiError")
        except GeminiError as e:
            assert "empty response after 4 retry attempts" in str(e), str(e)
            assert "gemini-3.1-pro-preview" in str(e) and "gemini-3.6-flash" in str(e), str(e)
    assert h.calls == 10, f"expected 5 per model x 2 models, got {h.calls}"


def test_image_empty_retry_does_not_adapt_generation():
    # Image redraw legitimately has no text; its responseModalities/config must be
    # left untouched across empty-retries (no thinking clamp, no token widening).
    with _Harness([_empty_response(), _image_response()]) as h:
        GeminiClient(TOKEN).generate_image("redraw", Path("nonexistent.png"))
    assert h.calls == 2
    # generate_image never sets maxOutputTokens or thinkingConfig, and the retry
    # must not add them.
    assert all("maxOutputTokens" not in c for c in h.gen_configs), h.gen_configs
    assert all("thinkingConfig" not in c for c in h.gen_configs), h.gen_configs


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
    assert h.sleeps == [2.0]


def test_first_two_429_then_success():
    err = pw_access.PWAccessError("vertex gemini error 429: RESOURCE_EXHAUSTED")
    with _Harness([err, err, _ok_response()]) as h:
        resp = GeminiClient(TOKEN).generate("prompt", [])
    assert resp.text == "Generated notes."
    assert h.calls == 3
    assert h.sleeps == [2.0, 4.0]


def test_all_429_exhausts_seven_attempts():
    errors = [
        pw_access.PWAccessError("vertex gemini error 429: quota exhausted")
        for _ in range(7)
    ]
    with _Harness(errors) as h:
        try:
            GeminiClient(TOKEN).generate("prompt", [])
            raise AssertionError("expected GeminiError")
        except GeminiError as e:
            assert e.status_code == 429
            assert "quota" in str(e).lower()
    assert h.calls == 7
    assert h.sleeps == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0]


def test_503_then_success():
    err = pw_access.PWAccessError("vertex gemini error 503: unavailable")
    with _Harness([err, _ok_response()]) as h:
        assert GeminiClient(TOKEN).generate("prompt", []).text == "Generated notes."
    assert h.calls == 2


def test_shared_gate_limits_concurrency():
    settings = ai_client.GeminiRequestSettings(1, 0, 7, 2, 60, 120)
    gate = ai_client.GeminiRequestGate(settings)
    active = 0
    maximum = 0
    lock = threading.Lock()

    def worker():
        nonlocal active, maximum
        with gate:
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert maximum == 1


def test_request_gate_enforces_spacing():
    settings = ai_client.GeminiRequestSettings(1, 3, 7, 2, 60, 120)
    gate = ai_client.GeminiRequestGate(settings)
    original_monotonic = ai_client.time.monotonic
    original_sleep = ai_client.time.sleep
    clock = [10.0]
    sleeps = []
    try:
        ai_client.time.monotonic = lambda: clock[0]

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        ai_client.time.sleep = fake_sleep
        with gate:
            pass
        clock[0] += 1
        with gate:
            pass
    finally:
        ai_client.time.monotonic = original_monotonic
        ai_client.time.sleep = original_sleep
    assert sleeps == [2.0]


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
