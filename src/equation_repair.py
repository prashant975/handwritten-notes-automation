"""Repair formulas the model wrote as plain text instead of tagged maths.

The prompt asks for ``[[MATH_INLINE: ...]]`` around every formula, but models
drift. This module is the safety net that runs between generation and DOCX
writing when "Strict equation preservation" is on.

It is deliberately CONSERVATIVE: a rule only fires when the surrounding text
proves the intent (the words "vector"/"cross product" near ``A x B = C``, or
"unit vector"/"cyclic" near ``i -> j -> k``). Anything already inside a maths
tag is never touched, and DTP notes are skipped because their exact English
format is parsed downstream.

Every repair is recorded so it can be written to run_log.json and shown to the
user rather than happening invisibly.
"""

from __future__ import annotations

import re

from .math_renderer import COMMON_FORMULAS, MATH_TAG_RE, flatten_math_tags, split_math_segments


def _tag(latex: str) -> str:
    return f"[[MATH_INLINE: {latex}]]"


# Context gates — a rule fires only when its keywords appear nearby.
_VECTOR_CTX = re.compile(r"\bcross[- ]product\b|\bvectors?\b|\bperpendicular\b|\bright[- ]hand\b", re.I)
_UNIT_CTX = re.compile(r"\bunit[- ]vectors?\b|\bcyclic\b|\bi\s*,\s*j\s*,\s*k\b|\bhat\b", re.I)

# A x B = C / A × B = C  (single capital letters only)
_CROSS_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z])\s*(?:×|x|X|\*)\s*([A-Z])\s*=\s*([A-Z])(?![A-Za-z0-9])")
# i -> j -> k  /  i → j → k
_CYCLIC_RE = re.compile(r"(?<![A-Za-z0-9])i\s*(?:->|→|,)\s*j\s*(?:->|→|,)\s*k(?![A-Za-z0-9])", re.I)
# "vector A", "vectors A and B", "a new vector C"
_VEC_LETTER_RE = re.compile(r"\b(vectors?)\s+([A-Z])(?![A-Za-z0-9])")
# "and B" directly after a repaired "vectors A"
_AND_LETTER_RE = re.compile(r"\band\s+([A-Z])(?![A-Za-z0-9])")
# 10^-3, x^-2 (caret + signed number, not already braced)
_NEG_POW_RE = re.compile(r"(?<![A-Za-z0-9^_])([0-9]+|[A-Za-z])\^(-\s*[0-9]+)(?![0-9}])")
_POS_POW_RE = re.compile(r"(?<![A-Za-z0-9^_])([A-Za-z])\^([2-9])(?![0-9}])")
_EXPLICIT_SUB_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z])_([A-Za-z0-9])(?![A-Za-z0-9}])")
_V_ZERO_RE = re.compile(r"(?<![A-Za-z0-9_])v0(?![A-Za-z0-9])")
_SULFATE_RE = re.compile(r"(?<![A-Za-z0-9_])SO4\s*\^\s*2-(?![A-Za-z0-9])", re.I)
# m/s2, m/s^2, ms-2, m s-2  -> m s^{-2}
_UNIT_PER_RE = re.compile(r"(?<![A-Za-z0-9])([a-zA-Z]{1,4})\s*/\s*([a-zA-Z]{1,3})\s*\^?\s*([2-9])(?![A-Za-z0-9])")
_UNIT_NEG_RE = re.compile(r"(?<![A-Za-z0-9])(m|km|cm|mol|kg|N|J|W|C|V|A)\s+?(s|L|m|kg)\s*\^?\s*-\s*([1-9])(?![A-Za-z0-9])")
# Bare chemical formulas from the whitelist (H2O, CO2, H2SO4 ...)
_CHEM_RE = re.compile(
    r"(?<![A-Za-z0-9_^{])(" + "|".join(re.escape(f) for f in sorted(COMMON_FORMULAS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_}])"
)


def _chem_latex(formula: str) -> str:
    """H2SO4 -> H_2SO_4 (subscript every digit run that follows a letter)."""
    return re.sub(r"(?<=[A-Za-z)])(\d+)", r"_\1", formula)


def _is_dtp(line: str) -> bool:
    return "note to dtp" in line.lower()


