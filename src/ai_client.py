from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pw_access

from .utils import image_mime_type


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
        google_token: str,
        model: str = "gemini-2.5-pro",
        *,
        session: "pw_access.UsageSession | None" = None,
        timeout: int = 300,
    ):
        self.google_token = (google_token or "").strip()
        self.model = model.strip() or "gemini-2.5-pro"
        self.session = session
        self.timeout = timeout
        if not self.google_token:
            raise GeminiError(
                "No Google token available. Sign in with your @pw.live Google "
                "account before generating notes (the PW proxy needs it)."
            )

    def _generate_content(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        # Retry transient Vertex errors (rate limit / 5xx) with backoff. Usage is
        # only accumulated on a 200 inside pw_access, so retries never double-log.
        resp = None
        for attempt in range(4):
            try:
                resp = pw_access.gemini_generate(
                    self.google_token,
                    model=model,
                    request=body,
                    session=self.session,
                )
                break
            except pw_access.PWAccessError as e:
                transient = any(f" {code}" in str(e) for code in (429, 500, 502, 503, 504))
                if transient and attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise GeminiError(str(e), provider="pw_proxy") from e
        result = resp.get("result")
        if not isinstance(result, dict):
            raise GeminiError(
                f"Proxy returned no Gemini result. Response: {json.dumps(resp)[:600]}",
                provider="pw_proxy",
                details=resp,
            )
        # Stash the proxy-level usage so callers can fall back to it.
        result.setdefault("_pw_usage", resp.get("usage") or {})
        return result

    def generate(self, prompt: str, image_paths: list[Path] | None = None, *, max_output_tokens: int = 12000, temperature: float = 0.15, max_images: int = 8) -> GeminiResponse:
        body = {
            "contents": [{"role": "user", "parts": _build_rest_parts(prompt, image_paths, max_images=max_images)}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_output_tokens},
        }
        _fit_body_to_budget(body)
        data = self._generate_content(self.model, body)
        text = _extract_text_from_response(data)
        if not text:
            raise GeminiError(
                f"Gemini (via proxy) returned no text. Raw response: {json.dumps(data)[:1000]}",
                provider="pw_proxy",
                details=data,
            )
        usage = _usage_from_mapping(data) or _usage_from_proxy({"usage": data.get("_pw_usage")})
        return GeminiResponse(text=text, provider="pw_proxy", model=self.model, raw=data, usage=usage)

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
        data = self._generate_content(image_model, body)
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
