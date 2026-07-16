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
from .image_regions import crop_region, detect_regions
from .image_tools import extract_pw_logo, smart_crop_image
from .models import SlideData

BODY_FONT = "Kalam"
BODY_SIZE = Pt(16)
HEADING_SIZE = Pt(18)
DARK_GREY = RGBColor(0x00, 0x00, 0x99)   # body text (#000099)
BOLD_COLOR = RGBColor(0xE9, 0x71, 0x32)  # bold words (#E97132)
BULLET_PREFIXES = ("•", "·", "-", "–")
BULLET_MARKERS = ("•", "◦", "▪", "–")
CHAR_SPACING_TWIPS = 30  # Expanded character spacing = 1.5 pt (1 pt = 20 twips)


def _set_char_spacing(run, twips: int = CHAR_SPACING_TWIPS):
    rpr = run._element.get_or_add_rPr()
    sp = rpr.find(qn("w:spacing"))
    if sp is None:
        sp = OxmlElement("w:spacing")
        rpr.append(sp)
    sp.set(qn("w:val"), str(twips))


def _set_no_same_style_spacing(p):
    ppr = p._element.get_or_add_pPr()
    if ppr.find(qn("w:contextualSpacing")) is None:
        ppr.append(OxmlElement("w:contextualSpacing"))


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
    try:
        p.style = "Body Text"
    except Exception:
        pass
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.line_spacing = 1.2
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    _set_no_same_style_spacing(p)
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

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "theta": "θ", "lambda": "λ", "mu": "μ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "phi": "φ", "omega": "ω",
}
_GREEK_UPPER = {
    "delta": "Δ", "omega": "Ω", "sigma": "Σ", "phi": "Φ", "theta": "Θ",
    "lambda": "Λ", "pi": "Π", "gamma": "Γ",
}
_GREEK_RE = re.compile(r"\b(" + "|".join(_GREEK) + r")\b", re.I)


def _greek_sub(m):
    """'theta' -> θ but 'Delta' -> Δ (capitalised word = capital letter)."""
    word = m.group(1)
    low = word.lower()
    if word[0].isupper() and low in _GREEK_UPPER:
        return _GREEK_UPPER[low]
    return _GREEK[low]

# LaTeX command -> symbol. Models emit LaTeX for maths even when told not to,
# so it is normalised here rather than trusted away in the prompt.
_LATEX_SYMBOLS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "equiv": "≡", "propto": "∝", "infty": "∞",
    "rightarrow": "→", "to": "→", "leftarrow": "←", "Rightarrow": "⇒",
    "sum": "Σ", "prod": "Π", "int": "∫", "partial": "∂", "nabla": "∇",
    "degree": "°", "circ": "°", "angle": "∠", "perp": "⊥", "parallel": "∥",
    "therefore": "∴", "because": "∵", "sqrt": "√",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "Delta": "Δ",
    "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ",
    "Theta": "Θ", "iota": "ι", "kappa": "κ", "lambda": "λ", "Lambda": "Λ",
    "mu": "μ", "nu": "ν", "xi": "ξ", "rho": "ρ", "sigma": "σ", "Sigma": "Σ",
    "tau": "τ", "upsilon": "υ", "phi": "φ", "Phi": "Φ", "chi": "χ",
    "psi": "ψ", "Psi": "Ψ", "omega": "ω", "Omega": "Ω", "pi": "π", "Pi": "Π",
}

# $..$, $$..$$, \(..\), \[..\] wrappers around a formula.
_MATH_DELIM_RE = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$|\\\((.+?)\\\)|\\\[(.+?)\\\]", re.S)
# \frac{a}{b}, \sqrt{x}, \sqrt[n]{x}, \vec{v}, \text{x}
_FRAC_RE = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT_BRACE_RE = re.compile(r"\\sqrt\s*(?:\[([^\]]*)\])?\s*\{([^{}]*)\}")
_VEC_RE = re.compile(r"\\(?:vec|overrightarrow|overline|hat|bar)\s*\{([^{}]*)\}")
_WRAP_RE = re.compile(r"\\(?:text|textbf|textit|mathrm|mathbf|mathit|operatorname)\s*\{([^{}]*)\}")
_LATEX_CMD_RE = re.compile(r"\\([A-Za-z]+)")

