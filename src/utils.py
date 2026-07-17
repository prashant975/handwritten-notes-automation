from __future__ import annotations

import json
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Iterable


def make_run_id(prefix: str = "run") -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}"


# Longest stem we allow in generated file names. The full output path is
# RUNS_DIR/run_<ts>_<uuid8>/output/<stem>_Concise_Notes.docx — with a typical
# install prefix that fixed part is already ~130-150 chars, and Windows (and
# Word COM's Documents.Open) cap paths at ~260. 80 chars of stem keeps every
# real path comfortably inside the limit.
_MAX_STEM_LEN = 80


def _cap_stem(name: str) -> str:
    return name[:_MAX_STEM_LEN].rstrip(" ._-") if len(name) > _MAX_STEM_LEN else name


def safe_name(name: str, default: str = "file") -> str:
    # Unicode-aware: keep letters/digits of ANY script plus their combining
    # marks — \w covers Devanagari consonants, and the extra ranges keep the
    # vowel signs/matras (ऀ-ॿ) and combining diacriticals so a Hindi
    # file name survives intact instead of collapsing to "_". Only true
    # separators/punctuation become "_"; "." and "-" are kept.
    name = re.sub(r"[^\w.\-̀-ͯऀ-ॿ]+", "_", name, flags=re.UNICODE).strip("._")
    return _cap_stem(name) or default


def preserve_filename(name: str, default: str = "file") -> str:
    """Keep the uploaded file's name EXACTLY for the output file — spaces, case,
    Unicode (Hindi), dots and dashes are all preserved. Only strips the handful
    of characters that are illegal in a Windows/most filesystem file name
    (<>:"/\\|?*), collapses runs of whitespace, trims trailing dots/spaces, and
    caps very long names so the full output path stays under Windows' ~260-char
    limit (long names otherwise kill the run AFTER the Gemini calls are billed)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", name)
    name = re.sub(r"\s+", " ", name).strip().strip(". ")
    return _cap_stem(name) or default


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def zip_dir(src_dir: Path, zip_path: Path) -> Path:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src_dir.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src_dir))
    return zip_path


def copy_input(input_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Sanitise the stem and keep the real extension, so a run never loses the
    # suffix that _extract_input dispatches on (would otherwise crash).
    dst = dest_dir / (safe_name(input_path.stem) + input_path.suffix.lower())
    shutil.copy2(input_path, dst)
    return dst


def image_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


def chunked(items: list, size: int) -> Iterable[list]:
    if size <= 0:
        size = len(items) or 1
    for i in range(0, len(items), size):
        yield items[i:i + size]


def first_nonempty_line(text: str, max_len: int = 120) -> str:
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if len(s) <= max_len:
            return s
        return s[:max_len].rstrip() + "..."
    return ""
