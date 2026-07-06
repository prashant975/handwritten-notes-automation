"""AI redraw of lecture diagrams into a handwritten notes style.

Uses the Gemini image model to convert a slide diagram (usually light text on a
dark slide) into a clean handwritten note: white background, blue handwritten
text/formulas, original diagram-line colours preserved but darkened where they
would vanish on white.
"""
from __future__ import annotations

from pathlib import Path

HANDWRITTEN_PROMPT = (
    "Create a handwritten notes-style image with a white background and blue "
    "handwritten text. The diagram's structure should remain the same as in the "
    "original image. All text and formulas should be handwritten, clear, "
    "high-resolution, and in blue. The diagram lines will retain their original "
    "color, but any light colors that would not be visible on a white background "
    "will be changed to a dark color. The image should be high resolution."
)


def redraw_diagram_handwritten(client, image_path: Path, out_dir: Path, *, image_model: str = "gemini-2.5-flash-image", transparent_bg: bool = True) -> Path | None:
    """Redraw one diagram via AI. Returns the new image path, or None on failure.

    When transparent_bg is True the white background is made transparent so the
    PW watermark shows through the inserted diagram.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        return None
    try:
        data = client.generate_image(HANDWRITTEN_PROMPT, image_path, image_model=image_model)
    except Exception:
        return None
    if not data:
        return None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{image_path.stem}_handwritten.png"
        out_path.write_bytes(data)
        # Validate it's a real image; discard otherwise.
        try:
            from PIL import Image

            with Image.open(out_path) as im:
                im.verify()
        except Exception:
            return None
        if transparent_bg:
            from .image_tools import make_white_transparent

            trans = make_white_transparent(out_path, out_dir / f"{image_path.stem}_handwritten_t.png")
            return trans
        return out_path
    except Exception:
        return None


def redraw_slides_handwritten(client, slides, slide_numbers, out_dir: Path, *, image_model: str = "gemini-2.5-flash-image", max_workers: int = 4) -> tuple[int, list[str]]:
    """Redraw the images of the given slide numbers in place (mutates image_path).

    Redraws run concurrently (network-bound). Returns (count_redrawn, warnings).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    warnings: list[str] = []
    count = 0
    targets = [s for s in slides if s.slide_no in slide_numbers and s.image_path and Path(s.image_path).exists()]
    if not targets:
        return 0, warnings

    def _do(s):
        return s, redraw_diagram_handwritten(client, s.image_path, out_dir, image_model=image_model)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as ex:
        futures = [ex.submit(_do, s) for s in targets]
        for fut in as_completed(futures):
            s, new = fut.result()  # redraw_diagram_handwritten never raises (returns None)
            if new:
                s.image_path = new
                count += 1
            else:
                warnings.append(f"AI redraw failed for slide {s.slide_no}; kept the original diagram image.")
    return count, warnings
