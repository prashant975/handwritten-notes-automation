from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pw_access

from .utils import image_mime_type

logger = logging.getLogger("concise_notes.gemini")


class GeminiError(RuntimeError):
    def __init__(self, message: str, *, provider: str = "", status_code: int | None = None, details: Any = None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.details = details


@dataclass
class GeminiResponse:
    text: str
    provider: str
    model: str
    raw: dict[str, Any] | None = None
    usage: dict[str, int] | None = None
    images_dropped: int = 0   # slide images omitted to fit the request budget


def _usage_from_mapping(data: dict[str, Any] | None) -> dict[str, int]:
    if not data:
        return {}
    usage = data.get("usageMetadata") or data.get("usage_metadata") or {}
    if not isinstance(usage, dict):
        return {}
    key_map = {
        "promptTokenCount": "tokens_input",
        "candidatesTokenCount": "tokens_output",
        "totalTokenCount": "tokens_total",
        "thoughtsTokenCount": "tokens_thinking",
        "prompt_token_count": "tokens_input",
        "candidates_token_count": "tokens_output",
        "total_token_count": "tokens_total",
        "thoughts_token_count": "tokens_thinking",
    }
    out: dict[str, int] = {}
    for src, dest in key_map.items():
        value = usage.get(src)
        if isinstance(value, int):
            out[dest] = value
    return out


def _usage_from_proxy(resp: dict[str, Any] | None) -> dict[str, int]:
    """Fall back to the proxy's own usage summary ({"tokens_in", "tokens_out"})
    when the raw Gemini response didn't carry usageMetadata."""
    usage = (resp or {}).get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    out: dict[str, int] = {}
    if isinstance(usage.get("tokens_in"), int):
        out["tokens_input"] = usage["tokens_in"]
    if isinstance(usage.get("tokens_out"), int):
        out["tokens_output"] = usage["tokens_out"]
    return out


# ---------------------------------------------------------------------------
# Retry/rate policy (shared by every Gemini call via GeminiClient)
# ---------------------------------------------------------------------------
_MAX_EMPTY_RETRIES = 4          # empty-but-"successful" responses: retry 1s, 2s, 4s, 8s
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_STATUS_RE = re.compile(r"\berror (\d{3})\b")
_TRANSIENT_NAMES = {
    "TooManyRequests", "ResourceExhausted", "ServiceUnavailable",
    "DeadlineExceeded",
}


def _env_number(name: str, default, minimum, maximum, cast):
    raw = os.getenv(name)
    try:
        value = cast(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        logger.warning("invalid %s=%r; using default %s", name, raw, default)
        return default
    if value < minimum or value > maximum:
        logger.warning("%s=%r outside %s..%s; using default %s",
                       name, raw, minimum, maximum, default)
        return default
    return value


@dataclass(frozen=True)
class GeminiRequestSettings:
    max_concurrency: int
    request_delay: float
    max_attempts: int
    initial_delay: float
    max_delay: float
    timeout: float

    @classmethod
    def from_env(cls):
        # Defaults tuned for SPEED: run several chunk calls at once with only a
        # short start-to-start gap. The old 1-at-a-time / 3s-gap defaults
        # serialized the whole deck and were the dominant source of slowness.
        # The Vertex regional fallback (pw_access) + the retry loop below absorb
        # the occasional 429 that higher concurrency can provoke.
        return cls(
            max_concurrency=_env_number("GEMINI_MAX_CONCURRENCY", 3, 1, 16, int),
            request_delay=_env_number("GEMINI_REQUEST_DELAY_SECONDS", 0.5, 0.0, 300.0, float),
            max_attempts=_env_number("GEMINI_MAX_RETRIES", 6, 1, 20, int),
            initial_delay=_env_number("GEMINI_RETRY_INITIAL_DELAY_SECONDS", 2.0, 0.1, 300.0, float),
            max_delay=_env_number("GEMINI_RETRY_MAX_DELAY_SECONDS", 60.0, 0.1, 900.0, float),
            timeout=_env_number("GEMINI_REQUEST_TIMEOUT_SECONDS", 120.0, 5.0, 900.0, float),
        )


class GeminiRequestGate:
    """Process-wide concurrency limiter and start-to-start request pacer."""

    def __init__(self, settings: GeminiRequestSettings):
        self.settings = settings
        self._semaphore = threading.BoundedSemaphore(settings.max_concurrency)
        self._spacing_lock = threading.Lock()
        self._last_start = 0.0

    def __enter__(self):
        self._semaphore.acquire()
        with self._spacing_lock:
            wait = self.settings.request_delay - (time.monotonic() - self._last_start)
            if wait > 0:
                time.sleep(wait)
            self._last_start = time.monotonic()
        return self

    def __exit__(self, *_):
        self._semaphore.release()


_REQUEST_SETTINGS = GeminiRequestSettings.from_env()
_REQUEST_GATE = GeminiRequestGate(_REQUEST_SETTINGS)

# Thinking budget (tokens) for note/vision generation. Gemini 2.5+/3.x are
# reasoning models; left uncapped, a chunk carrying several slide images can
# spend the ENTIRE output-token budget on internal thinking and return
# finishReason=MAX_TOKENS with EMPTY text (the "Gemini returned an empty response"
# failure). Capping the thinking leaves room for the actual notes — and A/B tests
# show low reasoning ≈ the same note quality, while being faster and cheaper.
# Set GEMINI_THINKING_BUDGET=-1 to disable the cap (use the model's dynamic default).
_THINKING_BUDGET = _env_number("GEMINI_THINKING_BUDGET", 2048, -1, 32768, int)

# Ceiling the adaptive empty-retry may widen maxOutputTokens up to. Denser decks
# need more room for visible text once thinking is stripped back.
_EMPTY_RETRY_MAX_OUTPUT = _env_number("GEMINI_EMPTY_RETRY_MAX_OUTPUT", 32000, 4000, 65536, int)


def _relax_generation_for_empty(cfg: dict[str, Any], retry: int) -> dict[str, int]:
    """Mutate a generationConfig IN PLACE so the *next* attempt is more likely to
    return visible text after an empty response.

    The dominant empty-response cause is a reasoning model spending its whole
    output budget on internal thinking (finishReason MAX_TOKENS, thoughtsTokenCount
    high, text ""). Retrying the identical request just repeats that outcome, so
    a deck dense enough to trigger it once would burn through every retry. Instead
    each empty retry (1) widens the visible-output budget and (2) progressively
    clamps the model's thinking — halving from a sane start, floored at 256 so
    models that require some thinking (e.g. 3.x Pro) stay valid. Together these
    cover both classes of deck: models that honour thinkingBudget get their
    thinking cut; models that ignore it still gain enough extra room after
    thinking for real text. This is what makes generation survive ANY deck."""
    cur_out = int(cfg.get("maxOutputTokens") or 16000)
    cfg["maxOutputTokens"] = min(_EMPTY_RETRY_MAX_OUTPUT, max(cur_out, int(cur_out * 1.5)))
    # Halve the CURRENT thinking budget each retry (cfg is mutated in place, so
    # this compounds: 2048 -> 1024 -> 512 -> 256), floored at 256 so models that
    # require some thinking (e.g. 3.x Pro) stay valid.
    cur_think = (cfg.get("thinkingConfig") or {}).get("thinkingBudget")
    if not isinstance(cur_think, int) or cur_think < 0:
        cur_think = 2048
    thinking = max(256, cur_think >> 1)
    cfg["thinkingConfig"] = {"thinkingBudget": thinking}
    return {"maxOutputTokens": cfg["maxOutputTokens"], "thinkingBudget": thinking}


def _status_from_error(e: Exception) -> int | None:
    """Extract the HTTP status from a PWAccessError.

    Prefers the structured `status_code` that pw_access now attaches, and falls
    back to parsing the message ("vertex gemini error 429: ...") so older kit
    versions and re-raised errors still classify correctly."""
    status = getattr(e, "status_code", None)
    if isinstance(status, int):
        return status
    m = _STATUS_RE.search(str(e))
    return int(m.group(1)) if m else None


def _is_transient_error(e: Exception, status: int | None) -> bool:
    if status in _TRANSIENT_STATUS:
        return True
    names = {type(e).__name__}
    names.update(type(base).__name__ for base in type(e).__mro__)
    text = str(e).lower()
    return bool(names & _TRANSIENT_NAMES) or any(
        marker.lower() in text for marker in _TRANSIENT_NAMES
    )


def _retry_after_seconds(e: Exception) -> float | None:
    for obj in (e, getattr(e, "response", None)):
        headers = getattr(obj, "headers", None)
        if headers:
            raw = headers.get("Retry-After") or headers.get("retry-after")
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
    return None


def _terminal_error_message(e: Exception, status: int | None, transient: bool) -> str:
    text = str(e)
    lowered = text.lower()
    if status in {401, 403}:
        return "Gemini authentication or permission failed. Reconnect Google and verify Vertex AI access."
    if status == 429 and any(word in lowered for word in ("quota", "billing", "exhausted")):
        return (
            "Gemini quota is exhausted after automatic retries. Verify Vertex AI "
            "quota and billing for the configured project, then retry this file."
        )
    if transient:
        return (
            "Gemini is temporarily unavailable or rate-limited after automatic "
            "retries. Wait a few minutes and retry this file."
        )
    if status in {400, 404, 422}:
        return "Gemini configuration or request is invalid. Verify the configured project, location, and model."
    return text


def _response_id(result: dict[str, Any] | None) -> str:
    return str((result or {}).get("responseId") or "-")


def _finish_reason(result: dict[str, Any] | None) -> str:
    try:
        return str((result or {}).get("candidates", [{}])[0].get("finishReason") or "-")
    except (IndexError, AttributeError, TypeError):
        return "-"


def _response_is_empty(result: dict[str, Any] | None, *, expect: str = "text") -> bool:
    """True when a structurally 'successful' Gemini response carries no output.

    Covers every empty-response shape seen in production: result not a dict,
    missing/empty candidates, candidate without content/parts, and parts whose
    text is "" or whitespace. For expect="image", inline image data counts as
    output instead (image responses legitimately have no text)."""
    if not isinstance(result, dict):
        return True
    candidates = result.get("candidates")
    if not candidates:
        return True
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        parts = (cand.get("content") or {}).get("parts") or []
        for p in parts:
            if not isinstance(p, dict):
                continue
            if expect == "image":
                inline = p.get("inline_data") or p.get("inlineData")
                if inline and inline.get("data"):
                    return False
            else:
                if str(p.get("text") or "").strip():
                    return False
    return True


def _extract_text_from_response(data: dict[str, Any]) -> str:
    candidates = data.get("candidates") or []
    parts: list[str] = []
    for cand in candidates:
        content = cand.get("content") or {}
        for p in content.get("parts") or []:
            if "text" in p:
                parts.append(p["text"])
    if parts:
        return "\n".join(parts).strip()
    if "output" in data:
        out = data["output"]
        if isinstance(out, str):
            return out.strip()
        if isinstance(out, list):
            return "\n".join(str(x) for x in out).strip()
    return ""


# Gemini now goes DIRECT to Vertex AI (not through the Vercel proxy), whose
# generateContent inline-request limit is ~20 MB. Pages are rendered as 2x PNGs
# (often 1-3 MB each), so we still downscale + re-encode them to JPEG before
# base64 to keep requests small and fast, with a generous hard byte budget as a
# final safety net so a huge deck can't build an oversized request.
_PROXY_BODY_BUDGET = 18_000_000         # bytes; < Vertex's ~20 MB request limit
_IMAGE_MAX_DIM = 1600                    # longest edge sent to Gemini
_IMAGE_JPEG_QUALITY = 80


def _encode_image_inline(image_path: Path) -> dict[str, Any] | None:
    """Return an inline_data part for one image, downscaled and JPEG-compressed
    so the request stays small. Falls back to the raw bytes if PIL is missing or
    the image can't be processed."""
    p = Path(image_path)
    if not p.exists():
        return None
    raw = p.read_bytes()
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            im.thumbnail((_IMAGE_MAX_DIM, _IMAGE_MAX_DIM))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=_IMAGE_JPEG_QUALITY, optimize=True)
            data = buf.getvalue()
        return {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(data).decode("ascii")}}
    except Exception:
        return {"inline_data": {"mime_type": image_mime_type(p), "data": base64.b64encode(raw).decode("ascii")}}


