from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
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


def _find_available_port(start: int = 8501, attempts: int = 100) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free localhost port found from {start} to {start + attempts - 1}.")


def _open_browser_when_ready(url: str) -> None:
    def _open() -> None:
        health_url = f"{url}/_stcore/health"
        for _ in range(90):
            try:
                with urllib.request.urlopen(health_url, timeout=1) as response:
                    if response.status == 200 and response.read().strip() == b"ok":
                        break
            except Exception:
                time.sleep(1)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()


def main() -> None:
    bundle_dir = _bundle_dir()
    runtime_dir = _runtime_dir()
    external_env = runtime_dir / ".env"
    port = str(_find_available_port())
    url = f"http://localhost:{port}"

    os.environ.setdefault("HANDWRITTEN_NOTES_ROOT", str(bundle_dir))
    os.environ.setdefault("HANDWRITTEN_NOTES_ENV", str(external_env if external_env.exists() else bundle_dir / ".env"))
    os.environ.setdefault("RUNS_DIR", str(runtime_dir / "runs"))
    os.environ.setdefault("OUTPUT_DIR", str(runtime_dir / "outputs"))
    os.environ["STREAMLIT_SERVER_PORT"] = port
    os.environ["STREAMLIT_BROWSER_SERVER_PORT"] = port
    os.environ["STREAMLIT_BROWSER_SERVER_ADDRESS"] = "localhost"
    os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
    os.environ["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] = "false"

    (runtime_dir / "runs").mkdir(exist_ok=True)
    (runtime_dir / "outputs").mkdir(exist_ok=True)

    # Streamlit reads secrets from "<cwd>/.streamlit/secrets.toml". When frozen,
    # the real secrets.toml is bundled inside the exe (see the .spec), so switch
    # the working directory to the bundle dir and let Streamlit find it there.
    # RUNS_DIR/OUTPUT_DIR are absolute (set above), so output still lands next to
    # the exe, not in the temp bundle.
    if getattr(sys, "frozen", False):
        os.chdir(str(bundle_dir))

    app_path = bundle_dir / "app.py"
    flag_options = {
        "server.port": int(port),
        "server.headless": True,
        "browser.serverPort": int(port),
        "browser.serverAddress": "localhost",
    }
    print(f"\nOpening Handwritten Notes Automation at {url}\n")
    _open_browser_when_ready(url)
    sys.argv = ["streamlit", "run", str(app_path), "--server.port", port, "--server.headless", "true"]
    bootstrap.run(str(app_path), False, [], flag_options)


if __name__ == "__main__":
    main()
