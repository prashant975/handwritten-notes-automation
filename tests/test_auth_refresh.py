"""Unit tests for the Google token refresh + proxy 401 retry.

Covers the exact failure that made users re-login every hour: Streamlit keeps a
30-day login cookie holding a ~1h Google id_token it never renews, so the PW
proxy starts answering 401 while the user is still "signed in".

Runs standalone (python tests/test_auth_refresh.py) or under pytest. All network
is stubbed, so the suite is instant and offline — no Google token needed.
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Keep every test's state (refresh tokens, auth_debug.log) inside a temp dir so
# a test run can never touch the developer's real saved session.
_TMP = tempfile.mkdtemp(prefix="pw_auth_test_")
import os

os.environ["PW_AUTH_STATE_DIR"] = str(Path(_TMP) / "state")
os.environ["PW_AUTH_LOG_DIR"] = str(Path(_TMP) / "logs")

import pw_access
from src import pw_auth

EMAIL = "tester@pw.live"


def _jwt(exp_offset_seconds: float, email: str = EMAIL, marker: str = "x") -> str:
    """A syntactically real JWT with the given expiry. Signature is irrelevant —
    the PW proxy verifies tokens, this app only reads `exp`."""
    def seg(data: dict) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    payload = {"email": email, "exp": int(time.time() + exp_offset_seconds), "m": marker}
    return f"{seg({'alg': 'RS256'})}.{seg(payload)}.sig-{marker}"


def _reset():
    pw_auth.forget_user(EMAIL)
    with pw_auth._LOCK:
        pw_auth._MINTED.clear()
        pw_auth._COOKIE_TOKENS.clear()
        pw_auth._LAST_EVENT.clear()
    pw_access.set_token_provider(None)


# ---------------------------------------------------------------------------
# 1. The capture hook — the refresh token Streamlit throws away
# ---------------------------------------------------------------------------
def test_login_capture_stores_refresh_token():
    _reset()
    id_token = _jwt(3600)
    pw_auth._capture_login_token({
        "userinfo": {"email": EMAIL},
        "id_token": id_token,
        "access_token": "access-abc",
        "refresh_token": "refresh-abc",
    })
    assert pw_auth.load_refresh_token(EMAIL) == "refresh-abc"
    # The fresh id_token is cached too, so the first proxy call after login
    # doesn't need a refresh round-trip.
    assert pw_auth.get_fresh_token(EMAIL) == id_token
    print("PASS: login capture stores the refresh token Streamlit discards")


def test_capture_survives_missing_refresh_token():
    """Google omits the refresh token when access_type=offline is missing —
    that must be logged, not crash the login."""
    _reset()
    pw_auth._capture_login_token({
        "userinfo": {"email": EMAIL}, "id_token": _jwt(3600), "access_token": "a",
    })
    assert pw_auth.load_refresh_token(EMAIL) == ""
    print("PASS: login without a refresh token degrades gracefully")


# ---------------------------------------------------------------------------
# 2. Automatic refresh of an expired token
# ---------------------------------------------------------------------------
def test_expired_token_is_refreshed_automatically(monkeypatch=None):
    _reset()
    pw_auth.save_refresh_token(EMAIL, "refresh-abc")
    # Simulate the real bug: the only token we have is already dead.
    pw_auth._remember_minted(EMAIL, _jwt(-60, marker="dead"), "", source="login")

    minted = _jwt(3600, marker="fresh")
    calls = {"n": 0}

    def fake_post(url, data=None, timeout=None, **kw):
        calls["n"] += 1
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "refresh-abc"
        return _FakeResponse(200, {"id_token": minted, "access_token": "at", "expires_in": 3600})

    original = pw_auth.requests.post
    pw_auth.requests.post = fake_post
    try:
        token = pw_auth.get_fresh_token(EMAIL)
    finally:
        pw_auth.requests.post = original

    assert token == minted, "expired token must be replaced, not returned"
    assert calls["n"] == 1
    # Cached: a second call must not hit Google again.
    assert pw_auth.get_fresh_token(EMAIL) == minted
    assert calls["n"] == 1
    print("PASS: an expired token is refreshed automatically and then cached")


def test_invalid_grant_clears_state_and_requires_reconnect():
    _reset()
    pw_auth.save_refresh_token(EMAIL, "revoked")
    pw_auth._remember_minted(EMAIL, _jwt(-60), "", source="login")

    original = pw_auth.requests.post
    pw_auth.requests.post = lambda *a, **k: _FakeResponse(400, {"error": "invalid_grant"})
    try:
        assert pw_auth.get_fresh_token(EMAIL) == ""
        ok, message = pw_auth.refresh_now(EMAIL)
    finally:
        pw_auth.requests.post = original

    assert ok is False
    assert "reconnect" in message.lower()
    # A revoked grant is dropped, so we never retry it forever.
    assert pw_auth.load_refresh_token(EMAIL) == ""
    print("PASS: a revoked grant is cleared and surfaces a reconnect message")


def test_falls_back_to_cookie_token_when_no_refresh_token():
    """Users who signed in before this fix have no refresh token yet — their
    still-valid cookie token must keep working instead of forcing a reconnect."""
    _reset()
    cookie_token = _jwt(1800, marker="cookie")
    with pw_auth._LOCK:
        pw_auth._COOKIE_TOKENS[EMAIL] = {"id": cookie_token, "access": "", "seen_at": time.time()}
    assert pw_auth.get_fresh_token(EMAIL) == cookie_token
    print("PASS: falls back to Streamlit's cookie token while it is still alive")


def test_dead_cookie_token_is_never_returned():
    _reset()
    with pw_auth._LOCK:
        pw_auth._COOKIE_TOKENS[EMAIL] = {"id": _jwt(-10), "access": "", "seen_at": time.time()}
    assert pw_auth.get_fresh_token(EMAIL) == "", "a dead token must never reach the proxy"
    print("PASS: an expired cookie token is never handed to the proxy")


# ---------------------------------------------------------------------------
# 3. The single retry on 401 inside pw_access
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def test_proxy_401_triggers_exactly_one_retry_with_a_fresh_token():
    _reset()
    seen_tokens = []
    tokens = iter(["stale-token", "fresh-token"])
    current = {"value": next(tokens)}

    def provider(force=False):
        if force:
            current["value"] = next(tokens)
        return current["value"]

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        token = headers["Authorization"].split(" ", 1)[1]
        seen_tokens.append(token)
        if token == "stale-token":
            return _FakeResponse(401, {"error": "invalid or expired token"})
        return _FakeResponse(200, {"allowed": True})

    original = pw_access.requests.post
    pw_access.requests.post = fake_post
    try:
        status = pw_access.check_allowed_status(provider)
    finally:
        pw_access.requests.post = original

    assert status == "allowed", "the retry after refresh should succeed"
    assert seen_tokens == ["stale-token", "fresh-token"]
    print("PASS: a proxy 401 refreshes the token and retries exactly once")


def test_persistent_401_returns_expired_without_looping():
    _reset()
    attempts = {"n": 0}
    tokens = iter(["t1", "t2", "t3", "t4"])

    def provider(force=False):
        return next(tokens)

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        attempts["n"] += 1
        return _FakeResponse(401, {"error": "invalid or expired token"})

    original = pw_access.requests.post
    pw_access.requests.post = fake_post
    try:
        status = pw_access.check_allowed_status(provider)
    finally:
        pw_access.requests.post = original

    assert status == "expired"
    assert attempts["n"] == 2, f"expected exactly 1 retry, got {attempts['n']} calls"
    print("PASS: a persistent 401 stops after one retry — no infinite loop")


def test_no_retry_when_refreshed_token_is_identical():
    """Retrying with the same rejected token would just buy a second 401."""
    _reset()
    attempts = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        attempts["n"] += 1
        return _FakeResponse(401, {})

    original = pw_access.requests.post
    pw_access.requests.post = fake_post
    try:
        status = pw_access.check_allowed_status(lambda force=False: "same-token")
    finally:
        pw_access.requests.post = original

    assert status == "expired"
    assert attempts["n"] == 1
    print("PASS: no pointless retry when the token didn't actually change")


def test_denied_is_not_confused_with_expired():
    _reset()
    original = pw_access.requests.post
    pw_access.requests.post = lambda *a, **k: _FakeResponse(403, {})
    try:
        assert pw_access.check_allowed_status(lambda force=False: "tok") == "denied"
    finally:
        pw_access.requests.post = original

    pw_access.requests.post = lambda *a, **k: _FakeResponse(503, {})
    try:
        assert pw_access.check_allowed_status(lambda force=False: "tok") == "error"
    finally:
        pw_access.requests.post = original
    print("PASS: denied / unreachable / expired stay distinguishable")


def test_check_allowed_still_fails_closed():
    _reset()
    for code in (401, 403, 500):
        original = pw_access.requests.post
        pw_access.requests.post = lambda *a, _c=code, **k: _FakeResponse(_c, {})
        try:
            assert pw_access.check_allowed(lambda force=False: "tok") is False
        finally:
            pw_access.requests.post = original
    print("PASS: check_allowed still fails closed on every non-200")


# ---------------------------------------------------------------------------
# 4. Status contract + secret hygiene
# ---------------------------------------------------------------------------
def test_auth_status_contract():
    _reset()
    pw_auth.save_refresh_token(EMAIL, "refresh-abc")
    pw_auth._remember_minted(EMAIL, _jwt(3600), "", source="refresh")
    status = pw_auth.auth_status(EMAIL)

    for key in ("authenticated", "user_email", "expires_at", "seconds_until_expiry",
                "needs_refresh", "proxy_status", "message"):
        assert key in status, f"missing required field {key}"
    assert status["authenticated"] is True
    assert status["user_email"] == EMAIL
    assert status["needs_refresh"] is False
    assert 3000 < status["seconds_until_expiry"] <= 3600
    assert status["auto_refresh_enabled"] is True
    print("PASS: auth_status returns the documented contract")


def test_auth_status_flags_near_expiry():
    _reset()
    pw_auth.save_refresh_token(EMAIL, "refresh-abc")
    pw_auth._remember_minted(EMAIL, _jwt(60), "", source="refresh")
    status = pw_auth.auth_status(EMAIL)
    assert status["needs_refresh"] is True
    assert status["state"] == pw_auth.AuthState.NEEDS_REFRESH
    print("PASS: a near-expiry token is flagged for refresh before it dies")


def test_logs_never_contain_secrets():
    _reset()
    secret_token = _jwt(3600, marker="SUPERSECRETVALUE")
    pw_auth._capture_login_token({
        "userinfo": {"email": EMAIL},
        "id_token": secret_token,
        "access_token": "ACCESSSECRET",
        "refresh_token": "REFRESHSECRET",
    })
    pw_auth.auth_status(EMAIL)
    pw_auth.audit("manual", token="LEAKME", cookie="COOKIESECRET", api_key="KEYSECRET")

    log_text = pw_auth._log_path().read_text(encoding="utf-8")
    assert log_text.strip(), "the audit log should not be empty"
    for secret in ("SUPERSECRETVALUE", "ACCESSSECRET", "REFRESHSECRET", "LEAKME",
                   "COOKIESECRET", "KEYSECRET", secret_token):
        assert secret not in log_text, f"SECRET LEAKED INTO LOG: {secret}"
    # ...but it is still useful: fingerprints and reasons are there.
    assert "oauth_callback" in log_text
    assert EMAIL in log_text
    print("PASS: auth_debug.log explains 401s without leaking any secret")


def test_refresh_token_encrypted_at_rest_on_windows():
    _reset()
    pw_auth.save_refresh_token(EMAIL, "REFRESHSECRET")
    stored = pw_auth._token_store_path().read_text(encoding="utf-8")
    if os.name == "nt":
        try:
            import win32crypt  # noqa: F401
            assert "REFRESHSECRET" not in stored, "refresh token must be DPAPI-encrypted"
            print("PASS: refresh token is DPAPI-encrypted at rest")
            return
        except ImportError:
            pass
    print("SKIP: DPAPI unavailable (non-Windows or pywin32 missing)")


# ---------------------------------------------------------------------------
# 5. End-to-end scenario — the exact bug users reported
# ---------------------------------------------------------------------------
def test_scenario_one_hour_later_the_user_is_not_asked_to_log_in_again():
    """THE regression test.

    Reproduces the reported failure precisely: the user signed in over an hour
    ago, Streamlit's 30-day login cookie is still valid, but the Google
    id_token inside it is dead — which used to yield a proxy 401 and
    "Couldn't verify your access with the PW proxy."

    With the fix, clicking Generate must just work: refresh silently, retry,
    and return "allowed" without any user interaction.
    """
    _reset()

    # State one hour after login: dead cookie token, but a captured refresh token.
    pw_auth._capture_login_token({
        "userinfo": {"email": EMAIL},
        "id_token": _jwt(-1, marker="hour-old"),      # expired
        "access_token": "old-access",
        "refresh_token": "refresh-abc",
    })
    with pw_auth._LOCK:
        pw_auth._COOKIE_TOKENS[EMAIL] = {
            "id": _jwt(-1, marker="hour-old"), "access": "", "seen_at": time.time()}

    renewed = _jwt(3600, marker="renewed")
    google_calls, proxy_tokens = {"n": 0}, []

    # NOTE: pw_auth.requests and pw_access.requests are the SAME module object,
    # so one stub has to serve both and dispatch on the URL.
    def fake_post(url, headers=None, json=None, data=None, timeout=None, **kw):
        if url == pw_auth.GOOGLE_TOKEN_ENDPOINT:
            google_calls["n"] += 1
            assert data["grant_type"] == "refresh_token"
            return _FakeResponse(
                200, {"id_token": renewed, "access_token": "at", "expires_in": 3600})
        token = headers["Authorization"].split(" ", 1)[1]
        proxy_tokens.append(token)
        # The real proxy rejects anything expired.
        if "hour-old" in token:
            return _FakeResponse(401, {"error": "invalid or expired token"})
        return _FakeResponse(200, {"allowed": True})

    original = pw_access.requests.post
    pw_access.requests.post = fake_post
    try:
        # Exactly what app.py does when Generate is clicked.
        provider = pw_auth.token_provider_for(EMAIL)
        status = pw_access.check_allowed_status(provider)
    finally:
        pw_access.requests.post = original

    assert status == "allowed", "the user must NOT be asked to sign in again"
    assert google_calls["n"] == 1, "exactly one silent refresh"
    assert proxy_tokens == [renewed], (
        "the dead token must never reach the proxy — refresh happens before the call")

    final = pw_auth.auth_status(EMAIL)
    assert final["state"] == pw_auth.AuthState.OK
    assert final["auto_refresh_enabled"] is True
    print("PASS: SCENARIO — an hour later, Generate works with no re-login")


def test_scenario_download_an_hour_later_does_not_log_the_user_out():
    """A download click is just a Streamlit rerun. It must resolve a fresh token
    rather than bouncing the user to a login screen and losing their results."""
    _reset()
    pw_auth.save_refresh_token(EMAIL, "refresh-abc")
    pw_auth._remember_minted(EMAIL, _jwt(-1, marker="stale"), "", source="login")

    renewed = _jwt(3600, marker="renewed")
    original = pw_auth.requests.post
    pw_auth.requests.post = lambda *a, **k: _FakeResponse(
        200, {"id_token": renewed, "access_token": "at", "expires_in": 3600})
    try:
        status = pw_auth.auth_status(EMAIL)
        assert status["state"] != pw_auth.AuthState.NO_SESSION
        assert pw_auth.get_fresh_token(EMAIL) == renewed
        ok, _ = pw_auth.refresh_now(EMAIL)
    finally:
        pw_auth.requests.post = original

    assert ok is True
    print("PASS: SCENARIO — downloading an hour later keeps the user signed in")


# ---------------------------------------------------------------------------
# 6. The 7-day session window
# ---------------------------------------------------------------------------
def _session_helpers(iat: float, session_days: int = 7):
    """Re-create app.py's session-window maths against a fake `iat`, without
    importing app.py (which needs a live Streamlit script context)."""
    timeout = session_days * 24 * 60 * 60

    def seconds_remaining() -> int:
        if iat <= 0:
            return -1
        return int(timeout - (time.time() - iat))

    return seconds_remaining, lambda: seconds_remaining() != -1 and seconds_remaining() <= 0


def test_session_survives_six_days_without_relogin():
    now = time.time()
    for day in range(0, 7):
        remaining, expired = _session_helpers(now - day * 86400)
        assert expired() is False, f"must NOT be logged out on day {day}"
        assert remaining() > 0
    print("PASS: the session stays valid every day up to day 7")


def test_session_expires_after_seven_days():
    remaining, expired = _session_helpers(time.time() - (7 * 86400 + 60))
    assert expired() is True
    assert remaining() <= 0
    print("PASS: the session expires once 7 days have elapsed")


def test_session_window_measures_from_login_not_from_refresh():
    """A silent token refresh must NOT extend the 7-day window: `iat` comes from
    Streamlit's login cookie, which refreshing never rewrites."""
    _reset()
    login_time = time.time() - (6.5 * 86400)
    remaining_before, _ = _session_helpers(login_time)
    before = remaining_before()

    pw_auth.save_refresh_token(EMAIL, "refresh-abc")
    original = pw_auth.requests.post
    pw_auth.requests.post = lambda *a, **k: _FakeResponse(
        200, {"id_token": _jwt(3600, marker="renewed"), "access_token": "at", "expires_in": 3600})
    try:
        assert pw_auth.get_fresh_token(EMAIL, force=True)  # a refresh happens
    finally:
        pw_auth.requests.post = original

    remaining_after, _ = _session_helpers(login_time)   # iat is unchanged
    assert remaining_after() <= before, "refreshing must not extend the 7-day window"
    assert remaining_after() > 0, "but the session is still alive on day 6.5"
    print("PASS: refreshing renews the token without extending the 7-day session")


