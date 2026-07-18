from __future__ import annotations

import re
import tempfile
import os
import base64
import time
from pathlib import Path

import requests
import streamlit as st

import pw_access
from src.ai_client import GeminiClient, GeminiError
from src.config import APP_NAME, APP_VERSION, DEFAULT_IMAGE_MODEL, DEFAULT_MODEL, SUBJECTS
from src.pipeline import run_pipeline

st.set_page_config(page_title="Concise Notes Automation", layout="wide")

BASE_DIR = Path(os.getenv("HANDWRITTEN_NOTES_ROOT", str(Path(__file__).parent))).expanduser()
LOGO_PATH = BASE_DIR / "assets" / "pw_logo.png"
SESSION_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
ALLOWED_EMAIL_DOMAIN = "pw.live"


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


def _google_proxy_token() -> str:
    """The Google token passed to the PW proxy. The proxy verifies a Google
    **id_token** (a JWT with the user's email), so prefer that and fall back to
    the access token. Requires `expose_tokens = ["access", "id"]` in secrets."""
    try:
        tokens = st.user.get("tokens", {})
    except Exception:
        tokens = {}

    def _get(key: str) -> str:
        try:
            return str(tokens.get(key, "") or "").strip()
        except Exception:
            return str(getattr(tokens, key, "") or "").strip()

    return _get("id") or _get("access")


ALLOWLIST_CACHE_TTL_SECONDS = 300  # re-check the proxy whitelist at most every 5 min


def _proxy_access_status(force: bool = False) -> str:
    """Return the PW proxy's authorization status ("allowed"/"denied"/"error")
    for the signed-in user, caching an "allowed" result in the session for
    ALLOWLIST_CACHE_TTL_SECONDS so we don't call the proxy on every Streamlit
    rerun. Non-allowed results are never cached, so a newly-whitelisted user is
    admitted on their next interaction. Does NOT touch the AI/usage pipeline."""
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
    status = pw_access.check_allowed_status(_google_proxy_token())
    st.session_state["_allow_cache"] = {"email": email, "status": status, "ts": now}
    return status


def _usage_logged(result) -> bool:
    """Whether the PW proxy recorded this run in the Usage Cost sheet."""
    return bool((result.metadata or {}).get("usage_logged"))


def _logout_user() -> None:
    if bool(getattr(st.user, "is_logged_in", False)):
        st.logout()
    st.rerun()


def _auth_session_expired() -> bool:
    """Expire signed-in users 7 days after the identity token was issued."""
    try:
        issued_at = int(st.user.get("iat", 0) or 0)
    except (TypeError, ValueError):
        return False

    return issued_at > 0 and time.time() - issued_at >= SESSION_TIMEOUT_SECONDS


def _google_token_expired() -> bool:
    """True once the signed-in user's Google token has expired.

    Streamlit keeps the login cookie for the full 7-day session but does NOT
    refresh the underlying Google id_token, which lives ~1 hour. Past its `exp`
    the PW proxy rejects it with 401 ("invalid or expired token"), so we must
    re-authenticate rather than keep sending a dead token."""
    try:
        exp = int(st.user.get("exp", 0) or 0)
    except (TypeError, ValueError):
        return False
    # 60s skew so we re-auth just before the proxy would start rejecting.
    return exp > 0 and time.time() >= (exp - 60)


def _proxy_error_detail(token: str) -> str:
    """One extra allowlist call (only on the error screen) to surface WHY the
    proxy couldn't verify access, so a stuck user can report/understand it.
    Cached for 20s in session state so reruns of the error screen (and multiple
    render sites in one run) don't stack up extra ~15s network calls."""
    cached = st.session_state.get("_proxy_err_detail")
    if isinstance(cached, dict) and (time.time() - float(cached.get("ts", 0))) < 20:
        return str(cached.get("detail", ""))
    detail = _proxy_error_detail_uncached(token)
    st.session_state["_proxy_err_detail"] = {"detail": detail, "ts": time.time()}
    return detail


def _proxy_error_detail_uncached(token: str) -> str:
    if not token:
        return "No Google token in your session — please sign in again."
    try:
        r = requests.post(
            f"{pw_access.proxy_base_url()}/api/allowlist",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"app": APP_NAME},
            timeout=15,
        )
        if r.status_code == 401:
            return "Proxy said 401 — your Google sign-in has expired. Please sign in again."
        if r.status_code >= 500:
            return f"Proxy is having trouble (HTTP {r.status_code}). Try again shortly."
        return f"Proxy responded HTTP {r.status_code}."
    except Exception as e:
        return f"Couldn't reach the proxy ({type(e).__name__}). Check your internet and retry."


