"""Pytest bootstrap: put the repo root on sys.path so `import src...` and
`import pw_access` resolve when the suite is run with `pytest` from anywhere.

The standalone tests (test_auth_refresh, test_gemini_retry, ...) already insert
this themselves for `python tests/test_x.py`; the pytest-style ones
(test_pipeline_batch, test_math_slide_filter, test_image_ai) rely on this.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
