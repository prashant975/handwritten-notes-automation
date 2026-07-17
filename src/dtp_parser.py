from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DtpNote:
    raw: str
    slide_no: int | None = None
    heading: str | None = None


# Greedy up to the last ')' on the same line so labels containing their own
# parentheses (e.g. "Cell wall (D)") don't truncate the match early.
DTP_RE = re.compile(r"\(\s*Note\s+to\s+DTP\s*:.*\)", re.I)
SLIDE_RE = re.compile(r"(?:slide\s*(?:no\.?|#)?|from\s+slide)\s*[:\-]?\s*(\d+)", re.I)
HEADING_RE = re.compile(r"under\s+the\s+heading\s+[\"“']([^\"”']+)[\"”']", re.I)


def parse_dtp_note(text: str) -> DtpNote | None:
    if "note to dtp" not in text.lower():
        return None
    slide_no = None
    m = SLIDE_RE.search(text)
    if m:
        try:
            slide_no = int(m.group(1))
        except ValueError:
            slide_no = None
    heading = None
    hm = HEADING_RE.search(text)
    if hm:
        heading = hm.group(1).strip()
    return DtpNote(raw=text, slide_no=slide_no, heading=heading)


def find_dtp_notes(text: str) -> list[DtpNote]:
    notes = []
    for m in DTP_RE.finditer(text):
        parsed = parse_dtp_note(m.group(0))
        if parsed:
            notes.append(parsed)
    return notes
