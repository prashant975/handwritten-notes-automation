from pathlib import Path

from PIL import Image

from src.image_ai import HANDWRITTEN_PROMPT, redraw_diagram_handwritten


EXPECTED_PROMPT = (
    "Create a handwritten notes-style image with a white background and blue "
    "handwritten text. The diagram's structure should remain the same as in the "
    "original image. All text and formulas should be handwritten, clear, "
    "high-resolution, and in blue. The diagram lines will retain their original "
    "color, but any light colors that would not be visible on a white background "
    "will be changed to a dark color. The image should be high resolution."
)


class RecordingImageClient:
    def __init__(self, generated_bytes: bytes):
        self.generated_bytes = generated_bytes
        self.calls = []

    def generate_image(self, prompt, image_path, *, image_model):
        self.calls.append((prompt, Path(image_path), image_model))
        return self.generated_bytes


def test_redraw_uses_required_prompt_and_keeps_white_background(tmp_path):
    source = tmp_path / "source.png"
    generated = tmp_path / "generated.png"
    Image.new("RGB", (12, 12), "black").save(source)
    Image.new("RGB", (12, 12), "white").save(generated)
    client = RecordingImageClient(generated.read_bytes())

    result = redraw_diagram_handwritten(client, source, tmp_path / "out")

    assert HANDWRITTEN_PROMPT == EXPECTED_PROMPT
    assert client.calls == [
        (EXPECTED_PROMPT, source, "gemini-2.5-flash-image")
    ]
    assert result is not None
    assert result.name == "source_handwritten.png"
    with Image.open(result) as image:
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (255, 255, 255)
