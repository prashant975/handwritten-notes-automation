from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor

from .docx_layout import add_title_block, add_watermark, is_kalam_installed, setup_footer, setup_header
from .dtp_parser import parse_dtp_note
from .image_tools import extract_pw_logo, smart_crop_image
from .models import SlideData

BODY_FONT = "Kalam"
BODY_SIZE = Pt(16)
HEADING_SIZE = Pt(18)
DARK_GREY = RGBColor(0x00, 0x00, 0x99)   # body text (#000099)
BOLD_COLOR = RGBColor(0xE9, 0x71, 0x32)  # bold words (#E97132)
BULLET_PREFIXES = ("•", "·", "-", "–")
CHAR_SPACING_TWIPS = 30  # Expanded character spacing = 1.5 pt (1 pt = 20 twips)


def _set_char_spacing(run, twips: int = CHAR_SPACING_TWIPS):
    rpr = run._element.get_or_add_rPr()
    sp = rpr.find(qn("w:spacing"))
    if sp is None:
        sp = OxmlElement("w:spacing")
        rpr.append(sp)
    sp.set(qn("w:val"), str(twips))


def _set_run_font(run, *, size=BODY_SIZE, bold=False, italic=False, underline=False, color=DARK_GREY):
    run.font.name = BODY_FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    run._element.rPr.rFonts.set(qn("w:cs"), BODY_FONT)
    run.font.size = size
    run.font.bold = bold
    run.font.italic = italic
    run.font.underline = underline
    run.font.color.rgb = color
    _set_char_spacing(run)


def _set_paragraph_base(p, *, bullet: bool = False, indent_level: int = 0):
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.line_spacing = 1.0
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    if bullet:
        # Each nesting level shifts the bullet right by ~0.7 cm while keeping the
        # hanging indent so wrapped lines align under the text, not the marker.
        base = 0.6 + 0.7 * max(0, indent_level)
        p.paragraph_format.left_indent = Cm(base)
        p.paragraph_format.first_line_indent = Cm(-0.6)
    else:
        p.paragraph_format.left_indent = Cm(0)
        p.paragraph_format.first_line_indent = Cm(0)


# Order matters: try ***bold-italic*** before **bold** before *italic* so the
# alternation consumes the longest marker first at each position.
_MD_RE = re.compile(r"(\*\*\*.+?\*\*\*|\*\*.+?\*\*|\*.+?\*)")


def _add_markdown_runs(p, text: str, *, size=BODY_SIZE, base_color=DARK_GREY):
    for part in _MD_RE.split(text):
        if part == "":
            continue
        bold = italic = False
        clean = part
        if part.startswith("***") and part.endswith("***") and len(part) >= 6:
            bold = italic = True
            clean = part[3:-3]
        elif part.startswith("**") and part.endswith("**") and len(part) >= 4:
            bold = True
            clean = part[2:-2]
        elif part.startswith("*") and part.endswith("*") and len(part) >= 2:
            italic = True
            clean = part[1:-1]
        run = p.add_run(clean)
        _set_run_font(run, size=size, bold=bold, italic=italic, color=BOLD_COLOR if bold else base_color)


def _is_dtp(line: str) -> bool:
    return "note to dtp" in line.lower()


def _is_bullet(line: str) -> bool:
    s = line.strip()
    if s.startswith(BULLET_PREFIXES):
        return True
    # Markdown-style "* item" bullet (a single asterisk + space), NOT "**bold**".
    if re.match(r"^\*\s+\S", s):
        return True
    return bool(re.match(r"^\s*\d+[.)]\s+", line))


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or _is_bullet(s) or _is_dtp(s):
        return False
    if len(s) > 140:
        return False
    if s.startswith("#"):
        return True
    if s.endswith(".") or s.endswith("।"):
        return False
    return True


def _add_heading(doc: Document, line: str):
    p = doc.add_paragraph()
    _set_paragraph_base(p)
    # Controlled spacing above/below the heading instead of a blank paragraph.
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(4)
    clean = line.strip().strip("#").strip()
    run = p.add_run(clean)
    _set_run_font(run, size=HEADING_SIZE, bold=True, underline=True, color=RGBColor(0, 0, 0))
    run.font.highlight_color = WD_COLOR_INDEX.BRIGHT_GREEN
    return p


