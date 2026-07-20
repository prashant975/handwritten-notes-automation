from __future__ import annotations

from .config import ROOT_DIR
from .models import SlideData

PROMPT_DIR = ROOT_DIR / "prompts"

# The two note styles the templates exist for, plus friendly aliases the UI or
# CLI might use. "Concise Notes" IS the summary mode.
_MODE_ALIASES = {
    "concise": "summary",
    "concise notes": "summary",
    "short": "summary",
    "brief": "summary",
    "full": "complete",
    "complete notes": "complete",
    "detailed": "complete",
}

# Exam-specific concise templates (prompts/<exam>_concise_en.txt).
_EXAM_ALIASES = {
    "jee": "jee",
    "neet": "neet",
    "jee + neet": "jee_neet",
    "jee+neet": "jee_neet",
    "jee neet": "jee_neet",
    "jee_neet": "jee_neet",
    "both": "jee_neet",
}


def normalize_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    return _MODE_ALIASES.get(m, m)


def normalize_exam(exam: str) -> str:
    e = (exam or "").strip().lower()
    return _EXAM_ALIASES.get(e, e)


def _candidate_names(subject: str, mode: str, language_code: str, exam: str = "") -> list[str]:
    """Template names to try, most specific first."""
    lang = (language_code or "en").lower()
    mode_n = normalize_mode(mode)
    exam_n = normalize_exam(exam)
    names: list[str] = []
    # An exam-specific CONCISE template wins when an exam is selected.
    if exam_n and mode_n == "summary":
        names.append(f"{exam_n}_concise_{lang}.txt")
        if lang != "en":
            # Only English exam templates ship. The language rule below still
            # forces Hindi output, so reuse the English exam template rather
            # than silently dropping back to the generic subject prompt.
            names.append(f"{exam_n}_concise_en.txt")
    names.append(f"{subject.lower()}_{mode_n}_{lang}.txt")
    return names


def prompt_file_name(subject: str, mode: str, language_code: str, exam: str = "") -> str:
    return _candidate_names(subject, mode, language_code, exam)[0]


def load_prompt_template(subject: str, mode: str, language_code: str, exam: str = "") -> str:
    names = _candidate_names(subject, mode, language_code, exam)
    for name in names:
        path = PROMPT_DIR / name
        if path.exists():
            return path.read_text(encoding="utf-8")
    available = sorted(p.name for p in PROMPT_DIR.glob("*.txt"))
    raise FileNotFoundError(
        f"No prompt template for subject='{subject}', mode='{normalize_mode(mode)}', "
        f"language='{language_code}', exam='{normalize_exam(exam) or 'none'}'. "
        f"Tried: {names} in {PROMPT_DIR}. Available templates: {available}"
    )


def format_slides(slides: list[SlideData]) -> str:
    chunks: list[str] = []
    for s in slides:
        text = (s.prompt_text or "").strip()
        if not text:
            text = "[No extractable text. Use attached slide image if provided.]"
        chunks.append(
            f"--- SLIDE {s.slide_no} ---\n"
            f"Heading detected: {s.heading or '[none]'}\n"
            f"Image available: {'yes' if s.image_path else 'no'}\n"
            f"Slide text / annotations:\n{text}\n"
        )
    return "\n".join(chunks)


def _language_rule(language_code: str) -> str:
    """A hard, template-independent directive that forces the output language.

    The slide text is often English even for a Hindi class, so without this the
    model tends to answer in English. Kept explicit about what stays in English
    (terms, formulas, NCERT wording) and about preserving the English DTP-note
    format that the downstream parser depends on.
    """
    if (language_code or "en").lower() == "hi":
        return (
            "\n- LANGUAGE (MANDATORY): Write ALL notes in HINDI using Devanagari script — topic "
            "headings, bullets, and explanations. Even when the slide text is in English, the notes "
            "you write MUST be in Hindi. Keep ONLY scientific/technical terms, proper nouns, chemical "
            "formulas, numbers, units, and exact NCERT English wording in English — do not translate "
            "those. Keep the section label 'Concepts Covered in the Class' EXACTLY in English, keep "
            "every '(Note to DTP: ...)' line in the EXACT English bracket format shown, and keep the "
            "contents of every [[MATH_INLINE: ...]] / [[MATH_BLOCK: ...]] tag as LaTeX, never translated."
        )
    return "\n- LANGUAGE (MANDATORY): Write ALL notes in ENGLISH."


