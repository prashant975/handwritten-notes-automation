"""
src/pw_auth.py — stable Google auth for the PW proxy (refresh + retry + diagnostics).

WHY THIS MODULE EXISTS (the root cause of the repeating 401)
------------------------------------------------------------
Streamlit's native OIDC login (`st.login` / `st.user`) persists the login in a
signed browser cookie whose `Max-Age` is **30 days**
(`AUTH_COOKIE_MAX_AGE_SECONDS`), but the only credentials it keeps inside that
cookie are the raw `id_token` and `access_token`::

    # streamlit/web/server/starlette/starlette_auth_routes.py, _auth_callback()
    tokens = {k: token[k] for k in ["id_token", "access_token"] if k in token}

The Google `id_token` in there is a JWT that **expires after ~1 hour**, and
Streamlit never renews it. Google's `refresh_token` — the one credential that
could renew it — is dropped on that line and never stored.

So, one hour after signing in:

  * ``st.user.is_logged_in`` is still ``True``   (cookie is good for 30 days)
  * ``st.user.tokens.id``     is a **dead JWT**  (expired after ~1 hour)
  * ``POST /api/allowlist``   returns **401**    -> "Couldn't verify your access
    with the PW proxy." / "Proxy said 401 — your Google sign-in has expired."

That is why users are "already signed in" yet still rejected, and why signing
out and back in never fixes it for long: a fresh login just buys another hour.

WHAT THIS MODULE DOES
---------------------
1. Asks Google for a real **refresh token** (`access_type=offline`, configured
   via ``authorize_params`` in ``.streamlit/secrets.toml``).
2. **Captures** that refresh token at login — Streamlit throws it away, so we
   wrap the one OAuth call that still has it (see ``install_login_hook``) and
   persist it outside the cookie, encrypted at rest with Windows DPAPI when
   available.
3. **Mints a fresh id_token on demand** from the refresh token, so the token
   handed to the PW proxy is never more than a few minutes old — no page
   reload, no re-login, and session state (your generated results) survives.
4. Exposes ``auth_status()`` — the equivalent of the ``GET /auth/status``
   endpoint — and ``refresh_now()`` (``POST /auth/refresh``).
5. Writes a **secret-free** audit trail to ``logs/auth_debug.log`` explaining
   every 401.

SECURITY
--------
No token, cookie, client secret or API key is ever written to the log; tokens
appear only as a short SHA-256 fingerprint so two different tokens can be told
apart without revealing either. The refresh token is stored outside the repo,
in the OS per-user state directory, DPAPI-encrypted on Windows.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

__all__ = [
    "install_login_hook",
    "bind_streamlit_session",
    "get_fresh_token",
    "token_provider_for",
    "refresh_now",
    "auth_status",
    "forget_user",
    "note_proxy_result",
    "audit",
    "GOOGLE_TOKEN_ENDPOINT",
    "AuthState",
]

GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Refresh once fewer than this many seconds remain on the id_token. Google
# id_tokens live ~3600s, so we renew at ~55 minutes and never hand the proxy a
# token that could die mid-request.
REFRESH_SKEW_SECONDS = int(os.getenv("PW_AUTH_REFRESH_SKEW", "300"))

# Hard floor: a token with less than this left is treated as already dead.
DEAD_TOKEN_SECONDS = 30

_REFRESH_TIMEOUT = 20
_LOG_MAX_BYTES = 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# Process-wide state.
#
# Keyed by email so this is correct for a multi-user deployment as well as the
# single-user desktop build: two signed-in users never share a cache entry, and
# nothing about "the current user" is held in a module-level global.
#
# This cache is deliberately NOT st.session_state: the generation pipeline fans
# Gemini calls out across a ThreadPoolExecutor, and worker threads have no
# Streamlit script context, so session_state is unreadable there.
# ---------------------------------------------------------------------------
_LOCK = threading.RLock()
_MINTED: dict[str, dict[str, Any]] = {}      # email -> minted token record
_COOKIE_TOKENS: dict[str, dict[str, Any]] = {}  # email -> token Streamlit holds
_LAST_EVENT: dict[str, dict[str, Any]] = {}  # email -> last proxy/refresh result
_LAST_STATUS_SIGNATURE: dict[str, tuple] = {}  # email -> last logged status shape


class AuthState:
    """Coarse auth outcomes, used to pick the user-facing message."""

    OK = "ok"                       # a fresh token is available
    NEEDS_REFRESH = "needs_refresh"  # token is near/at expiry, refresh possible
    REAUTH_REQUIRED = "reauth"      # cannot refresh — user must reconnect Google
    NO_SESSION = "no_session"       # not signed in at all


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def _state_dir() -> Path:
    """Per-user, per-machine state directory (never inside the repo or the
    PyInstaller dist, both of which can be read-only or shared)."""
    override = os.getenv("PW_AUTH_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "PWConciseNotes" / "auth"
    base = os.getenv("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "pw-concise-notes" / "auth"


def _token_store_path() -> Path:
    return _state_dir() / "google_refresh.json"


def _log_path() -> Path:
    override = os.getenv("PW_AUTH_LOG_DIR")
    if override:
        d = Path(override).expanduser()
    else:
        d = Path(
            os.getenv("HANDWRITTEN_NOTES_ROOT", str(Path(__file__).resolve().parents[1]))
        ).expanduser() / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception:
        # Frozen/read-only install dir — fall back to the per-user state dir.
        d = _state_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
    return d / "auth_debug.log"


# ---------------------------------------------------------------------------
# Secret-free diagnostics
# ---------------------------------------------------------------------------
def fingerprint(secret: str | None) -> str:
    """A short, stable, NON-reversible id for a token.

    Lets the log show "the token changed after refresh" without ever recording
    a credential. Never returns any part of the input.
    """
    if not secret:
        return "-"
    return hashlib.sha256(secret.encode("utf-8", "replace")).hexdigest()[:8]


# Field names that must never reach the log, whatever the caller passes.
_FORBIDDEN_LOG_KEYS = {
    "token", "id_token", "access_token", "refresh_token", "cookie", "cookies",
    "authorization", "client_secret", "api_key", "secret", "password",
    "credential", "code", "assertion",
}


def audit(event: str, **fields: Any) -> None:
    """Append one secret-free JSON line to logs/auth_debug.log.

    Any field whose name looks like a credential is dropped and replaced with a
    fingerprint, so a careless caller cannot leak a token into the log.
    """
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
    }
    for key, value in fields.items():
        if key.lower() in _FORBIDDEN_LOG_KEYS:
            record[f"{key}_fp"] = fingerprint(value if isinstance(value, str) else None)
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            record[key] = value
        else:
            record[key] = str(value)[:200]
    try:
        path = _log_path()
        if path.exists() and path.stat().st_size > _LOG_MAX_BYTES:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # Diagnostics must never break the app.
        pass


# ---------------------------------------------------------------------------
# Refresh-token storage (encrypted at rest on Windows via DPAPI)
# ---------------------------------------------------------------------------
def _dpapi_encrypt(plaintext: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import win32crypt  # provided by pywin32, already a Windows dependency

        blob = win32crypt.CryptProtectData(
            plaintext.encode("utf-8"), "pw-concise-notes", None, None, None, 0
        )
        return "dpapi:" + base64.b64encode(blob).decode("ascii")
    except Exception:
        return None


def _dpapi_decrypt(stored: str) -> str | None:
    if not stored.startswith("dpapi:"):
        return None
    try:
        import win32crypt

        blob = base64.b64decode(stored[len("dpapi:"):])
        _desc, plaintext = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
        return plaintext.decode("utf-8")
    except Exception:
        return None


# auth_status() runs on every Streamlit rerun, so the store is cached against
# the file's (mtime, size) and invalidated explicitly on write. Without this,
# every rerun re-read the file and re-ran a DPAPI decrypt several times over.
_STORE_CACHE: dict[str, Any] = {"stamp": None, "data": {}}


def _read_store() -> dict[str, Any]:
    path = _token_store_path()
    try:
        if not path.exists():
            return {}
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        with _LOCK:
            if _STORE_CACHE["stamp"] == stamp:
                return dict(_STORE_CACHE["data"])
        data = json.loads(path.read_text(encoding="utf-8")) or {}
        with _LOCK:
            _STORE_CACHE["stamp"] = stamp
            _STORE_CACHE["data"] = dict(data)
        return data
    except Exception:
        audit("token_store_read_failed", path=str(path))
        return {}


def _write_store(data: dict[str, Any]) -> None:
    path = _token_store_path()
    with _LOCK:
        _STORE_CACHE["stamp"] = None      # invalidate before the write lands
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(path)
    except Exception:
        audit("token_store_write_failed", path=str(path))


def save_refresh_token(email: str, refresh_token: str) -> None:
    """Persist a refresh token for `email`. Encrypted with DPAPI on Windows."""
    email = (email or "").strip().lower()
    if not email or not refresh_token:
        return
    stored = _dpapi_encrypt(refresh_token) or refresh_token
    data = _read_store()
    data[email] = {
        "refresh_token": stored,
        "encrypted": stored.startswith("dpapi:"),
        "updated_at": time.time(),
    }
    _write_store(data)
    audit(
        "refresh_token_saved",
        user_email=email,
        encrypted=stored.startswith("dpapi:"),
        refresh_token_fp=fingerprint(refresh_token),
    )


def load_refresh_token(email: str) -> str:
    email = (email or "").strip().lower()
    if not email:
        return ""
    entry = _read_store().get(email) or {}
    stored = str(entry.get("refresh_token") or "")
    if not stored:
        return ""
    return _dpapi_decrypt(stored) or (stored if not stored.startswith("dpapi:") else "")


def forget_user(email: str) -> None:
    """Drop every cached credential for `email` (used by Sign out / Reconnect)."""
    email = (email or "").strip().lower()
    with _LOCK:
        _MINTED.pop(email, None)
        _COOKIE_TOKENS.pop(email, None)
        _LAST_EVENT.pop(email, None)
        _LAST_STATUS_SIGNATURE.pop(email, None)
    data = _read_store()
    if email in data:
        data.pop(email, None)
        _write_store(data)
    audit("forget_user", user_email=email)


# ---------------------------------------------------------------------------
# JWT helpers (claims only — the PW proxy does the real verification)
# ---------------------------------------------------------------------------
def jwt_claims(token: str) -> dict[str, Any]:
    """Best-effort decode of a JWT payload. Never verifies and never raises."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8")) or {}
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return {}
    except Exception:
        return {}


