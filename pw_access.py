"""
pw_access.py — shared PW app-access client (drop-in for any PW app backend).

Copy this ONE file into an app, set APP_NAME below, and you get:
  - live app-wise whitelist checks,
  - append-only usage logging,
  - Gemini / Mathpix calls with keys that live ONLY on the proxy.

This module talks ONLY to the shared proxy. It NEVER contains a service-account
key or any provider (Gemini / Mathpix / ...) key. That is what makes API-key
safety automatic: the app simply has no key to leak.

Every call takes the signed-in user's Google token (the access token or id
token your app already obtains at login). The proxy verifies it, checks the
whitelist for APP_NAME, calls the paid API with its own key, logs usage, and
returns only the result.

Gemini uses Vertex routing (new PW kit): gemini_generate fetches a short-lived
Vertex token from the proxy and calls Vertex AI directly, so there is no ~4.5 MB
proxy body limit (this app sends large PDFs/page images inline).

IMPORTANT — Google tokens expire after ~1 HOUR (the app session may be 7 days,
but the Google token inside it is not). For anything that can run longer than
an hour, pass a TOKEN PROVIDER instead of a raw string: a callable that returns
a currently-valid token. Every helper here accepts either. With a provider, the
kit fetches a token before each request and, on a 401, refreshes once and
retries — so long runs survive the 1-hour expiry. This app's provider is
`src.pw_auth.token_provider_for(email)`.

--------------------------------------------------------------------------
KIT SYNC NOTE — synced with pw-app-kit 2026-07-22 (adds elevenlabs_tts).
This file INTENTIONALLY diverges from the kit template. Re-apply ALL of the
following on any future kit sync, or this app breaks:

  1. APP_NAME             — set to this app (template ships a placeholder).
  2. proxy_base_url()     — resolved at CALL time, not import time, so a
                            PW_PROXY_BASE_URL set by a late load_dotenv is
                            honoured (src.config imports after pw_access here).
  3. threading.Lock       — UsageSession and the Vertex cache are lock-guarded:
                            this app fans chunk calls across a ThreadPoolExecutor,
                            and the template's unguarded dict loses usage rows.
  4. PWAccessError.status_code — structured HTTP status, read by
                            src/ai_client._status_from_error for retry decisions.
  5. check_allowed_status → "expired" on 401, distinct from "error", so the UI
                            can say "session expired" vs "proxy unreachable".
  6. _post() force-refresh — the 401 retry force-renews the token and only
                            retries when it actually CHANGED (the template
                            re-calls the provider, which may return the same
                            cached string and buy a second identical 401).
  7. Vertex 401 handling  — _invalidate_vertex() + _get_vertex(force=True) so a
                            Vertex token that dies early is replaced, not replayed.
  8. set_token_provider() — process-wide fallback for bare-string callers.
--------------------------------------------------------------------------
"""
import os
import threading
from typing import Optional, List, Dict, Any, Callable, Union

import requests

# A "token" may be either the token string itself or a zero/one-arg callable
# returning a currently-valid token (see `src/pw_auth.token_provider_for`).
# Passing the callable is strongly preferred: it lets a long run resolve a
# FRESH token per call instead of reusing whatever was valid when it started,
# and it lets this module retry once with a renewed token after a 401.
TokenLike = Union[str, Callable[..., str]]

# --------------------------------------------------------------------------
# PER-APP CONFIG — the only thing each app changes.
# APP_NAME must EXACTLY match a header in row 1 of the `Whitelisted` tab.
# --------------------------------------------------------------------------
APP_NAME = "Handwritten Notes Automation"

# Point this at your proxy. Override per-environment with PW_PROXY_BASE_URL.
def proxy_base_url() -> str:
    """Resolve the proxy base URL at CALL time, so a PW_PROXY_BASE_URL set by
    a late load_dotenv (src.config imports after pw_access in some entrypoints)
    is still honoured instead of being silently ignored."""
    return os.environ.get(
        "PW_PROXY_BASE_URL", "https://pw-apps-proxy.vercel.app"
    ).rstrip("/")