def _build_rest_parts(prompt: str, image_paths: list[Path] | None = None, max_images: int = 8) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for image_path in (image_paths or [])[:max_images]:
        if not image_path:
            continue
        part = _encode_image_inline(Path(image_path))
        if part:
            parts.append(part)
    return parts


def _fit_body_to_budget(body: dict[str, Any], budget: int = _PROXY_BODY_BUDGET) -> int:
    """Safety net: if the serialized request still exceeds the proxy's size
    budget, drop trailing inline images (never the prompt text at index 0) until
    it fits. Returns how many images were dropped."""
    try:
        parts = body["contents"][0]["parts"]
    except (KeyError, IndexError, TypeError):
        return 0
    dropped = 0
    while len(json.dumps(body).encode("utf-8")) > budget:
        for i in range(len(parts) - 1, 0, -1):
            if "inline_data" in parts[i]:
                parts.pop(i)
                dropped += 1
                break
        else:
            break  # nothing left to drop but the prompt
    return dropped


class GeminiClient:
    """Gemini access through the shared PW proxy.

    The app ships no Gemini API key; the proxy holds it. Every call carries the
    signed-in user's Google token, which the proxy verifies (against the app
    whitelist) before calling Gemini with its own key and logging the usage.

    When a `session` (pw_access.UsageSession) is passed, each call's usage is
    accumulated into that session instead of logged per call, so a task that
    makes many Gemini calls produces ONE combined Usage Cost row on flush().
    """

    def __init__(
        self,
        google_token: "str | Callable[..., str]",
        model: str = "gemini-2.5-pro",
        *,
        session: "pw_access.UsageSession | None" = None,
        retry_callback: "Callable[[str], None] | None" = None,
        fallback_models: list[str] | None = None,
    ):
        # `google_token` may be a token string OR a provider callable (see
        # src/pw_auth.token_provider_for). Prefer the callable: a Google
        # id_token lives ~1 hour, so a batch that takes longer than that would
        # otherwise die half-way with a 401. The callable is resolved by
        # pw_access at the moment of each call, and force-renewed once if the
        # proxy rejects it.
        self.google_token = google_token if callable(google_token) else (google_token or "").strip()
        self.model = model.strip() or "gemini-2.5-pro"
        # Runtime fallback chain: models tried, in order, if `self.model` keeps
        # returning empty text (see _generate_content). Distinct from the health
        # check — this catches a model that is UP but empties on a specific deck.
        self.fallback_models = [
            m.strip() for m in (fallback_models or [])
            if isinstance(m, str) and m.strip() and m.strip() != self.model
        ]
        self.session = session
        self.retry_callback = retry_callback
        if not self.google_token:
            raise GeminiError(
                "No Google token available. Sign in with your @pw.live Google "
                "account before generating notes (the PW proxy needs it)."
            )
        if callable(self.google_token) and not str(pw_access._resolve_token(self.google_token) or ""):
            raise GeminiError(
                "Your Google sign-in could not be renewed. Click 'Reconnect "
                "Google' and sign in again, then start the generation."
            )

    def _generate_content(self, model: str, body: dict[str, Any], *, expect: str = "text") -> dict[str, Any]:
        """Call Gemini with resilience against BOTH failure modes we see in prod:

        1. Transient HTTP errors (429 rate limit, 5xx) -> retried with backoff.
           Auth failures (401/403) and other 4xx are NOT retried.
        2. "Successful" responses that contain no output — finishReason STOP but
           text is "" / parts missing / candidates missing (an intermittent
           model-side issue; the same prompt usually succeeds on a retry).
           Retried up to _MAX_EMPTY_RETRIES times with exponential backoff
           (1s, 2s, 4s). `expect` selects what counts as output: "text" for
           note generation, "image" for diagram redraw (which returns
           inline_data and NO text — it must not be misread as empty).
        3. A model that is UP but keeps returning empty text for THIS deck
           (e.g. a Pro model exhausting its output budget on internal thinking
           over an equation-dense slide). After the empty-retries above are
           spent, generation falls through to the next model in
           self.fallback_models (text only — the text fallbacks aren't image
           models), so one stubborn deck can't fail the whole run.

        The first non-empty response is returned immediately. Every attempt is
        logged with model, attempt number, response id, finish reason, and
        emptiness so intermittent behaviour is visible in the logs. Usage note:
        Gemini bills empty responses too, so their tokens still accumulate into
        the UsageSession — retries are never double-logged beyond what Vertex
        actually charged.
        """
        # Models to try, in order. Runtime fallback applies to text only.
        models = [model]
        if expect == "text":
            for m in self.fallback_models:
                if m and m not in models:
                    models.append(m)
        mi = 0
        current = models[0]
        empty_retries = 0
        transient_retries = 0
        attempt = 0
        while True:
            attempt += 1
            try:
                logger.info(
                    "gemini request: attempt=%d/%d model=%s operation=generateContent",
                    attempt, _REQUEST_SETTINGS.max_attempts, current,
                )
                with _REQUEST_GATE:
                    resp = pw_access.gemini_generate(
                        self.google_token,
                        model=current,
                        request=body,
                        session=self.session,
                        timeout=_REQUEST_SETTINGS.timeout,
                    )
            except Exception as e:
                status = _status_from_error(e)
                transient = _is_transient_error(e, status)
                if transient and attempt < _REQUEST_SETTINGS.max_attempts:
                    transient_retries += 1
                    calculated = min(
                        _REQUEST_SETTINGS.max_delay,
                        _REQUEST_SETTINGS.initial_delay * (2 ** (attempt - 1)),
                    )
                    delay = max(_retry_after_seconds(e) or 0.0, calculated + random.uniform(0, calculated * 0.25))
                    message = (
                        f"Gemini is temporarily rate-limited. Retrying in "
                        f"{delay:.0f} seconds (attempt {attempt + 1}/{_REQUEST_SETTINGS.max_attempts})."
                    )
                    logger.warning(
                        "gemini transient error: attempt=%d/%d status=%s model=%s "
                        "operation=generateContent retry_delay=%.2fs err=%s",
                        attempt, _REQUEST_SETTINGS.max_attempts, status, current, delay, str(e)[:200],
                    )
                    if self.retry_callback:
                        try:
                            self.retry_callback(message)
                        except Exception:
                            pass
                    time.sleep(delay)
                    continue
                logger.error(
                    "gemini final failure: attempt=%d/%d status=%s transient=%s "
                    "model=%s operation=generateContent err=%r",
                    attempt, _REQUEST_SETTINGS.max_attempts, status, transient, current, e,
                )
                raise GeminiError(
                    _terminal_error_message(e, status, transient),
                    provider="pw_proxy", status_code=status, details=str(e),
                ) from e

            result = resp.get("result") if isinstance(resp, dict) else None
            empty = _response_is_empty(result, expect=expect)
            logger.info(
                "gemini response: attempt=%d model=%s response_id=%s finish_reason=%s empty=%s",
                attempt, current, _response_id(result), _finish_reason(result), empty,
            )
            if not empty:
                if empty_retries or transient_retries or mi:
                    logger.info(
                        "gemini succeeded after retries: model=%s attempts=%d empty_retries=%d "
                        "transient_retries=%d model_switches=%d",
                        current, attempt, empty_retries, transient_retries, mi,
                    )
                # Stash the proxy usage + which model actually produced the text.
                result.setdefault("_pw_usage", resp.get("usage") or {})
                result.setdefault("_model_used", current)
                return result

            if empty_retries < _MAX_EMPTY_RETRIES:
                empty_retries += 1
                delay = float(2 ** (empty_retries - 1))     # 1s, 2s, 4s, 8s
                # Adapt the request before retrying so a deck that deterministically
                # empties out (thinking eats the whole budget) still converges to
                # real text instead of failing every identical retry. Text only —
                # image redraw has no text and must keep its responseModalities.
                relaxed = None
                if expect == "text":
                    relaxed = _relax_generation_for_empty(
                        body.setdefault("generationConfig", {}), empty_retries)
                logger.warning(
                    "gemini EMPTY response: model=%s attempt=%d response_id=%s finish_reason=%s "
                    "retry=%d/%d delay=%.0fs relaxed=%s",
                    current, attempt, _response_id(result), _finish_reason(result),
                    empty_retries, _MAX_EMPTY_RETRIES, delay, relaxed,
                )
                time.sleep(delay)
                continue

            # This model is spent. Fall through to the next fallback model (a
            # flash model that won't over-think) rather than failing the run.
            if mi < len(models) - 1:
                mi += 1
                nxt = models[mi]
                logger.warning(
                    "gemini EMPTY after %d retries on %s; falling back to %s",
                    _MAX_EMPTY_RETRIES, current, nxt,
                )
                if self.retry_callback:
                    try:
                        self.retry_callback(f"{current} returned no text; retrying with {nxt}.")
                    except Exception:
                        pass
                current = nxt
                empty_retries = 0
                attempt = 0        # give the new model its own transient budget
                continue

            logger.error(
                "gemini empty after all retries and fallbacks: models=%s response_id=%s finish_reason=%s",
                models, _response_id(result), _finish_reason(result),
            )
            raise GeminiError(
                f"Gemini returned an empty response after {_MAX_EMPTY_RETRIES} retry attempts"
                + (f" across {len(models)} models ({', '.join(models)})" if len(models) > 1 else "")
                + ".",
                provider="pw_proxy",
                details=result,
            )

    def generate(self, prompt: str, image_paths: list[Path] | None = None, *, max_output_tokens: int = 16000, temperature: float = 0.15, max_images: int = 8) -> GeminiResponse:
        gen_cfg = {"temperature": temperature, "maxOutputTokens": max_output_tokens}
        # Cap the model's internal thinking so it can't eat the whole output budget
        # (which produces an empty MAX_TOKENS response on image-heavy chunks).
        if _THINKING_BUDGET >= 0:
            gen_cfg["thinkingConfig"] = {"thinkingBudget": _THINKING_BUDGET}
        body = {
            "contents": [{"role": "user", "parts": _build_rest_parts(prompt, image_paths, max_images=max_images)}],
            "generationConfig": gen_cfg,
        }
        dropped = _fit_body_to_budget(body)
        data = self._generate_content(self.model, body)
        text = _extract_text_from_response(data)
        if not text:
            raise GeminiError(
                f"Gemini (via proxy) returned no text. Raw response: {json.dumps(data)[:1000]}",
                provider="pw_proxy",
                details=data,
            )
        usage = _usage_from_mapping(data) or _usage_from_proxy({"usage": data.get("_pw_usage")})
        return GeminiResponse(text=text, provider="pw_proxy",
                              model=data.get("_model_used") or self.model, raw=data,
                              usage=usage, images_dropped=dropped)

    def test_connection(self) -> GeminiResponse:
        return self.generate("Reply with exactly: OK", image_paths=[], max_output_tokens=32, temperature=0.0, max_images=0)

    def generate_image(self, prompt: str, image_path: Path, *, image_model: str = "gemini-2.5-flash-image") -> bytes:
        """Image-to-image generation through the proxy: send a prompt + source
        image, return PNG bytes. Raises GeminiError if no image was produced."""
        body = {
            "contents": [{"role": "user", "parts": _build_rest_parts(prompt, [image_path], max_images=1)}],
            "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
        }
        _fit_body_to_budget(body)
        data = self._generate_content(image_model, body, expect="image")
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                inline = part.get("inline_data") or part.get("inlineData")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        raise GeminiError(
            f"Image generation via proxy returned no image. Raw: {json.dumps(data)[:600]}",
            provider="pw_proxy",
        )


def generate_mock_notes(subject: str, mode: str, language: str, slide_count: int) -> str:
    if language == "hi":
        return f"""Concepts Covered in the Class:\n• Sample concept from uploaded lecture\n• Slide filtering and diagram note handling\n\nSample Heading\n• यह MOCK output है क्योंकि API call नहीं की गई या mock mode चालू है।\n• कुल {slide_count} slides/pages पढ़े गए।\n(Note to DTP: Insert the image with \"sample label 1\" and \"sample label 2\" given on slide no. 1 under the heading \"Sample Heading\".)"""
    return f"""Concepts Covered in the Class:\n• Sample concept from uploaded lecture\n• Slide filtering and diagram note handling\n\nSample Heading\n• This is MOCK output because API call was not used or mock mode is enabled.\n• {slide_count} slides/pages were read.\n(Note to DTP: Insert the image with \"sample label 1\" and \"sample label 2\" given on slide no. 1 under the heading \"Sample Heading\".)"""