def token_expiry(token: str) -> float:
    """Unix expiry of a JWT, or 0.0 when unknown."""
    try:
        return float(jwt_claims(token).get("exp") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def seconds_left(token: str) -> float:
    exp = token_expiry(token)
    return (exp - time.time()) if exp else 0.0


# ---------------------------------------------------------------------------
# Login hook — capture the refresh token Streamlit discards
# ---------------------------------------------------------------------------
def _capture_login_token(token: dict[str, Any]) -> None:
    """Called with Authlib's full token response at the end of the OAuth
    callback — the only moment the refresh token exists in this process."""
    info = token.get("userinfo") or {}
    try:
        email = str(info.get("email", "") or "").strip().lower()
    except Exception:
        email = ""
    refresh_token = str(token.get("refresh_token") or "")
    id_token = str(token.get("id_token") or "")

    if not email:
        email = str(jwt_claims(id_token).get("email", "") or "").strip().lower()

    audit(
        "oauth_callback",
        user_email=email or "-",
        got_refresh_token=bool(refresh_token),
        got_id_token=bool(id_token),
        id_token_fp=fingerprint(id_token),
        seconds_until_expiry=int(seconds_left(id_token)),
    )

    if email and refresh_token:
        save_refresh_token(email, refresh_token)
    elif email and not refresh_token:
        # Google only re-issues a refresh token when access_type=offline is on
        # the authorize URL (and prompt=consent forces a fresh one). If this
        # fires, .streamlit/secrets.toml is missing its [auth.google]
        # authorize_params block — see the README section this module ships with.
        audit("no_refresh_token_issued", user_email=email,
              hint="add authorize_params access_type=offline to [auth.google]")

    if email and id_token:
        _remember_minted(email, id_token, str(token.get("access_token") or ""),
                         source="login")


def install_login_hook() -> bool:
    """Wrap Streamlit's OAuth client so the refresh token survives login.

    Streamlit keeps only `id_token`/`access_token` from Google's token response.
    Rather than patch that (a moving target), we wrap the single call that still
    has the complete response — `client.authorize_access_token()` — via the
    module-level `_create_oauth_client` factory that `_auth_callback` looks up
    at call time. Nothing about Streamlit's own behaviour changes; we only read.

    Safe by construction: idempotent, and if Streamlit's internals ever move,
    this returns False and the app falls back to Streamlit's plain 1-hour token
    (i.e. today's behaviour) instead of breaking login.
    """
    try:
        from streamlit.web.server.starlette import starlette_auth_routes as routes
    except Exception:
        audit("login_hook_unavailable", reason="starlette_auth_routes not importable")
        return False

    if getattr(routes, "_pw_refresh_hook_installed", False):
        return True

    original_factory = getattr(routes, "_create_oauth_client", None)
    if not callable(original_factory):
        audit("login_hook_unavailable", reason="_create_oauth_client missing")
        return False

    def _patched_create_oauth_client(provider: str):
        client, redirect_uri = original_factory(provider)
        if not getattr(client, "_pw_capture_installed", False):
            inner = client.authorize_access_token

            async def _capturing_authorize_access_token(request, **kwargs):
                token = await inner(request, **kwargs)
                try:
                    _capture_login_token(dict(token))
                except Exception as exc:            # never break login
                    audit("capture_failed", error=type(exc).__name__)
                return token

            try:
                client.authorize_access_token = _capturing_authorize_access_token
                client._pw_capture_installed = True
            except Exception as exc:
                audit("capture_install_failed", error=type(exc).__name__)
        return client, redirect_uri

    try:
        routes._create_oauth_client = _patched_create_oauth_client
        routes._pw_refresh_hook_installed = True
    except Exception as exc:
        audit("login_hook_failed", error=type(exc).__name__)
        return False

    audit("login_hook_installed")
    return True


# ---------------------------------------------------------------------------
# Streamlit session binding
# ---------------------------------------------------------------------------
def _google_client_credentials() -> tuple[str, str]:
    """Read the OAuth client id/secret already configured for st.login.

    These come from .streamlit/secrets.toml — the same credentials Streamlit
    itself uses. Nothing is hardcoded here.
    """
    try:
        import streamlit as st

        section = st.secrets.get("auth", {}).get("google", {})
        return (
            str(section.get("client_id", "") or "").strip(),
            str(section.get("client_secret", "") or "").strip(),
        )
    except Exception:
        return "", ""


def bind_streamlit_session() -> str:
    """Record what Streamlit's cookie currently holds for the signed-in user.

    Called once per script run from the Streamlit thread (worker threads cannot
    read `st.user`). Returns the signed-in email, or "" when not signed in.
    """
    try:
        import streamlit as st

        if not bool(getattr(st.user, "is_logged_in", False)):
            return ""
        email = str(st.user.get("email", "") or "").strip().lower()
    except Exception:
        return ""
    if not email:
        return ""

    tokens: Any = {}
    try:
        tokens = st.user.get("tokens", {}) or {}
    except Exception:
        tokens = {}

    def _read(key: str) -> str:
        try:
            return str(tokens.get(key, "") or "").strip()
        except Exception:
            return str(getattr(tokens, key, "") or "").strip()

    with _LOCK:
        _COOKIE_TOKENS[email] = {
            "id": _read("id"),
            "access": _read("access"),
            "seen_at": time.time(),
        }
    return email


def _cookie_token(email: str) -> str:
    with _LOCK:
        entry = _COOKIE_TOKENS.get(email) or {}
    return str(entry.get("id") or entry.get("access") or "")


def _remember_minted(email: str, id_token: str, access_token: str, *, source: str) -> None:
    exp = token_expiry(id_token)
    with _LOCK:
        _MINTED[email] = {
            "id_token": id_token,
            "access_token": access_token,
            "exp": exp,
            "source": source,
            "minted_at": time.time(),
        }


# ---------------------------------------------------------------------------
# The refresh grant
# ---------------------------------------------------------------------------
def _mint_from_refresh_token(email: str) -> tuple[str, str]:
    """Exchange the stored refresh token for a brand-new id_token.

    Returns (id_token, error_code). error_code is "" on success.
    """
    refresh_token = load_refresh_token(email)
    if not refresh_token:
        return "", "no_refresh_token"

    client_id, client_secret = _google_client_credentials()
    if not client_id or not client_secret:
        return "", "oauth_client_not_configured"

    started = time.time()
    try:
        response = requests.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=_REFRESH_TIMEOUT,
        )
    except Exception as exc:
        audit("refresh_network_error", user_email=email, error=type(exc).__name__,
              elapsed_ms=int((time.time() - started) * 1000))
        return "", "network_error"

    if response.status_code != 200:
        code = ""
        try:
            code = str((response.json() or {}).get("error") or "")
        except Exception:
            code = ""
        audit("refresh_rejected", user_email=email, http_status=response.status_code,
              google_error=code or "-",
              elapsed_ms=int((time.time() - started) * 1000))
        if code == "invalid_grant":
            # Revoked, expired, or password changed — only a real re-consent fixes it.
            forget_user(email)
            return "", "invalid_grant"
        return "", code or f"http_{response.status_code}"

    try:
        payload = response.json() or {}
    except Exception:
        return "", "bad_response"

    id_token = str(payload.get("id_token") or "")
    access_token = str(payload.get("access_token") or "")
    if not id_token:
        # openid scope missing from the original grant.
        audit("refresh_no_id_token", user_email=email)
        return "", "no_id_token"

    _remember_minted(email, id_token, access_token, source="refresh")
    audit(
        "refresh_ok",
        user_email=email,
        id_token_fp=fingerprint(id_token),
        seconds_until_expiry=int(seconds_left(id_token)),
        elapsed_ms=int((time.time() - started) * 1000),
    )
    return id_token, ""


