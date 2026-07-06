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


def build_generation_prompt(subject: str, mode: str, language_code: str, slides: list[SlideData], *, chunk_label: str = "") -> str:
    template = load_prompt_template(subject, mode, language_code)
    label = f"\nYou are processing chunk: {chunk_label}." if chunk_label else ""
    subject_rules = ""
    if subject.lower() == "physics":
        subject_rules = (
            "\n- PHYSICS: Do NOT include any numerical problem solutions, solved examples, "
            "or step-by-step working. Include only the concept summary and instructional "
            "notes (definitions, formulas, key points). Keep formulas as reference, but omit "
            "worked-out solutions and the arithmetic of solving a specific question."
        )
    return f"""{template}

Important automation instructions:
- Preserve original slide/page numbers in every DTP note.
- If a slide image is attached and text extraction misses handwritten annotations, read the image and include the instructional annotations.
- Exclude questions, answer options, QR/ads/homework/thank-you content.
- If a slide has both instructional content and question boxes, keep only the instructional content.
- Do not mention that you used AI. Output only the final notes content.{subject_rules}
{label}

SLIDE DATA STARTS BELOW

{format_slides(slides)}
""".strip()


def build_merge_prompt(subject: str, mode: str, language_code: str, partial_notes: list[str]) -> str:
    template = load_prompt_template(subject, mode, language_code)
    joined = "\n\n".join(f"--- PART {i+1} ---\n{note}" for i, note in enumerate(partial_notes))
    return f"""{template}

You are merging partial notes from consecutive slide chunks into one final set of notes.
Rules:
- Preserve original topic order.
- Remove repeated Concepts Covered items.
- Merge duplicate headings only when they are the same heading from continuation slides.
- Keep DTP notes with their slide numbers exactly.
- Output only the final notes, no extra commentary.

PARTIAL NOTES START BELOW

{joined}
""".strip()