# A "*" that means multiplication: either tight between operands ("2*v_A") or
# spaced on BOTH sides ("a * b"). Markdown emphasis never looks like that — the
# opening "*" of *italic* has no operand before it and no space after it — so
# **bold** / *italic* survive for _MD_RE. This MUST run before the markdown
# split, otherwise "2*v_A*v_B" is read as italics and the asterisks are eaten.
_MULT_RE = re.compile(r"(?<=[0-9A-Za-z_)\]])(?:\*|\s+\*\s+)(?=[0-9A-Za-z_(\[√])")

# "v_AB", "v_A^2", "x_{net}" -> base + sub/superscript token.
_SCRIPT_RE = re.compile(r"([_^])(\{[^}]{1,40}\}|[A-Za-z0-9]+)")


def _normalize_math(text: str) -> str:
    """Normalise any maths notation the model emits into plain readable symbols.

    Handles LaTeX (\\frac, \\sqrt, \\theta, $...$), ASCII (sqrt(), *, theta,
    <=) and already-correct Unicode, so the result only ever uses real symbols
    plus '_'/'^' markers, which _add_scripted_runs turns into sub/superscripts.
    Runs BEFORE the markdown split so formula asterisks are never read as italics.
    """
    # LaTeX wrappers / delimiters / spacing.
    text = text.replace("\\\\", " ")
    text = re.sub(r"`([^`]*)`", r"\1", text)          # `v_AB` code ticks
    text = _MATH_DELIM_RE.sub(lambda m: next(g for g in m.groups() if g is not None), text)
    text = re.sub(r"\\left\s*|\\right\s*", "", text)
    text = re.sub(r"\\[,;:!>]", " ", text)
    # Structural LaTeX, innermost-first (a few passes handles simple nesting).
    for _ in range(4):
        new = _WRAP_RE.sub(r"\1", text)
        new = _VEC_RE.sub(r"\1", new)
        new = _FRAC_RE.sub(r"(\1)/(\2)", new)
        new = _SQRT_BRACE_RE.sub(lambda m: f"√({m.group(2)})", new)
        if new == text:
            break
        text = new
    # Remaining LaTeX commands -> symbols. An unknown command just loses its
    # backslash (\cos -> cos), since a stray backslash must never reach the page.
    text = _LATEX_CMD_RE.sub(lambda m: _LATEX_SYMBOLS.get(m.group(1), m.group(1)), text)
    # ASCII maths.
    text = re.sub(r"\bsqrt\s*\(", "√(", text, flags=re.I)
    text = _MULT_RE.sub("×", text)
    text = _GREEK_RE.sub(_greek_sub, text)
    for src, dst in (("<=", "≤"), (">=", "≥"), ("!=", "≠"), ("+/-", "±"),
                     ("->", "→"), ("&gt;", ">"), ("&lt;", "<")):
        text = text.replace(src, dst)
    return text


def _add_scripted_runs(p, clean: str, *, size, color, bold, italic, underline, highlight):
    """Add `clean` as runs, rendering v_AB / x^2 as real sub/superscript runs."""

    def _emit(chunk: str, *, sub: bool = False, sup: bool = False):
        if not chunk:
            return
        run = p.add_run(chunk)
        _set_run_font(run, size=size, bold=bold, italic=italic, underline=underline, color=color)
        if sub:
            run.font.subscript = True
        if sup:
            run.font.superscript = True
        if highlight is not None:
            run.font.highlight_color = highlight

    pos = 0
    for m in _SCRIPT_RE.finditer(clean):
        _emit(clean[pos:m.start()])
        token = m.group(2)
        if token.startswith("{"):
            token = token[1:-1]
        _emit(token, sub=m.group(1) == "_", sup=m.group(1) == "^")
        pos = m.end()
    _emit(clean[pos:])


def _add_markdown_runs(p, text: str, *, size=BODY_SIZE, base_color=DARK_GREY, default_bold: bool = False, underline: bool = False, highlight=None):
    for part in _MD_RE.split(_normalize_math(text)):
        if part == "":
            continue
        bold = default_bold
        italic = False
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
        _add_scripted_runs(
            p, clean, size=size,
            color=BOLD_COLOR if bold and not default_bold else base_color,
            bold=bold, italic=italic, underline=underline, highlight=highlight,
        )


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
    clean = line.strip().strip("#").strip()
    _add_markdown_runs(
        p,
        clean,
        size=HEADING_SIZE,
        base_color=RGBColor(0, 0, 0),
        default_bold=True,
        underline=True,
        highlight=WD_COLOR_INDEX.BRIGHT_GREEN,
    )
    return p


