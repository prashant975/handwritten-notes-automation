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
    "UsageSession",
    "gemini_generate",
    "mathpix_ocr",
    "sarvam_tts",
    "elevenlabs_tts",
]


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


def test_app_name_is_this_app_not_the_template_placeholder():
    assert pw_access.APP_NAME == "Handwritten Notes Automation", pw_access.APP_NAME
    template = KIT_TEMPLATE.read_text(encoding="utf-8")
    assert 'APP_NAME = "Final ZIP Package"' in template, (
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
def test_usage_session_is_thread_safe():
    """The pipeline fans Gemini chunk calls across a ThreadPoolExecutor; the
    kit template's unguarded dict would lose usage under that."""
    assert "_lock" in inspect.getsource(pw_access.UsageSession), (
        "UsageSession lost its threading lock — usage rows will be lost")

    import threading

    session = pw_access.UsageSession("tok", filename="f", input_unit="pages", count=1)
    threads = [
        threading.Thread(target=lambda: [session.add("gemini-2.5-pro", 10, 5) for _ in range(200)])
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    agg = session._by_model["gemini-2.5-pro"]
    assert agg["requests"] == 8 * 200, f"lost usage under concurrency: {agg['requests']}"
    assert agg["tokens_in"] == 8 * 200 * 10
    print("PASS: UsageSession accumulates correctly across 8 concurrent threads")


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


def test_vertex_cache_can_be_invalidated():
    assert hasattr(pw_access, "_invalidate_vertex")
    pw_access._vertex_cache.update({"token": "stale", "expiry": 9e9})
    pw_access._invalidate_vertex()
    assert pw_access._vertex_cache["token"] == ""
    assert pw_access._vertex_cache["expiry"] == 0.0
    print("PASS: a dead Vertex token can be invalidated instead of replayed")


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
    print(f"\n{len(tests)} kit contract tests passed.")


if __name__ == "__main__":
    _run_all()
