from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SlideData:
    slide_no: int
    heading: str = ""
    text: str = ""
    cleaned_text: str = ""
    image_path: Path | None = None
    filtered: bool = False
    filter_reason: str | None = None
    source_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def prompt_text(self) -> str:
        return self.cleaned_text if self.cleaned_text.strip() else self.text


@dataclass
class PipelineResult:
    run_dir: Path
    docx_path: Path | None
    pdf_path: Path | None
    zip_path: Path | None
    raw_notes_path: Path | None
    warnings: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)
