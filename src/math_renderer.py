"""Render LaTeX maths into a DOCX as native Word equations (OMML).

The notes text carries protected maths tags emitted by the AI prompt:

    [[MATH_INLINE: \\vec{A} \\times \\vec{B} = \\vec{C}]]
    [[MATH_BLOCK: a_n = \\frac{v^2}{r} ]]

Everything inside a tag is LaTeX and is rendered by one of three modes:

* ``omml``    - a real Word equation (``<m:oMath>``). Vector arrows, hats,
                fractions and radicals are true equation objects, so they
                survive to PDF and stay editable in Word. This is the default.
* ``unicode`` - a plain-text fallback using Unicode symbols (A⃗, î, H₂O, m s⁻²).
                Used when OMML is turned off or when a formula fails to build.
* ``plain``   - the raw LaTeX, for debugging what the model actually produced.

Word renders equation glyphs from a maths font; the handwritten body font
(Kalam) has no combining arrow/hat glyphs, so maths runs are forced to
``Cambria Math`` regardless of the body font.
"""

from __future__ import annotations

import re

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

# Font used for every maths run. Kalam lacks the combining arrow/hat glyphs, so
# forcing it on an equation is exactly what makes vectors/hats disappear.
MATH_FONT = "Cambria Math"

RENDER_OMML = "omml"
RENDER_UNICODE = "unicode"
RENDER_PLAIN = "plain"
RENDER_MODES = (RENDER_OMML, RENDER_UNICODE, RENDER_PLAIN)

# --------------------------------------------------------------------------
# Protected maths tags
# --------------------------------------------------------------------------
# DOTALL so a [[MATH_BLOCK: ... ]] can span lines. Non-greedy so two tags on one
# line stay separate.
MATH_TAG_RE = re.compile(r"\[\[MATH_(INLINE|BLOCK)\s*:\s*(.*?)\]\]", re.S)


def split_math_segments(text: str) -> list[tuple[str, str]]:
    """Split text into ``(kind, payload)`` where kind is text/inline/block."""
    out: list[tuple[str, str]] = []
    pos = 0
    for m in MATH_TAG_RE.finditer(text):
        if m.start() > pos:
            out.append(("text", text[pos:m.start()]))
        out.append(("inline" if m.group(1) == "INLINE" else "block", m.group(2).strip()))
        pos = m.end()
    if pos < len(text):
        out.append(("text", text[pos:]))
    return out


def has_math_tag(text: str) -> bool:
    return bool(MATH_TAG_RE.search(text))


def strip_math_tags(text: str) -> str:
    """Replace every tag with its bare LaTeX (used by quality reporting)."""
    return MATH_TAG_RE.sub(lambda m: m.group(2).strip(), text)


# --------------------------------------------------------------------------
# Symbol tables (canonical — docx_writer imports these)
# --------------------------------------------------------------------------
HAT, ARROW, BAR, DOT = "̂", "⃗", "̄", "̇"
# Precomposed letters look far better than base+combining for these two.
PRE_HAT = {"i": "î", "j": "ĵ"}

ACCENT_CHARS = {
    "vec": ARROW, "overrightarrow": ARROW, "overarrow": ARROW,
    "hat": HAT, "widehat": HAT,
    "bar": BAR, "overline": BAR,
    "dot": DOT,
}

LATEX_SYMBOLS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "ast": "×",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "equiv": "≡", "propto": "∝", "infty": "∞", "cong": "≅",
    "rightarrow": "→", "to": "→", "leftarrow": "←", "Rightarrow": "⇒",
    "leftrightarrow": "↔", "Leftrightarrow": "⇔", "implies": "⇒", "iff": "⇔",
    "sum": "Σ", "prod": "Π", "int": "∫", "oint": "∮", "partial": "∂", "nabla": "∇",
    "degree": "°", "circ": "°", "angle": "∠", "perp": "⊥", "parallel": "∥",
    "therefore": "∴", "because": "∵", "sqrt": "√", "cdots": "⋯", "ldots": "…",
    "vdots": "⋮", "prime": "′", "mid": "∣", "triangle": "△",
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩", "setminus": "∖",
    "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃",
    "nexists": "∄", "Leftarrow": "⇐", "mapsto": "↦",
    "langle": "⟨", "rangle": "⟩",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "Delta": "Δ",
    "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ",
    "vartheta": "θ", "Theta": "Θ", "iota": "ι", "kappa": "κ", "lambda": "λ",
    "Lambda": "Λ", "mu": "μ", "nu": "ν", "xi": "ξ", "Xi": "Ξ", "rho": "ρ",
    "varrho": "ρ", "sigma": "σ", "Sigma": "Σ", "tau": "τ", "upsilon": "υ",
    "phi": "ϕ", "varphi": "φ", "Phi": "Φ", "chi": "χ", "psi": "ψ", "Psi": "Ψ",
    "omega": "ω", "Omega": "Ω", "pi": "π", "Pi": "Π",
}

