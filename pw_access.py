"""
pw_access.py - PW App Kit v2 client for Python backends/local apps.

Copy this file into an app, set APP_NAME, and route all paid provider calls
through the shared PW proxy. The app ships no provider keys. The proxy verifies
the signed-in PW user, checks the app whitelist, makes the provider call with
proxy-held keys, and writes trusted raw usage logs to MongoDB.

Raw logging model:
  - One successful proxy/provider call = one raw row in MongoDB/sheet export
    (`Raw Usage Ledger Export`).
  - For multi-call tasks, create one task_id and pass it to every helper call.
  - Combine later in Sheets/AppScript by task_id + app + email + model.
  - Do not use client-side combined logging for normal AI calls.

Everywhere a google_token is accepted you may pass either:
  - the token string from login, or
  - a zero-arg function returning a fresh Google token.

The kit automatically exchanges the Google token for a proxy-issued 7-day
session pass, so long runs do not fail just because Google's token expires.

--------------------------------------------------------------------------
KIT SYNC NOTE — read before dropping in a newer pw-app-kit.zip
--------------------------------------------------------------------------
This file is the kit v2 template PLUS a small set of app-specific additions,
each marked "APP ADDITION" below. Re-apply them after any kit sync:

  1. APP_NAME set to "Handwritten Notes Automation".
  2. PWAccessError carries `.status_code` (src/ai_client.py retries 429/5xx).
  3. set_token_provider() + `force=` aware _resolve_token() (dead-token retry).
  4. proxy_base_url() resolved at CALL time (src/config's load_dotenv runs
     after this module is imported in some entry points).
  5. _invalidate_session() so the model health probe can drop a bad pass.
  6. check_allowed_status() returns "expired" for 401, distinct from "error".
  7. gemini_generate(timeout=...) per-call override for the health probe.
  8. Proxy error bodies truncated at 1200 chars, not 300 (300 cut Vertex's
     actual quota reason out of the message).
  9. A lock around session-pass minting — the pipeline fans calls across a
     ThreadPoolExecutor, so a cold start would otherwise mint 8 passes at once.

tests/test_pw_kit_contract.py asserts every one of these, so a future sync is a
safe diff rather than a gamble.
"""

import os
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

import requests


# ---------------------------------------------------------------------------
# Per-app config. APP_NAME must exactly match a header in Whitelisted row 1.
# ---------------------------------------------------------------------------
APP_NAME = "Handwritten Notes Automation"   # APP ADDITION (1)

PROXY_BASE_URL = os.environ.get(
    "PW_PROXY_BASE_URL", "https://pw-apps-proxy.vercel.app"
).rstrip("/")

_TIMEOUT = 30
_AI_TIMEOUT = 300
_LARGE_REQUEST_BYTES = 3_500_000
_ERROR_BODY_CHARS = 1200   # APP ADDITION (8)


