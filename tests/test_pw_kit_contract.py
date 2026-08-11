"""Contract tests: repo-root pw_access.py vs the pristine pw-app-kit template.

The root client intentionally diverges from the kit template (APP_NAME, thread
locks, call-time proxy URL, richer 401 handling — see the KIT SYNC NOTE in
pw_access.py). These tests make sure those divergences never silently drop a
capability the kit guarantees, so a future `pw-app-kit.zip` sync is a safe diff
rather than a gamble.

Runs standalone (python tests/test_pw_kit_contract.py) or under pytest. Offline:
nothing here touches the network.
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pw_access
from src import pw_auth

KIT_TEMPLATE = ROOT / "pw-app-kit" / "pw_access.py"

# Every public helper the kit doc promises an app can call.
KIT_PUBLIC_API = [
    "check_allowed",
    "check_allowed_status",
    "log_usage",
    "new_task_id",
    "UsageSession",
    "gemini_generate",
    "claude_generate",
    "gemini_tts",
    "gemini_image",
    "mathpix_ocr",
    "sarvam_tts",
    "elevenlabs_tts",
]

# The helpers this app actually routes provider traffic through. Each must
# accept task_id= so one run's rows can be grouped in `Raw Usage Ledger Export`.
APP_PROVIDER_HELPERS = ["gemini_generate", "mathpix_ocr", "gemini_image"]


class _FakeResponse:
    """Minimal stand-in for a requests.Response (offline; nothing hits the net)."""

    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _template_public_names() -> set[str]:
    """Public top-level functions/classes defined in the kit template."""
    tree = ast.parse(KIT_TEMPLATE.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
    return names


def test_root_client_is_a_superset_of_the_kit_template():
    """No kit capability may be lost in the merge."""
    assert KIT_TEMPLATE.exists(), f"kit template missing at {KIT_TEMPLATE}"
    missing = sorted(n for n in _template_public_names() if not hasattr(pw_access, n))
    assert not missing, f"root pw_access.py is missing kit functions: {missing}"
    print(f"PASS: root client exposes every public name in the kit template")


def test_documented_kit_api_is_present():
    missing = [name for name in KIT_PUBLIC_API if not hasattr(pw_access, name)]
    assert not missing, f"missing documented kit API: {missing}"
    print(f"PASS: all {len(KIT_PUBLIC_API)} documented kit helpers exist")


def test_every_app_call_site_matches_the_real_signature():
    """A kit sync must not drop a PARAMETER the app passes.

    Name-level checks above can't catch this: `gemini_generate` existed after
    one sync but had lost its `timeout=`, so every health probe and every note
    generation raised TypeError and the UI reported "No Gemini model is
    available". This walks each pw_access.* call in the app and binds its
    kwargs against the live signature, so the next sync fails HERE instead of
    at runtime.
    """
    import glob

    files = ["app.py", "run_cli.py"] + sorted(glob.glob(str(ROOT / "src" / "*.py")))
    problems: list[str] = []
    checked = 0
    for rel in files:
        path = ROOT / rel
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "pw_access"):
                continue
            attr = node.func.attr
            fn = getattr(pw_access, attr, None)
            if fn is None:
                problems.append(f"{path.name}:{node.lineno} pw_access.{attr} does not exist")
                continue
            if not callable(fn):
                continue
            try:
                params = inspect.signature(fn).parameters
            except (TypeError, ValueError):
                continue
            unknown = {k.arg for k in node.keywords if k.arg} - set(params)
            if unknown:
                problems.append(
                    f"{path.name}:{node.lineno} pw_access.{attr} does not accept {sorted(unknown)}")
            checked += 1

    assert not problems, "pw_access call sites broken by a kit sync:\n  " + "\n  ".join(problems)
    print(f"PASS: all {checked} pw_access call sites match the live signatures")


def test_gemini_generate_still_accepts_a_timeout():
    """Explicit guard for the parameter a sync actually dropped."""
    inspect.signature(pw_access.gemini_generate).bind(
        "token", model="gemini-3.5-flash", request={}, timeout=5)
    print("PASS: gemini_generate still accepts timeout=")


def test_app_name_is_this_app_not_the_template_placeholder():
    assert pw_access.APP_NAME == "Handwritten Notes Automation", pw_access.APP_NAME
    template = KIT_TEMPLATE.read_text(encoding="utf-8")
    assert 'APP_NAME = "SET-YOUR-APP-NAME"' in template, (
        "template placeholder changed — re-check the sync note")
    print("PASS: APP_NAME is set to this app, not the template placeholder")


# ---------------------------------------------------------------------------
# The kit's newest hard requirement: a TOKEN PROVIDER, not a cached string.
# ---------------------------------------------------------------------------
def test_kit_style_zero_arg_provider_is_accepted():
    """The kit documents a ZERO-ARG provider:  def google_token(): return ...
    The root client must accept exactly that, or every other PW app's provider
    would break when this client is reused."""
    calls = {"n": 0}

    def kit_provider():
        calls["n"] += 1
        return "kit-token"

    assert pw_access._resolve_token(kit_provider) == "kit-token"
    # force=True must still work against a provider that takes no arguments.
    assert pw_access._resolve_token(kit_provider, force=True) == "kit-token"
    assert calls["n"] >= 2
    print("PASS: a kit-style zero-arg token provider works, including force=True")


def test_force_aware_provider_gets_a_genuinely_fresh_token():
    def provider(force=False):
        return "fresh-token" if force else "cached-token"

    assert pw_access._resolve_token(provider) == "cached-token"
    assert pw_access._resolve_token(provider, force=True) == "fresh-token", (
        "force=True must bypass the provider's cache — this is what makes the "
        "single post-401 retry meaningful")
    print("PASS: a force-aware provider returns a genuinely fresh token on retry")


def test_plain_string_token_still_works():
    """Backwards compatibility: the kit still allows a raw string."""
    assert pw_access._resolve_token("  raw-token  ") == "raw-token"
    assert pw_access._resolve_token("") == ""
    print("PASS: plain string tokens still work (kit backwards compatibility)")


def test_pw_auth_provider_satisfies_the_kit_zero_arg_contract():
    """This app's real provider must be callable with NO arguments, because the
    kit template calls `google_token()` that way."""
    provider = pw_auth.token_provider_for("nobody@pw.live")
    assert callable(provider)
    signature = inspect.signature(provider)
    for name, param in signature.parameters.items():
        assert param.default is not inspect.Parameter.empty, (
            f"parameter {name!r} has no default — provider is not zero-arg callable")
    provider()  # must not raise
    print("PASS: pw_auth's provider is zero-arg callable, as the kit requires")


def test_broken_provider_fails_closed_instead_of_crashing():
    def exploding(force=False):
        raise RuntimeError("oauth backend down")

    assert pw_access._resolve_token(exploding) == "", (
        "a failing provider must yield an empty token (deny), not propagate")
    print("PASS: a broken token provider fails closed instead of crashing the run")


# ---------------------------------------------------------------------------
# Divergences the app depends on — each must actually be present.
# ---------------------------------------------------------------------------
def test_session_minting_is_thread_safe():
    """The pipeline fans chunk calls across a ThreadPoolExecutor, so a cold
    start has every thread reach _auth_token at once. Only ONE should mint a
    pass; the rest must reuse it rather than each POST /api/session."""
    assert "_session_lock" in inspect.getsource(pw_access._auth_token), (
        "session minting lost its lock — a cold start will mint one pass per thread")

    import threading
    import time

    mints = {"n": 0}
    original = pw_access.requests.post

    def slow_mint(url, **kw):
        assert url.endswith("/api/session"), url
        mints["n"] += 1
        time.sleep(0.05)          # widen the window a racing thread could enter
        return _FakeResponse(200, {"session_token": "pass-1",
                                   "expires_at_ms": (time.time() + 7 * 86400) * 1000})

    pw_access._invalidate_session()
    pw_access.requests.post = slow_mint
    try:
        threads = [threading.Thread(target=lambda: pw_access._auth_token("tok"))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        pw_access.requests.post = original
        pw_access._invalidate_session()

    assert mints["n"] == 1, f"8 threads minted {mints['n']} session passes, expected 1"
    print("PASS: 8 concurrent threads mint exactly one session pass")


# ---------------------------------------------------------------------------
# Kit v2: raw per-call logging keyed by task_id, NOT client-side combined rows.
# ---------------------------------------------------------------------------
def test_provider_helpers_accept_a_task_id():
    """Kit v2 requires one task_id per multi-call task, passed to every call."""
    for name in APP_PROVIDER_HELPERS:
        params = inspect.signature(getattr(pw_access, name)).parameters
        assert "task_id" in params, f"pw_access.{name} does not accept task_id="
    print(f"PASS: all {len(APP_PROVIDER_HELPERS)} provider helpers accept task_id=")


def test_task_id_reaches_the_proxy_payload():
    """The id must actually be SENT — an accepted-but-dropped kwarg would leave
    every row unlinkable in the raw ledger."""
    seen = {}
    original = pw_access.requests.post

    def capture(url, **kw):
        if url.endswith("/api/session"):
            return _FakeResponse(200, {})          # force the Google-token path
        seen["url"] = url
        seen["payload"] = kw.get("json") or {}
        return _FakeResponse(200, {"ok": True, "result": {}})

    pw_access._invalidate_session()
    pw_access.requests.post = capture
    try:
        pw_access.gemini_generate("tok", model="gemini-2.5-flash", request={},
                                  task_id="handwritten-notes-abc123")
    finally:
        pw_access.requests.post = original
        pw_access._invalidate_session()

    assert seen["payload"].get("task_id") == "handwritten-notes-abc123", seen["payload"]
    print("PASS: task_id is sent to the proxy on every provider call")


def test_no_client_side_combined_logging_is_sent():
    """Kit v2 forbids `log:false` — it was how the old kit suppressed per-call
    logging so the client could write one combined row instead."""
    source = KIT_TEMPLATE.read_text(encoding="utf-8")
    root = inspect.getsource(pw_access)
    for text, where in ((source, "kit template"), (root, "root client")):
        assert '"log"' not in text and "'log'" not in text, (
            f"{where} still suppresses per-call logging with a `log` flag")
    # And the compat shim must not secretly post a combined row either.
    assert "log_usage" not in inspect.getsource(pw_access.UsageSession.flush), (
        "UsageSession.flush() must be a no-op in v2, not a combined-row write")
    print("PASS: no log:false and no client-side combined row")


def test_new_task_id_is_unique_and_prefixed():
    a = pw_access.new_task_id("handwritten-notes")
    b = pw_access.new_task_id("handwritten-notes")
    assert a != b, "task ids must be unique per run"
    assert a.startswith("handwritten-notes-")
    print("PASS: new_task_id produces unique, prefixed ids")


def test_app_creates_one_task_id_per_run_and_passes_it_everywhere():
    """The pipeline must mint the id ONCE per file and thread it through every
    provider call — the whole point of task_id is that a run's rows share it."""
    source = (ROOT / "src" / "pipeline.py").read_text(encoding="utf-8")
    assert source.count("pw_access.new_task_id(") == 1, (
        "expected exactly one task_id per pipeline run")
    assert "task_id=task_id" in source, "the run's task_id is never passed on"
    assert "UsageSession" not in source, "pipeline still uses the legacy UsageSession"
    assert ".flush()" not in source, "pipeline still writes a client-side combined row"

    client_source = (ROOT / "src" / "ai_client.py").read_text(encoding="utf-8")
    assert "task_id=self.task_id" in client_source, (
        "GeminiClient does not forward its task_id to pw_access")
    print("PASS: one task_id per run, threaded through every provider call")