# Common chemical formulas. A whitelist (rather than a "letter followed by
# digits" rule) keeps "A4", "MP3", "Class 11" and "year 2024" untouched.
COMMON_FORMULAS = {
    "H2O", "H2O2", "CO2", "CO", "O2", "O3", "N2", "H2", "Cl2", "Br2", "I2", "F2",
    "SO2", "SO3", "NO2", "NO", "N2O", "N2O5", "P2O5", "CH4", "NH3", "HCl", "HBr",
    "H2S", "H2SO4", "HNO3", "H3PO4", "H2CO3", "CaCO3", "NaOH", "KOH", "Ca(OH)2",
    "CaO", "MgO", "Al2O3", "Fe2O3", "Fe3O4", "CuO", "Cu2O", "ZnO", "SiO2", "CaCl2",
    "MgCl2", "AlCl3", "FeCl3", "AgCl", "BaSO4", "CaSO4", "CuSO4", "ZnSO4", "FeSO4",
    "Na2SO4", "K2SO4", "KMnO4", "K2Cr2O7", "NH4Cl", "Na2CO3", "NaHCO3", "KNO3",
    "NaNO3", "C2H4", "C2H6", "C2H2", "C6H6", "C6H12O6", "C12H22O11", "C2H5OH",
    "CH3OH", "CH3COOH", "HCOOH", "Mg(OH)2",
}

# Upright multi-letter function names.
FUNCTIONS = {
    "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan",
    "sinh", "cosh", "tanh", "log", "ln", "exp", "lim", "max", "min", "det",
    "gcd", "lcm", "deg", "mod",
}

# Commands that only add space.
_SPACING_CMDS = {",", ";", ":", "!", " ", "quad", "qquad", "thinspace", "medspace"}
# Commands that wrap one argument that should be rendered as literal text.
_TEXT_WRAPPERS = {"text", "textbf", "textit", "mathrm", "mathbf", "mathit",
                  "mathsf", "operatorname", "textrm"}


def accent(base: str, comb: str) -> str:
    """Attach a combining accent, preferring a precomposed glyph."""
    base = base.strip()
    if not base:
        return base
    if comb == HAT and len(base) == 1 and base in PRE_HAT:
        return PRE_HAT[base]
    return base + comb


# --------------------------------------------------------------------------
# Unicode super/subscript fallback
# --------------------------------------------------------------------------
SUP_MAP = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶",
    "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼",
    "(": "⁽", ")": "⁾", "n": "ⁿ", "i": "ⁱ",
}
SUB_MAP = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆",
    "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋", "−": "₋", "=": "₌",
    "(": "₍", ")": "₎", "a": "ₐ", "e": "ₑ", "o": "ₒ", "x": "ₓ", "h": "ₕ",
    "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ", "p": "ₚ", "s": "ₛ", "t": "ₜ",
    "i": "ᵢ", "j": "ⱼ", "r": "ᵣ", "u": "ᵤ", "v": "ᵥ",
}


def _to_script(s: str, table: dict) -> str | None:
    """Map every char through `table`; None when any char has no Unicode form."""
    out = []
    for ch in s:
        if ch in table:
            out.append(table[ch])
        elif ch.isspace():
            continue
        else:
            return None
    return "".join(out) if out else None