# Protected-maths rules. These REPLACE the old "write formulas in plain text,
# never use LaTeX" instruction — which is exactly why vector arrows and hats
# never reached the DOCX: the model was being told not to emit them.
_MATH_TAG_RULES = (
    "\n- MATHS NOTATION (ABSOLUTELY NON-NEGOTIABLE): Wrap EVERY formula, scripted symbol, "
    "chemical formula and unit-with-a-power in a protected maths tag. Inline: "
    "[[MATH_INLINE: latex]]. A displayed equation on its own line: [[MATH_BLOCK: latex]]. "
    "Write real LaTeX inside the tag."
    "\n- Vectors keep their arrows: [[MATH_INLINE: \\vec{A} \\times \\vec{B} = \\vec{C}]]. "
    "Unit vectors keep their hats: [[MATH_INLINE: \\hat{i} \\rightarrow \\hat{j} \\rightarrow \\hat{k}]]. "
    "Chemistry keeps subscripts: [[MATH_INLINE: H_2O]], [[MATH_INLINE: CO_2]], [[MATH_INLINE: SO_4^{2-}]]. "
    "Units keep negative powers: [[MATH_INLINE: m\\,s^{-2}]], [[MATH_INLINE: mol\\,L^{-1}]]. "
    "Scripts are preserved: [[MATH_INLINE: x^2]], [[MATH_INLINE: v_0]], [[MATH_INLINE: a_n = \\frac{v^2}{r}]]."
    "\n- NEVER write a formula as bare text (A x B = C, i -> j -> k, H2O, CO2, ms-2). "
    "NEVER drop a vector arrow, unit-vector hat, power or subscript. NEVER replace \\times "
    "with a plain 'x' when it means a cross product. NEVER turn a formula into words only."
    "\n- If the slide shows an arrow, hat, bar, dot, subscript, superscript, Greek letter, angle, "
    "degree, ±, root or fraction, reproduce it EXACTLY as LaTeX inside a maths tag."
    "\n- Outside maths tags use plain prose only — no LaTeX, no $...$, no code fences, and no "
    "markdown emphasis (* or **)."
)

# Legacy behaviour: plain-text formulas normalised by docx_writer. Kept for when
# "Strict equation preservation" is switched off.
_PLAIN_MATH_RULES = (
    "\n- FORMULAS: Write formulas in plain text. Use '_' for subscripts and '^' for "
    "superscripts (e.g. v_AB = v_A - v_B, v_A^2, x_{net}). Write symbols directly: "
    "√ × ÷ ≤ ≥ ± θ α β Δ π. Never use $...$, code fences, or markdown emphasis "
    "(* or **) inside a formula — an asterisk is only ever multiplication."
)


def build_generation_prompt(
    subject: str,
    mode: str,
    language_code: str,
    slides: list[SlideData],
    *,
    chunk_label: str = "",
    exam: str = "",
    strict_math: bool = True,
) -> str:
    template = load_prompt_template(subject, mode, language_code, exam)
    label = f"\nYou are processing chunk: {chunk_label}." if chunk_label else ""
    # Applies to EVERY subject: Concise Notes are a concept summary only.
    subject_rules = (
        "\n- NO QUESTIONS, NO SOLUTIONS (STRICT, ALL SUBJECTS): Do NOT include any question, "
        "MCQ, answer option, exercise, practice problem, solved example, illustration, numerical "
        "problem, or step-by-step solution/working — not even as an example. Include ONLY the "
        "concept summary: definitions, formulas, and key points. Keep formulas as reference, but "
        "omit worked-out solutions and the arithmetic of solving any specific question."
    )
    subject_rules += _MATH_TAG_RULES if strict_math else _PLAIN_MATH_RULES
    if normalize_mode(mode) == "summary":
        subject_rules += (
            "\n- SUMMARY MODE (STRICT): Keep it short. Capture only the key concepts and "
            "essential points as brief one-line bullets. Do NOT write long explanations, "
            "background, examples, or elaboration. Prefer the fewest words that preserve the "
            "meaning. Aim for roughly 10-15% of the source length. Every bullet must be a single "
            "short line."
        )
    return f"""{template}

Important automation instructions:
- Preserve original slide/page numbers in every DTP note.
- If a slide image is attached and text extraction misses handwritten annotations, read the image and include the instructional annotations.
- Exclude questions, answer options, QR/ads/homework/thank-you content.
- If a slide has both instructional content and question boxes, keep only the instructional content.
- Do not mention that you used AI. Output only the final notes content.{subject_rules}{_language_rule(language_code)}
{label}

SLIDE DATA STARTS BELOW

{format_slides(slides)}
""".strip()


def build_merge_prompt(
    subject: str,
    mode: str,
    language_code: str,
    partial_notes: list[str],
    *,
    exam: str = "",
    strict_math: bool = True,
) -> str:
    template = load_prompt_template(subject, mode, language_code, exam)
    joined = "\n\n".join(f"--- PART {i+1} ---\n{note}" for i, note in enumerate(partial_notes))
    brevity = "\n- SUMMARY MODE (STRICT): Keep the merged notes short and concise; one-line bullets, no long explanations or elaboration." if normalize_mode(mode) == "summary" else ""
    math_rule = (
        "\n- Keep every [[MATH_INLINE: ...]] and [[MATH_BLOCK: ...]] tag EXACTLY as written, "
        "including the LaTeX inside it. Never unwrap a maths tag into plain text, and never drop "
        "a vector arrow, hat, power or subscript while merging."
        if strict_math else
        "\n- Keep formula notation exactly as-is ('_' subscripts, '^' superscripts, plain symbols)."
    )
    return f"""{template}

You are merging partial notes from consecutive slide chunks into one final set of notes.
Rules:
- Preserve original topic order.
- Remove repeated Concepts Covered items.
- Merge duplicate headings only when they are the same heading from continuation slides.
- Keep DTP notes with their slide numbers exactly.
- Never introduce questions, solved examples, or step-by-step solutions; drop any that slipped in.{math_rule}
- Output only the final notes, no extra commentary.{brevity}{_language_rule(language_code)}

PARTIAL NOTES START BELOW

{joined}
""".strip()
