from __future__ import annotations

import argparse
import os
from pathlib import Path
from src.config import (
    DEFAULT_IMAGE_MODEL,
    DEFAULT_MATH_RENDER_MODE,
    DEFAULT_MODEL,
    EXAMS,
    MATH_RENDER_MODES,
    MODES,
    SUBJECTS,
    SUPPORTED_EXTENSIONS,
)
from src.pipeline import run_batch, run_pipeline


def _collect_inputs(input_path: Path) -> list[Path]:
    if input_path.is_dir():
        files = sorted(p for p in input_path.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS)
        if not files:
            raise SystemExit(f"No supported files ({sorted(SUPPORTED_EXTENSIONS)}) found in folder: {input_path}")
        return files
    return [input_path]


def main():
    parser = argparse.ArgumentParser(description="Generate handwritten-style lecture notes from PPT/PDF.")
    parser.add_argument("--input", required=True, help="Path to a lecture PDF/PPTX/PPT, or a folder of them (batch mode)")
    parser.add_argument("--subject", required=True, choices=["auto"] + SUBJECTS, help="Lecture subject (required, matching the UI policy). 'auto' keyword-detects from the file — best effort; the detected subject is reported in the run warnings.")
    parser.add_argument("--language", default="English", choices=["English", "Hindi", "en", "hi"])
    parser.add_argument("--mode", default="summary", choices=MODES)
    parser.add_argument(
        "--exam",
        default="",
        choices=["", *EXAMS.values()],
        help="Use a JEE/NEET concise prompt: jee, neet, or jee_neet.",
    )
    parser.add_argument(
        "--math-render-mode",
        default=DEFAULT_MATH_RENDER_MODE,
        choices=list(MATH_RENDER_MODES.values()),
        help="DOCX equation output: omml (native Word), unicode, or plain debug.",
    )
    parser.add_argument(
        "--no-strict-math",
        action="store_true",
        help="Disable protected math-tag prompting, equation repair, and strict preservation.",
    )
    parser.add_argument("--google-token", default=os.getenv("PW_GOOGLE_TOKEN", ""), help="Signed-in @pw.live Google access/id token (or set PW_GOOGLE_TOKEN). Required unless --allow-mock is used; the PW proxy holds the Gemini key.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--no-images", action="store_true", help="Do not send rendered slide images to AI")
    parser.add_argument("--no-strict-filter", action="store_true")
    parser.add_argument("--allow-mock", action="store_true")
    parser.add_argument("--ai-redraw-diagrams", action="store_true", help="AI-redraw inserted diagrams in handwritten blue-on-white style (extra API quota)")
    parser.add_argument("--image-model", default=DEFAULT_IMAGE_MODEL, help="Gemini image model for diagram redraw (honours IMAGE_MODEL_NAME env var)")
    parser.add_argument("--dtp-note-policy", default="hide_note_insert_image", choices=["hide_note_insert_image", "keep_note_and_insert_image", "keep_note_only"], help="hide_note_insert_image removes the yellow DTP note and inserts only the image")
    args = parser.parse_args()
    inputs = _collect_inputs(Path(args.input))
    if not args.google_token and not args.allow_mock:
        raise SystemExit("No Google token. Pass --google-token (a signed-in @pw.live token) or set PW_GOOGLE_TOKEN. The PW proxy needs it to run Gemini. Use --allow-mock for a no-AI dry run.")
    # Pass a provider function, not a captured token string. This keeps the CLI
    # on the same pw_access contract as the UI and lets a launcher/token helper
    # replace PW_GOOGLE_TOKEN before a later proxy call.
    initial_token = args.google_token.strip()

    def google_token_provider(force: bool = False) -> str:
        del force  # The CLI has no OAuth refresh flow; re-read its environment.
        return str(os.getenv("PW_GOOGLE_TOKEN", "") or initial_token).strip()

    common = dict(
        subject=args.subject,
        language=args.language,
        mode=args.mode,
        exam=args.exam,
        strict_math=not args.no_strict_math,
        math_render_mode=args.math_render_mode,
        google_token=google_token_provider,
        model=args.model,
        send_images_to_ai=not args.no_images,
        strict_filter=not args.no_strict_filter,
        allow_mock=args.allow_mock,
        ai_redraw_diagrams=args.ai_redraw_diagrams,
        image_model=args.image_model,
        dtp_note_policy=args.dtp_note_policy,
    )

    if len(inputs) == 1:
        result = run_pipeline(inputs[0], **common)
        print(f"Run folder: {result.run_dir}")
        print(f"DOCX: {result.docx_path}")
        print(f"PDF: {result.pdf_path}")
        print(f"ZIP: {result.zip_path}")
        if result.warnings:
            print("Warnings:")
            for w in result.warnings:
                print(f"- {w}")
        return

    print(f"Batch mode: {len(inputs)} file(s)\n")

    def _progress(i, total, path):
        print(f"[{i}/{total}] Processing {path.name} ...")

    results = run_batch(inputs, progress_callback=_progress, **common)
    ok = sum(1 for _, r, _ in results if r)
    print(f"\nCompleted: {ok}/{len(results)} succeeded")
    for path, result, error in results:
        if result:
            print(f"- OK   {path.name} -> {result.docx_path}")
        else:
            print(f"- FAIL {path.name}: {error}")


if __name__ == "__main__":
    main()
