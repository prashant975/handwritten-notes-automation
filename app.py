from __future__ import annotations

import re
import tempfile
import os
import base64
import html
import time
from pathlib import Path

import streamlit as st

import pw_access
from src.ai_client import GeminiError
from src.config import APP_NAME, DEFAULT_MODEL
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


def _usage_tracking_label(result) -> str:
    """Usage is now logged by the PW proxy (one Gemini row per file). Reflect
    whether that write succeeded so the UI can warn on failure."""
    if (result.metadata or {}).get("usage_logged"):
        return "PW proxy (one Gemini row in the Usage Cost sheet)"
    return "PW proxy logging failed — usage was not recorded"


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

        /* Selected files */
        .selected-files { display: grid; gap: 0.55rem; margin: 0.9rem 0 0.4rem; }
        .selected-file {
            display: flex; align-items: center; gap: 0.85rem; padding: 0.8rem 0.9rem;
            border: 1px solid var(--border); border-radius: var(--radius-sm);
            background: var(--surface-alt); box-shadow: var(--shadow-sm);
        }
        .selected-file-icon {
            display: grid; place-items: center; width: 2.6rem; height: 2.6rem; border-radius: 10px;
            background: linear-gradient(180deg, var(--accent), var(--brand)); color: #ffffff;
            font-weight: 800; font-size: 0.7rem; letter-spacing: 0.03em; flex: 0 0 auto;
        }
        .selected-file-name { color: var(--text); font-weight: 700; overflow-wrap: anywhere; }
        .selected-file-size { margin-top: 0.15rem; color: var(--text-muted); font-size: 0.8rem; }

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


def _require_allowed_google_user() -> None:
    is_logged_in = bool(getattr(st.user, "is_logged_in", False))
    if not is_logged_in:
        _render_login_page()
        st.stop()

    if _auth_session_expired():
        _logout_user()
        st.stop()

    # Step 1 - identity: must be a @pw.live Google account (fast, friendly check).
    email = _current_user_email()
    if not email or not email.endswith("@" + ALLOWED_EMAIL_DOMAIN):
        _render_global_styles()
        st.error(f"Please sign in with your @{ALLOWED_EMAIL_DOMAIN} Google account.")
        if email:
            st.caption(f"Signed in as {email}")
        if st.button("Sign out", use_container_width=False):
            _logout_user()
        st.stop()

    # Step 2 - authorization: the PW proxy's "Whitelisted" sheet is the single
    # source of truth. Only users listed under this app's column may log in;
    # everyone else is stopped here, before they can see the app. Fails closed
    # if the proxy is unreachable. (Result is cached for a few minutes so this
    # doesn't call the proxy on every rerun.)
    status = _proxy_access_status()
    if status != "allowed":
        _render_global_styles()
        if status == "denied":
            st.error(
                "This account isn't authorized for this app. Ask an admin to add "
                f"your email to the '{APP_NAME}' column of the Whitelisted sheet."
            )
        else:  # "error" - proxy unreachable / token problem
            st.error(
                "Couldn't verify your access with the PW proxy (it may be "
                "temporarily unreachable, or you need to sign in again). Please "
                "try again in a moment."
            )
        st.caption(f"Signed in as {email}")
        if st.button("Sign out", use_container_width=False):
            _logout_user()
        st.stop()


if "ui_theme" not in st.session_state:
    st.session_state.ui_theme = "Dark"
_require_allowed_google_user()
_render_global_styles()
user_email = _current_user_email()
user_initial = (user_email[:1] or "U").upper()
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
SUBJECT_OPTIONS = {
    "Auto-detect": "auto",
    "Biology": "biology",
    "Physics": "physics",
    "Chemistry": "chemistry",
}
LANGUAGE_OPTIONS = ["English", "Hindi"]
SEND_IMAGES = True
STRICT_FILTER = True
DTP_POLICY = "hide_note_insert_image"
IMAGE_MODE = "smart_crop"
AI_REDRAW = True
IMAGE_MODEL = "gemini-2.5-flash-image"

if "results" not in st.session_state:
    st.session_state.results = []
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0


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
        "tracking": None,
        "error": None,
    }


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
        bullet_m = re.match(r"^(\*|•|·|-|–)\s+(.*)$", content)
        if bullet_m:
            out.append("  " * level + "- " + _normalize_math(bullet_m.group(2).strip()))
        elif content[0].isdigit() and content[1:2] in {".", ")"}:
            out.append("  " * level + _normalize_math(content))
        elif "note to dtp" in content.lower():
            continue  # hidden in output, so hide in preview too
        else:
            out.append(_normalize_math(content))
    return "\n".join(out)


