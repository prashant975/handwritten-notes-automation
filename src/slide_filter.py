from __future__ import annotations

import re
from copy import deepcopy
from .models import SlideData

HOUSEKEEPING_PATTERNS = [
    r"\bthank\s*you\b", r"\bwelcome\b", r"\bhomework\b", r"\bscan\s*(qr|code)\b", r"\bqr\s*code\b",
    r"\bdownload\s+.*\bapp\b", r"\bmaster\s+ncert\s+with\s+.*app\b", r"\btelegram\b", r"\bsubscribe\b",
    r"\byakeen\s+neet\s+module\b", r"\bexercise\s*[-–]\s*\d+\b",
]

HOUSEKEEPING_PATTERNS.extend([
    r"\blet[’']?s\s+grow\s+together\b", r"\bfollow\s+(me|us)\b", r"\bsocial\s+media\b",
    r"\binstagram\b", r"\bwhats\s*app\b", r"@[a-z0-9_.]{3,}",
    r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}",
])

QUESTION_PATTERNS = [
    r"\bASQ\s*[-:]?\s*\d+\b", r"\bMCQ\s*[-:]?\s*\d*\b",
    r"\bpractice\s+(question|problem)\b", r"\bquestion\s*[-:#]?\s*\d+\b",
    r"\b(single|multiple)\s+correct\b", r"\bchoose\s+the\s+correct\b",
    r"\bthen\s+the\s+value\s+of\b", r"\bthe\s+value\s+of\s+(?:the\s+)?(?:expression|following)\b",
    r"\bis\s+equal\s+to\s*[:?]", r"\boptions?\s*[:\-]",
]


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, flags=re.I | re.M) for p in patterns)


def unwanted_slide_reason(slide: SlideData, *, strict: bool = True) -> str | None:
    """Classify slides that must never become inserted note images.

    This public classifier is intentionally reused by both the early AI filter
    and the final DOCX insertion gate.  A filtered QR/ASQ slide therefore
    cannot re-enter through a hallucinated ``Note to DTP`` reference.
    """
    text = "\n".join([slide.heading or "", slide.text or "", slide.cleaned_text or ""])
    if _matches_any(text, HOUSEKEEPING_PATTERNS):
        if not re.search(r"topics?\s+to\s+be\s+covered|concepts?\s+covered", text, flags=re.I):
            return "housekeeping/promotion/homework"
    if strict and _matches_any(text, QUESTION_PATTERNS):
        return "question/MCQ/worked-example slide"
    if slide.filtered and slide.filter_reason:
        return slide.filter_reason
    return None


def _strip_question_blocks(text: str) -> tuple[str, bool]:
    if not text.strip():
        return text, False
    lines = text.splitlines()
    kept: list[str] = []
    removed = False
    in_question = False
    for line in lines:
        s = line.strip()
        if not s:
            if not in_question:
                kept.append(line)
            continue
        if re.search(r"^question$|\bcorrect\s+statement\b|\bincorrect\s+statement\b", s, flags=re.I):
            in_question = True
            removed = True
            continue
        if in_question:
            if not re.match(r"^\(?[A-D]\)?[.)]?\s+", s, flags=re.I) and len(s) > 35 and not re.search(r"\b(answer|solution)\b", s, flags=re.I):
                in_question = False
                kept.append(line)
            else:
                removed = True
            continue
        if re.match(r"^\(?[A-D]\)?[.)]?\s+", s, flags=re.I):
            removed = True
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    m = re.search(r"\bQuestion\b", cleaned, flags=re.I)
    if m and m.start() > 80:
        cleaned = cleaned[:m.start()].strip()
        removed = True
    return cleaned, removed


def filter_slides(slides: list[SlideData], strict: bool = True) -> tuple[list[SlideData], list[dict]]:
    kept: list[SlideData] = []
    report: list[dict] = []
    for slide in slides:
        s = deepcopy(slide)
        reason = unwanted_slide_reason(s, strict=strict)
        cleaned, removed_questions = _strip_question_blocks(s.text)
        s.cleaned_text = cleaned if strict else (s.text or cleaned)
        if reason:
            s.filtered = True
            s.filter_reason = reason
        elif strict and removed_questions and not cleaned.strip():
            s.filtered = True
            s.filter_reason = "pure question/problem slide"
        elif strict and not cleaned.strip() and not s.image_path:
            s.filtered = True
            s.filter_reason = "empty slide"
        else:
            kept.append(s)
        report.append({"slide_no": s.slide_no, "heading": s.heading, "filtered": s.filtered, "reason": s.filter_reason, "had_question_text_removed": removed_questions, "has_image": bool(s.image_path), "text_chars": len(s.text or ""), "cleaned_chars": len(s.cleaned_text or "")})
    return kept, report