# Import-time snapshot kept for backward compatibility (scripts print it).
PROXY_BASE_URL = proxy_base_url()

_TIMEOUT = 30       # allowlist / logging — fast
_AI_TIMEOUT = 300   # Gemini / Mathpix — can be slow


class PWAccessError(Exception):
    """Raised when a paid proxy call (Gemini/Mathpix) fails."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _headers(google_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {google_token}",
        "Content-Type": "application/json",
    }


# --------------------------------------------------------------------------
# Token resolution + single-retry-on-401
#
# A Google id_token lives ~1 hour. Anything that holds one for longer than that
# (a long batch, a download click an hour later) WILL see a 401 from the proxy.
# Resolving the token per call — and retrying exactly once with a force-renewed
# token after a 401 — is what makes those 401s self-healing instead of a dead
# end. Exactly one retry: never an infinite loop.
# --------------------------------------------------------------------------
_token_provider: Optional[Callable[..., str]] = None


def set_token_provider(provider: Optional[Callable[..., str]]) -> None:
    """Register a process-wide fallback token provider.

    Only used when a caller passes a bare token STRING that the proxy then
    rejects. Prefer passing the callable itself as `google_token` — that is
    per-request and therefore correct for multi-user deployments too.
    """
    global _token_provider
    _token_provider = provider


def _resolve_token(google_token: TokenLike, *, force: bool = False) -> str:
    """Turn a token-or-provider into a token string.

    `force=True` asks the provider to bypass its cache and mint a new token —
    used only for the single post-401 retry.
    """
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
                return ""
        except Exception:
            return ""
    return str(google_token or "").strip()


def _post(path: str, google_token: TokenLike, payload: dict, timeout: int):
    """POST to the proxy, retrying ONCE with a freshly minted token on 401.

    Returns the final `requests.Response`, or None when the proxy is
    unreachable. The retry is attempted only when the renewed token actually
    differs from the one that was rejected — otherwise it would just buy a
    second identical 401.
    """
    token = _resolve_token(google_token)
    url = f"{proxy_base_url()}{path}"
    try:
        r = requests.post(url, headers=_headers(token), json=payload, timeout=timeout)
    except Exception:
        return None

    if r.status_code != 401:
        return r

    fresh = _resolve_token(google_token, force=True)
    if not fresh or fresh == token:
        return r
    try:
        retried = requests.post(url, headers=_headers(fresh), json=payload, timeout=timeout)
    except Exception:
        return r
    retried.pw_retried_after_401 = True     # type: ignore[attr-defined]
    return retried


def check_allowed(google_token: TokenLike, app: str = APP_NAME) -> bool:
    """Live app-wise whitelist check. Call this before EVERY paid/main run.
    Returns True only if the proxy confirms the user is allowed for `app`.
    Any error or network failure returns False (fail closed / deny)."""
    return check_allowed_status(google_token, app) == "allowed"


def check_allowed_status(google_token: TokenLike, app: str = APP_NAME) -> str:
    """Like check_allowed, but distinguishes the outcomes so callers can tell a
    real "no" from a recoverable problem:
        "allowed"  — proxy verified the user IS allowed for this app
        "denied"   — proxy reached, user is NOT allowed (a real 'no')
        "expired"  — proxy reached, the Google token is expired/invalid (401);
                     recoverable by refreshing the token and retrying
        "error"    — proxy unreachable / server error (couldn't decide)
    Only "allowed" grants access — every other value fails closed.
    """
    token = _resolve_token(google_token)
    if not token:
        return "expired"
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
        # Survived the refresh-and-retry above, so the sign-in is genuinely dead.
        return "expired"
    return "error"  # 5xx/etc — can't be sure


def log_usage(
    google_token: TokenLike,
    *,
    filename: str,
    input_unit: str,
    count: Any,
    items: List[Dict[str, Any]],
    app: str = APP_NAME,
) -> Optional[dict]:
    """Append one usage row PER item to the `Usage Cost` tab. Use this only
    for usage the proxy didn't already log itself (the gemini_generate /
    mathpix_ocr helpers below log automatically). Never raises — returns None
    on failure so logging can't break the app.

    items example:
      [{"model": "gemini-2.5-flash", "tokens_in": 14500,
        "tokens_out": 2300, "cost_inr": 12.45}]
    """
    try:
        r = _post(
            "/api/usage-log",
            google_token,
            {
                "app": app,
                "filename": filename,
                "input_unit": input_unit,
                "count": count,
                "items": items,
            },
            _TIMEOUT,
        )
        if r is None or r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _accumulate(session, resp, default_model=""):
    usage = resp.get("usage") or {}
    session.add(resp.get("model") or default_model,
                usage.get("tokens_in", 0), usage.get("tokens_out", 0),
                resp.get("cost_inr"))


class UsageSession:
    """Accumulates a task's provider usage and writes ONE row per provider on
    flush() — so multiple calls to the same provider collapse into a single
    Usage Cost row (one Gemini row, one Mathpix row, one Sarvam row) instead of
    one row per call.

        s = pw_access.UsageSession(token, filename="chapter1.pdf",
                                   input_unit="No. of pages", count=20)
        pw_access.gemini_generate(token, model=..., request=..., session=s)
        pw_access.gemini_generate(token, model=..., request=..., session=s)
        s.flush()   # ONE gemini row with the summed tokens + cost

    Thread-safe: add() and flush() are guarded by a lock so a task may fan its
    provider calls out across threads (this app runs chunk calls concurrently)
    and still accumulate into a single session safely.
    """

    def __init__(self, google_token, *, filename="", input_unit="", count=None, app=APP_NAME):
        self.token = google_token
        self.filename = filename
        self.input_unit = input_unit
        self.count = count
        self.app = app
        self._by_model = {}  # model -> {tokens_in, tokens_out, cost_inr, cost_known, requests}
        self._lock = threading.Lock()

    def add(self, model, tokens_in=0, tokens_out=0, cost_inr=None):
        with self._lock:
            agg = self._by_model.setdefault(
                model or "", {"tokens_in": 0, "tokens_out": 0, "cost_inr": 0.0,
                              "cost_known": False, "requests": 0})
            agg["tokens_in"] += int(tokens_in or 0)
            agg["tokens_out"] += int(tokens_out or 0)
            agg["requests"] += 1
            if cost_inr is not None:
                agg["cost_inr"] += float(cost_inr or 0.0)
                agg["cost_known"] = True

    def flush(self):
        """Write one row per provider used this task (with its API-request count).
        Returns the proxy response, or None if nothing was accumulated. Call once,
        at the end of the task. If a provider's cost wasn't known client-side
        (token-vending Gemini), it's omitted so the proxy computes it."""
        with self._lock:
            items = []
            for m, v in self._by_model.items():
                item = {"model": m, "tokens_in": v["tokens_in"],
                        "tokens_out": v["tokens_out"], "requests": v["requests"]}
                if v["cost_known"]:
                    item["cost_inr"] = round(v["cost_inr"], 4)
                items.append(item)
            self._by_model = {}
        if not items:
            return None
        return log_usage(self.token, filename=self.filename, input_unit=self.input_unit,
                         count=self.count, items=items, app=self.app)