def _add_blank_line(doc: Document):
    p = doc.add_paragraph()
    _set_paragraph_base(p)
    return p


def _add_body(doc: Document, line: str, *, bullet: bool = False, indent_level: int = 0):
    p = doc.add_paragraph()
    _set_paragraph_base(p, bullet=bullet, indent_level=indent_level)
    if bullet:
        s = line.strip()
        # Normalise bullet markers and make nested bullets visibly different.
        m = re.match(r"^(\*|•|·|-|–)\s+(.*)$", s)
        if m:
            marker = BULLET_MARKERS[min(max(0, indent_level), len(BULLET_MARKERS) - 1)]
            s = f"{marker}\t{m.group(2)}"
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


def _pick_dtp_image(note, slide, run_dir: Path, region_cache: dict, used: dict):
    """Choose which image this DTP note should get.

    A slide often holds several diagrams and several notes point at the same
    slide. Give each note its own region (in reading order) so different parts
    land at their own place, instead of re-pasting the whole slide every time.
    Returns (path, already_cropped) or (None, False) when there is nothing left
    to add — the caller then skips rather than repeating an image.
    """
    src = Path(slide.image_path)
    if src not in region_cache:
        region_cache[src] = detect_regions(src)
    regions = region_cache[src]
    idx = used.get(src, 0)

    if len(regions) > 1:
        if idx >= len(regions):
            return None, False           # every region used -> don't repeat one
        crop = crop_region(src, regions[idx], run_dir / "inserted_images",
                           f"r{idx + 1}")
        used[src] = idx + 1
        if crop:
            return crop, True
        return (src, False) if idx == 0 else (None, False)

    # One indivisible diagram: insert it once, never again.
    if idx:
        return None, False
    used[src] = 1
    return src, False


def _insert_slide_image(doc: Document, image_path: Path, run_dir: Path, mode: str = "smart_crop", already_cropped: bool = False):
    if not image_path or not Path(image_path).exists():
        return False
    img = Path(image_path)
    # Skip smart-crop for transparent (AI-redrawn) images: cropping flattens the
    # alpha channel and we'd lose the see-through background over the watermark.
    # Region crops are already tight, so they are inserted as-is.
    if mode == "smart_crop" and not already_cropped and not _has_alpha(img):
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
    content_added = False
    region_cache: dict = {}   # slide image -> detected diagram regions
    used_regions: dict = {}   # slide image -> how many of its regions are placed
    for raw_line in notes_text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if _is_dtp(line):
            note = parse_dtp_note(line)
            _add_dtp(doc, line, dtp_note_policy)
            if dtp_note_policy in {"keep_note_and_insert_image", "hide_note_insert_image"}:
                if note and note.slide_no and note.slide_no in slide_map and slide_map[note.slide_no].image_path:
                    img, cropped = _pick_dtp_image(
                        note, slide_map[note.slide_no], run_dir, region_cache, used_regions
                    )
                    if img is None:
                        warnings.append(
                            f"Slide {note.slide_no} has no unused diagram left for this note, "
                            "so the image was not repeated here."
                        )
                    else:
                        ok = _insert_slide_image(doc, img, run_dir, mode=image_insert_mode,
                                                 already_cropped=cropped)
                        if ok:
                            inserted_images += 1
                        else:
                            warnings.append(f"Could not insert image for slide {note.slide_no}.")
                else:
                    warnings.append(f"DTP note found but slide image could not be matched: {line[:140]}")
            content_added = True
            continue
        if _is_heading(line):
            if content_added:
                _add_blank_line(doc)
            _add_heading(doc, line)
            content_added = True
        elif _is_bullet(line):
            leading = len(raw_line) - len(raw_line.lstrip(" \t"))
            indent_level = min(3, leading // 4) if leading else 0
            _add_body(doc, line, bullet=True, indent_level=indent_level)
            content_added = True
        else:
            _add_body(doc, line, bullet=False)
            content_added = True
    doc.save(str(output_path))
    warnings.append(f"Inserted {inserted_images} image(s) from DTP notes.")
    if not is_kalam_installed():
        warnings.append("Kalam handwritten font is not installed, so the notes render in a fallback font. Install Kalam (https://fonts.google.com/specimen/Kalam) for the handwritten PW style.")
    return output_path, warnings