# ---------------------------------------------------------------------------
# Public token access — the ONLY way the rest of the app gets a token
# ---------------------------------------------------------------------------
def get_fresh_token(email: str, *, force: bool = False) -> str:
    """Return a Google id_token that is valid right now, refreshing if needed.

    Never returns a token that is expired or about to expire. Returns "" when
    the user genuinely has to reconnect Google, so callers can show one clear
    message instead of bouncing them to a login screen on every rerun.

    `force=True` bypasses the cache — used by the single retry after a 401, so
    a token that the proxy rejected is never handed back a second time.
    """
    email = (email or "").strip().lower()
    if not email:
        return ""

    with _LOCK:
        cached = dict(_MINTED.get(email) or {})

    if not force and cached.get("id_token"):
        remaining = float(cached.get("exp") or 0.0) - time.time()
        if remaining > REFRESH_SKEW_SECONDS:
            return str(cached["id_token"])

    if force:
        # The proxy rejected what we had — drop it so we cannot serve it again.
        with _LOCK:
            _MINTED.pop(email, None)

    id_token, error = _mint_from_refresh_token(email)
    if id_token:
        return id_token

    # No refresh token yet (e.g. the user signed in before this fix shipped, or
    # secrets.toml lacks authorize_params). Fall back to Streamlit's own cookie
    # token while it is still genuinely alive — this keeps existing sessions
    # working instead of forcing everyone to reconnect at once.
    cookie_token = _cookie_token(email)
    if cookie_token and seconds_left(cookie_token) > DEAD_TOKEN_SECONDS:
        audit("using_cookie_token", user_email=email,
              reason=error or "no_refresh_token",
              seconds_until_expiry=int(seconds_left(cookie_token)),
              id_token_fp=fingerprint(cookie_token))
        return cookie_token

    audit("token_unavailable", user_email=email, reason=error or "expired",
          had_cookie_token=bool(cookie_token))
    return ""


