"""Equation-aware slide OCR through the PW proxy's Mathpix provider."""
from __future__ import annotations

import base64
import mimetypes
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pw_access


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _result_text(response: dict) -> str:
    result = response.get("result") or {}
    if not isinstance(result, dict):
        return ""
    return str(result.get("text") or result.get("latex_styled") or "").strip()


def enrich_math_slides(slides, google_token, task_id: str = "", *, max_workers: int = 4):
    """Append Mathpix OCR to image-backed slides and return non-fatal warnings.

    `task_id` is the id of the run this OCR belongs to (see
    pw_access.new_task_id). Every Mathpix call is logged by the proxy as its own
    raw row; sharing the task id lets reporting group them with the run's Gemini
    calls."""
    targets = [s for s in slides if s.image_path and Path(s.image_path).exists()]
    if not targets:
        return []

    warnings: list[str] = []

    def _ocr(slide):
        path = Path(slide.image_path)
        response = pw_access.mathpix_ocr(
            google_token,
            request={
                "src": _data_uri(path),
                "formats": ["text", "latex_styled"],
                "math_inline_delimiters": ["\\(", "\\)"],
                "math_display_delimiters": ["\\[", "\\]"],
                "include_line_data": False,
            },
            filename=path.name,
            count=1,
            task_id=task_id,
        )
        return slide, _result_text(response)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as executor:
        futures = [executor.submit(_ocr, slide) for slide in targets]
        for future in as_completed(futures):
            try:
                slide, text = future.result()
                if not text:
                    warnings.append(f"Math OCR returned no text for slide {slide.slide_no}; Gemini vision will read it.")
                    continue
                slide.metadata["mathpix_text"] = text
                # Build on cleaned_text, NOT raw slide.text: the slide filter
                # strips MCQ options/question blocks into cleaned_text, and
                # rebuilding from the raw text re-inserted that stripped content
                # into the AI prompt on every OCR-enriched chunk.
                original = (slide.cleaned_text or slide.text or "").strip()
                slide.cleaned_text = f"{original}\n\n[EQUATION OCR]\n{text}".strip()
            except Exception as exc:
                warnings.append(f"Math OCR failed for one slide ({type(exc).__name__}); Gemini vision will read it.")
    return warnings