# --------------------------------------------------------------------------
# LaTeX parser -> node tree
# --------------------------------------------------------------------------
# Nodes:
#   ("txt", str)                 literal text
#   ("fn",  str)                 upright function name
#   ("acc", combining_char, node)
#   ("frac", num_node, den_node)
#   ("rad", degree_node|None, node)
#   ("sup", base, exp) / ("sub", base, sub) / ("subsup", base, sub, sup)
#   ("grp", [nodes])

_TOKEN_RE = re.compile(r"\\([A-Za-z]+|.)|(\{)|(\})|(\^)|(_)|(\s+)|(.)", re.S)


def _tokenize(s: str) -> list[tuple[str, str]]:
    toks: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(s):
        cmd, ob, cb, sup, sub, sp, ch = m.groups()
        if cmd is not None:
            toks.append(("cmd", cmd))
        elif ob:
            toks.append(("{", ob))
        elif cb:
            toks.append(("}", cb))
        elif sup:
            toks.append(("^", sup))
        elif sub:
            toks.append(("_", sub))
        elif sp:
            toks.append(("sp", " "))
        elif ch:
            toks.append(("ch", ch))
    return toks


class _Parser:
    def __init__(self, toks: list[tuple[str, str]]):
        self.t = toks
        self.i = 0

    def _peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def parse(self, stop_at_close: bool = False) -> list:
        nodes: list = []
        while self.i < len(self.t):
            kind, val = self.t[self.i]
            if kind == "}":
                self.i += 1
                if stop_at_close:
                    return nodes
                continue
            if kind == "{":
                self.i += 1
                nodes.append(("grp", self.parse(stop_at_close=True)))
                continue
            if kind in ("^", "_"):
                self.i += 1
                base = nodes.pop() if nodes else ("txt", "")
                arg = self.parse_one()
                nodes.append(self._attach_script(base, kind, arg))
                continue
            if kind == "sp":
                self.i += 1
                nodes.append(("txt", " "))
                continue
            if kind == "ch":
                self.i += 1
                nodes.append(("txt", val))
                continue
            if kind == "cmd":
                self.i += 1
                node = self._command(val)
                if node is not None:
                    nodes.append(node)
                continue
            self.i += 1
        return nodes

    def _attach_script(self, base, kind: str, arg):
        """Fold a ^/_ onto `base`, merging x_a^b into a single subsup node."""
        if base and base[0] == "sub" and kind == "^":
            return ("subsup", base[1], base[2], arg)
        if base and base[0] == "sup" and kind == "_":
            return ("subsup", base[1], arg, base[2])
        return ("sup", base, arg) if kind == "^" else ("sub", base, arg)

    def parse_one(self):
        """Parse exactly one argument: a {group} or a single token."""
        kind, val = self._peek()
        if kind is None:
            return ("txt", "")
        if kind == "{":
            self.i += 1
            return ("grp", self.parse(stop_at_close=True))
        if kind == "cmd":
            self.i += 1
            return self._command(val) or ("txt", "")
        self.i += 1
        if kind == "sp":
            return ("txt", " ")
        return ("txt", val)

    def _command(self, name: str):
        if name in ACCENT_CHARS:
            return ("acc", ACCENT_CHARS[name], self.parse_one())
        if name in {"frac", "dfrac", "tfrac"}:
            return ("frac", self.parse_one(), self.parse_one())
        if name == "sqrt":
            deg = None
            if self._peek()[0] == "ch" and self._peek()[1] == "[":
                # \sqrt[n]{x}
                self.i += 1
                buf = []
                while self.i < len(self.t) and not (self.t[self.i][0] == "ch" and self.t[self.i][1] == "]"):
                    buf.append(self.t[self.i][1])
                    self.i += 1
                self.i += 1  # closing ]
                deg = ("txt", "".join(buf))
            return ("rad", deg, self.parse_one())
        if name in _TEXT_WRAPPERS:
            return ("grp", [self.parse_one()])
        if name in {"left", "right"}:
            # Keep the delimiter that follows, drop a "\left." placeholder.
            if self._peek()[0] == "ch" and self._peek()[1] == ".":
                self.i += 1
            return None
        if name in _SPACING_CMDS:
            return ("txt", " ")
        if name in FUNCTIONS:
            return ("fn", name)
        if name in LATEX_SYMBOLS:
            return ("txt", LATEX_SYMBOLS[name])
        if len(name) == 1 and not name.isalpha():
            return ("txt", name)          # escaped punctuation: \% \& \$
        return ("fn", name)               # unknown command -> upright name


