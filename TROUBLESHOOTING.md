# Troubleshooting Guide

## Gemini key format

Correct `.env`:

```env
GEMINI_API_KEY=YOUR_KEY_ONLY
GEMINI_MODEL=gemini-2.5-pro
GEMINI_PROVIDER=auto
```

The Streamlit UI can accept a full cURL/URL and extract the key automatically.

## Test key

```powershell
python check_gemini.py
```

Try providers one by one:

```powershell
python check_gemini.py --provider google_genai_sdk
python check_gemini.py --provider developer_rest
python check_gemini.py --provider aiplatform_rest
```

## Common Gemini errors

- 400/404: wrong model or endpoint. Try `gemini-2.5-flash`.
- 401/403: wrong/restricted/expired key. Create a new AI Studio key.
- 429: quota limit. Reduce images per call or use Flash.
- Empty response: prompt too large. Reduce slides per call.

## Windows activation

PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

CMD:

```cmd
.venv\Scripts\activate.bat
```
