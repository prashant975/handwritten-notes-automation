from src.models import SlideData
from src.slide_filter import filter_slides, unwanted_slide_reason


def _filtered(text: str) -> tuple[bool, str | None]:
    kept, report = filter_slides([SlideData(slide_no=1, text=text, cleaned_text=text)], strict=True)
    return not bool(kept), report[0]["reason"]


def test_filters_qr_social_promotion():
    filtered, reason = _filtered("Let's grow together. Scan the QR @AMARNATHANANDSIR +91 88260 18464")
    assert filtered
    assert reason == "housekeeping/promotion/homework"


def test_filters_asq_worked_question():
    filtered, reason = _filtered("ASQ-54 The value of expression cos²73° + cos²47° is equal to:")
    assert filtered
    assert reason == "question/MCQ/worked-example slide"


def test_filters_value_of_expression_question():
    filtered, reason = _filtered(
        "The value of expression cos² 73° + cos² 47° is equal to: A 1/2 B 3/4 C 1 D 5/4"
    )
    assert filtered
    assert reason == "question/MCQ/worked-example slide"


def test_keeps_instructional_trigonometry():
    filtered, _ = _filtered("Compound-angle identities\nsin(A+B)=sin A cos B + cos A sin B")
    assert not filtered


def test_insertion_classifier_rejects_previously_filtered_slide():
    slide = SlideData(
        slide_no=9,
        text="Concept text extraction was incomplete",
        filtered=True,
        filter_reason="question/MCQ/worked-example slide",
    )
    assert unwanted_slide_reason(slide) == "question/MCQ/worked-example slide"