def token_provider_for(email: str) -> Callable[..., str]:
    """A request-scoped callable that always yields a currently-valid token.

    Passed down into the pipeline instead of a token string so that a long
    generation run (or a download click an hour later) resolves a *fresh* token
    at the moment of each call, rather than reusing whatever was valid when the
    run started. This is what stops long batches dying half-way with a 401.
    """
    bound_email = (email or "").strip().lower()

    def _provider(force: bool = False) -> str:
        return get_fresh_token(bound_email, force=force)

    _provider.pw_auth_email = bound_email  # type: ignore[attr-defined]
    return _provider


def refresh_now(email: str) -> tuple[bool, str]:
    """The `/auth/refresh` equivalent. Returns (ok, message)."""
    email = (email or "").strip().lower()
    if not email:
        return False, "Not signed in."
    token = get_fresh_token(email, force=True)
    if token:
        return True, "Session refreshed."
    if not load_refresh_token(email):
        return False, (
            "No saved Google refresh token for this account — reconnect Google "
            "once to enable automatic refresh."
        )
    return False, "Google refused to refresh this session. Please reconnect Google."


# ---------------------------------------------------------------------------
# Status reporting (the `GET /auth/status` equivalent)
# ---------------------------------------------------------------------------
def note_proxy_result(email: str, *, status_code: int | None, outcome: str,
                      refreshed: bool = False, retried: bool = False,
                      endpoint: str = "") -> None:
    """Record the most recent proxy verification so the UI/debug panel can
    explain the last 401 without re-calling the proxy."""
    email = (email or "").strip().lower()
    with _LOCK:
        _LAST_EVENT[email] = {
            "status_code": status_code,
            "outcome": outcome,
            "refreshed": refreshed,
            "retried": retried,
            "endpoint": endpoint,
            "at": time.time(),
        }


