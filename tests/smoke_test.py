from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.dtp_parser import parse_dtp_note
from src.docx_writer import write_notes_docx
from src.models import SlideData

def test_dtp_parse():
    note = parse_dtp_note('(Note to DTP: Insert the image with "A" and "B" given on slide no. 9 under the heading "RIBOSOME".)')
    assert note.slide_no == 9
    assert note.heading == "RIBOSOME"

def test_docx(tmp_path):
    notes = 'Concepts Covered in the Class:\n• A\n\nHeading\n• Body point.'
    out, warnings = write_notes_docx(notes, tmp_path / 'out.docx', [SlideData(slide_no=1)], run_dir=tmp_path)
    assert out.exists()


def test_docx_never_inserts_filtered_question_slide(tmp_path):
    notes = (
        "Trigonometry\n"
        "• Compound-angle identity.\n"
        '(Note to DTP: Insert the image with "question" and "solution" '
        'given on slide no. 51 under the heading "ASQ-51".)'
    )
    slide = SlideData(
        slide_no=51,
        heading="ASQ-51",
        text="ASQ-51 then the value of sin 2A is:",
        image_path=tmp_path / "question.png",
        filtered=True,
        filter_reason="question/MCQ/worked-example slide",
    )
    out, warnings = write_notes_docx(
        notes,
        tmp_path / "blocked.docx",
        [slide],
        run_dir=tmp_path,
        dtp_note_policy="hide_note_insert_image",
    )
    assert out.exists()
    assert any("Skipped image from slide 51" in warning for warning in warnings)
    assert any("Inserted 0 image(s)" in warning for warning in warnings)
