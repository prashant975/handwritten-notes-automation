"""Tests for the JEE/NEET maths pipeline: LaTeX -> Unicode, LaTeX -> OMML,
equation repair, and the equation quality report.

Runs standalone (no pytest needed):
    .venv\\Scripts\\python.exe tests\\test_math_rendering.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.equation_quality import check_equations  # noqa: E402
from src.equation_repair import repair_equations  # noqa: E402
from src.math_renderer import (  # noqa: E402
    build_omath,
    latex_to_unicode,
    split_math_segments,
)
from src.prompt_builder import load_prompt_template, normalize_exam, prompt_file_name  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want):
    if got == want:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name}\n     got  {got!r}\n     want {want!r}")


def check_true(name: str, cond, detail=""):
    if cond:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


# --------------------------------------------------------------------------
# Unicode fallback
# --------------------------------------------------------------------------
def test_unicode():
    cases = {
        r"\vec{A} \times \vec{B} = \vec{C}": "A⃗ × B⃗ = C⃗",
        r"\hat{i} \rightarrow \hat{j} \rightarrow \hat{k}": "î → ĵ → k̂",
        r"\vec{A} \cdot \vec{B} = AB\cos\theta": "A⃗ · B⃗ = ABcosθ",
        r"x^2": "x²",
        r"v_0": "v₀",
        r"a_n": "aₙ",
        r"H_2O": "H₂O",
        r"CO_2": "CO₂",
        r"SO_4^{2-}": "SO₄²⁻",
        r"m\,s^{-2}": "m s⁻²",
        r"mol\,L^{-1}": "mol L⁻¹",
        r"10^{-3}": "10⁻³",
        r"\Delta\phi": "Δϕ",
        r"\bar{x}": "x̄",
    }
    for latex, want in cases.items():
        check(f"unicode {latex}", latex_to_unicode(latex), want)


# --------------------------------------------------------------------------
# Native OMML
# --------------------------------------------------------------------------
def test_omml_structures():
    from lxml import etree

    def tags(latex):
        om = build_omath(latex)
        assert om is not None, latex
        return {etree.QName(e).localname for e in om.iter()}

    check_true("omml vec -> m:acc", "acc" in tags(r"\vec{A}"))
    check_true("omml hat -> m:acc", "acc" in tags(r"\hat{i}"))
    check_true("omml frac -> m:f", "f" in tags(r"\frac{v^2}{r}"))
    check_true("omml sqrt -> m:rad", "rad" in tags(r"\sqrt{2}"))
    check_true("omml x^2 -> m:sSup", "sSup" in tags("x^2"))
    check_true("omml v_0 -> m:sSub", "sSub" in tags("v_0"))
    check_true("omml SO_4^{2-} -> m:sSubSup", "sSubSup" in tags(r"SO_4^{2-}"))

    # The accent character must be the real combining arrow / hat.
    from lxml import etree as _et
    xml = _et.tostring(build_omath(r"\vec{A}"), encoding="unicode")
    check_true("omml arrow accent char U+20D7", "⃗" in xml, xml[:200])
    xml = _et.tostring(build_omath(r"\hat{i}"), encoding="unicode")
    check_true("omml hat accent char U+0302", "̂" in xml, xml[:200])
    # Maths runs must not inherit the handwritten body font.
    check_true("omml uses Cambria Math", "Cambria Math" in xml)


# --------------------------------------------------------------------------
# Tag splitting
# --------------------------------------------------------------------------
def test_tags():
    seg = split_math_segments("cross product is [[MATH_INLINE: \\vec{A}]], done.")
    check("split inline", seg, [("text", "cross product is "), ("inline", r"\vec{A}"), ("text", ", done.")])
    seg = split_math_segments("[[MATH_BLOCK:\na_n = 1\n]]")
    check("split block", seg, [("block", "a_n = 1")])


# --------------------------------------------------------------------------
# Equation repair (the acceptance-criteria input)
# --------------------------------------------------------------------------
ACCEPTANCE = """Vector Cross Product
• The cross product of two vectors A and B gives a new vector C (A x B = C) that is perpendicular to the plane containing both A and B.