def last_proxy_result(email: str) -> dict[str, Any]:
    with _LOCK:
        return dict(_LAST_EVENT.get((email or "").strip().lower()) or {})


def _iso(ts: float) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def auth_status(email: str = "", *, probe_proxy: bool = False) -> dict[str, Any]:
    """Everything the UI needs to decide what to show, in one call.

    Mirrors the requested `GET /auth/status` contract. Purely local by default
    (no network), so it is safe to call on every rerun; pass `probe_proxy=True`
    to additionally confirm against the PW proxy.
    """
    email = (email or "").strip().lower()
    if not email:
        return {
            "authenticated": False,
            "user_email": "",
            "expires_at": None,
            "seconds_until_expiry": 0,
            "needs_refresh": False,
            "proxy_status": "unknown",
            "state": AuthState.NO_SESSION,
            "has_refresh_token": False,
            "auto_refresh_enabled": False,
            "token_source": "none",
            "last_refresh_at": None,
            "last_proxy_status_code": None,
            "last_401_reason": None,
            "token_fingerprint": "-",
            "message": "Not signed in.",
        }

    with _LOCK:
        minted = dict(_MINTED.get(email) or {})
    has_refresh_token = bool(load_refresh_token(email))

    token = str(minted.get("id_token") or "")
    source = str(minted.get("source") or "")
    if not token:
        token = _cookie_token(email)
        source = "cookie" if token else "none"

    remaining = int(seconds_left(token)) if token else 0
    expires_at = _iso(token_expiry(token)) if token else None
    needs_refresh = bool(token) and remaining <= REFRESH_SKEW_SECONDS

    last = last_proxy_result(email)
    last_code = last.get("status_code")
    proxy_status = {
        "allowed": "ok",
        "expired": "expired",
        "denied": "denied",
        "error": "failed",
    }.get(str(last.get("outcome") or ""), "unknown")

    if not token and not has_refresh_token:
        state, message = AuthState.REAUTH_REQUIRED, (
            "Your Google session has expired and cannot be renewed automatically. "
            "Reconnect Google to continue."
        )
    elif not token:
        state, message = AuthState.NEEDS_REFRESH, "Session needs a refresh."
    elif needs_refresh and has_refresh_token:
        state, message = AuthState.NEEDS_REFRESH, "Session will refresh automatically."
    elif needs_refresh:
        state, message = AuthState.REAUTH_REQUIRED, (
            "Your Google sign-in is about to expire and this session has no saved "
            "refresh token. Reconnect Google once to enable automatic refresh."
        )
    else:
        state, message = AuthState.OK, "Signed in and verified."

    status: dict[str, Any] = {
        "authenticated": bool(token) or has_refresh_token,
        "user_email": email,
        "expires_at": expires_at,
        "seconds_until_expiry": max(0, remaining),
        "needs_refresh": needs_refresh,
        "proxy_status": proxy_status,
        "state": state,
        "has_refresh_token": has_refresh_token,
        "auto_refresh_enabled": has_refresh_token,
        "token_source": source or "none",
        "last_refresh_at": _iso(float(minted.get("minted_at") or 0.0)),
        "last_proxy_status_code": last_code,
        "last_401_reason": (
            last.get("outcome") if last_code == 401 else None
        ),
        "last_proxy_refreshed": bool(last.get("refreshed")),
        "last_proxy_retried": bool(last.get("retried")),
        "token_fingerprint": fingerprint(token),
        "message": message,
    }

    if probe_proxy:
        import pw_access

        outcome = pw_access.check_allowed_status(token_provider_for(email))
        status["proxy_status"] = {
            "allowed": "ok", "expired": "expired",
            "denied": "denied", "error": "failed",
        }.get(outcome, "unknown")
        status["authenticated"] = status["authenticated"] and outcome != "expired"

    # auth_status() runs on every Streamlit rerun (sidebar indicator, debug
    # panel), so logging unconditionally would bury the interesting lines under
    # thousands of identical "ok" entries. Log only when something actually
    # changed, or whenever the state is not healthy.
    signature = (state, status["proxy_status"], status["token_fingerprint"], has_refresh_token)
    with _LOCK:
        changed = _LAST_STATUS_SIGNATURE.get(email) != signature
        if changed:
            _LAST_STATUS_SIGNATURE[email] = signature
    if changed or state != AuthState.OK:
        audit(
            "auth_status",
            user_email=email,
            endpoint="/auth/status",
            state=state,
            seconds_until_expiry=status["seconds_until_expiry"],
            needs_refresh=needs_refresh,
            has_refresh_token=has_refresh_token,
            token_source=status["token_source"],
            proxy_status=status["proxy_status"],
            token_fp=status["token_fingerprint"],
        )
    return status
