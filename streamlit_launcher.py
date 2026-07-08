from __future__ import annotations

import os
import sys
from pathlib import Path

from streamlit.web import bootstrap


def _bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def _runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main() -> None:
    bundle_dir = _bundle_dir()
    runtime_dir = _runtime_dir()
    external_env = runtime_dir / ".env"

    os.environ.setdefault("HANDWRITTEN_NOTES_ROOT", str(bundle_dir))
    os.environ.setdefault("HANDWRITTEN_NOTES_ENV", str(external_env if external_env.exists() else bundle_dir / ".env"))
    os.environ.setdefault("RUNS_DIR", str(runtime_dir / "runs"))
    os.environ.setdefault("OUTPUT_DIR", str(runtime_dir / "outputs"))
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "false")
    os.environ.setdefault("STREAMLIT_GLOBAL_DEVELOPMENT_MODE", "false")

    (runtime_dir / "runs").mkdir(exist_ok=True)
    (runtime_dir / "outputs").mkdir(exist_ok=True)

    app_path = bundle_dir / "app.py"
    sys.argv = ["streamlit", "run", str(app_path)]
    bootstrap.run(str(app_path), False, [], {})


if __name__ == "__main__":
    main()
