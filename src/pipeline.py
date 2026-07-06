from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .ai_client import GeminiClient, GeminiError, generate_mock_notes
from .config import LANGUAGE_CODES, RUNS_DIR, SUPPORTED_EXTENSIONS
from .docx_layout import derive_chapter_title
from .docx_writer import write_notes_docx
from .extract_pdf import extract_pdf
from .extract_pptx import extract_pptx
from .models import PipelineResult, SlideData
from .pdf_exporter import export_docx_to_pdf
from .prompt_builder import build_generation_prompt, build_merge_prompt
from .quality_checker import quality_check
from .slide_filter import filter_slides
from .utils import chunked, copy_input, ensure_dir, make_run_id, safe_name, write_json, zip_dir


def _load_slides_from_run(run_dir: Path) -> list[SlideData]:
    """Reconstruct SlideData objects from a run's slides_raw.json (for rebuilds)."""
    import json

    data = json.loads((run_dir / "slides_raw.json").read_text(encoding="utf-8"))
    slides: list[SlideData] = []
    for s in data:
        img = s.get("image_path")
        slides.append(
            SlideData(
                slide_no=s["slide_no"],
                heading=s.get("heading", ""),
                text=s.get("text", ""),
                cleaned_text=s.get("cleaned_text", ""),
                image_path=Path(img) if img else None,
                source_type=s.get("source_type", ""),
                metadata=s.get("metadata", {}) or {},
            )
        )
    return slides


def rebuild_outputs(
    run_dir: Path,
    edited_notes_text: str,
    *,
    stem: str,
    image_insert_mode: str = "smart_crop",
    dtp_note_policy: str = "keep_note_and_insert_image",
    subject: str = "",
    chapter_title: str | None = None,
) -> tuple[Path, Path | None, list[str]]:
    """Regenerate DOCX/PDF from (possibly user-edited) notes, reusing a run folder.

    Returns (docx_path, pdf_path, warnings). Used by the UI's edit-and-rebuild flow.
    Subject/chapter default to values recorded in run_metadata.json when omitted.
    """
    import json

    run_dir = Path(run_dir)
    slides = _load_slides_from_run(run_dir)
    meta_path = run_dir / "run_metadata.json"
    if (not subject or chapter_title is None) and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            subject = subject or meta.get("subject", "")
            if chapter_title is None:
                chapter_title = derive_chapter_title(Path(meta.get("input_file", stem)).stem)
        except Exception:
            pass
    if chapter_title is None:
        chapter_title = derive_chapter_title(stem)
    output_dir = ensure_dir(run_dir / "output")
    stem = safe_name(stem)
    (run_dir / "notes_raw.txt").write_text(edited_notes_text, encoding="utf-8")
    docx_path = output_dir / f"{stem}_handwritten_notes.docx"
    docx_path, warnings = write_notes_docx(
        edited_notes_text, docx_path, slides, run_dir=run_dir,
        image_insert_mode=image_insert_mode, dtp_note_policy=dtp_note_policy,
        subject=subject, chapter_title=chapter_title,
    )
    pdf_path, pdf_warning = export_docx_to_pdf(docx_path, output_dir)
    if pdf_warning:
        warnings.append(pdf_warning)
    return docx_path, pdf_path, warnings


def _extract_input(input_path: Path, run_dir: Path) -> tuple[list[SlideData], list[str]]:
    ext = input_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}. Supported: {sorted(SUPPORTED_EXTENSIONS)}")
    if ext == ".pdf":
        return extract_pdf(input_path, run_dir), []
    if ext in {".pptx", ".ppt"}:
        return extract_pptx(input_path, run_dir)
    raise ValueError(f"Unsupported file type: {ext}")


