from __future__ import annotations

from pathlib import Path

from .dtp_parser import find_dtp_notes
from .models import SlideData


def quality_check(notes_text: str, slides: list[SlideData], docx_path: Path | None, pdf_path: Path | None, *, send_images_to_ai: bool = True) -> list[str]:
    warnings: list[str] = []
    if "Concepts Covered" not in notes_text:
        warnings.append("Missing 'Concepts Covered in the Class' section.")
    if len(notes_text.strip()) < 100:
        warnings.append("Generated notes look too short. Check API output or extraction.")
    notes = find_dtp_notes(notes_text)
    slide_nums = {s.slide_no for s in slides}
    for note in notes:
        if not note.slide_no:
            warnings.append(f"DTP note missing slide number: {note.raw[:120]}")
        elif note.slide_no not in slide_nums:
            warnings.append(f"DTP note references slide {note.slide_no}, but that slide was not found.")
    if not docx_path or not docx_path.exists():
        warnings.append("DOCX was not created.")
    if not pdf_path or not pdf_path.exists():
        warnings.append("PDF was not created. Install LibreOffice or Microsoft Word/pywin32 for PDF export.")
    if not any((s.prompt_text or "").strip() for s in slides):
        if send_images_to_ai and any(s.image_path for s in slides):
            warnings.append("No text layer found in the source; notes were generated from slide images (vision).")
        else:
            warnings.append("No extractable text found. Enable 'Send slide images to AI' for scanned/image-only lecture files.")
    return warnings
