# Troubleshooting Guide

## No API key — the app uses the PW proxy

There is **no Gemini key** in this app. All Gemini calls go through the shared
PW proxy (`pw_access.py`), which holds the key. Your `.env` only picks the model:

```env
MODEL_NAME=gemini-2.5-pro
IMAGE_MODEL_NAME=gemini-3.1-flash-image
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

- "Permission denied": your email isn't in the app's Whitelisted column, or you
  signed in with a non-`@pw.live` account.
- "Proxy unreachable": the app **fails closed** — no one can generate until the
  proxy is reachable again. There is no local allowlist fallback. Wait and retry.
- Empty response: prompt too large. Reduce slides per call.

## Asked to sign in again on every restart

Almost always the **wrong address**. Open the app at `http://localhost:8501`,
never `http://127.0.0.1:8501`.

Browsers treat `localhost` and `127.0.0.1` as different sites. The login cookie
is stored against the host in `redirect_uri` (`localhost`), so on `127.0.0.1`:

- Streamlit rejects the login cookie outright (`Origin mismatch` in the server
  log), and
- Google's sign-in loses its OAuth `state` cookie, so the sign-in **fails
  silently** and drops you back on the login page with no error.

That combination is what produces the "I keep logging in and it keeps asking
again" loop. The app now detects it and shows a **Wrong address** screen with a
link to the correct URL; the launcher and `START_WINDOWS.bat` open `localhost`.

If you changed the address deliberately, update `redirect_uri` in
`.streamlit/secrets.toml` **and** add the same URL to the Google Cloud console's
Authorized redirect URIs.

## How long a sign-in lasts

**7 days.** One sign-in covers 7 days, across app restarts and machine reboots.
The underlying Google token only lives ~1 hour, but the app renews it silently
in the background, so you should never be asked to sign in mid-session.

- The sidebar shows `Auth status: OK` and "Session valid for N more days".
- Change the length with `PW_SESSION_DAYS` in `.env` (default `7`).
- The window is measured from when you signed in, and renewing the token does
  **not** extend it — so day 8 always means a fresh sign-in.
- Keep the Google OAuth consent screen on **Internal** (or Published). In
  **Testing** status Google expires refresh tokens after 7 days, which caps the
  session at 7 days regardless of `PW_SESSION_DAYS`.

## "Session expired" / repeated 401 from the PW proxy

Streamlit keeps its login cookie for 30 days, but the Google **id_token** inside
it expires after ~1 hour and Streamlit never renews it. That is why the app used
to show *"Couldn't verify your access with the PW proxy — Proxy said 401"* about
an hour after signing in, even though the user was still signed in, and why
signing out and back in only helped for another hour.

The app now captures a Google **refresh token** at login and renews the id_token
silently in the background (`src/pw_auth.py`). You should not have to sign in
again for 30 days.

If "Session expired" still appears:

1. **Check the sidebar.** "Auth status: OK" means the session is healthy. If it
   says *reconnect needed*, click **Reconnect Google** once — that re-issues the
   refresh token and enables silent renewal from then on.
2. **Confirm the config.** `.streamlit/secrets.toml` must contain, under
   `[auth.google]`:
   ```toml
   authorize_params = { access_type = "offline", prompt = "consent select_account", include_granted_scopes = "true" }
   ```
   Without `access_type = "offline"` Google never issues a refresh token, and
   the hourly expiry comes straight back. It must live in `authorize_params` —
   Authlib silently drops `access_type` from `client_kwargs`.
3. **Keep `cookie_secret` stable.** If it changes between restarts, every user
   is logged out on restart. Generate it once and leave it alone.
4. **Read the log.** `logs/auth_debug.log` (next to `runs/` in the packaged app)
   records one JSON line per auth event and says exactly why any 401 happened.
   It never contains tokens, cookies or keys — only SHA-256 fingerprints — so it
   is safe to attach to a bug report:
   ```powershell
   Get-Content logs\auth_debug.log -Tail 20
   ```
   Useful events: `refresh_ok` (silent renewal worked), `refresh_rejected` with
   `google_error: invalid_grant` (access revoked — reconnect once),
   `no_refresh_token_issued` (step 2 above is missing).
5. **Developer mode.** Start with `DEBUG=true` in `.env`, or open
   `http://localhost:8501/?debug=1`, to get an **Auth debug** panel showing auth
   status, proxy status, token expiry, last 401 reason, and a *Force refresh now*
   button.

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
