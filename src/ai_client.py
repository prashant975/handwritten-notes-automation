from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .config import extract_api_key
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


def _usage_from_object(obj: Any) -> dict[str, int]:
    usage = getattr(obj, "usage_metadata", None) or getattr(obj, "usageMetadata", None)
    if not usage:
        return {}
    out: dict[str, int] = {}
    attr_map = {
        "prompt_token_count": "tokens_input",
        "candidates_token_count": "tokens_output",
        "total_token_count": "tokens_total",
        "thoughts_token_count": "tokens_thinking",
    }
    for attr, dest in attr_map.items():
        value = getattr(usage, attr, None)
        if isinstance(value, int):
            out[dest] = value
    return out


def _diagnose_http_error(status_code: int | None, body: str) -> str:
    base = f"HTTP {status_code}: {body[:800]}" if status_code else body[:800]
    if status_code in {400, 404}:
        return base + "\nLikely cause: wrong model name, wrong endpoint, or unsupported API version. Try gemini-2.5-flash or provider=developer_rest."
    if status_code in {401, 403}:
        return base + "\nLikely cause: invalid/expired API key, Gemini API not enabled, key restricted incorrectly, or pasted full curl instead of only key. Create/rotate a new AI Studio key."
    if status_code == 429:
        return base + "\nLikely cause: quota/rate limit. Wait, reduce slide images, or use gemini-2.5-flash."
    if status_code and status_code >= 500:
        return base + "\nLikely cause: temporary Google service/server issue. Retry later or switch provider."
    return base


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


def _build_rest_parts(prompt: str, image_paths: list[Path] | None = None, max_images: int = 8) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for image_path in (image_paths or [])[:max_images]:
        if not image_path or not Path(image_path).exists():
            continue
        data = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        parts.append({"inline_data": {"mime_type": image_mime_type(Path(image_path)), "data": data}})
    return parts


