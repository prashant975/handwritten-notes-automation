from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import time

import pw_access

from . import version
from .ai_client import GeminiClient, GeminiError, generate_mock_notes
from .config import DEFAULT_MATH_RENDER_MODE, LANGUAGE_CODES, RUNS_DIR, SUPPORTED_EXTENSIONS
from .docx_layout import derive_chapter_title
from .docx_writer import write_notes_docx
from .equation_quality_checker import check_equations, warnings_from_report, write_reports
from .equation_repair import repair_equations
from .extract_pdf import extract_pdf
from .extract_pptx import extract_pptx
from .math_ocr import enrich_math_slides
from .models import PipelineResult, SlideData
from .pdf_exporter import export_docx_to_pdf
from .prompt_builder import build_generation_prompt, build_merge_prompt
from .quality_checker import quality_check
from .slide_filter import filter_slides
from .utils import chunked, copy_input, ensure_dir, make_run_id, preserve_filename, write_json, zip_dir


def _api_call_metadata(resp, **extra) -> dict:
    metadata = {"provider": resp.provider, "model": resp.model}
    metadata.update(extra)
    if getattr(resp, "usage", None):
        metadata.update(resp.usage)
    return metadata


def _model_is_pro(model: str) -> bool:
    return "pro" in (model or "").lower()


