from __future__ import annotations

from pathlib import Path


def make_white_transparent(image_path: Path, out_path: Path | None = None, *, threshold: int = 236, soft: int = 22) -> Path:
    """Turn the (near-)white background of an image transparent.

    Used on the AI-redrawn diagrams so the PW watermark shows through. Dark/coloured
    strokes stay opaque; a soft ramp near the threshold keeps stroke edges smooth.
    Returns the written path (a PNG). On any failure the original path is returned.
    """
    try:
        from PIL import Image
    except Exception:
        return image_path
    try:
        img = Image.open(image_path).convert("RGBA")
        gray = img.convert("L")

        def _alpha(p):
            if p >= threshold:
                return 0
            if p >= threshold - soft:
                return int(255 * (threshold - p) / soft)
            return 255

        alpha = gray.point(_alpha)
        img.putalpha(alpha)
        out = Path(out_path) if out_path else image_path.with_name(image_path.stem + "_t.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out)
        return out
    except Exception:
        return image_path


def _write_logo_pair(logo_gray, out_dir: Path) -> tuple[Path, Path]:
    """Given a clean grayscale (dark strokes on white) logo, write a crisp logo
    plus a faint watermark. `logo_gray` is a PIL 'L' image."""
    from PIL import Image, ImageChops, ImageOps

    logo_gray = ImageOps.autocontrast(logo_gray, cutoff=1)
    # Trim surrounding white margin so the mark fills the header/watermark.
    inverted = ImageChops.invert(logo_gray)
    bbox = inverted.getbbox()
    if bbox:
        pad = max(2, int(min(logo_gray.size) * 0.02))
        left = max(0, bbox[0] - pad)
        top = max(0, bbox[1] - pad)
        right = min(logo_gray.width, bbox[2] + pad)
        bottom = min(logo_gray.height, bbox[3] + pad)
        logo_gray = logo_gray.crop((left, top, right, bottom))
    # Upscale small crops with a high-quality filter for crisper print output.
    target_h = 420
    if logo_gray.height < target_h:
        scale = target_h / logo_gray.height
        logo_gray = logo_gray.resize(
            (max(1, int(logo_gray.width * scale)), target_h), Image.LANCZOS
        )
    logo_rgb = logo_gray.convert("RGB")
    out_dir.mkdir(parents=True, exist_ok=True)
    logo_path = out_dir / "pw_logo.png"
    logo_rgb.save(logo_path, dpi=(300, 300))
    # Faint neutral-grey watermark, enlarged and softened.
    wm = Image.blend(Image.new("RGB", logo_rgb.size, (255, 255, 255)), logo_rgb, 0.13)
    wm_path = out_dir / "pw_watermark.png"
    wm.save(wm_path, dpi=(300, 300))
    return logo_path, wm_path


def load_logo_override(out_dir: Path) -> tuple[Path | None, Path | None]:
    """Use a high-res logo from assets/pw_logo.png if the user supplied one.

    This takes priority over slide extraction, giving the best possible quality.
    """
    try:
        from PIL import Image, ImageOps

        from .config import ROOT_DIR
    except Exception:
        return None, None
    asset = ROOT_DIR / "assets" / "pw_logo.png"
    if not asset.exists():
        return None, None
    try:
        img = Image.open(asset)
        if img.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        gray = ImageOps.grayscale(img.convert("RGB"))
        return _write_logo_pair(gray, out_dir)
    except Exception:
        return None, None


def extract_pw_logo(slide_image_path: Path, out_dir: Path) -> tuple[Path | None, Path | None]:
    """Extract the PW logo from a slide's top-right corner.

    PW slides carry a bright ring + "PW" mark on a dark background. We isolate the
    bright strokes and render an anti-aliased dark-on-white logo (background colour
    is discarded, edges stay smooth). A high-res assets/pw_logo.png overrides this.
    Returns (logo_path, watermark_path), or (None, None) if no compact logo found.
    """
    override = load_logo_override(out_dir)
    if override[0]:
        return override
    try:
        from PIL import Image
    except Exception:
        return None, None
    try:
        img = Image.open(slide_image_path).convert("RGB")
        w, h = img.size
        # The logo lives in the top-right ~18% width x ~22% height region.
        region = img.crop((int(w * 0.82), 0, w, int(h * 0.22)))
        gray = region.convert("L")
        # Bright (logo) pixels on the dark slide background locate the bbox.
        mask = gray.point(lambda p: 255 if p > 110 else 0)
        bbox = mask.getbbox()
        if not bbox:
            return None, None
        left, top, right, bottom = bbox
        bw, bh = right - left, bottom - top
        # A logo is compact and roughly square; reject wide banners / stray text.
        if bw < 24 or bh < 24 or bw > region.width * 0.95:
            return None, None
        aspect = bw / bh if bh else 99
        if aspect < 0.5 or aspect > 2.0:
            return None, None
        pad = 8
        rw, rh = region.size
        box = (max(0, left - pad), max(0, top - pad), min(rw, right + pad), min(rh, bottom + pad))
        # Anti-aliased dark-on-white: invert the grayscale crop (keeps smooth edges).
        crop_gray = gray.crop(box).point(lambda p: 255 - p)
        return _write_logo_pair(crop_gray, out_dir)
    except Exception:
        return None, None


def smart_crop_image(image_path: Path, out_dir: Path, padding: int = 24, tolerance: int = 28) -> Path:
    try:
        from PIL import Image, ImageChops
    except Exception:
        return image_path
    try:
        img = Image.open(image_path).convert("RGB")
        w, h = img.size
        corners = [img.getpixel((0, 0)), img.getpixel((w - 1, 0)), img.getpixel((0, h - 1)), img.getpixel((w - 1, h - 1))]
        bg = tuple(int(sum(c[i] for c in corners) / 4) for i in range(3))
        bg_img = Image.new("RGB", img.size, bg)
        diff = ImageChops.difference(img, bg_img).convert("L")
        mask = diff.point(lambda p: 255 if p > tolerance else 0)
        bbox = mask.getbbox()
        if not bbox:
            return image_path
        left, top, right, bottom = bbox
        left = max(0, left - padding)
        top = max(0, top - padding)
        right = min(w, right + padding)
        bottom = min(h, bottom + padding)
        if (right - left) < w * 0.25 or (bottom - top) < h * 0.25:
            return image_path
        cropped = img.crop((left, top, right, bottom))
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{image_path.stem}_smartcrop.png"
        cropped.save(out_path)
        return out_path
    except Exception:
        return image_path
