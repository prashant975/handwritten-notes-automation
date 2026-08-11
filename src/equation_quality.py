"""Post-generation check that maths notation survived into the notes.

Runs after equation repair and reports what is still written as plain text, so
a JEE/NEET note that silently lost its vector arrows is visible instead of
shipping broken. Produces warnings for the UI plus two artefacts in the run
folder: equation_quality_report.json and equation_quality_report.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .math_renderer import COMMON_FORMULAS, MATH_TAG_RE, split_math_segments

# Same context gates as the repair step, so the report and the repairer agree.
_VECTOR_CTX = re.compile(r"\bcross[- ]product\b|\bvectors?\b|\bperpendicular\b", re.I)
_UNIT_CTX = re.compile(r"\bunit[- ]vectors?\b|\bcyclic\b|\bhat\b", re.I)

_CROSS_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z])\s*(?:×|x|X|\*)\s*([A-Z])\s*=\s*([A-Z])(?![A-Za-z0-9])")
_CYCLIC_RE = re.compile(r"(?<![A-Za-z0-9])i\s*(?:->|→|,)\s*j\s*(?:->|→|,)\s*k(?![A-Za-z0-9])", re.I)
_CHEM_RE = re.compile(
    r"(?<![A-Za-z0-9_^{])(" + "|".join(re.escape(f) for f in sorted(COMMON_FORMULAS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_}])"
)
# The caret is REQUIRED: with `\^?` this matched plain subtraction — real
# production notes ("radial nodes = n - l - 1", "nodes = n-2") produced 9
# missing_superscript false positives per file, each inflating the issue count
# that decides whether a chunk is re-run through paid Mathpix OCR.
_NEG_POW_RE = re.compile(r"(?<![A-Za-z0-9^_{])([0-9]+|[A-Za-z])\^\s*-\s*([1-9])(?![0-9}])")
_POS_POW_RE = re.compile(r"(?<![A-Za-z0-9^_{])([A-Za-z])\^([2-9])(?![0-9}])")
_V_ZERO_RE = re.compile(r"(?<![A-Za-z0-9_])v0(?![A-Za-z0-9])")
_SULFATE_RE = re.compile(r"(?<![A-Za-z0-9_])SO4\s*\^\s*2-(?![A-Za-z0-9])", re.I)
_BARE_EQUATION_RE = re.compile(r"[A-Za-z0-9\)]\s*=\s*[A-Za-z0-9\(\\]")
_HAS_NOTATION_RE = re.compile(r"[√×÷≤≥≠±∝∫∑→←⇒⇔∂∇°∠⊥]|[A-Za-z0-9]\^|[A-Za-z0-9]_")

_ISSUE_LABELS = {
    "missing_vector_arrow": "Possible missing vector arrow",
    "missing_unit_vector_hat": "Possible missing unit-vector hat",
    "missing_subscript": "Possible missing subscript",
    "missing_superscript": "Possible missing superscript",
    "untagged_formula": "Formula not wrapped in a maths tag",
}


def _issue(kind: str, line_no: int, snippet: str) -> dict:
    return {
        "type": kind,
        "line": line_no,
        "snippet": snippet.strip()[:160],
        "message": f"{_ISSUE_LABELS.get(kind, kind)} near: {snippet.strip()[:80]}",
    }


def check_equations(notes_text: str) -> dict:
    """Scan the (already repaired) notes for maths that is still plain text."""
    issues: list[dict] = []
    tagged = len(MATH_TAG_RE.findall(notes_text))
    doc_vector_ctx = bool(_VECTOR_CTX.search(notes_text))
    doc_unit_ctx = bool(_UNIT_CTX.search(notes_text))

    for n, line in enumerate(notes_text.splitlines(), start=1):
        if not line.strip() or "note to dtp" in line.lower():
            continue
        # Only look at text OUTSIDE maths tags — tagged maths is already correct.
        plain = "".join(p for k, p in split_math_segments(line) if k == "text")
        if not plain.strip():
            continue

        if doc_vector_ctx:
            for m in _CROSS_RE.finditer(plain):
                issues.append(_issue("missing_vector_arrow", n, m.group(0)))
        if doc_unit_ctx:
            for m in _CYCLIC_RE.finditer(plain):
                issues.append(_issue("missing_unit_vector_hat", n, m.group(0)))
        for m in _CHEM_RE.finditer(plain):
            issues.append(_issue("missing_subscript", n, m.group(1)))
        for m in _NEG_POW_RE.finditer(plain):
            issues.append(_issue("missing_superscript", n, m.group(0)))
        for m in _POS_POW_RE.finditer(plain):
            issues.append(_issue("untagged_formula", n, m.group(0)))
        for m in _V_ZERO_RE.finditer(plain):
            issues.append(_issue("missing_subscript", n, m.group(0)))
        for m in _SULFATE_RE.finditer(plain):
            issues.append(_issue("missing_subscript", n, m.group(0)))
        # A bare "x = y" line carrying notation but no tag.
        if _BARE_EQUATION_RE.search(plain) and _HAS_NOTATION_RE.search(plain):
            issues.append(_issue("untagged_formula", n, plain))

    counts: dict[str, int] = {}
    for i in issues:
        counts[i["type"]] = counts.get(i["type"], 0) + 1
    return {
        "tagged_formula_count": tagged,
        "issue_count": len(issues),
        "issues_by_type": counts,
        "issues": issues,
        "passed": not issues,
    }


def warnings_from_report(report: dict, *, limit: int = 12) -> list[str]:
    """Short, user-facing warnings for the Streamlit panel."""
    out = [i["message"] for i in report.get("issues", [])[:limit]]
    extra = report.get("issue_count", 0) - len(out)
    if extra > 0:
        out.append(f"…and {extra} more equation issue(s) — see equation_quality_report.md.")
    return out


def _markdown(report: dict, repairs: list[dict]) -> str:
    lines = ["# Equation Quality Report", ""]
    lines.append(f"- Tagged formulas: **{report.get('tagged_formula_count', 0)}**")
    lines.append(f"- Issues found: **{report.get('issue_count', 0)}**")
    lines.append(f"- Automatic repairs applied: **{len(repairs)}**")
    lines.append(f"- Status: **{'PASS' if report.get('passed') else 'NEEDS REVIEW'}**")
    lines.append("")
    if repairs:
        lines += ["## Repairs applied", "", "| Line | Rule | Before | After |", "|---|---|---|---|"]
        for r in repairs:
            lines.append(f"| {r.get('line','')} | {r.get('rule','')} | `{r.get('before','')}` | `{r.get('after','')}` |")
        lines.append("")
    if report.get("issues"):
        lines += ["## Remaining issues", "", "| Line | Type | Snippet |", "|---|---|---|"]
        for i in report["issues"]:
            lines.append(f"| {i['line']} | {i['type']} | `{i['snippet']}` |")
        lines.append("")
    else:
        lines += ["## Remaining issues", "", "None — all detected formulas are inside maths tags.", ""]
    return "\n".join(lines)


def write_reports(run_dir: Path, report: dict, repairs: list[dict]) -> tuple[Path, Path]:
    """Write equation_quality_report.json and .md into the run folder."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(report)
    payload["repairs"] = repairs
    json_path = run_dir / "equation_quality_report.json"
    md_path = run_dir / "equation_quality_report.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(_markdown(report, repairs), encoding="utf-8")
    return json_path, md_path