def run_pipeline(
    input_path: Path,
    *,
    subject: str,
    language: str,
    mode: str,
    api_key: str = "",
    model: str = "gemini-2.5-pro",
    provider: str = "auto",
    send_images_to_ai: bool = True,
    strict_filter: bool = True,
    batch_size: int | None = None,
    max_images_per_call: int = 8,
    allow_mock: bool = False,
    image_insert_mode: str = "smart_crop",
    dtp_note_policy: str = "keep_note_and_insert_image",
    ai_redraw_diagrams: bool = False,
    image_model: str = "gemini-2.5-flash-image",
) -> PipelineResult:
    input_path = Path(input_path)
    run_dir = ensure_dir(RUNS_DIR / make_run_id("run"))
    warnings: list[str] = []
    raw_notes_path: Path | None = None
    docx_path: Path | None = None
    pdf_path: Path | None = None
    zip_path: Path | None = None
    try:
        copied_input = copy_input(input_path, run_dir / "input")
        slides, extract_warnings = _extract_input(copied_input, run_dir)
        warnings.extend(extract_warnings)
        if not subject or subject.strip().lower() in {"auto", ""}:
            from .subject_detect import detect_subject

            slide_text = "\n".join((s.heading or "") + " " + (s.text or "") for s in slides[:12])
            subject = detect_subject(input_path.stem, slide_text)
            warnings.append(f"Subject auto-detected as: {subject}")
        write_json(run_dir / "slides_raw.json", [s.__dict__ for s in slides])
        active_slides, filter_report = filter_slides(slides, strict=strict_filter)
        write_json(run_dir / "filter_report.json", filter_report)
        write_json(run_dir / "slides_for_ai.json", [s.__dict__ for s in active_slides])
        if not active_slides:
            raise RuntimeError("No slides/pages left after filtering. Disable strict filtering and try again.")
        language_code = LANGUAGE_CODES.get(language, language).lower()
        if batch_size is None or batch_size <= 0:
            batch_size = 6 if send_images_to_ai else 18
        partial_notes: list[str] = []
        api_metadata: list[dict] = []
        client = None
        if allow_mock and not api_key:
            notes_text = generate_mock_notes(subject, mode, language_code, len(active_slides))
            partial_notes = [notes_text]
            api_metadata.append({"provider": "mock", "model": "mock"})
        else:
            client = GeminiClient(api_key=api_key, model=model, provider=provider)
            chunks = list(chunked(active_slides, batch_size))

            def _run_chunk(i: int, slide_chunk):
                prompt = build_generation_prompt(subject, mode, language_code, slide_chunk, chunk_label=f"{i}/{len(chunks)}")
                images = [s.image_path for s in slide_chunk if s.image_path] if send_images_to_ai else []
                resp = client.generate(prompt, images, max_images=max_images_per_call)
                (run_dir / f"notes_chunk_{i:02d}.txt").write_text(resp.text, encoding="utf-8")
                return i, resp

            # Run chunk calls concurrently (they are network-bound). Cap workers to
            # stay clear of rate limits; the client already retries on 429.
            chunk_results: dict[int, object] = {}
            with ThreadPoolExecutor(max_workers=min(4, max(1, len(chunks)))) as ex:
                futures = [ex.submit(_run_chunk, i, ch) for i, ch in enumerate(chunks, start=1)]
                for fut in as_completed(futures):
                    try:
                        i, resp = fut.result()
                    except GeminiError:
                        raise
                    except Exception as e:
                        raise GeminiError(f"Gemini generation failed: {e}") from e
                    chunk_results[i] = resp
            for i in sorted(chunk_results):
                resp = chunk_results[i]
                partial_notes.append(resp.text)
                api_metadata.append({"provider": resp.provider, "model": resp.model, "chunk": i})
            if len(partial_notes) == 1:
                notes_text = partial_notes[0]
            else:
                merge_prompt = build_merge_prompt(subject, mode, language_code, partial_notes)
                try:
                    resp = client.generate(merge_prompt, [], max_images=0)
                    notes_text = resp.text
                    api_metadata.append({"provider": resp.provider, "model": resp.model, "chunk": "merge"})
                except Exception as e:
                    warnings.append(f"Merge call failed; concatenating chunk outputs instead. Error: {e}")
                    notes_text = "\n\n".join(partial_notes)
        raw_notes_path = run_dir / "notes_raw.txt"
        raw_notes_path.write_text(notes_text, encoding="utf-8")

        # Optional: AI-redraw the diagrams referenced by DTP notes into handwritten
        # blue-on-white style, then insert those instead of the raw slide crops.
        if ai_redraw_diagrams and client is not None:
            from .dtp_parser import find_dtp_notes
            from .image_ai import redraw_slides_handwritten

            dtp_slide_nums = {n.slide_no for n in find_dtp_notes(notes_text) if n.slide_no}
            if dtp_slide_nums:
                redraw_dir = ensure_dir(run_dir / "ai_diagrams")
                n_redrawn, redraw_warnings = redraw_slides_handwritten(client, slides, dtp_slide_nums, redraw_dir, image_model=image_model)
                warnings.extend(redraw_warnings)
                warnings.append(f"AI-redrew {n_redrawn} diagram image(s) in handwritten style.")
                api_metadata.append({"provider": "image", "model": image_model, "redrawn": n_redrawn})

        output_dir = ensure_dir(run_dir / "output")
        stem = safe_name(input_path.stem)
        docx_path = output_dir / f"{stem}_handwritten_notes.docx"
        chapter_title = derive_chapter_title(input_path.stem)
        docx_path, docx_warnings = write_notes_docx(notes_text, docx_path, slides, run_dir=run_dir, image_insert_mode=image_insert_mode, dtp_note_policy=dtp_note_policy, subject=subject, chapter_title=chapter_title)
        warnings.extend(docx_warnings)
        pdf_path, pdf_warning = export_docx_to_pdf(docx_path, output_dir)
        if pdf_warning:
            warnings.append(pdf_warning)
        warnings.extend(quality_check(notes_text, slides, docx_path, pdf_path, send_images_to_ai=send_images_to_ai))
        write_json(run_dir / "run_metadata.json", {"input_file": str(input_path), "copied_input": str(copied_input), "subject": subject, "language": language_code, "mode": mode, "model": model, "provider_requested": provider, "send_images_to_ai": send_images_to_ai, "strict_filter": strict_filter, "batch_size": batch_size, "max_images_per_call": max_images_per_call, "api_calls": api_metadata, "warnings": warnings, "docx_path": str(docx_path) if docx_path else None, "pdf_path": str(pdf_path) if pdf_path else None})
        zip_path = zip_dir(run_dir, run_dir.with_suffix(".zip"))
        return PipelineResult(run_dir=run_dir, docx_path=docx_path, pdf_path=pdf_path, zip_path=zip_path, raw_notes_path=raw_notes_path, warnings=warnings, metadata={"api_calls": api_metadata})
    except Exception as e:
        warnings.append(str(e))
        try:
            write_json(run_dir / "error.json", {"error": str(e), "warnings": warnings})
            zip_path = zip_dir(run_dir, run_dir.with_suffix(".zip"))
        except Exception:
            pass
        raise


def run_batch(
    input_paths: list[Path],
    *,
    progress_callback=None,
    **kwargs,
) -> list[tuple[Path, PipelineResult | None, str | None]]:
    """Run the pipeline over many files, isolating per-file failures.

    Returns a list of (input_path, result_or_None, error_message_or_None) so one
    bad file never aborts the whole batch. `progress_callback(index, total, path)`
    is called before each file if provided.
    """
    results: list[tuple[Path, PipelineResult | None, str | None]] = []
    total = len(input_paths)
    for i, path in enumerate(input_paths, start=1):
        path = Path(path)
        if progress_callback:
            try:
                progress_callback(i, total, path)
            except Exception:
                pass
        try:
            result = run_pipeline(path, **kwargs)
            results.append((path, result, None))
        except Exception as e:
            results.append((path, None, str(e)))
    return results
