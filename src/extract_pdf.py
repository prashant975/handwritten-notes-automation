from __future__ import annotations

from pathlib import Path

from .models import SlideData
from .utils import ensure_dir, first_nonempty_line


def extract_pdf(pdf_path: Path, run_dir: Path, render_scale: float = 2.0) -> list[SlideData]:
    try:
        import fitz  # PyMuPDF
    except Exception as e:
        raise RuntimeError("PyMuPDF is not installed. Run: pip install pymupdf") from e

    render_dir = ensure_dir(run_dir / "rendered")
    slides: list[SlideData] = []
    doc = fitz.open(str(pdf_path))
    matrix = fitz.Matrix(render_scale, render_scale)
    for idx, page in enumerate(doc, start=1):
        text = page.get_text("text") or ""
        image_path = render_dir / f"slide_{idx:03d}.png"
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        pix.save(str(image_path))
        slides.append(SlideData(slide_no=idx, heading=first_nonempty_line(text), text=text, cleaned_text=text, image_path=image_path, source_type="pdf", metadata={"page_index": idx - 1}))
    doc.close()
    return slides