def _add_body(doc: Document, line: str, *, bullet: bool = False, indent_level: int = 0):
    p = doc.add_paragraph()
    _set_paragraph_base(p, bullet=bullet, indent_level=indent_level)
    if bullet:
        s = line.strip()
        # Normalise any bullet marker (*, •, ·, -, –) to a "•<tab>" prefix.
        m = re.match(r"^(\*|•|·|-|–)\s+(.*)$", s)
        if m:
            s = "•\t" + m.group(2)
        _add_markdown_runs(p, s)
    else:
        _add_markdown_runs(p, line.strip())
    return p


def _add_dtp(doc: Document, line: str, policy: str):
    if policy == "hide_note_insert_image":
        return None
    p = doc.add_paragraph()
    _set_paragraph_base(p)
    run = p.add_run(line.strip())
    _set_run_font(run, size=BODY_SIZE, bold=False, color=RGBColor(255, 0, 0))
    run.font.highlight_color = WD_COLOR_INDEX.YELLOW
    return p


def _has_alpha(image_path: Path) -> bool:
    try:
        from PIL import Image

        with Image.open(image_path) as im:
            return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    except Exception:
        return False


def _insert_slide_image(doc: Document, image_path: Path, run_dir: Path, mode: str = "smart_crop"):
    if not image_path or not Path(image_path).exists():
        return False
    img = Path(image_path)
    # Skip smart-crop for transparent (AI-redrawn) images: cropping flattens the
    # alpha channel and we'd lose the see-through background over the watermark.
    if mode == "smart_crop" and not _has_alpha(img):
        img = smart_crop_image(img, run_dir / "inserted_images")
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    try:
        run = p.add_run()
        run.add_picture(str(img), width=Inches(5.8))
        return True
    except Exception:
        return False


def write_notes_docx(notes_text: str, output_path: Path, slides: list[SlideData], *, run_dir: Path, image_insert_mode: str = "smart_crop", dtp_note_policy: str = "keep_note_and_insert_image", subject: str = "", chapter_title: str = "") -> tuple[Path, list[str]]:
    warnings: list[str] = []
    slide_map = {s.slide_no: s for s in slides}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(2.54)
    section.right_margin = Cm(2.54)
    style = doc.styles["Normal"]
    style.font.name = BODY_FONT
    style._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    style._element.rPr.rFonts.set(qn("w:cs"), BODY_FONT)
    style.font.size = BODY_SIZE

    # PW branding: extract the logo from a slide (trying several, since the title
    # slide often lacks a clean top-right mark), then apply the header (page# +
    # logo), footer, and background watermark.
    logo_path = wm_path = None
    candidates = [s.image_path for s in slides if s.image_path and Path(s.image_path).exists()]
    for img in candidates[:6]:
        logo_path, wm_path = extract_pw_logo(Path(img), run_dir / "branding")
        if logo_path:
            break
    try:
        setup_header(section, logo_path)
        setup_footer(section)
        if wm_path:
            add_watermark(section, wm_path)
    except Exception as e:
        warnings.append(f"PW branding (header/footer/watermark) could not be applied: {e}")

    add_title_block(doc, subject, chapter_title)

    inserted_images = 0
    for raw_line in notes_text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if _is_dtp(line):
            note = parse_dtp_note(line)
            _add_dtp(doc, line, dtp_note_policy)
            if dtp_note_policy in {"keep_note_and_insert_image", "hide_note_insert_image"}:
                if note and note.slide_no and note.slide_no in slide_map and slide_map[note.slide_no].image_path:
                    ok = _insert_slide_image(doc, slide_map[note.slide_no].image_path, run_dir, mode=image_insert_mode)
                    if ok:
                        inserted_images += 1
                    else:
                        warnings.append(f"Could not insert image for slide {note.slide_no}.")
                else:
                    warnings.append(f"DTP note found but slide image could not be matched: {line[:140]}")
            continue
        if _is_heading(line):
            _add_heading(doc, line)
        elif _is_bullet(line):
            leading = len(raw_line) - len(raw_line.lstrip(" \t"))
            indent_level = min(3, leading // 4) if leading else 0
            _add_body(doc, line, bullet=True, indent_level=indent_level)
        else:
            _add_body(doc, line, bullet=False)
    doc.save(str(output_path))
    warnings.append(f"Inserted {inserted_images} image(s) from DTP notes.")
    if not is_kalam_installed():
        warnings.append("Kalam handwritten font is not installed, so the notes render in a fallback font. Install Kalam (https://fonts.google.com/specimen/Kalam) for the handwritten PW style.")
    return output_path, warnings
