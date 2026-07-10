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
"""
import os
from typing import Optional, List, Dict, Any

import requests

# --------------------------------------------------------------------------
# PER-APP CONFIG — the only thing each app changes.
# APP_NAME must EXACTLY match a header in row 1 of the `Whitelisted` tab.
# --------------------------------------------------------------------------
APP_NAME = "Final ZIP Package"

# Point this at your proxy. Override per-environment with PW_PROXY_BASE_URL.
PROXY_BASE_URL = os.environ.get(
    "PW_PROXY_BASE_URL", "https://pw-apps-proxy.vercel.app"
).rstrip("/")

_TIMEOUT = 30       # allowlist / logging — fast
_AI_TIMEOUT = 300   # Gemini / Mathpix — can be slow


class PWAccessError(Exception):
    """Raised when a paid proxy call (Gemini/Mathpix) fails."""


def _headers(google_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {google_token}",
        "Content-Type": "application/json",
    }


def check_allowed(google_token: str, app: str = APP_NAME) -> bool:
    """Live app-wise whitelist check. Call this before EVERY paid/main run.
    Returns True only if the proxy confirms the user is allowed for `app`.
    Any error or network failure returns False (fail closed / deny)."""
    return check_allowed_status(google_token, app) == "allowed"


def check_allowed_status(google_token: str, app: str = APP_NAME) -> str:
    """Like check_allowed, but distinguishes the three outcomes so callers can
    implement 'proxy is the gate, with a local fallback if it's unreachable':
        "allowed"  — proxy verified the user IS allowed for this app
        "denied"   — proxy reached, user is NOT allowed (a real 'no')
        "error"    — proxy unreachable / bad token / server error (couldn't decide)
    """
    if not google_token:
        return "denied"
    try:
        r = requests.post(
            f"{PROXY_BASE_URL}/api/allowlist",
            headers=_headers(google_token),
            json={"app": app},
            timeout=_TIMEOUT,
        )
        if r.status_code == 200:
            return "allowed" if bool(r.json().get("allowed")) else "denied"
        if r.status_code == 403:
            return "denied"
        return "error"  # 401/5xx/etc — can't be sure
    except Exception:
        return "error"


def log_usage(
    google_token: str,
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
        r = requests.post(
            f"{PROXY_BASE_URL}/api/usage-log",
            headers=_headers(google_token),
            json={
                "app": app,
                "filename": filename,
                "input_unit": input_unit,
                "count": count,
                "items": items,
            },
            timeout=_TIMEOUT,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _accumulate(session, resp, default_model=""):
    usage = resp.get("usage") or {}
    session.add(resp.get("model") or default_model,
                usage.get("tokens_in", 0), usage.get("tokens_out", 0),
                resp.get("cost_inr", 0))


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
    """

    def __init__(self, google_token, *, filename="", input_unit="", count=None, app=APP_NAME):
        self.token = google_token
        self.filename = filename
        self.input_unit = input_unit
        self.count = count
        self.app = app
        self._by_model = {}  # model -> {tokens_in, tokens_out, cost_inr}

    def add(self, model, tokens_in=0, tokens_out=0, cost_inr=0.0):
        agg = self._by_model.setdefault(
            model or "", {"tokens_in": 0, "tokens_out": 0, "cost_inr": 0.0})
        agg["tokens_in"] += int(tokens_in or 0)
        agg["tokens_out"] += int(tokens_out or 0)
        agg["cost_inr"] += float(cost_inr or 0.0)

    def flush(self):
        """Write one row per provider used this task. Returns the proxy response,
        or None if nothing was accumulated. Call once, at the end of the task."""
        items = [
            {"model": m, "tokens_in": v["tokens_in"], "tokens_out": v["tokens_out"],
             "cost_inr": round(v["cost_inr"], 4)}
            for m, v in self._by_model.items()
        ]
        self._by_model = {}
        if not items:
            return None
        return log_usage(self.token, filename=self.filename, input_unit=self.input_unit,
                         count=self.count, items=items, app=self.app)


def gemini_generate(
    google_token: str,
    *,
    model: str,
    request: dict,
    filename: str = "",
    input_unit: str = "",
    count: Any = None,
    app: str = APP_NAME,
    session: "UsageSession" = None,
) -> dict:
    """Call Gemini THROUGH the proxy. The proxy holds GEMINI_API_KEY, calls
    Gemini, logs usage, and returns:
        {"ok": True, "result": <raw generateContent response>,
         "usage": {...}, "cost_inr": ...}
    `result` is the unmodified Gemini response, so existing parsing is unchanged.
    When `session` is given, this call is NOT logged on its own — its usage is
    added to the session and written once by session.flush()."""
    payload = {
        "app": app, "model": model, "request": request,
        "filename": filename, "input_unit": input_unit, "count": count,
    }
    if session is not None:
        payload["log"] = False
    r = requests.post(
        f"{PROXY_BASE_URL}/api/gemini/generate",
        headers=_headers(google_token),
        json=payload,
        timeout=_AI_TIMEOUT,
    )
    if r.status_code != 200:
        raise PWAccessError(f"gemini proxy error {r.status_code}: {r.text[:300]}")
    data = r.json()
    if session is not None:
        _accumulate(session, data, default_model=model)
    return data


def mathpix_ocr(
    google_token: str,
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
    r = requests.post(
        f"{PROXY_BASE_URL}/api/mathpix/ocr",
        headers=_headers(google_token),
        json=payload,
        timeout=_AI_TIMEOUT,
    )
    if r.status_code != 200:
        raise PWAccessError(f"mathpix proxy error {r.status_code}: {r.text[:300]}")
    data = r.json()
    if session is not None:
        _accumulate(session, data, default_model="Mathpix OCR")
    return data


def sarvam_tts(
    google_token: str,
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
    r = requests.post(
        f"{PROXY_BASE_URL}/api/sarvam/tts",
        headers=_headers(google_token),
        json=payload,
        timeout=_AI_TIMEOUT,
    )
    if r.status_code != 200:
        raise PWAccessError(f"sarvam proxy error {r.status_code}: {r.text[:300]}")
    data = r.json()
    if session is not None:
        _accumulate(session, data, default_model="Sarvam TTS")
    return data
