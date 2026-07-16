from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


PORT = 8501


def _bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def _runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _open_browser_when_ready(url: str) -> None:
    def _open() -> None:
        health_url = f"{url}/_stcore/health"
        for _ in range(120):
            try:
                with urllib.request.urlopen(health_url, timeout=1) as response:
                    if response.status == 200 and response.read().strip() == b"ok":
                        webbrowser.open(url)
                        return
            except Exception:
                time.sleep(1)
        print(f"Server did not become ready automatically. Try opening {url}")

    threading.Thread(target=_open, daemon=True).start()


def _patch_streamlit_static_dir(bundle_dir: Path) -> None:
    static_dir = bundle_dir / "streamlit" / "static"
    print(f"Streamlit static folder: {static_dir}")
    print(f"Streamlit static folder exists: {static_dir.exists()}")
    if not static_dir.exists():
        return
    try:
        import streamlit.config as config
        import streamlit.file_util as file_util
        import streamlit.web.server.starlette.starlette_static_routes as static_routes

        config.set_option("global.developmentMode", False, "launcher")
        config.set_option("server.headless", True, "launcher")
        config.set_option("logger.hideWelcomeMessage", True, "launcher")
        config.set_option("server.port", PORT, "launcher")

        def _static_dir() -> str:
            return str(static_dir)

        file_util.get_static_dir = _static_dir
        static_routes.file_util.get_static_dir = _static_dir
    except Exception as exc:
        print(f"Could not patch Streamlit static folder: {exc}")


def main() -> None:
    url = f"http://127.0.0.1:{PORT}"
    if not _port_is_free(PORT):
        print("")
        print(f"Port {PORT} is already in use.")
        print("Close any old HandwrittenNotesAppTeam.exe window/process, then run this app again.")
        print("")
        input("Press Enter to exit...")
        return

    bundle_dir = _bundle_dir()
    runtime_dir = _runtime_dir()
    external_env = runtime_dir / ".env"

    os.environ.setdefault("HANDWRITTEN_NOTES_ROOT", str(bundle_dir))
    os.environ.setdefault("HANDWRITTEN_NOTES_ENV", str(external_env if external_env.exists() else bundle_dir / ".env"))
    os.environ.setdefault("RUNS_DIR", str(runtime_dir / "runs"))
    os.environ.setdefault("OUTPUT_DIR", str(runtime_dir / "outputs"))
    os.environ["STREAMLIT_SERVER_PORT"] = str(PORT)
    os.environ["STREAMLIT_BROWSER_SERVER_ADDRESS"] = "localhost"
    os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
    os.environ["STREAMLIT_LOGGER_HIDE_WELCOME_MESSAGE"] = "true"
    os.environ["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] = "false"

    (runtime_dir / "runs").mkdir(exist_ok=True)
    (runtime_dir / "outputs").mkdir(exist_ok=True)

    if getattr(sys, "frozen", False):
        os.chdir(str(bundle_dir))
        _patch_streamlit_static_dir(bundle_dir)

    app_path = bundle_dir / "app.py"
    flag_options = {
        "global.developmentMode": False,
        "server.port": PORT,
        "server.headless": True,
        "logger.hideWelcomeMessage": True,
        "browser.serverAddress": "localhost",
    }
    print("")
    print(f"Opening Concise Notes Automation at {url}")
    print("Keep this window open while using the app.")
    print("")
    _open_browser_when_ready(url)
    sys.argv = [
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(PORT),
        "--server.headless",
        "true",
        "--logger.hideWelcomeMessage",
        "true",
        "--global.developmentMode",
        "false",
    ]
    from streamlit.web import bootstrap

    bootstrap.run(str(app_path), False, [], flag_options)


if __name__ == "__main__":
    main()