def parse_latex(latex: str) -> list:
    return _Parser(_tokenize(latex)).parse()


# --------------------------------------------------------------------------
# Node tree -> Unicode text
# --------------------------------------------------------------------------
def _nodes_to_unicode(nodes: list) -> str:
    return "".join(_node_to_unicode(n) for n in nodes)


def _node_to_unicode(node) -> str:
    kind = node[0]
    if kind in ("txt", "fn"):
        return node[1]
    if kind == "grp":
        return _nodes_to_unicode(node[1])
    if kind == "acc":
        return accent(_node_to_unicode(node[2]), node[1])
    if kind == "frac":
        return f"({_node_to_unicode(node[1])})/({_node_to_unicode(node[2])})"
    if kind == "rad":
        inner = _node_to_unicode(node[2])
        return f"√({inner})"
    if kind in ("sup", "sub"):
        base = _node_to_unicode(node[1])
        arg = _node_to_unicode(node[2])
        table = SUP_MAP if kind == "sup" else SUB_MAP
        mapped = _to_script(arg, table)
        # No Unicode form -> keep the ^/_ marker so docx_writer turns it into a
        # real Word superscript/subscript run (better than dropping it).
        return base + (mapped if mapped is not None else ("^" if kind == "sup" else "_") + _brace(arg))
    if kind == "subsup":
        base = _node_to_unicode(node[1])
        sub = _node_to_unicode(node[2])
        sup = _node_to_unicode(node[3])
        msub = _to_script(sub, SUB_MAP)
        msup = _to_script(sup, SUP_MAP)
        out = base + (msub if msub is not None else "_" + _brace(sub))
        out += msup if msup is not None else "^" + _brace(sup)
        return out
    return ""


def _brace(s: str) -> str:
    return s if len(s) == 1 else "{" + s + "}"


def latex_to_unicode(latex: str) -> str:
    """LaTeX -> readable Unicode (A⃗, î, H₂O, m s⁻², SO₄²⁻).

    Any script that has no Unicode glyph is left as a ``^``/``_`` marker for
    docx_writer's real superscript/subscript runs.
    """
    try:
        return _nodes_to_unicode(parse_latex(latex)).strip()
    except Exception:
        return latex


# --------------------------------------------------------------------------
# Node tree -> OMML (native Word equation)
# --------------------------------------------------------------------------
def _el(tag: str):
    return OxmlElement(tag)


def _val(tag: str, value: str):
    e = _el(tag)
    e.set(qn("m:val"), value)
    return e


