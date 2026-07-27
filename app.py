from __future__ import annotations

import re
import tempfile
import os
import base64
import time
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st

import pw_access
from src import model_router as mr
from src import pw_auth
from src import version
from src.ai_client import GeminiClient, GeminiError
from src.config import (
    APP_NAME,
    APP_VERSION,
    DEBUG,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_MATH_RENDER_MODE,
    DEFAULT_MODEL,
    DEFAULT_PROCESSING_MODE,
    EXAMS,
    MATH_RENDER_MODES,
    MODEL_ROUTING_MODE,
    PROCESSING_MODES,
    SUBJECTS,
)
from src.pipeline import run_pipeline

st.set_page_config(page_title="Concise Notes Automation", layout="wide")

# Capture the Google refresh token at login. Streamlit keeps only the ~1h
# id_token, so without this every session dies after an hour with a proxy 401.
# Installed here (import time, before any OAuth callback can run) and safe to
# call on every rerun. See src/pw_auth.py for the full explanation.
PW_REFRESH_HOOK_OK = pw_auth.install_login_hook()

BASE_DIR = Path(os.getenv("HANDWRITTEN_NOTES_ROOT", str(Path(__file__).parent))).expanduser()
LOGO_PATH = BASE_DIR / "assets" / "pw_logo.png"
ALLOWED_EMAIL_DOMAIN = "pw.live"


def _session_days() -> int:
    """How long one sign-in lasts, in days (PW standard: 7)."""
    try:
        return max(1, int(os.getenv("PW_SESSION_DAYS", "7")))
    except (TypeError, ValueError):
        return 7


SESSION_TIMEOUT_DAYS = _session_days()
SESSION_TIMEOUT_SECONDS = SESSION_TIMEOUT_DAYS * 24 * 60 * 60


# The local email-allowlist subsystem (old Google sheet / workbook / text file)
# has been removed. The PW proxy's "Whitelisted" sheet is now the SINGLE source
# of truth for who may use this app (checked via pw_access.check_allowed at
# generate time). If the proxy is unreachable, the app fails closed.


def _google_auth_configured() -> bool:
    try:
        google_auth = st.secrets.get("auth", {}).get("google", {})
        client_id = str(google_auth.get("client_id", "")).strip()
        client_secret = str(google_auth.get("client_secret", "")).strip()
    except Exception:
        return False

    placeholder_values = ("", "your-google-oauth-client-id", "your-google-oauth-client-secret")
    return (
        client_id.endswith(".apps.googleusercontent.com")
        and client_secret not in placeholder_values
        and not client_id.startswith("your-google-oauth-client-id")
    )


def _current_user_email() -> str:
    email = ""
    try:
        email = st.user.get("email", "")
    except Exception:
        email = ""
    return str(email).strip().lower()


def _token_provider():
    """A callable that yields a CURRENTLY-VALID Google token for this user.

    Never hand a bare token string to the proxy or the pipeline: a Google
    id_token lives ~1 hour, so anything holding one for longer (a long batch, a
    download click an hour later) hits a 401. This provider re-resolves — and
    silently refreshes — the token at the moment of each call.
    """
    return pw_auth.token_provider_for(_current_user_email())


def _google_proxy_token(force: bool = False) -> str:
    """The Google token to send to the PW proxy, refreshed if it is near expiry.

    Replaces the old "read st.user.tokens once" behaviour, which returned the
    frozen ~1h token straight out of Streamlit's 30-day login cookie.
    """
    return pw_auth.get_fresh_token(_current_user_email(), force=force)


ALLOWLIST_CACHE_TTL_SECONDS = 300  # re-check the proxy whitelist at most every 5 min


def _proxy_access_status(force: bool = False) -> str:
    """Return the PW proxy's authorization status for the signed-in user:
    "allowed" / "denied" / "expired" / "error".

    An "allowed" result is cached in the session for ALLOWLIST_CACHE_TTL_SECONDS
    so we don't call the proxy on every Streamlit rerun. Non-allowed results are
    never cached, so a newly-whitelisted user is admitted on their next
    interaction. Token refresh and the single 401 retry happen inside
    pw_access — by the time "expired" comes back, renewal has already failed.
    Does NOT touch the AI/usage pipeline.
    """
    email = _current_user_email()
    now = time.time()
    cache = st.session_state.get("_allow_cache")
    if (
        not force
        and isinstance(cache, dict)
        and cache.get("status") == "allowed"
        and cache.get("email") == email
        and (now - float(cache.get("ts", 0))) < ALLOWLIST_CACHE_TTL_SECONDS
    ):
        return "allowed"
    status = pw_access.check_allowed_status(_token_provider())
    st.session_state["_allow_cache"] = {"email": email, "status": status, "ts": now}
    pw_auth.note_proxy_result(
        email,
        status_code={"allowed": 200, "denied": 403, "expired": 401}.get(status),
        outcome=status,
        endpoint="/api/allowlist",
    )
    return status


def _usage_logged(result) -> bool:
    """Whether the PW proxy recorded this run in the Usage Cost sheet."""
    return bool((result.metadata or {}).get("usage_logged"))


def _clear_local_auth_state(email: str = "", *, forget_refresh_token: bool = True) -> None:
    """Drop every cached auth artefact for this user, frontend and backend.

    A logout that clears only the Streamlit cookie leaves our minted token and
    the stored refresh token behind, so the "next" user of the same machine
    would inherit them. Clearing both keeps the two sides in step.
    """
    email = email or _current_user_email()
    for key in ("_allow_cache", "_proxy_err_detail", "_admitted_email", "_auth_refresh_attempted"):
        st.session_state.pop(key, None)
    if forget_refresh_token and email:
        pw_auth.forget_user(email)


def _logout_user() -> None:
    email = _current_user_email()
    _clear_local_auth_state(email)
    pw_auth.audit("logout", user_email=email or "-")
    if bool(getattr(st.user, "is_logged_in", False)):
        st.logout()
    st.rerun()


def _reconnect_google() -> None:
    """Full reconnect: clear local auth state, then start Google sign-in.

    Used when the refresh genuinely fails (revoked grant, changed password).
    `st.login` returns to this same page after Google, so the user lands back
    where they were rather than at a generic landing screen.
    """
    email = _current_user_email()
    _clear_local_auth_state(email)
    pw_auth.audit("reconnect_requested", user_email=email or "-")
    try:
        st.login("google")
    except Exception:
        _logout_user()