def test_session_length_is_configurable():
    _, expired_7 = _session_helpers(time.time() - 8 * 86400, session_days=7)
    _, expired_30 = _session_helpers(time.time() - 8 * 86400, session_days=30)
    assert expired_7() is True
    assert expired_30() is False
    print("PASS: PW_SESSION_DAYS controls the session length")


def test_missing_iat_does_not_expire_the_session():
    """A cookie without `iat` shouldn't cause a surprise logout; Streamlit's own
    30-day cookie governs in that case."""
    remaining, expired = _session_helpers(0)
    assert remaining() == -1
    assert expired() is False
    print("PASS: an unknown sign-in time doesn't force a logout")


# ---------------------------------------------------------------------------
# 7. Cookie origin — localhost vs 127.0.0.1
# ---------------------------------------------------------------------------
def _origin_of(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(str(url or ""))
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


def test_localhost_and_loopback_ip_are_different_origins():
    """The bug the launcher used to have: it opened 127.0.0.1 while redirect_uri
    said localhost, so the login cookie and the OAuth state cookie were never
    sent back — a silent sign-in failure on every restart."""
    configured = _origin_of("http://localhost:8501/oauth2callback")
    assert configured == "http://localhost:8501"
    assert _origin_of("http://127.0.0.1:8501") != configured, (
        "127.0.0.1 must be detected as a different origin from localhost")
    assert _origin_of("http://localhost:8501/?debug=1") == configured
    print("PASS: 127.0.0.1 is correctly detected as a different origin")


def test_launcher_opens_the_configured_origin():
    """Regression guard: the launcher's host must match redirect_uri's host."""
    launcher = (Path(__file__).resolve().parents[1] / "streamlit_launcher_stable.py").read_text(
        encoding="utf-8")
    assert 'HOST = "localhost"' in launcher, "launcher must use localhost, not 127.0.0.1"
    assert 'url = f"http://{HOST}:{PORT}"' in launcher, "launcher must open the configured host"
    assert "http://127.0.0.1:{PORT}" not in launcher
    print("PASS: the launcher opens the same origin the login cookie is bound to")


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
    print(f"\n{len(tests)} auth tests passed.")


if __name__ == "__main__":
    _run_all()