def _math_run(text: str, *, size=None, color=None, italic: bool | None = None):
    """An <m:r> maths run pinned to the maths font."""
    r = _el("m:r")
    rpr = _el("w:rPr")
    fonts = _el("w:rFonts")
    fonts.set(qn("w:ascii"), MATH_FONT)
    fonts.set(qn("w:hAnsi"), MATH_FONT)
    fonts.set(qn("w:cs"), MATH_FONT)
    rpr.append(fonts)
    if italic is False:
        i = _el("w:i")
        i.set(qn("w:val"), "0")
        rpr.append(i)
    if size is not None:
        sz = _el("w:sz")
        sz.set(qn("w:val"), str(int(size.pt * 2)))
        rpr.append(sz)
    if color is not None:
        c = _el("w:color")
        c.set(qn("w:val"), str(color))
        rpr.append(c)
    r.append(rpr)
    t = _el("m:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    return r


def _wrap(tag: str, children: list):
    e = _el(tag)
    for c in children:
        e.append(c)
    return e


def _nodes_to_omml(nodes: list, **kw) -> list:
    out = []
    for n in nodes:
        out.extend(_node_to_omml(n, **kw))
    return out


def _node_to_omml(node, **kw) -> list:
    kind = node[0]
    if kind == "txt":
        return [_math_run(node[1], **kw)] if node[1] else []
    if kind == "fn":
        return [_math_run(node[1], italic=False, **{k: v for k, v in kw.items() if k != "italic"})]
    if kind == "grp":
        return _nodes_to_omml(node[1], **kw)
    if kind == "acc":
        acc = _el("m:acc")
        pr = _el("m:accPr")
        pr.append(_val("m:chr", node[1]))
        acc.append(pr)
        acc.append(_wrap("m:e", _node_to_omml(node[2], **kw)))
        return [acc]
    if kind == "frac":
        f = _el("m:f")
        f.append(_wrap("m:num", _node_to_omml(node[1], **kw)))
        f.append(_wrap("m:den", _node_to_omml(node[2], **kw)))
        return [f]
    if kind == "rad":
        rad = _el("m:rad")
        pr = _el("m:radPr")
        if node[1] is None:
            pr.append(_val("m:degHide", "1"))
        rad.append(pr)
        rad.append(_wrap("m:deg", _node_to_omml(node[1], **kw) if node[1] is not None else []))
        rad.append(_wrap("m:e", _node_to_omml(node[2], **kw)))
        return [rad]
    if kind in ("sup", "sub"):
        tag = "m:sSup" if kind == "sup" else "m:sSub"
        e = _el(tag)
        e.append(_wrap("m:e", _node_to_omml(node[1], **kw)))
        e.append(_wrap("m:sup" if kind == "sup" else "m:sub", _node_to_omml(node[2], **kw)))
        return [e]
    if kind == "subsup":
        e = _el("m:sSubSup")
        e.append(_wrap("m:e", _node_to_omml(node[1], **kw)))
        e.append(_wrap("m:sub", _node_to_omml(node[2], **kw)))
        e.append(_wrap("m:sup", _node_to_omml(node[3], **kw)))
        return [e]
    return []


def build_omath(latex: str, *, size=None, color=None):
    """LaTeX -> an ``<m:oMath>`` element, or None when it cannot be built."""
    try:
        nodes = parse_latex(latex)
        children = _nodes_to_omml(nodes, size=size, color=color)
        if not children:
            return None
        return _wrap("m:oMath", children)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Public DOCX helpers
# --------------------------------------------------------------------------
def add_inline_math(paragraph, latex: str, *, mode: str = RENDER_OMML,
                    size=None, color=None, fallback_writer=None) -> str:
    """Append inline maths to `paragraph`.

    Returns the mode actually used ("omml", "unicode" or "plain") so the caller
    can report when a formula had to fall back.
    """
    if mode == RENDER_PLAIN:
        _fallback(paragraph, f"[[MATH_INLINE: {latex}]]", size=size, color=color,
                  fallback_writer=fallback_writer)
        return RENDER_PLAIN
    if mode == RENDER_OMML:
        om = build_omath(latex, size=size, color=color)
        if om is not None:
            paragraph._p.append(om)
            return RENDER_OMML
    _fallback(paragraph, latex_to_unicode(latex), size=size, color=color,
              fallback_writer=fallback_writer)
    return RENDER_UNICODE


def add_block_math(doc, latex: str, *, mode: str = RENDER_OMML, size=None,
                   color=None, fallback_writer=None, paragraph=None):
    """Add a centred display equation as its own paragraph."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    p = paragraph if paragraph is not None else doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if mode == RENDER_OMML:
        om = build_omath(latex, size=size, color=color)
        if om is not None:
            # oMathPara marks it as a display equation rather than inline maths.
            para = _el("m:oMathPara")
            para.append(om)
            p._p.append(para)
            return p, RENDER_OMML
    used = RENDER_PLAIN if mode == RENDER_PLAIN else RENDER_UNICODE
    txt = f"[[MATH_BLOCK: {latex}]]" if mode == RENDER_PLAIN else latex_to_unicode(latex)
    _fallback(p, txt, size=size, color=color, fallback_writer=fallback_writer)
    return p, used


def _fallback(paragraph, text: str, *, size=None, color=None, fallback_writer=None):
    """Write plain text for a formula, using the caller's run writer if given."""
    if fallback_writer is not None:
        fallback_writer(paragraph, text, size=size, color=color)
        return
    run = paragraph.add_run(text)
    run.font.name = MATH_FONT
    if size is not None:
        run.font.size = size
    if color is not None:
        run.font.color.rgb = color