def _repair_line(line: str, doc_ctx: str) -> tuple[str, list[dict]]:
    """Repair one line's untagged segments. Returns (new_line, repairs)."""
    repairs: list[dict] = []
    ctx = line + "\n" + doc_ctx
    vector_ctx = bool(_VECTOR_CTX.search(ctx))
    unit_ctx = bool(_UNIT_CTX.search(ctx))

    def note(rule, before, after, reason):
        repairs.append({"rule": rule, "before": before, "after": after, "reason": reason})

    out_parts: list[str] = []
    for kind, payload in split_math_segments(line):
        if kind != "text":
            # Already protected maths — re-emit the tag untouched.
            out_parts.append(f"[[MATH_{'INLINE' if kind == 'inline' else 'BLOCK'}: {payload}]]")
            continue
        s = payload

        # Repair explicit script syntax before inserting any other tags, so
        # these regexes never match LaTeX inside a newly-created tag.
        def _sulfate(m):
            latex = r"SO_4^{2-}"
            note("ionic_formula", m.group(0), latex, "sulfate formula written without protected scripts")
            return _tag(latex)
        s = _SULFATE_RE.sub(_sulfate, s)

        def _pospow(m):
            latex = f"{m.group(1)}^{m.group(2)}"
            note("positive_power", m.group(0), latex, "explicit positive power written without a maths tag")
            return _tag(latex)
        s = _POS_POW_RE.sub(_pospow, s)

        def _explicit_sub(m):
            latex = f"{m.group(1)}_{m.group(2)}"
            note("explicit_subscript", m.group(0), latex, "explicit subscript written without a maths tag")
            return _tag(latex)
        s = _EXPLICIT_SUB_RE.sub(_explicit_sub, s)

        def _vzero(m):
            latex = "v_0"
            note("common_subscript", m.group(0), latex, "standard initial-velocity symbol written as v0")
            return _tag(latex)
        s = _V_ZERO_RE.sub(_vzero, s)

        if unit_ctx:
            def _cyc(m):
                note("unit_vector_cyclic", m.group(0), r"\hat{i} \rightarrow \hat{j} \rightarrow \hat{k}",
                     "unit-vector / cyclic-order context")
                return _tag(r"\hat{i} \rightarrow \hat{j} \rightarrow \hat{k}")
            s = _CYCLIC_RE.sub(_cyc, s)

        if vector_ctx:
            def _cross(m):
                a, b, c = m.group(1), m.group(2), m.group(3)
                latex = rf"\vec{{{a}}} \times \vec{{{b}}} = \vec{{{c}}}"
                note("vector_cross_product", m.group(0), latex, "cross-product / vector context")
                return _tag(latex)
            s = _CROSS_RE.sub(_cross, s)

            def _vecletter(m):
                word, letter = m.group(1), m.group(2)
                note("vector_letter", m.group(0), rf"{word} \vec{{{letter}}}", "letter directly after the word 'vector'")
                return f"{word} " + _tag(rf"\vec{{{letter}}}")
            s2 = _VEC_LETTER_RE.sub(_vecletter, s)
            # "vectors A and B" -> also wrap the letter after "and"
            if s2 != s:
                def _andletter(m):
                    letter = m.group(1)
                    note("vector_letter", m.group(0), rf"and \vec{{{letter}}}", "letter joined by 'and' to a vector")
                    return "and " + _tag(rf"\vec{{{letter}}}")
                s2 = _AND_LETTER_RE.sub(_andletter, s2, count=1)
            s = s2

        def _chem(m):
            f = m.group(1)
            latex = _chem_latex(f)
            note("chemistry_formula", f, latex, "known chemical formula written without subscripts")
            return _tag(latex)
        s = _CHEM_RE.sub(_chem, s)

        def _unitper(m):
            a, b, p = m.group(1), m.group(2), m.group(3)
            latex = rf"{a}\,{b}^{{-{p}}}"
            note("unit_exponent", m.group(0), latex, "unit written with a slash instead of a negative power")
            return _tag(latex)
        s = _UNIT_PER_RE.sub(_unitper, s)

        def _unitneg(m):
            a, b, p = m.group(1), m.group(2), m.group(3)
            latex = rf"{a}\,{b}^{{-{p}}}"
            note("unit_exponent", m.group(0), latex, "unit written with a bare minus exponent")
            return _tag(latex)
        s = _UNIT_NEG_RE.sub(_unitneg, s)

        # Generic negative powers run after compound units, otherwise the
        # ``s^-2`` portion of ``m s^-2`` would be tagged before the unit rule
        # could preserve the complete unit.
        def _negpow(m):
            base, exp = m.group(1), m.group(2).replace(" ", "")
            latex = f"{base}^{{{exp}}}"
            note("negative_power", m.group(0), latex, "negative exponent written inline")
            return _tag(latex)
        s = _NEG_POW_RE.sub(_negpow, s)

        out_parts.append(s)
    return "".join(out_parts), repairs


def repair_equations(notes_text: str) -> tuple[str, list[dict]]:
    """Repair untagged formulas across the notes.

    Returns ``(repaired_text, repairs)``; each repair records the line number,
    rule, before/after and why it fired.
    """
    # Rebalance nested/unclosed maths tags FIRST, so a malformed tag (e.g. a
    # sub-expression wrapped in its own [[MATH_INLINE:]] inside an outer formula)
    # is a single clean tag before either the per-line repair or the downstream
    # equation-quality check sees it. Without this the leaked "[[MATH_INLINE:"
    # reads as an untagged formula and needlessly triggers Mathpix OCR.
    notes_text = flatten_math_tags(notes_text)
    lines = notes_text.splitlines()
    # Whole-document context so a heading like "Vector Cross Product" still
    # licenses a repair on the bullet beneath it.
    doc_ctx = notes_text[:20000]
    out: list[str] = []
    all_repairs: list[dict] = []
    for n, line in enumerate(lines, start=1):
        if _is_dtp(line) or not line.strip():
            out.append(line)
            continue
        new_line, repairs = _repair_line(line, doc_ctx)
        for r in repairs:
            r["line"] = n
        all_repairs.extend(repairs)
        out.append(new_line)
    return "\n".join(out), all_repairs
