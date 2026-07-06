from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import extract_api_key
from src.dtp_parser import parse_dtp_note
from src.docx_writer import write_notes_docx
from src.models import SlideData

def test_key_extract():
    assert extract_api_key("https://x?key=ABC123") == "ABC123"
    assert extract_api_key("GEMINI_API_KEY=XYZ") == "XYZ"

def test_dtp_parse():
    note = parse_dtp_note('(Note to DTP: Insert the image with "A" and "B" given on slide no. 9 under the heading "RIBOSOME".)')
    assert note.slide_no == 9
    assert note.heading == "RIBOSOME"

def test_docx(tmp_path):
    notes = 'Concepts Covered in the Class:\n• A\n\nHeading\n• Body point.'
    out, warnings = write_notes_docx(notes, tmp_path / 'out.docx', [SlideData(slide_no=1)], run_dir=tmp_path)
    assert out.exists()