SKC
• The cross product of unit vectors follows a cyclic order: i -> j -> k.
"""


def test_repair_acceptance():
    fixed, repairs = repair_equations(ACCEPTANCE)
    check_true("repair produces vec A", "[[MATH_INLINE: \\vec{A}]]" in fixed, fixed)
    check_true("repair produces vec B", "[[MATH_INLINE: \\vec{B}]]" in fixed, fixed)
    check_true("repair produces vec C", "[[MATH_INLINE: \\vec{C}]]" in fixed, fixed)
    check_true("repair produces cross product",
               "[[MATH_INLINE: \\vec{A} \\times \\vec{B} = \\vec{C}]]" in fixed, fixed)
    check_true("repair produces cyclic hats",
               "[[MATH_INLINE: \\hat{i} \\rightarrow \\hat{j} \\rightarrow \\hat{k}]]" in fixed, fixed)
    check_true("repair recorded", len(repairs) >= 5, repairs)


def test_repair_chem_units():
    text = ("Chemistry Basics\n"
            "• Water is H2O and carbon dioxide is CO2.\n"
            "• Acceleration is m/s2 and a value of 10^-3.\n")
    fixed, _ = repair_equations(text)
    check_true("repair H2O", "[[MATH_INLINE: H_2O]]" in fixed, fixed)
    check_true("repair CO2", "[[MATH_INLINE: CO_2]]" in fixed, fixed)
    check_true("repair m/s2", "[[MATH_INLINE: m\\,s^{-2}]]" in fixed, fixed)
    check_true("repair 10^-3", "[[MATH_INLINE: 10^{-3}]]" in fixed, fixed)


def test_repair_is_conservative():
    """No vector/chemistry context => nothing may be rewritten."""
    text = ("Exam Info\n"
            "• Bring an A4 sheet; an MP3 player is not allowed.\n"
            "• Class 11 students in year 2024 scored 10 x 2 = 20 marks.\n"
            "• The file slides_raw.json is saved.\n")
    fixed, repairs = repair_equations(text)
    check("conservative: text unchanged", fixed.strip(), text.strip())
    check("conservative: no repairs", len(repairs), 0)


def test_repair_never_touches_existing_tags():
    text = "• Already tagged: [[MATH_INLINE: \\vec{A} \\times \\vec{B} = \\vec{C}]] and vectors A here.\n"
    fixed, _ = repair_equations(text)
    check_true("existing tag survives once",
               fixed.count("[[MATH_INLINE: \\vec{A} \\times \\vec{B} = \\vec{C}]]") == 1, fixed)
    check_true("no nested tag", "[[MATH_INLINE: [[" not in fixed, fixed)


# --------------------------------------------------------------------------
# Quality report
# --------------------------------------------------------------------------
def test_quality():
    rep_before = check_equations(ACCEPTANCE)
    types = rep_before["issues_by_type"]
    check_true("flags missing vector arrow", types.get("missing_vector_arrow", 0) >= 1, types)
    check_true("flags missing unit-vector hat", types.get("missing_unit_vector_hat", 0) >= 1, types)

    fixed, _ = repair_equations(ACCEPTANCE)
    rep_after = check_equations(fixed)
    check("clean after repair", rep_after["issue_count"], 0)
    check_true("counts tagged formulas", rep_after["tagged_formula_count"] >= 5, rep_after)


# --------------------------------------------------------------------------
# Exam prompt resolution
# --------------------------------------------------------------------------
def test_exam_prompts():
    check("normalize JEE + NEET", normalize_exam("JEE + NEET"), "jee_neet")
    check("exam prompt name", prompt_file_name("physics", "concise", "en", "jee"), "jee_concise_en.txt")
    for exam in ("jee", "neet", "jee_neet"):
        tpl = load_prompt_template("physics", "concise", "en", exam)
        check_true(f"{exam} template loads", len(tpl) > 500)
        check_true(f"{exam} template demands math tags", "[[MATH_INLINE:" in tpl)
    # No exam -> falls back to the subject template.
    check("no exam falls back", prompt_file_name("physics", "concise", "en", ""), "physics_summary_en.txt")
    # Hindi + exam -> reuses the English exam template rather than crashing.
    tpl = load_prompt_template("physics", "concise", "hi", "jee")
    check_true("hindi exam falls back to en template", "[[MATH_INLINE:" in tpl)


def test_prompt_no_longer_forbids_latex():
    """The root cause: the old prompt said 'Never use LaTeX'."""
    from src.prompt_builder import build_generation_prompt
    from src.models import SlideData

    slides = [SlideData(slide_no=1, heading="Vectors", text="A x B = C")]
    p = build_generation_prompt("physics", "concise", "en", slides, exam="jee", strict_math=True)
    check_true("prompt no longer forbids LaTeX", "Never use LaTeX" not in p)
    check_true("prompt demands math tags", "[[MATH_INLINE:" in p)
    check_true("prompt demands vec", "\\vec{A}" in p)


def main():
    test_unicode()
    test_omml_structures()
    test_tags()
    test_repair_acceptance()
    test_repair_chem_units()
    test_repair_is_conservative()
    test_repair_never_touches_existing_tags()
    test_quality()
    test_exam_prompts()
    test_prompt_no_longer_forbids_latex()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} TEST(S) FAILED: {FAILURES}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
