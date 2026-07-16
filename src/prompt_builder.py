from __future__ import annotations

from .config import ROOT_DIR
from .models import SlideData

PROMPT_DIR = ROOT_DIR / "prompts"


def prompt_file_name(subject: str, mode: str, language_code: str) -> str:
    return f"{subject.lower()}_{mode.lower()}_{language_code.lower()}.txt"


def load_prompt_template(subject: str, mode: str, language_code: str) -> str:
    path = PROMPT_DIR / prompt_file_name(subject, mode, language_code)
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


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
            "those. Keep the section label 'Concepts Covered in the Class' EXACTLY in English, and "
            "keep every '(Note to DTP: ...)' line in the EXACT English bracket format shown."
        )
    return "\n- LANGUAGE (MANDATORY): Write ALL notes in ENGLISH."


def build_generation_prompt(subject: str, mode: str, language_code: str, slides: list[SlideData], *, chunk_label: str = "") -> str:
    template = load_prompt_template(subject, mode, language_code)
    label = f"\nYou are processing chunk: {chunk_label}." if chunk_label else ""
    # Applies to EVERY subject: Concise Notes are a concept summary only.
    subject_rules = (
        "\n- NO QUESTIONS, NO SOLUTIONS (STRICT, ALL SUBJECTS): Do NOT include any question, "
        "MCQ, answer option, exercise, practice problem, solved example, illustration, numerical "
        "problem, or step-by-step solution/working — not even as an example. Include ONLY the "
        "concept summary: definitions, formulas, and key points. Keep formulas as reference, but "
        "omit worked-out solutions and the arithmetic of solving any specific question."
        "\n- FORMULAS: Write formulas in plain text. Use '_' for subscripts and '^' for "
        "superscripts (e.g. v_AB = v_A - v_B, v_A^2, x_{net}). Write symbols directly: "
        "√ × ÷ ≤ ≥ ± θ α β Δ π. Never use LaTeX, $...$, code fences, or markdown emphasis "
        "(* or **) inside a formula — an asterisk is only ever multiplication."
    )
    if mode.lower() == "summary":
        subject_rules += (
            "\n- SUMMARY MODE (STRICT): Keep it short. Capture only the key concepts and "
            "essential points as brief one-line bullets. Do NOT write long explanations, "
            "background, examples, or elaboration. Prefer the fewest words that preserve the "
            "meaning. Aim for roughly 8-10% of the source length. Every bullet must be a single "
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


def build_merge_prompt(subject: str, mode: str, language_code: str, partial_notes: list[str]) -> str:
    template = load_prompt_template(subject, mode, language_code)
    joined = "\n\n".join(f"--- PART {i+1} ---\n{note}" for i, note in enumerate(partial_notes))
    brevity = "\n- SUMMARY MODE (STRICT): Keep the merged notes short and concise; one-line bullets, no long explanations or elaboration." if mode.lower() == "summary" else ""
    return f"""{template}

You are merging partial notes from consecutive slide chunks into one final set of notes.
Rules:
- Preserve original topic order.
- Remove repeated Concepts Covered items.
- Merge duplicate headings only when they are the same heading from continuation slides.
- Keep DTP notes with their slide numbers exactly.
- Never introduce questions, solved examples, or step-by-step solutions; drop any that slipped in.
- Keep formula notation exactly as-is ('_' subscripts, '^' superscripts, plain symbols).
- Output only the final notes, no extra commentary.{brevity}{_language_rule(language_code)}

PARTIAL NOTES START BELOW

{joined}
""".strip()
