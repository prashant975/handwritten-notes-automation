from pathlib import Path
import getpass
from src.config import extract_api_key

print("Paste your Gemini curl/URL/raw key below. Input will be hidden if terminal supports it.")
value = getpass.getpass("Gemini key/curl: ")
key = extract_api_key(value)
if not key:
    raise SystemExit("No key detected.")
Path(".env").write_text(f"GEMINI_API_KEY={key}\nGEMINI_MODEL=gemini-2.5-pro\nGEMINI_PROVIDER=auto\n", encoding="utf-8")
print("Created .env without printing the key.")
