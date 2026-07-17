from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def find_soffice() -> str | None:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if exe:
        return exe
    for c in [r"C:\Program Files\LibreOffice\program\soffice.exe", r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"]:
        if Path(c).exists():
            return c
    return None


def _export_with_libreoffice(docx_path: Path, output_dir: Path) -> tuple[Path | None, str | None]:
    soffice = find_soffice()
    if not soffice:
        return None, "LibreOffice/soffice was not found."
    try:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(output_dir), str(docx_path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
        pdf_path = output_dir / f"{docx_path.stem}.pdf"
        if pdf_path.exists():
            return pdf_path, None
        return None, "LibreOffice ran but did not create a PDF."
    except Exception as e:
        return None, f"LibreOffice PDF export failed: {e}"


def _export_with_word(docx_path: Path, output_dir: Path) -> tuple[Path | None, str | None]:
    if sys.platform != "win32":
        return None, "Microsoft Word export is available only on Windows."
    try:
        import pythoncom
        import win32com.client
    except Exception as e:
        return None, f"pywin32 is not installed or Word COM is unavailable: {e}"
    pdf_path = output_dir / f"{docx_path.stem}.pdf"
    word = None
    doc = None
    try:
        pythoncom.CoInitialize()
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(str(docx_path.resolve()))
        doc.SaveAs(str(pdf_path.resolve()), FileFormat=17)
        if pdf_path.exists():
            return pdf_path, None
        return None, "Microsoft Word ran but did not create a PDF."
    except Exception as e:
        return None, f"Microsoft Word PDF export failed: {e}"
    finally:
        # Always close/quit, even when Open or SaveAs raised — otherwise every
        # failed export leaks an invisible WINWORD.EXE holding the docx open.
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def export_docx_to_pdf(docx_path: Path, output_dir: Path) -> tuple[Path | None, str | None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf, err1 = _export_with_libreoffice(docx_path, output_dir)
    if pdf:
        return pdf, None
    pdf, err2 = _export_with_word(docx_path, output_dir)
    if pdf:
        return pdf, None
    return None, f"PDF export failed. {err1} {err2}"