def _format_file_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{size} B"


def _render_selected_files(files) -> None:
    if not files:
        return
    rows = []
    for uploaded in files:
        name = html.escape(uploaded.name)
        size = html.escape(_format_file_size(uploaded.size))
        suffix = html.escape(Path(uploaded.name).suffix.replace(".", "").upper() or "FILE")
        rows.append(
            f"""
            <div class="selected-file">
                <div class="selected-file-icon">{suffix[:4]}</div>
                <div>
                    <div class="selected-file-name">{name}</div>
                    <div class="selected-file-size">{size}</div>
                </div>
            </div>
            """
        )
    st.markdown('<div class="selected-files">' + "".join(rows) + "</div>", unsafe_allow_html=True)


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
        key=f"lecture_files_{st.session_state.uploader_key}",
    )
    opt_col1, opt_col2 = st.columns(2)
    with opt_col1:
        subject_label = st.selectbox(
            "Subject",
            list(SUBJECT_OPTIONS.keys()),
            index=0,
            help="Pick the subject, or let the app detect it from the file.",
        )
    with opt_col2:
        language_label = st.selectbox(
            "Notes language",
            LANGUAGE_OPTIONS,
            index=0,
            help="The language the generated notes should be written in.",
        )
    SUBJECT = SUBJECT_OPTIONS[subject_label]
    LANGUAGE = language_label
    run_button = st.button("Generate Concise Notes", type="primary", disabled=not uploaded_files, use_container_width=True)

if run_button and uploaded_files:
    google_token = _google_proxy_token()
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
        st.error(
            "Couldn't verify your access with the PW proxy (it may be temporarily "
            "unreachable, or you need to sign in again). Please retry in a moment."
        )

    if authorized:
        st.session_state.results = []
        total = len(uploaded_files)
        # Animated status container so it's always clear the task is running
        # (a plain progress bar can look "stuck" during the long AI call).
        with st.status(f"Generating Concise Notes for {total} file(s)…", expanded=True) as status:
            progress = st.progress(0.0, text="Starting…")
            with tempfile.TemporaryDirectory() as tmpdir:
                for i, uploaded in enumerate(uploaded_files, start=1):
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
                        result_dict["tracking"] = _usage_tracking_label(result)
                        st.session_state.results.append(result_dict)
                        st.write(f"✅ Finished **{uploaded.name}**")
                    except GeminiError as e:
                        st.session_state.results.append({"name": uploaded.name, "error": f"Gemini API failed: {e}", "notes": "", "run_dir": None, "tracking": None})
                        st.write(f"❌ Failed **{uploaded.name}**")
                    except Exception as e:
                        st.session_state.results.append({"name": uploaded.name, "error": f"Processing failed: {e}", "notes": "", "run_dir": None, "tracking": None})
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
            if r.get("tracking"):
                tracking = str(r["tracking"])
                if "not updated" in tracking.lower() or "failed" in tracking.lower():
                    st.warning(f"Usage logging failed on the PW proxy: {tracking}")
                else:
                    st.caption(f"Usage tracked via {tracking}.")
            for warning in r.get("warnings") or []:
                st.warning(str(warning))
            c1, c2, c3 = st.columns(3)
            with c1:
                if r["docx"] and Path(r["docx"]).exists():
                    st.download_button("Download DOCX", Path(r["docx"]).read_bytes(), file_name=Path(r["docx"]).name, key=f"docx_{idx}", use_container_width=True)
                else:
                    st.caption("DOCX was not created.")
            with c2:
                if r["pdf"] and Path(r["pdf"]).exists():
                    st.download_button("Download PDF", Path(r["pdf"]).read_bytes(), file_name=Path(r["pdf"]).name, key=f"pdf_{idx}", use_container_width=True)
                else:
                    st.caption("PDF unavailable. Install Microsoft Word or LibreOffice on this laptop, or use the DOCX.")
            with c3:
                if r["zip"] and Path(r["zip"]).exists():
                    st.download_button("Download ZIP", Path(r["zip"]).read_bytes(), file_name=Path(r["zip"]).name, key=f"zip_{idx}", use_container_width=True)
                else:
                    st.caption("Run ZIP was not created.")

            tab_preview, tab_images = st.tabs(["Preview", "Diagrams"])
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