# Vertex token cache. The SA token is identical for every user (it authenticates
# the proxy's service account, not the end user), so it's shared process-wide.
# Per-user authorization is enforced by check_allowed() before each run.
_vertex_cache = {"token": "", "project": "", "location": "global", "expiry": 0.0}
_vertex_lock = threading.Lock()


def _invalidate_vertex():
    """Drop the cached Vertex token so the next call fetches a new one. Called
    when Vertex itself answers 401 — otherwise a token that went bad early
    (proxy restart, revoked SA) would keep being replayed until its nominal
    expiry."""
    with _vertex_lock:
        _vertex_cache.update({"token": "", "expiry": 0.0})


def _get_vertex(google_token: TokenLike, app=APP_NAME, *, force=False):
    """Fetch (and cache) a short-lived Vertex token from the proxy; refresh
    ~10 min before expiry. Guarded by a lock so concurrent chunk calls don't
    each trigger a separate token fetch.

    `_post` renews the USER's Google token and retries once if the proxy
    rejects it with 401, so a batch that outlives the user's ~1h sign-in keeps
    running instead of failing half-way."""
    import time
    with _vertex_lock:
        now = time.time()
        if not force and _vertex_cache["token"] and now < _vertex_cache["expiry"] - 600:
            return _vertex_cache
        r = _post("/api/vertex/token", google_token, {"app": app}, _TIMEOUT)
        if r is None:
            raise PWAccessError("vertex token error: proxy unreachable")
        if r.status_code != 200:
            raise PWAccessError(
                f"vertex token error {r.status_code}: {r.text[:300]}",
                status_code=r.status_code,
            )
        d = r.json()
        _vertex_cache.update({
            "token": d.get("token", ""),
            "project": d.get("project", ""),
            "location": d.get("location", "global"),
            "expiry": now + int(d.get("expires_in", 3300)),
        })
        return dict(_vertex_cache)