def _reauth_screen(message: str) -> None:
    """Show a clean 'sign in again' screen (used when the Google token expired)."""
    _render_global_styles()
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=96)
    st.warning(message)
    st.caption(f"Signed in as {_current_user_email()}")
    if st.button("Sign in again", type="primary"):
        try:
            st.login("google")          # user click -> re-auth, fetches a fresh token
        except Exception:
            _logout_user()
    st.stop()


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
    """Gate ENTRY to the app. Deliberately does NOT re-validate the short-lived
    Google token on every rerun — otherwise a download-button click (which is a
    rerun) would bounce an admitted user to a re-auth screen once their ~1h token
    expired, interrupting the download and looking like a logout. Token freshness
    is enforced only where it's actually needed: at generation time.
    """
    is_logged_in = bool(getattr(st.user, "is_logged_in", False))
    if not is_logged_in:
        _render_login_page()
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

    # Authorization via the PW proxy "Whitelisted" sheet (single source of truth).
    status = _proxy_access_status()
    if status == "allowed":
        _mark_admitted(email)
        return
    # Once admitted this session, DON'T bounce the user on a later transient
    # "error" (usually just their 1h token expiring) — they must stay able to
    # view and download the files they already generated. Only a hard "denied"
    # (removed from the sheet) or a first-time failure blocks entry.
    if status == "error" and _is_admitted(email):
        return

    _render_global_styles()
    if status == "denied":
        st.session_state.pop("_admitted_email", None)
        st.error(
            "This account isn't authorized for this app. Ask an admin to add "
            f"your email to the '{APP_NAME}' column of the Whitelisted sheet."
        )
    else:  # first-time "error" - proxy unreachable / token problem
        detail = _proxy_error_detail(_google_proxy_token())
        st.error("Couldn't verify your access with the PW proxy.")
        st.caption(detail)
        if "sign in again" in detail.lower():
            if st.button("Sign in again", type="primary"):
                try:
                    st.login("google")
                except Exception:
                    _logout_user()
    st.caption(f"Signed in as {email}")
    if st.button("Sign out", use_container_width=False):
        _logout_user()
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

with st.sidebar:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=92)
    _render_theme_picker("sidebar_theme")
    st.markdown("### Profile")
    st.caption(_current_user_email())
    if st.button("Sign out", key="sidebar_sign_out", use_container_width=True):
        _logout_user()
    if st.button("Test Gemini connection", key="sidebar_test_gemini", use_container_width=True):
        with st.spinner("Testing Gemini via the PW proxy…"):
            try:
                resp = GeminiClient(_google_proxy_token(), model=DEFAULT_MODEL).test_connection()
                st.success(f"Gemini OK — {resp.model} via {resp.provider}.")
            except Exception as exc:
                st.error(f"Gemini test failed: {exc}")
    st.caption(f"Build {APP_VERSION}")