class GeminiClient:
    def __init__(self, api_key: str, model: str = "gemini-2.5-pro", provider: str = "auto", timeout: int = 240, max_retries: int = 3):
        self.api_key = extract_api_key(api_key)
        self.model = model.strip() or "gemini-2.5-pro"
        self.provider = provider.strip().lower() or "auto"
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        if not self.api_key:
            raise GeminiError("Gemini API key is empty. Put only the key after ?key= into .env or paste it in the app.")

    def providers_to_try(self) -> list[str]:
        if self.provider == "auto":
            return ["google_genai_sdk", "developer_rest", "aiplatform_rest"]
        return [self.provider]

    def generate(self, prompt: str, image_paths: list[Path] | None = None, *, max_output_tokens: int = 12000, temperature: float = 0.15, max_images: int = 8) -> GeminiResponse:
        errors: list[str] = []
        for provider in self.providers_to_try():
            try:
                if provider == "google_genai_sdk":
                    return self._generate_sdk(prompt, image_paths or [], max_output_tokens, temperature, max_images)
                if provider == "developer_rest":
                    return self._generate_developer_rest(prompt, image_paths or [], max_output_tokens, temperature, max_images)
                if provider == "aiplatform_rest":
                    return self._generate_aiplatform_rest(prompt, image_paths or [], max_output_tokens, temperature, max_images)
                errors.append(f"Unknown provider: {provider}")
            except Exception as e:
                errors.append(f"[{provider}] {e}")
        raise GeminiError("All Gemini providers failed:\n" + "\n\n".join(errors), provider=self.provider)

    def _generate_sdk(self, prompt: str, image_paths: list[Path], max_output_tokens: int, temperature: float, max_images: int) -> GeminiResponse:
        try:
            from google import genai
            from google.genai import types
        except Exception as e:
            raise GeminiError("google-genai SDK is not installed. Run: pip install google-genai", provider="google_genai_sdk") from e
        client = genai.Client(api_key=self.api_key)
        parts = [types.Part.from_text(text=prompt)]
        for image_path in image_paths[:max_images]:
            if Path(image_path).exists():
                parts.append(types.Part.from_bytes(data=Path(image_path).read_bytes(), mime_type=image_mime_type(Path(image_path))))
        contents = [types.Content(role="user", parts=parts)]
        config = types.GenerateContentConfig(temperature=temperature, max_output_tokens=max_output_tokens)
        try:
            response = client.models.generate_content(model=self.model, contents=contents, config=config)
        except TypeError:
            # Older google-genai builds used `generation_config=` instead of `config=`.
            # Fall back to that (still passing the token limit) rather than dropping it,
            # which would silently truncate long notes to the SDK default.
            response = client.models.generate_content(model=self.model, contents=contents, generation_config=config)
        text = getattr(response, "text", "") or ""
        if not text.strip():
            raise GeminiError("SDK returned empty text.", provider="google_genai_sdk")
        return GeminiResponse(text=text.strip(), provider="google_genai_sdk", model=self.model, raw={}, usage=_usage_from_object(response))

    def _post_generate_content(self, url: str, prompt: str, image_paths: list[Path], max_output_tokens: int, temperature: float, max_images: int, provider: str) -> GeminiResponse:
        body = {
            "contents": [{"role": "user", "parts": _build_rest_parts(prompt, image_paths, max_images=max_images)}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_output_tokens},
        }
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}
        resp = None
        for attempt in range(self.max_retries + 1):
            resp = requests.post(url, headers=headers, json=body, timeout=self.timeout)
            # Retry only on rate limiting (429) and transient server errors (5xx).
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < self.max_retries:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait = float(retry_after) if retry_after else 2.0 * (2 ** attempt)
                    except ValueError:
                        wait = 2.0 * (2 ** attempt)
                    time.sleep(min(wait, 30.0))
                    continue
            break
        if resp.status_code >= 400:
            raise GeminiError(_diagnose_http_error(resp.status_code, resp.text), provider=provider, status_code=resp.status_code, details=resp.text)
        try:
            data = resp.json()
        except Exception as e:
            raise GeminiError(f"Gemini returned non-JSON response: {resp.text[:800]}", provider=provider) from e
        text = _extract_text_from_response(data)
        if not text:
            raise GeminiError(f"Gemini returned no text. Raw response: {json.dumps(data)[:1000]}", provider=provider, details=data)
        return GeminiResponse(text=text, provider=provider, model=self.model, raw=data, usage=_usage_from_mapping(data))

    def _generate_developer_rest(self, prompt: str, image_paths: list[Path], max_output_tokens: int, temperature: float, max_images: int) -> GeminiResponse:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        return self._post_generate_content(url, prompt, image_paths, max_output_tokens, temperature, max_images, "developer_rest")

    def _generate_aiplatform_rest(self, prompt: str, image_paths: list[Path], max_output_tokens: int, temperature: float, max_images: int) -> GeminiResponse:
        url = f"https://aiplatform.googleapis.com/v1/publishers/google/models/{self.model}:generateContent?key={self.api_key}"
        return self._post_generate_content(url, prompt, image_paths, max_output_tokens, temperature, max_images, "aiplatform_rest")

    def test_connection(self) -> GeminiResponse:
        return self.generate("Reply with exactly: OK", image_paths=[], max_output_tokens=32, temperature=0.0, max_images=0)

    def generate_image(self, prompt: str, image_path: Path, *, image_model: str = "gemini-2.5-flash-image") -> bytes:
        """Image-to-image generation: send a prompt + source image, return PNG bytes.

        Tries the google-genai SDK first, then the developer REST endpoint. Raises
        GeminiError if no image could be produced.
        """
        errors: list[str] = []
        try:
            return self._generate_image_sdk(prompt, image_path, image_model)
        except Exception as e:
            errors.append(f"[sdk] {e}")
        try:
            return self._generate_image_rest(prompt, image_path, image_model)
        except GeminiError:
            raise
        except Exception as e:
            errors.append(f"[rest] {e}")
        raise GeminiError("Image generation failed:\n" + "\n".join(errors), provider="image")

    def _generate_image_sdk(self, prompt: str, image_path: Path, image_model: str) -> bytes:
        try:
            from google import genai
            from google.genai import types
        except Exception as e:
            raise GeminiError("google-genai SDK is not installed.", provider="image_sdk") from e
        client = genai.Client(api_key=self.api_key)
        parts = [
            types.Part.from_text(text=prompt),
            types.Part.from_bytes(data=Path(image_path).read_bytes(), mime_type=image_mime_type(Path(image_path))),
        ]
        contents = [types.Content(role="user", parts=parts)]
        try:
            config = types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"])
            response = client.models.generate_content(model=image_model, contents=contents, config=config)
        except TypeError:
            response = client.models.generate_content(model=image_model, contents=contents)
        for cand in getattr(response, "candidates", None) or []:
            content = getattr(cand, "content", None)
            for part in (getattr(content, "parts", None) or []):
                inline = getattr(part, "inline_data", None)
                data = getattr(inline, "data", None) if inline else None
                if data:
                    return data if isinstance(data, bytes) else base64.b64decode(data)
        raise GeminiError("SDK returned no image part.", provider="image_sdk")

    def _generate_image_rest(self, prompt: str, image_path: Path, image_model: str) -> bytes:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{image_model}:generateContent?key={self.api_key}"
        body = {
            "contents": [{"role": "user", "parts": _build_rest_parts(prompt, [image_path], max_images=1)}],
            "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
        }
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}
        resp = requests.post(url, headers=headers, json=body, timeout=self.timeout)
        if resp.status_code >= 400:
            raise GeminiError(_diagnose_http_error(resp.status_code, resp.text), provider="image_rest", status_code=resp.status_code)
        data = resp.json()
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                inline = part.get("inline_data") or part.get("inlineData")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        raise GeminiError(f"REST returned no image. Raw: {json.dumps(data)[:600]}", provider="image_rest")


def generate_mock_notes(subject: str, mode: str, language: str, slide_count: int) -> str:
    if language == "hi":
        return f"""Concepts Covered in the Class:\n• Sample concept from uploaded lecture\n• Slide filtering and diagram note handling\n\nSample Heading\n• यह MOCK output है क्योंकि API call नहीं की गई या mock mode चालू है।\n• कुल {slide_count} slides/pages पढ़े गए।\n(Note to DTP: Insert the image with \"sample label 1\" and \"sample label 2\" given on slide no. 1 under the heading \"Sample Heading\".)"""
    return f"""Concepts Covered in the Class:\n• Sample concept from uploaded lecture\n• Slide filtering and diagram note handling\n\nSample Heading\n• This is MOCK output because API call was not used or mock mode is enabled.\n• {slide_count} slides/pages were read.\n(Note to DTP: Insert the image with \"sample label 1\" and \"sample label 2\" given on slide no. 1 under the heading \"Sample Heading\".)"""