def _session_issued_at() -> int:
    """When this sign-in happened (the id_token's `iat`), or 0 if unknown.

    Read from Streamlit's login cookie, which is written once at login and is
    NOT rewritten by our silent token refresh — so the 7-day window is measured
    from the actual sign-in, and refreshing can never extend it indefinitely.
    """
    try:
        return int(st.user.get("iat", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _session_seconds_remaining() -> int:
    """Seconds left in the 7-day session; -1 when it can't be determined."""
    issued_at = _session_issued_at()
    if issued_at <= 0:
        return -1
    return int(SESSION_TIMEOUT_SECONDS - (time.time() - issued_at))


def _auth_session_expired() -> bool:
    """Expire signed-in users SESSION_TIMEOUT_DAYS (default 7) after sign-in."""
    remaining = _session_seconds_remaining()
    return remaining != -1 and remaining <= 0


def _session_remaining_label() -> str:
    """Human summary of how much of the 7-day session is left."""
    remaining = _session_seconds_remaining()
    if remaining < 0:
        return ""
    days, hours = divmod(max(0, remaining) // 3600, 24)
    if days >= 1:
        return f"Session valid for {days} more day{'s' if days != 1 else ''}"
    if hours >= 1:
        return f"Session expires in {hours} hour{'s' if hours != 1 else ''}"
    return "Session expires in under an hour"


def _ensure_fresh_session() -> tuple[bool, str]:
    """Make sure a usable Google token exists BEFORE anything calls the proxy.

    This is the automatic-refresh step: if the token is near expiry it is
    renewed silently from the stored refresh token — no page reload, no
    re-login, and nothing in st.session_state is lost. Returns
    (ok, message); ok=False means the user genuinely has to reconnect.
    """
    email = _current_user_email()
    if not email:
        return False, "Not signed in."
    status = pw_auth.auth_status(email)
    if status["state"] == pw_auth.AuthState.OK:
        return True, ""
    ok, message = pw_auth.refresh_now(email)
    pw_auth.audit(
        "auto_refresh",
        user_email=email,
        endpoint="/auth/refresh",
        refresh_attempted=True,
        retry_succeeded=ok,
        had_refresh_token=status["has_refresh_token"],
        state_before=status["state"],
    )
    return ok, message


# Distinct, actionable messages per failure cause — replaces the single
# "Couldn't verify your access with the PW proxy." for every possible problem.
_PROXY_MESSAGES = {
    "expired": (
        "Session expired",
        "Your Google sign-in could not be renewed automatically. "
        "Click **Reconnect Google** below to sign in once more.",
    ),
    "denied": (
        "Permission denied",
        "This Google account isn't on the allowlist for this app.",
    ),
    "error": (
        "Proxy unreachable",
        "The PW proxy couldn't be reached, so access can't be verified right now. "
        "Check your internet connection and try again in a moment.",
    ),
    "no_token": (
        "Token refresh failed",
        "There's no valid Google token in this session and it couldn't be renewed. "
        "Reconnect Google to continue.",
    ),
}


def _proxy_error_detail(status: str = "error") -> str:
    """A specific, human explanation of why access verification failed.

    Costs no network call: the reason is already known from the allowlist
    result and the local auth status (the old version fired an extra ~15s
    proxy request from the error screen just to learn the status code).
    """
    email = _current_user_email()
    auth = pw_auth.auth_status(email)
    if status == "expired" and not auth["has_refresh_token"]:
        return (
            "Your Google sign-in expired and this session has no saved refresh "
            "token, so it couldn't be renewed automatically. Reconnect Google "
            "once — after that, refresh happens silently in the background."
        )
    if status == "expired":
        return (
            "Google refused to renew this sign-in (the access was most likely "
            "revoked, or the account password changed). Reconnect Google to continue."
        )
    if status == "error":
        return _PROXY_MESSAGES["error"][1]
    return _PROXY_MESSAGES.get(status, _PROXY_MESSAGES["error"])[1]


def _render_reconnect_screen(title: str, detail: str) -> None:
    """The single dead-end screen: shown ONLY when automatic refresh has already
    been tried and failed, never as a first response to a 401."""
    _render_global_styles()
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=96)
    st.warning(f"**{title}** — {detail}")
    if _current_user_email():
        st.caption(f"Signed in as {_current_user_email()}")
    col_a, col_b = st.columns([1, 3])
    with col_a:
        if st.button("Reconnect Google", type="primary", key="reconnect_google_screen"):
            _reconnect_google()
    with col_b:
        if st.button("Sign out", key="sign_out_screen"):
            _logout_user()
    _render_auth_debug_panel(expanded=True)
    st.stop()


def _origin_of(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return ""


def _configured_origin() -> str:
    """The ONE origin a login is valid on — derived from `redirect_uri`."""
    try:
        return _origin_of(st.secrets.get("auth", {}).get("redirect_uri", ""))
    except Exception:
        return ""


def _browser_origin() -> str:
    """The origin the user actually has open (empty if unavailable)."""
    try:
        return _origin_of(st.context.url)
    except Exception:
        return ""


def _require_matching_origin() -> None:
    """Refuse to run on an origin where sign-in cannot possibly stick.

    `http://localhost:8501` and `http://127.0.0.1:8501` are DIFFERENT cookie
    origins. On the wrong one, two things break at once: Streamlit rejects the
    login cookie outright ("Origin mismatch"), and Authlib's OAuth `state`
    cookie is not sent back to the callback, so the sign-in fails silently and
    dumps the user back on the login page. The result is an endless
    "log in -> still logged out -> log in again" loop with no error message.

    Detecting it here turns that loop into one clear instruction.
    """
    expected = _configured_origin()
    actual = _browser_origin()
    if not expected or not actual or actual == expected:
        return

    pw_auth.audit("origin_mismatch", expected_origin=expected, actual_origin=actual)
    _render_global_styles()
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=96)
    st.error(
        f"**Wrong address** — this app is open at `{actual}`, but sign-in only "
        f"works at `{expected}`.\n\n"
        "Signing in here cannot work: the browser keeps the login cookie under a "
        "different address, so you would be asked to sign in again every time."
    )
    st.link_button(f"Open {expected}", expected, type="primary")
    st.caption(
        "`localhost` and `127.0.0.1` are treated as different sites by browsers, "
        "even though both point at this same computer. Use the link above (or "
        "update `redirect_uri` in .streamlit/secrets.toml if you meant to change "
        "the address)."
    )
    st.stop()


def _developer_mode() -> bool:
    """Debug panel visibility: DEBUG=true in .env, or ?debug=1 on the URL."""
    if DEBUG:
        return True
    try:
        return str(st.query_params.get("debug", "")).lower() in {"1", "true", "yes"}
    except Exception:
        return False


def _render_auth_debug_panel(*, expanded: bool = False) -> None:
    """Developer-mode auth diagnostics — the same fields as auth_debug.log.

    Deliberately fingerprint-only: it shows that the token CHANGED after a
    refresh without ever displaying a token, so a user can safely screenshot
    this panel when reporting a problem.
    """
    if not _developer_mode():
        return
    status = pw_auth.auth_status(_current_user_email())
    with st.expander("Auth debug", expanded=expanded):
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Auth status", status["state"])
        col_b.metric("Proxy status", status["proxy_status"])
        seconds = status["seconds_until_expiry"]
        col_c.metric("Token expires in", f"{seconds // 60}m {seconds % 60}s" if seconds else "—")
        st.json(
            {
                "authenticated": status["authenticated"],
                "user_email": status["user_email"],
                "expires_at": status["expires_at"],
                "seconds_until_expiry": status["seconds_until_expiry"],
                "needs_refresh": status["needs_refresh"],
                "proxy_status": status["proxy_status"],
                "message": status["message"],
                "auto_refresh_enabled": status["auto_refresh_enabled"],
                "token_source": status["token_source"],
                "token_fingerprint": status["token_fingerprint"],
                "last_refresh_at": status["last_refresh_at"],
                "last_verification_code": status["last_proxy_status_code"],
                "last_401_reason": status["last_401_reason"],
                "refresh_attempted": status["last_proxy_refreshed"],
                "retry_after_401": status["last_proxy_retried"],
                "login_hook_installed": PW_REFRESH_HOOK_OK,
                "proxy_base_url": pw_access.proxy_base_url(),
                "session_days": SESSION_TIMEOUT_DAYS,
                "session_seconds_remaining": _session_seconds_remaining(),
                "configured_origin": _configured_origin(),
                "browser_origin": _browser_origin(),
            }
        )
        st.caption(
            "No token, cookie or secret is shown here or written to "
            "logs/auth_debug.log — only a SHA-256 fingerprint."
        )
        if st.button("Force refresh now", key="debug_force_refresh"):
            ok, message = pw_auth.refresh_now(_current_user_email())
            st.session_state.pop("_allow_cache", None)
            (st.success if ok else st.error)(message)
            st.rerun()


def _active_theme() -> str:
    theme = st.session_state.get("ui_theme", "Dark")
    return "Dark" if theme == "Dark" else "Light"


def _render_theme_picker(key: str) -> None:
    current = _active_theme()
    st.radio(
        "Appearance",
        ["Light", "Dark"],
        index=0 if current == "Light" else 1,
        key=key,
        horizontal=True,
    )
    if st.session_state.get(key) != st.session_state.get("ui_theme"):
        st.session_state.ui_theme = st.session_state[key]
        st.rerun()


def _render_global_styles() -> None:
    is_dark = _active_theme() == "Dark"
    theme_vars = """
            --page: #eef2f8;
            --surface: #ffffff;
            --surface-muted: #f3f6fb;
            --surface-alt: #fafbfe;
            --border: #e4e9f1;
            --border-strong: #cbd5e2;
            --text: #0f1b2e;
            --text-soft: #46566b;
            --text-muted: #74839a;
            --brand: #17408b;
            --brand-strong: #0f2f6b;
            --brand-soft: #eaf1fb;
            --accent: #2f6bd8;
            --accent-strong: #1f56bd;
            --warning-bg: #fff8e6;
            --error-bg: #fdecec;
            --success-bg: #e9f9f1;
            --shadow-sm: 0 1px 2px rgba(16, 32, 58, 0.06);
            --shadow: 0 6px 20px rgba(16, 32, 58, 0.08);
            --shadow-lg: 0 18px 44px rgba(16, 32, 58, 0.12);
            --ring: rgba(47, 107, 216, 0.28);
    """
    if is_dark:
        theme_vars = """
            --page: #0b1220;
            --surface: #111a2b;
            --surface-muted: #16223a;
            --surface-alt: #0f1829;
            --border: #24324a;
            --border-strong: #35486a;
            --text: #eef3fb;
            --text-soft: #c2cddd;
            --text-muted: #8b9bb4;
            --brand: #5b9bff;
            --brand-strong: #8ab8ff;
            --brand-soft: #15233c;
            --accent: #3b82f6;
            --accent-strong: #60a5fa;
            --warning-bg: #3a2f0d;
            --error-bg: #3a1620;
            --success-bg: #0b2e22;
            --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4);
            --shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
            --shadow-lg: 0 20px 50px rgba(0, 0, 0, 0.5);
            --ring: rgba(96, 165, 250, 0.35);
        """

    st.markdown(
        """
        <style>
        :root {
__THEME_VARS__
            --radius: 14px;
            --radius-sm: 10px;
        }
        .stApp {
            background:
                radial-gradient(1100px 460px at 100% -8%, var(--brand-soft) 0%, transparent 55%),
                var(--page);
            color: var(--text);
            font-family: "Inter", "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif;
            -webkit-font-smoothing: antialiased;
        }
        [data-testid="stHeader"] { background: transparent; border: 0; }
        [data-testid="stMainBlockContainer"] {
            max-width: 900px;
            padding-top: 2rem;
            padding-bottom: 4rem;
        }
        [data-testid="stSidebar"] {
            background: var(--surface);
            border-right: 1px solid var(--border);
        }
        [data-testid="stSidebar"] [data-testid="stImage"] { display: flex; justify-content: center; }
        h1, h2, h3, h4, h5, h6,
        p, label, span, small,
        [data-testid="stMarkdownContainer"],
        [data-testid="stMarkdownContainer"] * {
            color: var(--text);
        }
        [data-testid="stCaptionContainer"],
        [data-testid="stCaptionContainer"] * {
            color: var(--text-muted) !important;
        }

        /* Header */
        .app-header {
            position: relative;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            margin-bottom: 1.5rem;
            padding: 1.35rem 1.6rem;
            border: 1px solid var(--border);
            border-radius: var(--radius);
            background: linear-gradient(180deg, var(--surface) 0%, var(--surface-alt) 100%);
            box-shadow: var(--shadow);
            overflow: hidden;
        }
        .app-header::before {
            content: "";
            position: absolute; left: 0; top: 0; bottom: 0; width: 4px;
            background: linear-gradient(180deg, var(--accent), var(--brand));
        }
        .app-brand { display: flex; align-items: center; gap: 0.95rem; min-width: 0; }
        .app-logo {
            width: 46px; height: 46px; object-fit: contain; border-radius: 12px;
            background: #ffffff; border: 1px solid var(--border); padding: 0.3rem;
            box-shadow: var(--shadow-sm);
        }
        .app-title {
            color: var(--text);
            font-size: clamp(1.5rem, 2.6vw, 2rem);
            line-height: 1.12; font-weight: 800; letter-spacing: -0.02em; margin: 0;
        }
        .app-subtitle { color: var(--text-soft); font-size: 0.92rem; margin-top: 0.28rem; }
        .user-box {
            max-width: 16rem; color: var(--text-muted); font-size: 0.72rem;
            text-transform: uppercase; letter-spacing: 0.06em; text-align: right;
            line-height: 1.5; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .user-box b {
            display: block; color: var(--text-soft); font-size: 0.9rem;
            text-transform: none; letter-spacing: 0; font-weight: 700;
        }

        /* Cards / panels */
        [data-testid="stVerticalBlockBorderWrapper"] {
            padding: 1.5rem 1.6rem !important;
            border: 1px solid var(--border) !important;
            border-radius: var(--radius) !important;
            background: var(--surface) !important;
            box-shadow: var(--shadow);
        }
        .panel-title {
            margin: 0 0 0.3rem; color: var(--text);
            font-size: 1.15rem; font-weight: 800; letter-spacing: -0.01em;
        }
        .panel-copy { margin: 0 0 1.1rem; color: var(--text-soft); font-size: 0.92rem; }

        /* Buttons */
        div.stButton > button,
        div.stDownloadButton > button,
        [data-testid="stFileUploader"] button {
            border-radius: var(--radius-sm); font-weight: 650; min-height: 2.7rem;
            transition: transform 0.16s ease, box-shadow 0.16s ease, filter 0.16s ease, background 0.16s ease, border-color 0.16s ease;
            letter-spacing: 0.01em;
        }
        div.stButton > button[kind="primary"],
        div.stDownloadButton > button {
            background: linear-gradient(180deg, var(--accent) 0%, var(--accent-strong) 100%);
            border: 1px solid var(--accent-strong); color: #ffffff;
            box-shadow: 0 6px 16px rgba(31, 86, 189, 0.28);
        }
        div.stButton > button[kind="primary"]:hover,
        div.stDownloadButton > button:hover {
            filter: brightness(1.05); transform: translateY(-1px);
            box-shadow: 0 10px 22px rgba(31, 86, 189, 0.34); color: #ffffff;
        }
        div.stButton > button[kind="primary"]:active,
        div.stDownloadButton > button:active { transform: translateY(0); }
        div.stButton > button:not([kind="primary"]),
        [data-testid="stFileUploader"] button {
            background: var(--surface); border: 1px solid var(--border-strong); color: var(--brand);
        }
        div.stButton > button:not([kind="primary"]):hover {
            border-color: var(--accent); color: var(--accent); background: var(--brand-soft);
        }
        div.stButton > button:focus-visible,
        div.stDownloadButton > button:focus-visible { outline: none; box-shadow: 0 0 0 3px var(--ring); }
        div.stButton > button:disabled,
        div.stButton > button[kind="primary"]:disabled {
            background: var(--surface-muted) !important; border: 1px solid var(--border) !important;
            color: var(--text-muted) !important; opacity: 1 !important; box-shadow: none; transform: none;
        }

        /* File uploader */
        [data-testid="stFileUploader"] { padding: 0; background: transparent; border: 0; }
        [data-testid="stFileUploader"] section {
            min-height: 7rem; border: 1.5px dashed var(--border-strong);
            border-radius: var(--radius-sm); background: var(--surface-muted);
            transition: border-color 0.16s ease, background 0.16s ease;
        }
        [data-testid="stFileUploader"] section:hover { border-color: var(--accent); background: var(--brand-soft); }
        [data-testid="stFileUploader"] section * { color: var(--text) !important; }
        /* Native selected-file chips (Streamlit stFileChip) - theme aware */
        [data-testid="stFileChips"] { margin-top: 0.75rem; display: grid; gap: 0.5rem; }
        [data-testid="stFileChip"] {
            background: var(--surface-alt) !important; border: 1px solid var(--border) !important;
            border-radius: var(--radius-sm) !important; box-shadow: var(--shadow-sm);
            padding: 0.55rem 0.7rem !important;
        }
        [data-testid="stFileChip"] * { color: var(--text) !important; }
        [data-testid="stFileChipName"] { color: var(--text) !important; font-weight: 650; }
        [data-testid="stFileChipDeleteBtn"] svg { color: var(--text-muted) !important; fill: var(--text-muted) !important; }
        [data-testid="stFileChipDeleteBtn"]:hover svg { color: var(--text) !important; fill: var(--text) !important; }

        /* Alerts + expander + status + progress */
        [data-testid="stExpander"] {
            background: var(--surface); border: 1px solid var(--border) !important;
            border-radius: var(--radius-sm); box-shadow: var(--shadow-sm);
        }
        [data-testid="stAlert"] {
            border: 1px solid var(--border); border-left-width: 4px; border-radius: var(--radius-sm);
        }
        [data-testid="stAlert"] * { color: var(--text) !important; }
        div[data-testid="stAlert"][kind="warning"] { background: var(--warning-bg); border-left-color: #d9a625; }
        div[data-testid="stAlert"][kind="error"] { background: var(--error-bg); border-left-color: #d64545; }
        div[data-testid="stAlert"][kind="success"] { background: var(--success-bg); border-left-color: #1f9d63; }
        div[data-testid="stAlert"][kind="info"] { background: var(--brand-soft); border-left-color: var(--accent); }
        [data-testid="stProgress"] div[role="progressbar"] > div,
        [data-testid="stProgress"] > div > div > div > div {
            background: linear-gradient(90deg, var(--accent), var(--brand));
        }

        /* Tabs */
        [data-testid="stTabs"] [data-baseweb="tab-list"] { gap: 0.25rem; border-bottom: 1px solid var(--border); }
        [data-testid="stTabs"] [data-baseweb="tab"] { font-weight: 650; }
        [data-testid="stTabs"] [aria-selected="true"] { color: var(--accent) !important; }

        hr { border-color: var(--border); }

        /* Login page */
        .auth-kicker {
            display: inline-block; margin-bottom: 0.6rem; padding: 0.25rem 0.7rem;
            font-size: 0.72rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
            color: var(--accent); background: var(--brand-soft); border: 1px solid var(--border);
            border-radius: 999px;
        }
        .auth-title { font-size: 1.7rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text); margin: 0.2rem 0 0.5rem; }
        .auth-copy { color: var(--text-soft); font-size: 0.95rem; line-height: 1.6; margin-bottom: 1.25rem; max-width: 30rem; }

        @media (max-width: 720px) {
            .app-header { align-items: flex-start; flex-direction: column; }
            .user-box { max-width: 100%; text-align: left; }
        }
        </style>
        """.replace("__THEME_VARS__", theme_vars),
        unsafe_allow_html=True,
    )
def _render_login_page() -> None:
    _render_global_styles()
    google_configured = _google_auth_configured()
    with st.container():
        st.markdown('<div class="auth-shell">', unsafe_allow_html=True)
        if LOGO_PATH.exists():
            st.image(str(LOGO_PATH), width=120)
        _render_theme_picker("login_theme")
        st.markdown(
            """
            <div class="auth-kicker">Restricted profile login</div>
            <div class="auth-title">Concise Notes Automation</div>
            <div class="auth-copy">
                Sign in with your Google account to generate PW-style Concise Notes.
                Access is limited to approved email addresses from the app allowlist.
            </div>
            """,
            unsafe_allow_html=True,
        )

        if google_configured:
            if st.button("Continue with Google", type="primary", use_container_width=True):
                try:
                    st.login("google")
                except Exception as exc:
                    st.error("Google login failed. Check `.streamlit/secrets.toml` and restart the app.")
                    st.caption(str(exc))
        else:
            st.error(
                "Google OAuth is not configured yet. Replace the placeholder "
                "`client_id` and `client_secret` in `.streamlit/secrets.toml` "
                "with real Google OAuth credentials, then restart the app."
            )

        st.markdown("</div>", unsafe_allow_html=True)


def _mark_admitted(email: str) -> None:
    st.session_state["_admitted_email"] = email


def _is_admitted(email: str) -> bool:
    return bool(email) and st.session_state.get("_admitted_email") == email


def _require_allowed_google_user() -> None:
    """Gate ENTRY to the app.

    Order matters here — it is what stops the "signed in but 401" loop:

      1. bind this run to the signed-in user (worker threads can't read st.user)
      2. refresh the Google token if it is near expiry — silently, in-process,
         with NO page reload, so nothing in st.session_state is lost
      3. only then ask the proxy whether the user is allowed

    A download click is just a rerun, so step 2 also means an hour-old session
    keeps downloading instead of being bounced to a login screen. The user is
    only ever sent to the reconnect screen after an automatic refresh has
    actually been attempted and failed.
    """
    # Before anything else: if the app is open on an origin where the login
    # cookie can't stick, say so instead of letting the user loop on sign-in.
    _require_matching_origin()

    is_logged_in = bool(getattr(st.user, "is_logged_in", False))
    if not is_logged_in:
        _render_login_page()
        st.stop()

    # Snapshot what Streamlit's cookie holds, so the pipeline's worker threads
    # (which have no Streamlit context) can still resolve a token.
    pw_auth.bind_streamlit_session()

    # Sessions created before token exposure/offline consent was enabled can
    # contain a valid Streamlit identity cookie but no Google token at all.
    # Such a cookie cannot be refreshed and calling st.login while retaining it
    # can leave the browser in the same half-signed-in state. Clear that stale
    # cookie first; the next user-clicked "Continue with Google" performs a
    # genuine OAuth consent and captures the refresh token needed thereafter.
    stale_auth = pw_auth.auth_status(_current_user_email())
    if (
        stale_auth["state"] == pw_auth.AuthState.REAUTH_REQUIRED
        and not stale_auth["has_refresh_token"]
        and stale_auth["token_source"] == "none"
    ):
        email = _current_user_email()
        _clear_local_auth_state(email)
        pw_auth.audit("stale_login_cookie_cleared", user_email=email or "-")
        st.logout()
        st.stop()

    if _auth_session_expired():   # 7-day cap, local check
        _logout_user()
        st.stop()

    # Identity: must be a @pw.live Google account (fast, local check).
    email = _current_user_email()
    if not email or not email.endswith("@" + ALLOWED_EMAIL_DOMAIN):
        _render_global_styles()
        st.error(f"Please sign in with your @{ALLOWED_EMAIL_DOMAIN} Google account.")
        if email:
            st.caption(f"Signed in as {email}")
        if st.button("Sign out", use_container_width=False):
            _logout_user()
        st.stop()

    # Refresh BEFORE the proxy call, so the proxy is never handed a dead token.
    refreshed_ok, refresh_message = _ensure_fresh_session()

    # Authorization via the PW proxy "Whitelisted" sheet (single source of truth).
    status = _proxy_access_status()
    if status == "allowed":
        _mark_admitted(email)
        return

    # "expired" survived pw_access's own refresh-and-retry, so one more forced
    # refresh + re-check is the last automatic step before we ask the user.
    if status == "expired" and not st.session_state.get("_auth_refresh_attempted"):
        st.session_state["_auth_refresh_attempted"] = True
        forced_ok, _ = pw_auth.refresh_now(email)
        if forced_ok:
            status = _proxy_access_status(force=True)
            if status == "allowed":
                st.session_state.pop("_auth_refresh_attempted", None)
                _mark_admitted(email)
                return

    # Once admitted this session, DON'T bounce the user on a later transient
    # proxy "error" (network blip) — they must stay able to view and download
    # the files they already generated. A hard "denied" or "expired" still gates.
    if status == "error" and _is_admitted(email):
        return

    _render_global_styles()
    if status == "denied":
        st.session_state.pop("_admitted_email", None)
        st.error(
            "**Permission denied** — this account isn't authorized for this app. "
            f"Ask an admin to add your email to the '{APP_NAME}' column of the "
            "Whitelisted sheet."
        )
        st.caption(f"Signed in as {email}")
        if st.button("Sign out", use_container_width=False):
            _logout_user()
        _render_auth_debug_panel()
        st.stop()

    if status == "expired":
        _render_reconnect_screen("Session expired", _proxy_error_detail("expired"))

    # Proxy unreachable / 5xx — a retry usually fixes it; no re-login needed.
    st.error(f"**{_PROXY_MESSAGES['error'][0]}** — {_PROXY_MESSAGES['error'][1]}")
    if not refreshed_ok and refresh_message:
        st.caption(refresh_message)
    st.caption(f"Signed in as {email}")
    col_a, col_b = st.columns([1, 3])
    with col_a:
        if st.button("Try again", type="primary"):
            st.session_state.pop("_allow_cache", None)
            st.rerun()
    with col_b:
        if st.button("Sign out", use_container_width=False):
            _logout_user()
    _render_auth_debug_panel()
    st.stop()


if "ui_theme" not in st.session_state:
    st.session_state.ui_theme = "Dark"
_require_allowed_google_user()
_render_global_styles()
user_email = _current_user_email()
logo_html = ""
if LOGO_PATH.exists():
    logo_data = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    logo_html = f'<img class="app-brand-logo" src="data:image/png;base64,{logo_data}" alt="PW logo">'

def _model_probe():
    """A health probe bound to the signed-in user's token provider."""
    return mr.make_probe(_token_provider())


def _refresh_model_health(force: bool = True) -> None:
    cfg = mr.load_routing_config()
    try:
        mr.check_models(cfg.all_models(), _model_probe(), cfg, force=force)
    except Exception as exc:  # never let a health check break the page
        st.warning(f"Couldn't check model availability: {exc}")


def _manual_overrides() -> dict:
    """Task->model overrides when 'auto' is off (empty dict when auto/unset)."""
    if st.session_state.get("model_auto", MODEL_ROUTING_MODE != "manual"):
        return {}
    return {
        task: st.session_state.get(f"manual_{task}")
        for task in ("notes", "vision", "qc")
        if st.session_state.get(f"manual_{task}")
    }


def _render_model_panel() -> None:
    """Model Availability status table + Refresh + auto/manual selection."""
    cfg = mr.load_routing_config()
    st.markdown("### Model Availability")
    st.toggle(
        "Use recommended model automatically",
        value=st.session_state.get("model_auto", MODEL_ROUTING_MODE != "manual"),
        key="model_auto",
        help="ON = the app picks the best available model per task. OFF = choose models yourself below.",
    )
    cached = mr.peek_health(cfg.all_models())
    display = [
        {"Model": r["model"], "Status": r["status"], "Latency": r["latency"],
         "Cost": r["cost"], "Used For": r["used_for"]}
        for r in mr.status_rows(cfg, cached)
    ]
    st.table(display)
    age = mr.cache_age_seconds()
    if age is None:
        st.caption("Not checked yet — click Refresh (also runs automatically on first generate).")
    else:
        mins = int(age // 60)
        st.caption(f"Checked {'just now' if mins == 0 else f'{mins} min ago'} · cached {cfg.health_cache_minutes} min.")
    if st.button("Refresh Model Availability", key="refresh_models", use_container_width=True):
        with st.spinner("Checking model availability…"):
            _refresh_model_health(force=True)
        st.rerun()
    if not st.session_state.get("model_auto", MODEL_ROUTING_MODE != "manual"):
        with st.expander("Manual model selection (advanced)"):
            models = cfg.all_models()
            for task, label in (("notes", "Notes model"), ("vision", "Vision model"), ("qc", "QC model")):
                st.selectbox(label, models, key=f"manual_{task}",
                             index=models.index(st.session_state[f"manual_{task}"])
                             if st.session_state.get(f"manual_{task}") in models else 0)


with st.sidebar:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=92)
    _render_theme_picker("sidebar_theme")
    st.markdown("### Profile")
    st.caption(_current_user_email())

    # Auth status, so a user can see at a glance that their session is healthy
    # instead of finding out only when Generate fails.
    _status = pw_auth.auth_status(_current_user_email())
    if _status["state"] == pw_auth.AuthState.OK:
        st.success("Auth status: OK")
    elif _status["state"] == pw_auth.AuthState.NEEDS_REFRESH and _status["auto_refresh_enabled"]:
        st.info("Auth status: refreshing automatically")
    else:
        st.warning("Auth status: reconnect needed")
    # The 7-day session is what the user actually cares about; the underlying
    # ~1h Google token renewing itself is an implementation detail.
    _session_label = _session_remaining_label()
    if _session_label:
        st.caption(
            f"{_session_label}."
            + (" Signing in again isn't needed until then."
               if _status["auto_refresh_enabled"] else "")
        )
    if not _status["auto_refresh_enabled"]:
        st.caption(
            "Automatic refresh is off for this session — reconnect Google once "
            "to enable it and stop the hourly sign-in prompts."
        )
        if st.button("Reconnect Google", key="sidebar_reconnect", use_container_width=True):
            _reconnect_google()

    if st.button("Sign out", key="sidebar_sign_out", use_container_width=True):
        _logout_user()
    if st.button("Test Gemini connection", key="sidebar_test_gemini", use_container_width=True):
        with st.spinner("Testing Gemini via the PW proxy…"):
            try:
                resp = GeminiClient(_token_provider(), model=DEFAULT_MODEL).test_connection()
                st.success(f"Gemini OK — {resp.model} via {resp.provider}.")
            except Exception as exc:
                st.error(f"Gemini test failed: {exc}")
    st.divider()
    # Model routing is backend-controlled. The availability panel (status table,
    # manual override, refresh) is developer-only — hidden from end users, shown
    # with DEBUG=true or ?debug=1 for troubleshooting.
    if _developer_mode():
        _render_model_panel()
    _render_auth_debug_panel()
    st.caption(f"Build {APP_VERSION} · {version.APP_NAME} {version.APP_VERSION}")

# Dynamic identity line (recomputed every rerun, so it reflects the load time).
_now = version.now()
# Release/build date shown next to the version (config build stamp "2026.07.27"
# -> "27-07-2026"); falls back to the raw stamp if it isn't a dotted date.
_bd = str(APP_VERSION).split(".")
_release_date = f"{_bd[2]}-{_bd[1]}-{_bd[0]}" if len(_bd) == 3 else str(APP_VERSION)
st.markdown(
    f"""
    <div class="app-header">
        <div class="app-brand">
            {logo_html.replace("app-brand-logo", "app-logo")}
            <div>
                <div class="app-title">{version.APP_NAME}</div>
                <div class="app-subtitle">Upload a PDF or PowerPoint and download generated notes.</div>
                <div class="app-subtitle">
                    Version: <b>{version.APP_VERSION} ({_release_date})</b> &nbsp;·&nbsp;
                    Date: <b>{version.format_date(_now)}</b> &nbsp;·&nbsp;
                    Time: <b>{version.format_time(_now)}</b>
                </div>
            </div>
        </div>
        <div class="user-box">Signed in as<b>{user_email}</b></div>
    </div>
    """,
    unsafe_allow_html=True,
)
# ---- Fixed configuration --------------------------------------------------
# Subject, language, exam, note type and maths rendering are chosen by the user
# in the upload panel below. Derived from config so the UI, CLI, and prompt
# templates always offer the same lists.
SUBJECT_OPTIONS = {s.capitalize(): s for s in SUBJECTS}
LANGUAGE_OPTIONS = ["English", "Hindi"]
EXAM_OPTIONS = dict(EXAMS)                     # label -> prompt key
NOTE_TYPE_OPTIONS = {"Concise Notes": "summary", "Complete Notes": "complete"}
MATH_RENDER_OPTIONS = dict(MATH_RENDER_MODES)  # label -> omml/unicode/plain
SEND_IMAGES = True
STRICT_FILTER = True
DTP_POLICY = "hide_note_insert_image"
IMAGE_MODE = "smart_crop"
AI_REDRAW = True
IMAGE_MODEL = DEFAULT_IMAGE_MODEL   # honours the IMAGE_MODEL_NAME env var

if "results" not in st.session_state:
    st.session_state.results = []


def _result_to_dict(name: str, result) -> dict:
    notes = ""
    repaired_notes = result.run_dir / "notes_repaired.txt"
    notes_path = repaired_notes if repaired_notes.exists() else result.raw_notes_path
    if notes_path and notes_path.exists():
        notes = notes_path.read_text(encoding="utf-8")
    equation_report = result.run_dir / "equation_quality_report.md"
    return {
        "name": name,
        "run_dir": str(result.run_dir),
        "docx": str(result.docx_path) if result.docx_path else None,
        "pdf": str(result.pdf_path) if result.pdf_path else None,
        "zip": str(result.zip_path) if result.zip_path else None,
        "equation_report": str(equation_report) if equation_report.exists() else None,
        "notes": notes,
        "warnings": list(result.warnings or []),
        "usage_logged": _usage_logged(result),
        "equation_warnings": list((result.metadata or {}).get("equation_warnings") or []),
        "equation_repairs": (result.metadata or {}).get("equation_repairs", 0),
        "equation_issues": (result.metadata or {}).get("equation_issues", 0),
        "tagged_formula_count": (result.metadata or {}).get("tagged_formula_count", 0),
        "run_summary": (result.metadata or {}).get("run_summary") or {},
        "error": None,
    }


@st.cache_data(show_spinner=False, max_entries=64)
def _file_bytes(path: str, size: int, mtime: float) -> bytes:
    """Read a finished output file's bytes ONCE and cache them (keyed on
    path+size+mtime). Streamlit reruns the whole script on every download-button
    click; without this cache each click would re-read every output file from
    disk into memory, which is what made batches of downloads slow and flaky."""
    return Path(path).read_bytes()


def _download_button(col, label: str, path: str | None, mime: str, key: str, missing: str) -> None:
    """One self-contained download button. Bytes are loaded once from the
    persistent run folder (never from a temp dir), so a click just re-serves the
    already-prepared file — it never re-runs generation."""
    with col:
        p = Path(path) if path else None
        if p and p.exists():
            stat = p.stat()
            st.download_button(
                label,
                data=_file_bytes(str(p), stat.st_size, stat.st_mtime),
                file_name=p.name,
                mime=mime,
                key=key,
                use_container_width=True,
            )
        else:
            st.caption(missing)


_MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MIME_PDF = "application/pdf"
_MIME_ZIP = "application/zip"
_MIME_MARKDOWN = "text/markdown"


def _notes_to_markdown(notes: str) -> str:
    # Normalise the same maths the DOCX writer does, so the preview shows real
    # symbols and — crucially — multiplication asterisks are turned into "×"
    # before st.markdown can eat them as emphasis (e.g. "2*v_A*v_B").
    try:
        from src.docx_writer import _normalize_math
    except Exception:
        def _normalize_math(t):
            return t
    out: list[str] = []
    for raw in notes.splitlines():
        line = raw.rstrip()
        if not line.strip():
            out.append("")
            continue
        leading = len(raw) - len(raw.lstrip(" \t"))
        level = min(3, leading // 4)
        content = line.strip()
        # Check DTP notes FIRST (mirroring the DOCX writer): the model sometimes
        # emits them as bullets, and a bullet-shaped DTP note must stay hidden
        # in the preview just like it is in the document.
        if "note to dtp" in content.lower():
            continue
        bullet_m = re.match(r"^(\*|•|·|-|–)\s+(.*)$", content)
        if bullet_m:
            out.append("  " * level + "- " + _normalize_math(bullet_m.group(2).strip()))
        elif content[0].isdigit() and content[1:2] in {".", ")"}:
            out.append("  " * level + _normalize_math(content))
        else:
            out.append(_normalize_math(content))
    return "\n".join(out)


with st.container(border=True):
    st.markdown(
        """
        <div class="panel-title">Upload file</div>
        <div class="panel-copy">Choose PDF, PPTX, or PPT files to generate notes.</div>
        """,
        unsafe_allow_html=True,
    )
    uploaded_files = st.file_uploader(
        "Upload lecture file(s)",
        type=["pdf", "pptx", "ppt"],
        accept_multiple_files=True,
        label_visibility="collapsed",
        key="lecture_files",
    )
    opt_col1, opt_col2 = st.columns(2)
    with opt_col1:
        subject_label = st.selectbox(
            "Subject",
            list(SUBJECT_OPTIONS.keys()),
            index=None,
            placeholder="Select subject…",
            help="Pick the lecture's subject (required).",
        )
    with opt_col2:
        language_label = st.selectbox(
            "Notes language",
            LANGUAGE_OPTIONS,
            index=0,
            help="The language the generated notes should be written in.",
        )
    opt_col3, opt_col4 = st.columns(2)
    with opt_col3:
        exam_label = st.selectbox(
            "Exam",
            list(EXAM_OPTIONS.keys()),
            index=None,
            placeholder="No specific exam",
            help="Picks a JEE/NEET-specific concise-notes prompt. Leave empty to use the subject prompt.",
        )
    with opt_col4:
        note_type_label = st.selectbox(
            "Note type",
            list(NOTE_TYPE_OPTIONS.keys()),
            index=0,
            help="Concise Notes = a short concept summary. Complete Notes = full detail.",
        )

    with st.expander("Equation settings", expanded=False):
        math_label = st.selectbox(
            "Math rendering",
            list(MATH_RENDER_OPTIONS.keys()),
            index=0,
            help=(
                "Native Word Equation / OMML gives true Word equations (vector arrows, hats, "
                "fractions) that survive to PDF. Unicode fallback writes symbols as text. "
                "Plain text debug shows the raw LaTeX tags."
            ),
        )
        strict_math = st.checkbox(
            "Strict equation preservation",
            value=True,
            help=(
                "Forces the AI to wrap every formula in a maths tag, and repairs any formula "
                "that still arrives as plain text (A x B = C, i -> j -> k, H2O) before the "
                "DOCX is written."
            ),
        )

    # Processing mode, image analysis and model routing are controlled entirely
    # from the BACKEND (PROCESSING_MODE env / config defaults) — no end-user
    # widgets. The health check + model routing still run silently at generate
    # time; this just resolves the mode the backend selected.
    PROCESSING_MODE = DEFAULT_PROCESSING_MODE
    AUTO_SELECTED = PROCESSING_MODE == "auto"
    if AUTO_SELECTED:
        _cfg_preview = mr.load_routing_config()
        _preview_health = mr.peek_health(_cfg_preview.all_models())
        _effective_mode = (mr.recommend_mode(_cfg_preview, _preview_health)[0]
                           if _preview_health else mr.DEFAULT_MODE)
    else:
        _effective_mode = PROCESSING_MODE if PROCESSING_MODE in mr.MODE_PROFILES else mr.DEFAULT_MODE
    _profile = mr.MODE_PROFILES[_effective_mode]
    analyze_images = _profile.vision_default_on

    SUBJECT = SUBJECT_OPTIONS.get(subject_label) if subject_label else None
    LANGUAGE = language_label
    EXAM = EXAM_OPTIONS.get(exam_label, "") if exam_label else ""
    MODE = NOTE_TYPE_OPTIONS.get(note_type_label, "summary")
    MATH_RENDER_MODE = MATH_RENDER_OPTIONS.get(math_label, DEFAULT_MATH_RENDER_MODE)
    STRICT_MATH = bool(strict_math)
    if uploaded_files and not SUBJECT:
        st.caption("Select a subject to enable generation.")
    run_button = st.button(
        f"Generate {note_type_label}",
        type="primary",
        disabled=not uploaded_files or not SUBJECT,
        use_container_width=True,
    )

if run_button and uploaded_files:
    # Pass the PROVIDER, not a token string: a batch can easily outlive the ~1h
    # Google token, and the provider refreshes it silently between calls instead
    # of the run dying half-way with a 401.
    google_token = _token_provider()

    # Validate auth ONCE, here, before generation starts — never during the run
    # and never per file write. A mid-run auth check is what used to abandon a
    # half-finished batch.
    refreshed_ok, refresh_message = _ensure_fresh_session()
    access_status = _proxy_access_status()
    if access_status == "expired" and refreshed_ok:
        access_status = _proxy_access_status(force=True)
    authorized = access_status == "allowed"

    if access_status == "denied":
        st.error(
            f"**{_PROXY_MESSAGES['denied'][0]}** — your Google account is not "
            "authorized for this app on the PW proxy. Ask an admin to add your "
            f"email under the '{APP_NAME}' column of the Whitelisted sheet."
        )
    elif access_status == "expired":
        st.error(f"**{_PROXY_MESSAGES['expired'][0]}** — {_proxy_error_detail('expired')}")
        if not refreshed_ok and refresh_message:
            st.caption(refresh_message)
        if st.button("Reconnect Google", type="primary", key="reconnect_generate"):
            _reconnect_google()
    elif access_status == "error":
        # The PW proxy Whitelisted sheet is the ONLY source of truth — there is
        # no local fallback. If the proxy can't be reached, fail closed.
        st.error(f"**{_PROXY_MESSAGES['error'][0]}** — {_PROXY_MESSAGES['error'][1]}")

    if authorized:
        # ---- Model routing: health-check ONCE, then pick one model per task ----
        # (cached for MODEL_HEALTH_CACHE_MINUTES; a stale/empty cache probes here).
        _cfg = mr.load_routing_config()
        decision = None
        run_mode = PROCESSING_MODE
        run_profile = _profile
        auto_reason = None
        notes_fallbacks: list[str] = []
        vision_fallbacks: list[str] = []
        try:
            with st.spinner("Preparing…"):
                _health = mr.check_models(_cfg.all_models(), _model_probe(), _cfg)
            # Auto: pick the concrete speed mode from the freshly-probed health.
            if PROCESSING_MODE == "auto":
                run_mode, auto_reason = mr.recommend_mode(_cfg, _health)
                run_profile = mr.MODE_PROFILES[run_mode]
            decision = mr.resolve(run_mode, _cfg, _health, manual=_manual_overrides())
            # Runtime fallback chains: all health-available models per task, in
            # order. If the chosen (primary) model empties on a stubborn deck,
            # the pipeline retries that chunk on the next model here — so one
            # equation-dense file can't fail the whole run.
            notes_fallbacks = mr.available_chain("notes", run_mode, _cfg, _health)
            vision_fallbacks = mr.available_chain("vision", run_mode, _cfg, _health)
        except mr.RouterError as e:
            st.error(str(e))
        except Exception as e:
            st.error(f"Model routing failed: {e}")

    if authorized and decision is not None:
        # Which model was picked is backend detail — surface it only in developer
        # mode (DEBUG / ?debug=1), never to end users.
        if _developer_mode():
            if auto_reason:
                st.success(f"Auto-selected **{mr.MODE_LABELS[run_mode]}** — {auto_reason}")
            rt_cols = st.columns(3)
            rt_cols[0].metric("Notes model", mr.model_label(decision.notes.model))
            rt_cols[1].metric("Vision model", mr.model_label(decision.vision.model) if decision.vision.model else "—")
            rt_cols[2].metric("Pro model used", "Yes" if decision.pro_used else "No")
            for reason in decision.reasons():
                st.info(reason)

        st.session_state.results = []
        total = len(uploaded_files)
        # Animated status container so it's always clear the task is running
        # (a plain progress bar can look "stuck" during the long AI call).
        with st.status(f"Generating Concise Notes for {total} file(s)…", expanded=True) as status:
            progress = st.progress(0.0, text="Starting…")
            with tempfile.TemporaryDirectory() as tmpdir:
                for i, uploaded in enumerate(uploaded_files, start=1):
                    # No auth check here on purpose. Long batches used to abort
                    # mid-way once the ~1h token died; now `google_token` is a
                    # provider that renews itself between calls, so a batch runs
                    # to completion however long it takes.
                    status.update(label=f"Processing {i} of {total} — {uploaded.name}")
                    progress.progress((i - 1) / total, text=f"[{i}/{total}] {uploaded.name}")
                    st.write(f"⏳ Reading pages and generating notes for **{uploaded.name}**…")
                    tmp_path = Path(tmpdir) / uploaded.name
                    tmp_path.write_bytes(uploaded.getbuffer())
                    try:
                        result = run_pipeline(
                            tmp_path, subject=SUBJECT, language=LANGUAGE, mode=MODE,
                            google_token=google_token, model=decision.notes.model,
                            send_images_to_ai=analyze_images, strict_filter=STRICT_FILTER,
                            allow_mock=False, image_insert_mode=IMAGE_MODE, dtp_note_policy=DTP_POLICY,
                            ai_redraw_diagrams=run_profile.redraw_diagrams, image_model=IMAGE_MODEL,
                            retry_callback=st.write,
                            exam=EXAM, strict_math=STRICT_MATH,
                            math_render_mode=MATH_RENDER_MODE,
                            processing_mode=run_mode,
                            notes_model=decision.notes.model,
                            vision_model=decision.vision.model or decision.notes.model,
                            notes_fallbacks=notes_fallbacks,
                            vision_fallbacks=vision_fallbacks,
                            qc_model=(decision.qc.model if decision.qc else None),
                            qc_level=run_profile.qc_level,
                            routing_summary=decision.summary(),
                        )
                        result_dict = _result_to_dict(uploaded.name, result)
                        st.session_state.results.append(result_dict)
                        st.write(f"✅ Finished **{uploaded.name}**")
                    except GeminiError as e:
                        st.session_state.results.append({"name": uploaded.name, "error": f"Gemini API failed: {e}", "notes": "", "run_dir": None})
                        st.write(f"❌ Failed **{uploaded.name}**")
                    except Exception as e:
                        st.session_state.results.append({"name": uploaded.name, "error": f"Processing failed: {e}", "notes": "", "run_dir": None})
                        st.write(f"❌ Failed **{uploaded.name}**")
                    progress.progress(i / total, text=f"[{i}/{total}] {uploaded.name}")
            ok = sum(1 for r in st.session_state.results if not r.get("error"))
            status.update(
                label=f"Completed {ok} of {total} file(s)",
                state="complete" if ok == total else "error",
                expanded=False,
            )
        ok = sum(1 for r in st.session_state.results if not r.get("error"))
        (st.success if ok == total else st.warning)(f"Completed: {ok}/{total} file(s).")


results = st.session_state.results
if results:
    st.divider()
    for idx, r in enumerate(results):
        with st.expander(r["name"] + ("  ❌" if r.get("error") else ""), expanded=(len(results) == 1 or bool(r.get("error")))):
            if r.get("error"):
                st.error(r["error"])
                continue
            if r.get("usage_logged") is True:
                st.caption("Usage tracked in the PW Usage Cost sheet.")
            elif r.get("usage_logged") is False:
                st.warning("Usage logging failed on the PW proxy — this run was not recorded in the Usage Cost sheet.")
            # ---- Run summary: time + cost transparency ----
            _rs = r.get("run_summary") or {}
            if _rs:
                s1, s2, s3 = st.columns(3)
                s1.metric("Time taken", f"{_rs.get('total_processing_seconds', 0)}s")
                s2.metric("Slides processed", _rs.get("slides_processed", 0))
                s3.metric("Sent to vision", _rs.get("slides_sent_to_vision", 0))
                # Which models ran is backend detail — developer mode only.
                if _developer_mode():
                    st.caption(
                        f"Mode: **{_rs.get('processing_mode','?').title()}** · "
                        f"Notes: **{mr.model_label(_rs.get('notes_model',''))}** · "
                        f"Vision: **{mr.model_label(_rs.get('vision_model',''))}** · "
                        f"QC: **{(mr.model_label(_rs['qc_model']) if _rs.get('qc_model') else 'off')}** · "
                        f"Pro used: **{'Yes' if _rs.get('pro_used') else 'No'}** · "
                        f"Fallback: **{'Yes' if _rs.get('fallback_used') else 'No'}**"
                    )
                st.caption(
                    f"App {_rs.get('app_version','')} · started {_rs.get('started_at','')} · "
                    f"ended {_rs.get('ended_at','')}"
                )
                for reason in _rs.get("routing_reasons") or []:
                    st.caption(f"↳ {reason}")
            # Equation warnings get their own tab, so keep them out of the
            # general warning stack rather than showing each twice.
            _eq_warnings = set(r.get("equation_warnings") or [])
            for warning in r.get("warnings") or []:
                if str(warning) in _eq_warnings:
                    continue
                st.warning(str(warning))
            # Stable per-run keys so the same button identity survives reruns
            # (a download click is a rerun) — prevents Streamlit from re-issuing
            # or duplicating downloads.
            rkey = re.sub(r"[^A-Za-z0-9]+", "_", (r.get("run_dir") or str(idx)).split("/")[-1].split("\\")[-1]) or str(idx)
            c1, c2, c3, c4 = st.columns(4)
            _download_button(c1, "Download Notes DOCX", r.get("docx"), _MIME_DOCX, f"docx_{rkey}", "DOCX was not created.")
            _download_button(c2, "Download Notes PDF", r.get("pdf"), _MIME_PDF, f"pdf_{rkey}",
                             "PDF unavailable. Install Microsoft Word or LibreOffice on this laptop, or use the DOCX.")
            _download_button(c3, "Download Equation Report", r.get("equation_report"), _MIME_MARKDOWN,
                             f"equations_{rkey}", "Equation report was not created.")
            _download_button(c4, "Download Full Run ZIP", r.get("zip"), _MIME_ZIP, f"zip_{rkey}", "Run ZIP was not created.")

            tab_preview, tab_images, tab_equations, tab_debug = st.tabs(
                ["Preview", "Diagrams", "Equations", "Debug"]
            )
            with tab_preview:
                st.markdown(_notes_to_markdown(r["notes"]))
            with tab_images:
                run_dir = Path(r["run_dir"]) if r["run_dir"] else None
                imgs = []
                if run_dir and (run_dir / "ai_diagrams").exists():
                    imgs = sorted((run_dir / "ai_diagrams").glob("*_t.png")) or sorted((run_dir / "ai_diagrams").glob("*.png"))
                if not imgs and run_dir and (run_dir / "inserted_images").exists():
                    imgs = sorted((run_dir / "inserted_images").glob("*.png"))
                if imgs:
                    cols = st.columns(2)
                    for j, img in enumerate(imgs):
                        with cols[j % 2]:
                            st.image(str(img), use_container_width=True)
                else:
                    st.caption("No diagrams inserted for this file.")
            with tab_equations:
                m1, m2, m3 = st.columns(3)
                m1.metric("Formulas rendered", r.get("tagged_formula_count", 0))
                m2.metric("Auto-repaired", r.get("equation_repairs", 0))
                m3.metric("Issues", r.get("equation_issues", 0))
                eq_warnings = r.get("equation_warnings") or []
                if eq_warnings:
                    st.markdown("**Equation Quality Warnings**")
                    for w in eq_warnings:
                        st.warning(str(w))
                else:
                    st.success("No equation issues detected — every formula found is proper maths.")
                rd_eq = Path(r["run_dir"]) if r.get("run_dir") else None
                md_report = rd_eq / "equation_quality_report.md" if rd_eq else None
                if md_report and md_report.exists():
                    with st.expander("Full equation quality report"):
                        st.markdown(md_report.read_text(encoding="utf-8"))
            with tab_debug:
                rd = Path(r["run_dir"]) if r.get("run_dir") else None
                meta = rd / "run_metadata.json" if rd else None
                if meta and meta.exists():
                    import json as _json
                    try:
                        st.json(_json.loads(meta.read_text(encoding="utf-8")))
                    except Exception:
                        st.caption("Could not read run_metadata.json.")
                else:
                    st.caption("No run log found for this file.")
                if rd:
                    st.caption(f"Run folder: {rd}")

# ---- Footer -----------------------------------------------------------------
st.divider()
st.caption(version.footer_text())