def test_proxy_url_resolves_at_call_time():
    """src.config's load_dotenv runs AFTER pw_access is imported in some entry
    points, so an import-time constant would silently ignore PW_PROXY_BASE_URL."""
    import os

    original = os.environ.get("PW_PROXY_BASE_URL")
    os.environ["PW_PROXY_BASE_URL"] = "https://example.invalid/"
    try:
        assert pw_access.proxy_base_url() == "https://example.invalid"
    finally:
        if original is None:
            os.environ.pop("PW_PROXY_BASE_URL", None)
        else:
            os.environ["PW_PROXY_BASE_URL"] = original
    print("PASS: proxy base URL is resolved at call time, honouring late .env loads")


def test_error_carries_http_status():
    err = pw_access.PWAccessError("vertex gemini error 429: rate limited", status_code=429)
    assert err.status_code == 429, "src/ai_client retry logic reads .status_code"
    assert pw_access.PWAccessError("no status").status_code is None
    print("PASS: PWAccessError carries a structured HTTP status")


def test_expired_is_distinct_from_error():
    """The UI needs to say 'session expired' vs 'proxy unreachable'; the kit
    template collapses both into 'error'."""
    source = inspect.getsource(pw_access.check_allowed_status)
    assert '"expired"' in source, "401 must map to 'expired', not 'error'"
    assert '"denied"' in source and '"error"' in source
    print("PASS: check_allowed_status distinguishes expired / denied / error")


def test_session_pass_can_be_invalidated():
    """The direct-Vertex path (and its token cache) was removed in the kit's
    Gemini migration — the 7-day session pass is now the only cached
    credential, and a dead one must be droppable instead of replayed."""
    assert hasattr(pw_access, "_invalidate_session")
    pw_access._session.update({"token": "stale", "expiry": 9e9})
    pw_access._invalidate_session()
    assert pw_access._session["token"] == ""
    assert pw_access._session["expiry"] == 0.0
    print("PASS: a dead session pass can be invalidated instead of replayed")


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
    print(f"\n{len(tests)} kit contract tests passed.")


if __name__ == "__main__":
    _run_all()
