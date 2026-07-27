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
DEFAULT_MODEL = (os.getenv("MODEL_NAME") or os.getenv("GEMINI_MODEL", "gemini-3.5-flash")).strip() or "gemini-3.5-flash"
DEFAULT_IMAGE_MODEL = (os.getenv("IMAGE_MODEL_NAME") or os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")).strip() or "gemini-2.5-flash-image"

RUNS_DIR = Path(os.getenv("RUNS_DIR", str(ROOT_DIR / "runs"))).expanduser()
OUTPUTS_DIR = Path(os.getenv("OUTPUT_DIR", str(ROOT_DIR / "outputs"))).expanduser()
APP_NAME = os.getenv("APP_NAME", "Handwritten Notes Automation")
# Build stamp — shown in the sidebar so stale team installs are identifiable at
# a glance. Bump when sharing a new team build. The semantic app version shown
# in the header ("Version: v2.1.0") lives in src/version.py.
APP_VERSION = "2026.07.27"
DEBUG = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes", "y"}

SUPPORTED_EXTENSIONS = {".pdf", ".pptx", ".ppt"}
LANGUAGE_CODES = {"English": "en", "Hindi": "hi", "en": "en", "hi": "hi"}
SUBJECTS = ["biology", "physics", "chemistry", "mathematics"]
MODES = ["complete", "summary"]

# Target exam -> the prompts/<value>_concise_en.txt template used for concise
# notes. "" means "no exam-specific prompt, use the subject template".
EXAMS = {"JEE": "jee", "NEET": "neet", "JEE + NEET": "jee_neet"}

# How maths inside [[MATH_INLINE:]] / [[MATH_BLOCK:]] tags is written to DOCX.
MATH_RENDER_MODES = {
    "Native Word Equation / OMML": "omml",
    "Unicode fallback": "unicode",
    "Plain text debug": "plain",
}
DEFAULT_MATH_RENDER_MODE = "omml"

# Processing speed modes (see src/model_router.py). "Auto" is the default: the
# app health-checks the models and picks Fast/Balanced for you.
PROCESSING_MODES = {
    "Auto (Recommended)": "auto",
    "Fast Mode": "fast",
    "Balanced Mode": "balanced",
    "High Quality Mode": "high_quality",
}
DEFAULT_PROCESSING_MODE = (os.getenv("PROCESSING_MODE", "high_quality") or "high_quality").strip().lower()
# "auto" = router picks the model per task; "manual" = user chooses in the UI.
MODEL_ROUTING_MODE = (os.getenv("MODEL_ROUTING_MODE", "auto") or "auto").strip().lower()
