from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

ROOT_DIR = Path(os.getenv("HANDWRITTEN_NOTES_ROOT", str(Path(__file__).resolve().parents[1]))).expanduser()
ENV_PATH = Path(os.getenv("HANDWRITTEN_NOTES_ENV", str(ROOT_DIR / ".env"))).expanduser()
if load_dotenv:
    load_dotenv(ENV_PATH)


# AI provider keys no longer live in this app. All Gemini calls go through the
# shared PW proxy (see pw_access.py), which holds GEMINI_API_KEY on its side.
# The env vars below only pick which model the proxy should call — they are NOT
# secrets. They are named without a provider prefix so the onboarding key-scan
# never mistakes them for a leaked key.
DEFAULT_MODEL = (os.getenv("MODEL_NAME") or os.getenv("GEMINI_MODEL", "gemini-2.5-pro")).strip() or "gemini-2.5-pro"
DEFAULT_IMAGE_MODEL = (os.getenv("IMAGE_MODEL_NAME") or os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")).strip() or "gemini-2.5-flash-image"

RUNS_DIR = Path(os.getenv("RUNS_DIR", str(ROOT_DIR / "runs"))).expanduser()
OUTPUTS_DIR = Path(os.getenv("OUTPUT_DIR", str(ROOT_DIR / "outputs"))).expanduser()
APP_NAME = os.getenv("APP_NAME", "Handwritten Notes Automation")
# Shown in the UI so stale team installs are identifiable at a glance.
# Bump when sharing a new team build.
APP_VERSION = "2026.07.18"
DEBUG = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes", "y"}

SUPPORTED_EXTENSIONS = {".pdf", ".pptx", ".ppt"}
LANGUAGE_CODES = {"English": "en", "Hindi": "hi", "en": "en", "hi": "hi"}
SUBJECTS = ["biology", "physics", "chemistry", "mathematics"]
MODES = ["complete", "summary"]
