from __future__ import annotations

import argparse
from pathlib import Path
from src.config import DEFAULT_MODEL, DEFAULT_PROVIDER, GEMINI_API_KEY, SUPPORTED_EXTENSIONS, extract_api_key
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
    parser.add_argument("--subject", required=True, choices=["biology", "physics", "chemistry"])
    parser.add_argument("--language", default="English", choices=["English", "Hindi", "en", "hi"])
    parser.add_argument("--mode", default="summary", choices=["summary", "complete"])
    parser.add_argument("--api-key", default=GEMINI_API_KEY, help="Gemini API key or full curl/URL containing ?key=")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, choices=["auto", "google_genai_sdk", "developer_rest", "aiplatform_rest"])
    parser.add_argument("--no-images", action="store_true", help="Do not send rendered slide images to AI")
    parser.add_argument("--no-strict-filter", action="store_true")
    parser.add_argument("--allow-mock", action="store_true")
    parser.add_argument("--ai-redraw-diagrams", action="store_true", help="AI-redraw inserted diagrams in handwritten blue-on-white style (extra API quota)")
    parser.add_argument("--image-model", default="gemini-2.5-flash-image", help="Gemini image model for diagram redraw")
    parser.add_argument("--dtp-note-policy", default="hide_note_insert_image", choices=["hide_note_insert_image", "keep_note_and_insert_image", "keep_note_only"], help="hide_note_insert_image removes the yellow DTP note and inserts only the image")
    args = parser.parse_args()
    inputs = _collect_inputs(Path(args.input))
    common = dict(subject=args.subject, language=args.language, mode=args.mode, api_key=extract_api_key(args.api_key), model=args.model, provider=args.provider, send_images_to_ai=not args.no_images, strict_filter=not args.no_strict_filter, allow_mock=args.allow_mock, ai_redraw_diagrams=args.ai_redraw_diagrams, image_model=args.image_model, dtp_note_policy=args.dtp_note_policy)

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