def gemini_generate(
    google_token: TokenLike,
    *,
    model: str,
    request: dict,
    filename: str = "",
    input_unit: str = "",
    count: Any = None,
    app: str = APP_NAME,
    session: "UsageSession" = None,
) -> dict:
    """Call Gemini via Vertex AI. Fetches a short-lived Vertex token from the
    proxy (cached), then calls Vertex DIRECTLY — so there is NO 4.5 MB proxy body
    limit (important for large PDFs/images). Returns
        {"ok": True, "result": <raw generateContent response>,
         "model": ..., "usage": {...}, "cost_inr": None}
    `result` has the same shape as the Gemini API, so existing parsing is
    unchanged. Cost is computed by the proxy when the usage row is written.
    When `session` is given, usage is added to it and written by session.flush()."""
    def _call(vertex):
        host = ("aiplatform.googleapis.com" if vertex["location"] == "global"
                else f"{vertex['location']}-aiplatform.googleapis.com")
        url = (f"https://{host}/v1/projects/{vertex['project']}/locations/{vertex['location']}"
               f"/publishers/google/models/{model}:generateContent")
        return requests.post(
            url,
            headers={"Authorization": f"Bearer {vertex['token']}",
                     "Content-Type": "application/json"},
            json=request,
            timeout=_AI_TIMEOUT,
        )

    v = _get_vertex(google_token, app)
    r = _call(v)
    if r.status_code == 401:
        # The Vertex token died (expired early / proxy rotated its SA). Drop it,
        # mint one more, and retry EXACTLY once — never a loop.
        _invalidate_vertex()
        r = _call(_get_vertex(google_token, app, force=True))
    if r.status_code != 200:
        raise PWAccessError(
            f"vertex gemini error {r.status_code}: {r.text[:300]}",
            status_code=r.status_code,
        )
    data = r.json()
    um = data.get("usageMetadata") or {}
    tin = int(um.get("promptTokenCount") or 0)
    tout = int((um.get("candidatesTokenCount") or 0) + (um.get("thoughtsTokenCount") or 0))
    if session is not None:
        session.add(model, tin, tout, None)  # cost computed by the proxy at flush
    else:
        log_usage(google_token, filename=filename, input_unit=input_unit, count=count,
                  items=[{"model": model, "tokens_in": tin, "tokens_out": tout, "requests": 1}],
                  app=app)
    return {"ok": True, "result": data, "model": model,
            "usage": {"tokens_in": tin, "tokens_out": tout}, "cost_inr": None}