class PWAccessError(Exception):
    """Raised when a paid proxy/provider call fails.

    APP ADDITION (2): carries the HTTP status so callers can distinguish a 429
    (retryable rate-limit) from a hard failure. src/ai_client.py reads
    `.status_code` to decide whether to back off and retry.
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def new_task_id(prefix: str = "task") -> str:
    """Create a stable id for one user task/run.

    Pass the SAME id to every provider call a task makes; the proxy stamps it on
    each raw row so reporting can combine by Task ID + App + Email + Model.
    """
    safe = "".join(ch for ch in str(prefix or "task") if ch.isalnum() or ch in "-_")
    return f"{safe or 'task'}-{uuid.uuid4().hex}"


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# APP ADDITION (3): a process-wide fallback token provider, used only when a
# caller passed a bare token STRING that the proxy rejected.
_token_provider: Optional[Callable[..., str]] = None


def set_token_provider(provider: Optional[Callable[..., str]]) -> None:
    """Register a process-wide fallback token provider.

    Only used when a caller passes a bare token STRING that the proxy then
    rejects. Prefer passing the callable itself as `google_token` — that is
    per-request and therefore correct for multi-user deployments too."""
    global _token_provider
    _token_provider = provider


def _resolve_token(google_token, *, force: bool = False) -> str:
    """Turn a token-or-provider into a token string.

    `google_token` may be a plain string OR a callable (a "token provider")
    returning a currently-valid Google token. APP ADDITION (3): `force=True`
    asks the provider to bypass its cache and mint a new token — used only for
    the post-401 retry. Providers that take no arguments are supported, and a
    raising provider yields "" (fail closed) rather than propagating."""
    if callable(google_token):
        try:
            return str(google_token(force=force) or "").strip()
        except TypeError:            # provider takes no arguments
            try:
                return str(google_token() or "").strip()
            except Exception:
                return ""
        except Exception:
            return ""
    if force and _token_provider is not None:
        try:
            return str(_token_provider(force=True) or "").strip()
        except TypeError:
            try:
                return str(_token_provider() or "").strip()
            except Exception:
                pass
        except Exception:
            pass
    return str(google_token or "").strip()


# The proxy-issued 7-day session pass, cached per process. All calls ride on
# the pass, so Google's ~1-hour token expiry cannot interrupt a run.
_session = {"token": "", "expiry": 0.0}
# APP ADDITION (9): the pipeline fans chunk calls across a ThreadPoolExecutor
# (GEMINI_MAX_CONCURRENCY=6, Mathpix max_workers=4). Without this lock a cold
# start has every thread mint its own pass simultaneously.
_session_lock = threading.Lock()


# APP ADDITION (4) + (5) — kept across kit upgrades. src/model_router.py and
# app.py depend on both.
def proxy_base_url() -> str:
    """Resolve the proxy base URL at CALL time, so a PW_PROXY_BASE_URL set by
    a late load_dotenv (src.config imports after pw_access in some entrypoints)
    is still honoured instead of being silently ignored."""
    return os.environ.get(
        "PW_PROXY_BASE_URL", "https://pw-apps-proxy.vercel.app"
    ).rstrip("/")


def _invalidate_session() -> None:
    """Drop the cached session pass so the next call mints a fresh one.

    The model health probe calls this when a concurrent cold start leaves a
    half-minted pass — without it, one bad pass marks every model unavailable
    until the cache TTL expires."""
    with _session_lock:
        _session.update({"token": "", "expiry": 0.0})


def _session_is_fresh() -> bool:
    import time
    return bool(_session["token"]) and time.time() < _session["expiry"] - 60


def _auth_token(google_token, force_new: bool = False) -> str:
    """Return a cached 7-day proxy session pass, or mint one from Google.

    If the proxy doesn't offer sessions (older deploy) or the exchange fails,
    gracefully fall back to sending the Google token — every endpoint accepts
    both."""
    seen = _session["token"]
    if not force_new and _session_is_fresh():
        return seen

    with _session_lock:                      # APP ADDITION (9)
        # Another thread may have minted while we waited on the lock. Reuse its
        # pass — but on a forced mint only if it is genuinely NOT the pass we
        # already tried, or we would just replay a rejected credential.
        if _session_is_fresh() and (not force_new or _session["token"] != seen):
            return _session["token"]
        # APP ADDITION (3): on a forced mint, ask the token provider to bypass
        # ITS cache too — otherwise a dead Google token is replayed and the
        # mint fails again.
        g = _resolve_token(google_token, force=force_new)
        try:
            r = requests.post(
                f"{proxy_base_url()}/api/session",
                headers=_headers(g),
                json={},
                timeout=_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                token = data.get("session_token") or ""
                if token:
                    _session["token"] = token
                    _session["expiry"] = float(data.get("expires_at_ms") or 0) / 1000.0
                    return token
        except Exception:
            pass  # network blip — fall back to the Google token for this call
        return g


def _post(path: str, google_token, payload: Dict[str, Any], timeout: int):
    """POST to the proxy. On 401, mint a fresh proxy session pass once."""
    sent = _auth_token(google_token)
    r = requests.post(
        f"{proxy_base_url()}{path}",
        headers=_headers(sent),
        json=payload,
        timeout=timeout,
    )
    if r.status_code == 401:
        # APP ADDITION (3): mint a genuinely fresh credential and retry ONCE —
        # but only if it actually differs. Replaying the same rejected token can
        # only buy a second 401, so skip the pointless round-trip.
        fresh = _auth_token(google_token, force_new=True)
        if fresh and fresh != sent:
            r = requests.post(
                f"{proxy_base_url()}{path}",
                headers=_headers(fresh),
                json=payload,
                timeout=timeout,
            )
    return r


def check_allowed(google_token, app: str = APP_NAME) -> bool:
    """Call before every paid/main action. Fail closed."""
    return check_allowed_status(google_token, app) == "allowed"


def check_allowed_status(google_token, app: str = APP_NAME) -> str:
    """Return one of: allowed, denied, expired, error.

    APP ADDITION (6): the stock kit collapses 401 into "error"; this app's auth
    layer needs "expired" to distinguish a dead sign-in (prompt re-login) from
    an unreachable proxy (retry). Only "allowed" grants access — every other
    value fails closed.
    """
    token = _resolve_token(google_token)
    if not token:
        return "expired"
    try:
        r = _post("/api/allowlist", google_token, {"app": app}, _TIMEOUT)
        if r is None:
            return "error"
        if r.status_code == 200:
            try:
                return "allowed" if bool(r.json().get("allowed")) else "denied"
            except Exception:
                return "error"
        if r.status_code == 403:
            return "denied"
        if r.status_code == 401:
            # Survived the mint-and-retry inside _post, so the sign-in is dead.
            return "expired"
        return "error"
    except Exception:
        return "error"


def log_usage(
    google_token,
    *,
    filename: str,
    input_unit: str,
    count: Any,
    items: List[Dict[str, Any]],
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
) -> Optional[dict]:
    """Legacy/manual audit endpoint.

    Do not use this for normal AI calls. The provider helpers below already
    create trusted raw logs inside the proxy. This function exists only for
    unusual/manual audit cases and never writes trusted cost rows to the raw
    export sheet.
    """
    try:
        r = _post(
            "/api/usage-log",
            google_token,
            {
                "app": app,
                "filename": filename,
                "task_id": task_id,
                "input_unit": input_unit,
                "count": count,
                "items": items,
                "video_duration": video_duration,
            },
            _TIMEOUT,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


class UsageSession:
    """Backward-compatible task context.

    Old kit versions used UsageSession to suppress per-call logging and write a
    combined manual row on flush(). In v2, the proxy logs every raw provider
    call. This class now only provides one shared task_id plus optional default
    metadata for old code that already passes session=.

    New code in this app does NOT use it — it creates a task_id with
    new_task_id() and passes task_id= to every helper instead.
    """

    def __init__(
        self,
        google_token,
        *,
        filename: str = "",
        input_unit: str = "",
        count: Any = None,
        video_duration: str = "",
        app: str = APP_NAME,
        task_id: str = "",
    ):
        self.token = google_token
        self.task_id = task_id or new_task_id()
        self.filename = filename
        self.input_unit = input_unit
        self.count = count
        self.video_duration = video_duration
        self.app = app

    def add(self, model="", tokens_in=0, tokens_out=0, cost_inr=None):
        return None

    def flush(self):
        """Compatibility no-op. Raw provider rows are already logged."""
        return {
            "ok": True,
            "task_id": self.task_id,
            "note": "raw provider calls already logged by proxy",
        }


def _apply_task_context(payload: Dict[str, Any], session=None, task_id: str = "") -> None:
    if session is not None:
        if not task_id:
            task_id = getattr(session, "task_id", "")
        if payload.get("app") == APP_NAME and getattr(session, "app", APP_NAME) != APP_NAME:
            payload["app"] = getattr(session, "app")
        for key in ("filename", "input_unit", "video_duration"):
            if not payload.get(key) and getattr(session, key, ""):
                payload[key] = getattr(session, key)
        if (
            (payload.get("count") is None or payload.get("count") == "")
            and getattr(session, "count", None) is not None
        ):
            payload["count"] = getattr(session, "count")
    if task_id:
        payload["task_id"] = task_id


def _raise_proxy_error(name: str, r) -> None:
    if r.status_code != 200:
        # APP ADDITION (2) + (8): keep the HTTP status structured, and keep
        # enough of the body that Vertex's actual reason (which quota, which
        # model) survives instead of being cut mid-JSON at 300 chars.
        raise PWAccessError(
            f"{name} proxy error {r.status_code}: {r.text[:_ERROR_BODY_CHARS]}",
            r.status_code,
        )


def _upload_large_json(google_token, app: str, request: dict, provider_name: str) -> str:
    import json

    request_bytes = json.dumps(request).encode("utf-8")
    if len(request_bytes) <= _LARGE_REQUEST_BYTES:
        return ""

    up = _post("/api/gemini/upload-url", google_token, {"app": app}, _TIMEOUT)
    _raise_proxy_error(f"{provider_name} upload-url", up)

    pr = requests.put(
        up.json()["upload_url"],
        data=request_bytes,
        headers={"Content-Type": "application/json"},
        timeout=_AI_TIMEOUT,
    )
    _raise_proxy_error(f"{provider_name} upload", pr)
    return pr.json().get("url", "")


def gemini_generate(
    google_token,
    *,
    model: str,
    request: dict,
    filename: str = "",
    input_unit: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
    # APP ADDITION (7): per-call timeout override. The model health probe uses a
    # short one so a hung model can't stall the page; None keeps _AI_TIMEOUT.
    timeout: Optional[float] = None,
) -> dict:
    """Call Gemini through the proxy. Returns a Gemini-shaped result."""
    payload = {
        "app": app,
        "model": model,
        "request": request,
        "filename": filename,
        "input_unit": input_unit,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    blob_url = _upload_large_json(google_token, payload["app"], request, "gemini")
    if blob_url:
        payload.pop("request")
        payload["request_blob_url"] = blob_url

    r = _post("/api/gemini/generate", google_token, payload,
              _AI_TIMEOUT if timeout is None else timeout)
    _raise_proxy_error("gemini", r)
    return r.json()


def mathpix_ocr(
    google_token,
    *,
    request: dict,
    filename: str = "",
    count: Any = 1,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call Mathpix OCR through the proxy."""
    payload = {
        "app": app,
        "request": request,
        "filename": filename,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/mathpix/ocr", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("mathpix", r)
    return r.json()


def sarvam_tts(
    google_token,
    *,
    request: dict,
    filename: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call Sarvam TTS through the proxy."""
    payload = {
        "app": app,
        "request": request,
        "filename": filename,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/sarvam/tts", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("sarvam", r)
    return r.json()


def elevenlabs_tts(
    google_token,
    *,
    voice_id: str,
    request: dict,
    output_format: str = "mp3_44100_128",
    filename: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call ElevenLabs TTS through the proxy."""
    payload = {
        "app": app,
        "voice_id": voice_id,
        "request": request,
        "output_format": output_format,
        "filename": filename,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/elevenlabs/tts", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("elevenlabs", r)
    return r.json()


def claude_generate(
    google_token,
    *,
    model: str,
    request: dict,
    filename: str = "",
    input_unit: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call Claude through the proxy."""
    payload = {
        "app": app,
        "model": model,
        "request": request,
        "filename": filename,
        "input_unit": input_unit,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    blob_url = _upload_large_json(google_token, payload["app"], request, "claude")
    if blob_url:
        payload.pop("request")
        payload["request_blob_url"] = blob_url

    r = _post("/api/claude/generate", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("claude", r)
    return r.json()


def gemini_tts(
    google_token,
    *,
    text: str,
    voice: str = "Kore",
    model: str = "gemini-3.1-flash-tts-preview",
    filename: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call Gemini TTS through the proxy."""
    payload = {
        "app": app,
        "model": model,
        "text": text,
        "voice": voice,
        "filename": filename,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/gemini/tts", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("gemini tts", r)
    return r.json()


def gemini_image(
    google_token,
    *,
    prompt: str,
    model: str = "gemini-3.1-flash-image",
    filename: str = "",
    count: Any = 1,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call Gemini image generation through the proxy."""
    payload = {
        "app": app,
        "model": model,
        "prompt": prompt,
        "filename": filename,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/gemini/image", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("gemini image", r)
    return r.json()