st.markdown(
    f"""
    <div class="app-header">
        <div class="app-brand">
            {logo_html.replace("app-brand-logo", "app-logo")}
            <div>
                <div class="app-title">Concise Notes Automation</div>
                <div class="app-subtitle">Upload a PDF or PowerPoint and download generated notes.</div>
            </div>
        </div>
        <div class="user-box">Signed in as<b>{user_email}</b></div>
    </div>
    """,
    unsafe_allow_html=True,
)
# ---- Fixed configuration --------------------------------------------------
MODE = "summary"
# Subject + notes language are chosen by the user in the upload panel below.
# Derived from config.SUBJECTS so the UI, CLI, and prompt templates always
# offer the same subject list.
SUBJECT_OPTIONS = {s.capitalize(): s for s in SUBJECTS}
LANGUAGE_OPTIONS = ["English", "Hindi"]
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
    if result.raw_notes_path and result.raw_notes_path.exists():
        notes = result.raw_notes_path.read_text(encoding="utf-8")
    return {
        "name": name,
        "run_dir": str(result.run_dir),
        "docx": str(result.docx_path) if result.docx_path else None,
        "pdf": str(result.pdf_path) if result.pdf_path else None,
        "zip": str(result.zip_path) if result.zip_path else None,
        "notes": notes,
        "warnings": list(result.warnings or []),
        "usage_logged": _usage_logged(result),
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
    SUBJECT = SUBJECT_OPTIONS.get(subject_label) if subject_label else None
    LANGUAGE = language_label
    if uploaded_files and not SUBJECT:
        st.caption("Select a subject to enable generation.")
    run_button = st.button(
        "Generate Concise Notes",
        type="primary",
        disabled=not uploaded_files or not SUBJECT,
        use_container_width=True,
    )

if run_button and uploaded_files:
    google_token = _google_proxy_token()
    # (Token freshness was already gated at the top of this script run; the
    # per-file check inside the batch loop below covers expiry DURING a batch.)
    # Re-check authorization at generate time as defense-in-depth (login already
    # gated on this). The PW proxy Whitelisted sheet is the single source of
    # truth; uses the cached result to avoid an extra proxy round-trip.
    access_status = _proxy_access_status()
    authorized = access_status == "allowed"
    if access_status == "denied":
        st.error(
            "Your Google account is not authorized for this app on the PW proxy. "
            "Ask an admin to add your email under the "
            f"'{APP_NAME}' column of the Whitelisted sheet."
        )
    elif access_status == "error":
        # The PW proxy Whitelisted sheet is the ONLY source of truth — there is
        # no local fallback. If the proxy can't be reached, fail closed.
        st.error("Couldn't verify your access with the PW proxy.")
        st.caption(_proxy_error_detail(google_token))

    if authorized:
        st.session_state.results = []
        total = len(uploaded_files)
        # Animated status container so it's always clear the task is running
        # (a plain progress bar can look "stuck" during the long AI call).
        with st.status(f"Generating Concise Notes for {total} file(s)…", expanded=True) as status:
            progress = st.progress(0.0, text="Starting…")
            with tempfile.TemporaryDirectory() as tmpdir:
                for i, uploaded in enumerate(uploaded_files, start=1):
                    # Long batches can outlive the ~1h Google token. Stop cleanly
                    # with the finished files intact instead of a cryptic 401.
                    if _google_token_expired():
                        st.warning(
                            "Your Google sign-in expired during this batch. "
                            f"{i - 1} of {total} file(s) finished. Sign in again "
                            "and generate the remaining files."
                        )
                        break
                    status.update(label=f"Processing {i} of {total} — {uploaded.name}")
                    progress.progress((i - 1) / total, text=f"[{i}/{total}] {uploaded.name}")
                    st.write(f"⏳ Reading pages and generating notes for **{uploaded.name}**…")
                    tmp_path = Path(tmpdir) / uploaded.name
                    tmp_path.write_bytes(uploaded.getbuffer())
                    try:
                        result = run_pipeline(
                            tmp_path, subject=SUBJECT, language=LANGUAGE, mode=MODE,
                            google_token=google_token, model=DEFAULT_MODEL,
                            send_images_to_ai=SEND_IMAGES, strict_filter=STRICT_FILTER,
                            allow_mock=False, image_insert_mode=IMAGE_MODE, dtp_note_policy=DTP_POLICY,
                            ai_redraw_diagrams=AI_REDRAW, image_model=IMAGE_MODEL,
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
            for warning in r.get("warnings") or []:
                st.warning(str(warning))
            # Stable per-run keys so the same button identity survives reruns
            # (a download click is a rerun) — prevents Streamlit from re-issuing
            # or duplicating downloads.
            rkey = re.sub(r"[^A-Za-z0-9]+", "_", (r.get("run_dir") or str(idx)).split("/")[-1].split("\\")[-1]) or str(idx)
            c1, c2, c3 = st.columns(3)
            _download_button(c1, "Download DOCX", r.get("docx"), _MIME_DOCX, f"docx_{rkey}", "DOCX was not created.")
            _download_button(c2, "Download PDF", r.get("pdf"), _MIME_PDF, f"pdf_{rkey}",
                             "PDF unavailable. Install Microsoft Word or LibreOffice on this laptop, or use the DOCX.")
            _download_button(c3, "Download ZIP", r.get("zip"), _MIME_ZIP, f"zip_{rkey}", "Run ZIP was not created.")

            tab_preview, tab_images, tab_debug = st.tabs(["Preview", "Diagrams", "Debug"])
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