def mathpix_ocr(
    google_token: TokenLike,
    *,
    request: dict,
    filename: str = "",
    count: Any = 1,
    app: str = APP_NAME,
    session: "UsageSession" = None,
) -> dict:
    """Call Mathpix THROUGH the proxy. The proxy holds the Mathpix keys, calls
    Mathpix, logs usage, and returns {"ok": True, "result": <mathpix response>,
    "cost_inr": ...}. When `session` is given, usage is accumulated and written
    once by session.flush() instead of logged per call."""
    payload = {"app": app, "request": request, "filename": filename, "count": count}
    if session is not None:
        payload["log"] = False
    r = _post("/api/mathpix/ocr", google_token, payload, _AI_TIMEOUT)
    if r is None:
        raise PWAccessError("mathpix proxy error: proxy unreachable")
    if r.status_code != 200:
        raise PWAccessError(
            f"mathpix proxy error {r.status_code}: {r.text[:300]}",
            status_code=r.status_code,
        )
    data = r.json()
    if session is not None:
        _accumulate(session, data, default_model="Mathpix OCR")
    return data


def sarvam_tts(
    google_token: TokenLike,
    *,
    request: dict,
    filename: str = "",
    count: Any = None,
    app: str = APP_NAME,
    session: "UsageSession" = None,
) -> dict:
    """Call Sarvam Text-to-Speech THROUGH the proxy. The proxy holds
    SARVAM_API_KEY, calls Sarvam, logs usage (per character), and returns
    {"ok": True, "result": <sarvam response with base64 audio>, "cost_inr": ...}.
    `count` = characters billed; if omitted the proxy derives it from the text.
    When `session` is given, usage is accumulated and written once by
    session.flush() instead of logged per call."""
    payload = {"app": app, "request": request, "filename": filename, "count": count}
    if session is not None:
        payload["log"] = False
    r = _post("/api/sarvam/tts", google_token, payload, _AI_TIMEOUT)
    if r is None:
        raise PWAccessError("sarvam proxy error: proxy unreachable")
    if r.status_code != 200:
        raise PWAccessError(
            f"sarvam proxy error {r.status_code}: {r.text[:300]}",
            status_code=r.status_code,
        )
    data = r.json()
    if session is not None:
        _accumulate(session, data, default_model="Sarvam TTS")
    return data


def elevenlabs_tts(
    google_token: TokenLike,
    *,
    voice_id: str,
    request: dict,
    output_format: str = "mp3_44100_128",
    filename: str = "",
    count: Any = None,
    app: str = APP_NAME,
    session: "UsageSession" = None,
) -> dict:
    """Call ElevenLabs Text-to-Speech THROUGH the proxy. The proxy holds
    ELEVENLABS_API_KEY, calls ElevenLabs, logs usage (per character), and
    returns {"ok": True, "result": {"audio_base64": ..., "content_type":
    "audio/mpeg", "output_format": ...}, "cost_inr": ...}.
    `request` is the raw ElevenLabs TTS body: {"text": ..., "model_id":
    "eleven_multilingual_v2", "voice_settings": {...}}. `voice_id` is the
    ElevenLabs voice to use. `count` = characters billed; if omitted the proxy
    derives it from request["text"]. When `session` is given, usage is
    accumulated and written once by session.flush() instead of logged per call.

    This app generates notes, not audio, so nothing calls this today — it is
    kept at kit parity so a future kit sync stays a clean diff."""
    payload = {"app": app, "voice_id": voice_id, "request": request,
               "output_format": output_format, "filename": filename, "count": count}
    if session is not None:
        payload["log"] = False
    r = _post("/api/elevenlabs/tts", google_token, payload, _AI_TIMEOUT)
    if r is None:
        raise PWAccessError("elevenlabs proxy error: proxy unreachable")
    if r.status_code != 200:
        raise PWAccessError(
            f"elevenlabs proxy error {r.status_code}: {r.text[:300]}",
            status_code=r.status_code,
        )
    data = r.json()
    if session is not None:
        _accumulate(session, data, default_model="ElevenLabs TTS")
    return data
