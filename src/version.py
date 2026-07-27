"""Central app identity + India-time helpers.

One place for the app name, version and timezone so the frontend header, the
footer, and every run_log.json agree. IST has no daylight saving, so a fixed
UTC+5:30 offset is always correct and needs no IANA tz database (which Windows
lacks unless the `tzdata` package is installed) — we still prefer zoneinfo when
it is available.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

# Ensure .env is loaded before we read APP_VERSION/APP_TIMEZONE, whatever the
# import order (config.py also loads it; load_dotenv is idempotent).
try:  # pragma: no cover
    from .config import ENV_PATH  # importing config runs load_dotenv(ENV_PATH)

    _ = ENV_PATH
except Exception:  # pragma: no cover
    pass

APP_NAME = os.getenv("APP_NAME", "Handwritten Notes Automation")
APP_VERSION = os.getenv("APP_VERSION", "v2.2.0")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Kolkata")

# IST is a fixed +05:30 offset (no DST) — a reliable fallback on any OS.
_IST_FIXED = timezone(timedelta(hours=5, minutes=30), name="IST")


def _tz():
    """The configured timezone, via zoneinfo when possible, else fixed IST."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(APP_TIMEZONE)
    except Exception:
        return _IST_FIXED


def now() -> datetime:
    """Timezone-aware 'now' in the app timezone (Asia/Kolkata by default)."""
    return datetime.now(_tz())


def format_date(dt: datetime | None = None) -> str:
    """DD-MM-YYYY."""
    return (dt or now()).strftime("%d-%m-%Y")


def format_time(dt: datetime | None = None) -> str:
    """hh:mm:ss AM/PM IST."""
    dt = dt or now()
    return dt.strftime("%I:%M:%S %p").lstrip("0") + " IST"


def format_datetime(dt: datetime | None = None) -> str:
    """DD-MM-YYYY hh:mm:ss AM/PM IST — used in the footer and run_log.json."""
    dt = dt or now()
    return f"{format_date(dt)} {format_time(dt)}"


def iso_now() -> str:
    """Machine-readable timestamp for logs (keeps the offset)."""
    return now().isoformat(timespec="seconds")


def footer_text() -> str:
    return f"Generated with {APP_NAME} {APP_VERSION} | {format_datetime()}"
