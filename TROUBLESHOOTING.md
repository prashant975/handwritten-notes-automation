# Troubleshooting Guide

## No API key — the app uses the PW proxy

There is **no Gemini key** in this app. All Gemini calls go through the shared
PW proxy (`pw_access.py`), which holds the key. Your `.env` only picks the model:

```env
MODEL_NAME=gemini-2.5-pro
IMAGE_MODEL_NAME=gemini-2.5-flash-image
```

Access is per-user: your `@pw.live` email must be in the **Handwritten Notes
Automation** column of the shared Whitelisted sheet.

## Test proxy access

```powershell
python check_gemini.py --google-token "<signed-in @pw.live token>"
```

You can also run the full onboarding self-check:

```powershell
python verify_onboarding.py .env "<optional token>"
```

## Common errors

- "not authorized for this app": your email isn't in the app's Whitelisted
  column, or you signed in with a non-`@pw.live` account.
- `gemini proxy error 401`: the Google token is missing/expired — sign in again.
- Proxy unreachable: the app falls back to the local email allowlist for access,
  but Gemini calls still need the proxy to be up.
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
