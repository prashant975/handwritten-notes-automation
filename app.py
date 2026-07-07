from __future__ import annotations

import re
import tempfile
from pathlib import Path

import streamlit as st

from src.ai_client import GeminiError
from src.config import APP_NAME, DEFAULT_MODEL, DEFAULT_PROVIDER, GEMINI_API_KEY
from src.pipeline import run_pipeline

st.set_page_config(page_title=APP_NAME, layout="wide")
st.title("Handwritten Notes Automation")
st.caption("Upload a lecture PPT/PDF and download PW-style handwritten notes.")

# ---- Fixed configuration (no user-facing settings) -------------------------
LANGUAGE = "English"
MODE = "summary"
SUBJECT = "auto"            # auto-detected from the file
SEND_IMAGES = True
STRICT_FILTER = True
DTP_POLICY = "hide_note_insert_image"
IMAGE_MODE = "smart_crop"
AI_REDRAW = True
IMAGE_MODEL = "gemini-2.5-flash-image"

if "results" not in st.session_state:
    st.session_state.results = []


def _result_to_dict(name: str, result) -> dict:
    notes = ""
    if result.raw_notes_path and result.raw_notes_path.exists():
        notes = result.raw_notes_path.read_text(encoding="utf-8")
    return {
        "name": name,
        "run_dir": str(result.run_dir),
        "docx": str(result.docx_path) if result.docx_path else None,
        "pdf": str(result.pdf_path) if result.pdf_path else None,
        "zip": str(result.zip_path) if result.zip_path else None,
        "notes": notes,
        "error": None,
    }


def _notes_to_markdown(notes: str) -> str:
    out: list[str] = []
    for raw in notes.splitlines():
        line = raw.rstrip()
        if not line.strip():
            out.append("")
            continue
        leading = len(raw) - len(raw.lstrip(" \t"))
        level = min(3, leading // 4)
        content = line.strip()
        bullet_m = re.match(r"^(\*|•|·|-|–)\s+(.*)$", content)
        if bullet_m:
            out.append("  " * level + "- " + bullet_m.group(2).strip())
        elif content[0].isdigit() and content[1:2] in {".", ")"}:
            out.append("  " * level + content)
        elif "note to dtp" in content.lower():
            continue  # hidden in output, so hide in preview too
        else:
            out.append(content)
    return "\n".join(out)


uploaded_files = st.file_uploader("Upload lecture file(s)", type=["pdf", "pptx", "ppt"], accept_multiple_files=True)
run_button = st.button("Generate handwritten notes", type="primary", disabled=not uploaded_files)

if run_button and uploaded_files:
    if not GEMINI_API_KEY:
        st.error("No Gemini API key configured. Add GEMINI_API_KEY to the .env file.")
    else:
        st.session_state.results = []
        total = len(uploaded_files)
        progress = st.progress(0.0, text=f"Starting {total} file(s)...")
        with tempfile.TemporaryDirectory() as tmpdir:
            for i, uploaded in enumerate(uploaded_files, start=1):
                progress.progress((i - 1) / total, text=f"[{i}/{total}] {uploaded.name}")
                tmp_path = Path(tmpdir) / uploaded.name
                tmp_path.write_bytes(uploaded.getbuffer())
                try:
                    result = run_pipeline(
                        tmp_path, subject=SUBJECT, language=LANGUAGE, mode=MODE,
                        api_key=GEMINI_API_KEY, model=DEFAULT_MODEL, provider=DEFAULT_PROVIDER,
                        send_images_to_ai=SEND_IMAGES, strict_filter=STRICT_FILTER,
                        allow_mock=False, image_insert_mode=IMAGE_MODE, dtp_note_policy=DTP_POLICY,
                        ai_redraw_diagrams=AI_REDRAW, image_model=IMAGE_MODEL,
                    )
                    st.session_state.results.append(_result_to_dict(uploaded.name, result))
                except GeminiError as e:
                    st.session_state.results.append({"name": uploaded.name, "error": f"Gemini API failed: {e}", "notes": "", "run_dir": None})
                except Exception as e:
                    st.session_state.results.append({"name": uploaded.name, "error": f"Processing failed: {e}", "notes": "", "run_dir": None})
        progress.progress(1.0, text="Done")
        ok = sum(1 for r in st.session_state.results if not r.get("error"))
        st.success(f"Completed: {ok}/{total} file(s).")


results = st.session_state.results
if results:
    st.divider()
    for idx, r in enumerate(results):
        with st.expander(r["name"] + ("  ❌" if r.get("error") else ""), expanded=(len(results) == 1 or bool(r.get("error")))):
            if r.get("error"):
                st.error(r["error"])
                continue
            c1, c2, c3 = st.columns(3)
            with c1:
                if r["docx"] and Path(r["docx"]).exists():
                    st.download_button("Download DOCX", Path(r["docx"]).read_bytes(), file_name=Path(r["docx"]).name, key=f"docx_{idx}")
            with c2:
                if r["pdf"] and Path(r["pdf"]).exists():
                    st.download_button("Download PDF", Path(r["pdf"]).read_bytes(), file_name=Path(r["pdf"]).name, key=f"pdf_{idx}")
            with c3:
                if r["zip"] and Path(r["zip"]).exists():
                    st.download_button("Download ZIP", Path(r["zip"]).read_bytes(), file_name=Path(r["zip"]).name, key=f"zip_{idx}")

            tab_preview, tab_images = st.tabs(["Preview", "Diagrams"])
            with tab_preview:
                st.markdown(_notes_to_markdown(r["notes"]))
            with tab_images:
                run_dir = Path(r["run_dir"]) if r["run_dir"] else None
                imgs = []
                if run_dir and (run_dir / "ai_diagrams").exists():
                    imgs = sorted((run_dir / "ai_diagrams").glob("*_t.png")) or sorted((run_dir / "ai_diagrams").glob("*.png"))
                if not imgs and run_dir and (run_dir / "inserted_images").exists():
                    imgs = sorted((run_dir / "inserted_images").glob("*.png"))
                if imgs:
                    cols = st.columns(2)
                    for j, img in enumerate(imgs):
                        with cols[j % 2]:
                            st.image(str(img), use_container_width=True)
                else:
                    st.caption("No diagrams inserted for this file.")
