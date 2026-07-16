"""Find the separate diagram regions on a slide image.

A slide often carries more than one diagram, and different DTP notes point at
different ones. Detecting the regions lets each note get its own crop, instead
of the whole slide being pasted again for every note that mentions the slide.

The work is done on a downscaled mask (fast), then the boxes are scaled back to
full resolution so the crop stays sharp.
"""
from __future__ import annotations

from pathlib import Path

WORK_WIDTH = 360        # segmentation runs on a mask this wide
MIN_AREA_FRAC = 0.015   # ignore specks: a region must cover >= 1.5% of the slide
MIN_SIDE_FRAC = 0.08    # ...and be at least this fraction of the slide's width/height
FULL_SLIDE_FRAC = 0.90  # a box this large IS the whole slide, not a diagram
MAX_REGIONS = 6
PAD_FRAC = 0.012        # breathing room around each crop


def _content_mask(img, sw: int, sh: int):
    """Binary mask (PIL 'L') of 'this pixel is content, not background'."""
    from PIL import Image, ImageChops

    if img.mode in ("RGBA", "LA"):
        # AI-redrawn diagrams are transparent-backed: alpha IS the content mask.
        alpha = img.convert("RGBA").split()[-1].resize((sw, sh), Image.BILINEAR)
        return alpha.point(lambda p: 255 if p > 16 else 0)
    small = img.convert("RGB").resize((sw, sh), Image.BILINEAR)
    corners = [
        small.getpixel((0, 0)), small.getpixel((sw - 1, 0)),
        small.getpixel((0, sh - 1)), small.getpixel((sw - 1, sh - 1)),
    ]
    bg = tuple(int(sum(c[i] for c in corners) / 4) for i in range(3))
    diff = ImageChops.difference(small, Image.new("RGB", small.size, bg)).convert("L")
    return diff.point(lambda p: 255 if p > 28 else 0)


def _components(arr) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of 4-connected blobs in a 2-D bool array."""
    import numpy as np

    h, w = arr.shape
    seen = np.zeros((h, w), dtype=bool)
    boxes: list[tuple[int, int, int, int]] = []
    for y0 in range(h):
        for x0 in range(w):
            if not arr[y0, x0] or seen[y0, x0]:
                continue
            stack = [(y0, x0)]
            seen[y0, x0] = True
            minx = maxx = x0
            miny = maxy = y0
            while stack:
                cy, cx = stack.pop()
                minx = min(minx, cx); maxx = max(maxx, cx)
                miny = min(miny, cy); maxy = max(maxy, cy)
                for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                    if 0 <= ny < h and 0 <= nx < w and arr[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            boxes.append((minx, miny, maxx + 1, maxy + 1))
    return boxes


def _overlaps(a, b, slack: int) -> bool:
    return not (a[2] + slack < b[0] or b[2] + slack < a[0]
                or a[3] + slack < b[1] or b[3] + slack < a[1])


def _merge(boxes, slack: int):
    """Merge boxes that touch/overlap (within `slack`) until nothing changes."""
    boxes = list(boxes)
    changed = True
    while changed:
        changed = False
        out = []
        while boxes:
            cur = boxes.pop()
            hit = True
            while hit:
                hit = False
                for i, other in enumerate(boxes):
                    if _overlaps(cur, other, slack):
                        cur = (min(cur[0], other[0]), min(cur[1], other[1]),
                               max(cur[2], other[2]), max(cur[3], other[3]))
                        boxes.pop(i)
                        hit = True
                        changed = True
                        break
            out.append(cur)
        boxes = out
    return boxes


def detect_regions(image_path: Path) -> list[tuple[int, int, int, int]]:
    """Return full-resolution boxes of the distinct diagram regions on a slide.

    Reading order (top-to-bottom, left-to-right). Returns [] when the slide is
    one single block of content (caller should then use the whole-slide crop).
    """
    try:
        from PIL import Image, ImageFilter
        import numpy as np
    except Exception:
        return []
    try:
        with Image.open(image_path) as im:
            im.load()
            W, H = im.size
            if W < 40 or H < 40:
                return []
            sw = min(WORK_WIDTH, W)
            sh = max(1, int(H * (sw / W)))
            mask = _content_mask(im, sw, sh)

        # Dilate so the strokes and labels of ONE diagram fuse into ONE blob,
        # while genuinely separate diagrams stay apart.
        mask = mask.filter(ImageFilter.MaxFilter(5))
        mask = mask.filter(ImageFilter.MaxFilter(5))

        arr = np.asarray(mask) > 0
        if not arr.any():
            return []
        boxes = _components(arr)
        boxes = _merge(boxes, slack=max(2, int(sw * 0.02)))

        area_small = sw * sh
        keep = []
        for (l, t, r, b) in boxes:
            bw, bh = r - l, b - t
            if (bw * bh) < MIN_AREA_FRAC * area_small:
                continue
            if bw < MIN_SIDE_FRAC * sw and bh < MIN_SIDE_FRAC * sh:
                continue
            # Drop the PW logo: small mark parked in the top-right corner.
            if l > sw * 0.78 and t < sh * 0.22 and bw < sw * 0.2 and bh < sh * 0.2:
                continue
            keep.append((l, t, r, b))
        if len(keep) < 2:
            return []  # single block -> let the caller use the whole-slide crop

        # If one blob is basically the entire slide, the slide isn't separable.
        for (l, t, r, b) in keep:
            if (r - l) * (b - t) > FULL_SLIDE_FRAC * area_small:
                return []

        keep.sort(key=lambda x: (x[1], x[0]))          # reading order
        keep = keep[:MAX_REGIONS]

        scale = W / sw
        padx, pady = int(W * PAD_FRAC), int(H * PAD_FRAC)
        out = []
        for (l, t, r, b) in keep:
            out.append((
                max(0, int(l * scale) - padx),
                max(0, int(t * scale) - pady),
                min(W, int(r * scale) + padx),
                min(H, int(b * scale) + pady),
            ))
        return out
    except Exception:
        return []


def crop_region(image_path: Path, box, out_dir: Path, tag: str) -> Path | None:
    """Crop `box` out of the slide image, preserving transparency. None on failure."""
    try:
        from PIL import Image

        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = Path(out_dir) / f"{Path(image_path).stem}_{tag}.png"
        with Image.open(image_path) as im:
            im.load()
            im.crop(tuple(box)).save(out_path)
        return out_path
    except Exception:
        return None