def _needs_vision(slide, *, processing_mode: str, vision_on: bool) -> bool:
    """Decide whether THIS slide's image must go to the AI.

    Sending every full-slide image is the biggest avoidable cost. A slide only
    needs vision when the image actually carries content:
      * extraction basically failed (empty/tiny text) — needed in ANY mode,
      * Balanced — only image-dominant slides (little text),
      * High Quality — every slide image (thorough),
      * Fast — nothing beyond the failed-extraction case.
    """
    if not getattr(slide, "image_path", None):
        return False
    text = (slide.prompt_text or "").strip()
    if len(text) < 40:
        return True                       # no usable text — the image is the slide
    if not vision_on:
        return False
    if processing_mode == "high_quality":
        return True
    if processing_mode == "balanced":
        return len(text) < 220            # image-dominant slide
    return False                          # fast mode: text was fine, skip the image


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
    google_token: "str | Callable[..., str]" = "",
    model: str = "gemini-2.5-pro",
    send_images_to_ai: bool = True,
    strict_filter: bool = True,
    batch_size: int | None = None,
    max_images_per_call: int = 4,
    allow_mock: bool = False,
    image_insert_mode: str = "smart_crop",
    dtp_note_policy: str = "keep_note_and_insert_image",
    ai_redraw_diagrams: bool = True,
    image_model: str = "gemini-2.5-flash-image",
    exam: str = "",
    strict_math: bool = True,
    math_render_mode: str = DEFAULT_MATH_RENDER_MODE,
    retry_callback: "Callable[[str], None] | None" = None,
    # --- speed-mode + model routing (see src/model_router.py) ---
    processing_mode: str = "balanced",
    notes_model: str | None = None,      # defaults to `model`
    vision_model: str | None = None,     # defaults to notes_model
    qc_model: str | None = None,
    qc_level: str = "basic",             # "off" | "basic" | "strict"
    routing_summary: dict | None = None,
) -> PipelineResult:
    started = time.monotonic()
    started_at = version.format_datetime()
    notes_model = (notes_model or model or "gemini-2.5-pro").strip()
    vision_model = (vision_model or notes_model).strip()
    model = notes_model                  # notes model is the primary generation model
    input_path = Path(input_path)
    # Every entry point (Streamlit, CLI, tests, or a future caller) must pass the
    # proxy gate immediately before a paid task.  Keeping the gate here prevents
    # a secondary entry point from accidentally bypassing the UI-level check.
    # check_allowed fails closed for missing tokens, proxy errors, and denials.
    if not allow_mock and not pw_access.check_allowed(google_token):
        raise PermissionError("Not authorized for this app.")
    run_dir = ensure_dir(RUNS_DIR / make_run_id("run"))
    warnings: list[str] = []
    raw_notes_path: Path | None = None
    docx_path: Path | None = None
    pdf_path: Path | None = None
    zip_path: Path | None = None
    usage_session = None
    try:
        copied_input = copy_input(input_path, run_dir / "input")
        slides, extract_warnings = _extract_input(copied_input, run_dir)
        warnings.extend(extract_warnings)
        if not subject or subject.strip().lower() in {"auto", ""}:
            from .subject_detect import detect_subject

            slide_text = "\n".join((s.heading or "") + " " + (s.text or "") for s in slides[:30])
            subject = detect_subject(input_path.stem, slide_text)
            warnings.append(f"Subject auto-detected as: {subject}")
        write_json(run_dir / "slides_raw.json", [s.__dict__ for s in slides])
        active_slides, filter_report = filter_slides(slides, strict=strict_filter)
        if not active_slides:
            raise RuntimeError("No slides/pages left after filtering. Disable strict filtering and try again.")
        language_code = LANGUAGE_CODES.get(language, language).lower()
        if batch_size is None or batch_size <= 0:
            # Small image batches keep each Vertex request lean (ai_client
            # downscales pages to JPEG under an 18 MB budget) and keep the
            # model's attention per page high; text-only chunks can be larger.
            batch_size = 4 if send_images_to_ai else 18
        partial_notes: list[str] = []
        api_metadata: list[dict] = []
        total_slides = len(slides)
        active_slide_count = len(active_slides)
        client = None
        vision_slide_total = 0
        # One UsageSession per file (=per task). Every Gemini call below is tagged
        # with `session=` so the proxy accumulates their usage and writes ONE
        # combined Gemini row per file to the `Usage Cost` tab on flush().
        usage_session = pw_access.UsageSession(
            google_token,
            filename=input_path.name,
            input_unit="No. of pages",
            count=active_slide_count,
        )
        # Mathpix is purpose-built for printed and handwritten equation OCR.
        # Scan mathematics slides through the PW proxy, then filter again using
        # the recovered image text before Gemini sees the deck.
        if subject.strip().lower() == "mathematics" and not (allow_mock and not google_token):
            warnings.extend(enrich_math_slides(active_slides, google_token, usage_session))
            active_slides, post_ocr_report = filter_slides(active_slides, strict=strict_filter)
            filter_report.extend({**entry, "stage": "post_math_ocr"} for entry in post_ocr_report)
            active_slide_count = len(active_slides)
            if not active_slides:
                raise RuntimeError("No instructional slides remained after promotion/question filtering.")
        write_json(run_dir / "filter_report.json", filter_report)
        write_json(run_dir / "slides_for_ai.json", [s.__dict__ for s in active_slides])
        if allow_mock and not google_token:  # a provider callable is truthy -> real run
            notes_text = generate_mock_notes(subject, mode, language_code, len(active_slides))
            partial_notes = [notes_text]
            api_metadata.append({"provider": "mock", "model": "mock"})
            usage_session = None
        else:
            # Notes model handles text chunks; the (possibly different) vision
            # model handles any chunk carrying slide images. Both share the one
            # UsageSession so cost still collapses to one row per model.
            notes_client = GeminiClient(
                google_token, model=notes_model, session=usage_session,
                retry_callback=retry_callback,
            )
            vision_client = notes_client if vision_model == notes_model else GeminiClient(
                google_token, model=vision_model, session=usage_session,
                retry_callback=retry_callback,
            )
            client = notes_client        # used by the optional diagram-redraw path
            chunks = list(chunked(active_slides, batch_size))

            def _run_chunk(i: int, slide_chunk):
                prompt = build_generation_prompt(subject, mode, language_code, slide_chunk, chunk_label=f"{i}/{len(chunks)}", exam=exam, strict_math=strict_math)
                # Selective vision: only attach images that actually carry content.
                vision_slides = [
                    s for s in slide_chunk
                    if _needs_vision(s, processing_mode=processing_mode, vision_on=send_images_to_ai)
                ]
                images = [s.image_path for s in vision_slides]
                client = vision_client if images else notes_client
                resp = client.generate(prompt, images, max_images=max_images_per_call)
                (run_dir / f"notes_chunk_{i:02d}.txt").write_text(resp.text, encoding="utf-8")
                return i, resp, len(vision_slides)

            # Run chunk calls concurrently (they are network-bound). Cap workers to
            # stay clear of rate limits; the client retries transient 429/5xx.
            chunk_results: dict[int, object] = {}
            with ThreadPoolExecutor(max_workers=min(4, max(1, len(chunks)))) as ex:
                futures = [ex.submit(_run_chunk, i, ch) for i, ch in enumerate(chunks, start=1)]
                try:
                    for fut in as_completed(futures):
                        try:
                            i, resp, n_vision = fut.result()
                        except GeminiError:
                            raise
                        except Exception as e:
                            raise GeminiError(f"Gemini generation failed: {e}") from e
                        chunk_results[i] = resp
                        vision_slide_total += n_vision
                except BaseException:
                    # One chunk failed terminally — abandon the queued chunks so
                    # they don't keep calling (and billing) Gemini pointlessly.
                    ex.shutdown(wait=False, cancel_futures=True)
                    raise
            images_dropped = 0
            for i in sorted(chunk_results):
                resp = chunk_results[i]
                partial_notes.append(resp.text)
                images_dropped += getattr(resp, "images_dropped", 0)
                api_metadata.append(_api_call_metadata(resp, chunk=i))
            if images_dropped:
                warnings.append(
                    f"{images_dropped} slide image(s) were omitted from AI calls to fit "
                    "the request size limit; those pages were processed from text only."
                )
            if len(partial_notes) == 1:
                notes_text = partial_notes[0]
            else:
                merge_prompt = build_merge_prompt(subject, mode, language_code, partial_notes, exam=exam, strict_math=strict_math)
                try:
                    resp = notes_client.generate(merge_prompt, [], max_images=0)
                    notes_text = resp.text
                    api_metadata.append(_api_call_metadata(resp, chunk="merge"))
                except Exception as e:
                    warnings.append(f"Merge call failed; concatenating chunk outputs instead. Error: {e}")
                    notes_text = "\n\n".join(partial_notes)
        raw_notes_path = run_dir / "notes_raw.txt"
        raw_notes_path.write_text(notes_text, encoding="utf-8")

        # Equation safety net: repair formulas the model left as plain text, then
        # report whatever notation is still missing. Repairs are recorded rather
        # than applied silently.
        # Equation repair is a correctness step (keeps math notation), so it runs
        # whenever strict_math is on regardless of QC level. The equation QUALITY
        # report/warnings are the "QC" part and are skipped when QC is off.
        equation_repairs: list[dict] = []
        if strict_math:
            notes_text, equation_repairs = repair_equations(notes_text)
            if equation_repairs:
                (run_dir / "notes_repaired.txt").write_text(notes_text, encoding="utf-8")
                warnings.append(
                    f"Repaired {len(equation_repairs)} formula(s) that were written as plain text."
                )
        equation_report = {"tagged_formula_count": 0, "issue_count": 0,
                           "issues": [], "issues_by_type": {}, "passed": True}
        equation_warnings: list[str] = []
        if qc_level != "off":
            equation_report = check_equations(notes_text)
            write_reports(run_dir, equation_report, equation_repairs)
            equation_warnings = warnings_from_report(equation_report)
            warnings.extend(equation_warnings)

        # Optional: AI-redraw the diagrams referenced by DTP notes into handwritten
        # blue-on-white style, then insert those instead of the raw slide crops.
        if ai_redraw_diagrams and client is not None:
            from .dtp_parser import find_dtp_notes
            from .image_ai import redraw_slides_handwritten

            dtp_slide_nums = {n.slide_no for n in find_dtp_notes(notes_text) if n.slide_no}
            if dtp_slide_nums:
                redraw_dir = ensure_dir(run_dir / "ai_diagrams")
                # Only slides that survived the strict content filter may enter
                # the redraw/insertion path.  The original list contains QR,
                # promotion and ASQ/MCQ slides for audit purposes only.
                n_redrawn, redraw_warnings = redraw_slides_handwritten(
                    client, active_slides, dtp_slide_nums, redraw_dir, image_model=image_model
                )
                warnings.extend(redraw_warnings)
                warnings.append(f"AI-redrew {n_redrawn} diagram image(s) in handwritten style.")
                api_metadata.append({"provider": "image", "model": image_model, "redrawn": n_redrawn})

        # All Gemini calls for this file are done — write the single combined
        # Usage Cost row via the proxy (one Gemini row, tokens + cost summed).
        usage_logged = False
        if usage_session is not None:
            usage_logged = usage_session.flush() is not None

        output_dir = ensure_dir(run_dir / "output")
        chapter_title = derive_chapter_title(input_path.stem)
        # Output name = the uploaded file's name kept EXACTLY (spaces, case,
        # Hindi, etc. preserved) + "_Concise_Notes".
        output_stem = preserve_filename(input_path.stem)
        docx_path = output_dir / f"{output_stem}_Concise_Notes.docx"
        docx_path, docx_warnings = write_notes_docx(
            notes_text, docx_path, active_slides, run_dir=run_dir,
            image_insert_mode=image_insert_mode, dtp_note_policy=dtp_note_policy,
            subject=subject, chapter_title=chapter_title,
            math_render_mode=math_render_mode,
        )
        warnings.extend(docx_warnings)
        pdf_path, pdf_warning = export_docx_to_pdf(docx_path, output_dir)
        if pdf_warning:
            warnings.append(pdf_warning)
        warnings.extend(quality_check(
            notes_text, active_slides, docx_path, pdf_path,
            send_images_to_ai=send_images_to_ai,
        ))
        # ---- Run summary: version, timing, models used, fallback/pro flags ----
        routing_summary = routing_summary or {}
        pro_used = bool(routing_summary.get("pro_used")) or _model_is_pro(notes_model) or _model_is_pro(vision_model)
        run_summary = {
            "app_version": version.APP_VERSION,
            "app_name": version.APP_NAME,
            "started_at": started_at,
            "ended_at": version.format_datetime(),
            "generated_at": version.iso_now(),
            "total_processing_seconds": round(time.monotonic() - started, 1),
            "processing_mode": processing_mode,
            "slides_processed": active_slide_count,
            "slides_sent_to_vision": vision_slide_total,
            "notes_model": notes_model,
            "vision_model": vision_model,
            "qc_model": (qc_model if qc_level != "off" else None),
            "qc_level": qc_level,
            "fallback_used": bool(routing_summary.get("fallback_used")),
            "pro_used": pro_used,
            "routing_reasons": routing_summary.get("reasons", []),
        }
        write_json(run_dir / "run_log.json", {
            **run_summary,
            "strict_math": strict_math,
            "math_render_mode": math_render_mode,
            "exam": exam,
            "equation_repairs": equation_repairs,
            "equation_issues": equation_report.get("issues", []),
            "tagged_formula_count": equation_report.get("tagged_formula_count", 0),
        })
        run_metadata = {
            "run_summary": run_summary,
            "input_file": str(input_path),
            "copied_input": str(copied_input),
            "subject": subject,
            "language": language_code,
            "mode": mode,
            "exam": exam,
            "strict_math": strict_math,
            "math_render_mode": math_render_mode,
            "equation_repairs": len(equation_repairs),
            "equation_issues": equation_report.get("issue_count", 0),
            "tagged_formula_count": equation_report.get("tagged_formula_count", 0),
            "equation_warnings": equation_warnings,
            "model": model,
            "provider_requested": "pw_proxy",
            "usage_logged": usage_logged,
            "send_images_to_ai": send_images_to_ai,
            "strict_filter": strict_filter,
            "batch_size": batch_size,
            "max_images_per_call": max_images_per_call,
            "total_slides": total_slides,
            "active_slide_count": active_slide_count,
            "input_unit": "slide/page",
            "api_calls": api_metadata,
            "warnings": warnings,
            "docx_path": str(docx_path) if docx_path else None,
            "pdf_path": str(pdf_path) if pdf_path else None,
        }
        write_json(run_dir / "run_metadata.json", run_metadata)
        zip_path = zip_dir(run_dir, run_dir.with_suffix(".zip"))
        return PipelineResult(run_dir=run_dir, docx_path=docx_path, pdf_path=pdf_path, zip_path=zip_path, raw_notes_path=raw_notes_path, warnings=warnings, metadata=run_metadata)
    except Exception as e:
        warnings.append(str(e))
        # Vertex is billed per successful call, so log whatever usage already
        # accumulated before the failure — otherwise real cost goes unrecorded.
        if usage_session is not None:
            try:
                usage_session.flush()
            except Exception:
                pass
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
