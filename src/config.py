from __future__ import annotations

import os
import re
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"
if load_dotenv:
    load_dotenv(ENV_PATH)


def extract_api_key(value: str | None) -> str:
    """Accept raw key, full URL, or pasted curl and return only the API key."""
    if not value:
        return ""
    v = str(value).strip().strip('"').strip("'")
    if not v:
        return ""
    v = v.replace("\\\n", " ").replace("\n", " ").strip()
    m = re.search(r"[?&]key=([^\s'\"&]+)", v)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    m = re.search(r"x-goog-api-key\s*[:=]\s*([^\s'\"]+)", v, flags=re.I)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    m = re.search(r"(?:GEMINI_API_KEY|GOOGLE_API_KEY)\s*=\s*([^\s'\"]+)", v, flags=re.I)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    return v


def mask_secret(value: str | None) -> str:
    key = extract_api_key(value)
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


GEMINI_API_KEY = extract_api_key(
    os.getenv("GEMINI_API_KEY")
    or os.getenv("GOOGLE_API_KEY")
    or os.getenv("GEMINI_CURL")
    or os.getenv("GEMINI_URL")
)
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro").strip() or "gemini-2.5-pro"
DEFAULT_PROVIDER = os.getenv("GEMINI_PROVIDER", "auto").strip().lower() or "auto"
DEFAULT_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image").strip() or "gemini-2.5-flash-image"

RUNS_DIR = Path(os.getenv("RUNS_DIR", str(ROOT_DIR / "runs"))).expanduser()
OUTPUTS_DIR = Path(os.getenv("OUTPUT_DIR", str(ROOT_DIR / "outputs"))).expanduser()
APP_NAME = os.getenv("APP_NAME", "Handwritten Notes Automation")
DEBUG = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes", "y"}

SUPPORTED_EXTENSIONS = {".pdf", ".pptx", ".ppt"}
LANGUAGE_CODES = {"English": "en", "Hindi": "hi", "en": "en", "hi": "hi"}
SUBJECTS = ["biology", "physics", "chemistry"]
MODES = ["complete", "summary"]
PROVIDERS = ["auto", "google_genai_sdk", "developer_rest", "aiplatform_rest"]
