# Connect your app to the PW shared proxy — drop-in onboarding

> **Developer:** put this `pw-app-kit` folder into your app, then tell your AI
> assistant (Anti-Gravity):
>
> **“Onboard this app to the PW proxy following CONNECT-TO-PW-PROXY.md. My APP_NAME is '<your exact app name>'.”**
>
> Then do the 2 human steps below. The AI does everything else.

---

## Fixed facts (already set up — never change these)

| Thing | Value |
|---|---|
| Proxy base URL | `https://pw-apps-proxy.vercel.app` |
| Control sheet (ONE shared sheet for ALL apps) | `https://docs.google.com/spreadsheets/d/1aaF3y0VsgyB_YcyfDK33VWcCagzwxBPOwjoe2TvfbHE` |
| Allowed sign-in domain | `pw.live` |
| Providers already on the proxy (no keys in your app) | Gemini → `gemini_generate` · Mathpix → `mathpix_ocr` · Sarvam TTS → `sarvam_tts` |

**Your app ships ZERO API keys.** The proxy holds them all.

---

## Client by app shape (local, hosted backend, or pure frontend)

Works for ANY host — local desktop, your own Vercel/Render, a static site. Pick
the client that fits; the steps are otherwise identical.

| App shape | Who calls the proxy | Client to copy in |
|---|---|---|
| Local desktop, OR hosted app **with a backend** (FastAPI, Node, Vercel functions) | the backend | `pw_access.py` (Python) or `pw_access.js` (Node) |
| **Pure frontend / SPA**, no backend (static React/Vue on Vercel, etc.) | the browser, directly | `pw_access.js` (browser) |

- The proxy has **open CORS + Bearer-token auth**, so a browser on any domain can
  call it directly. The browser only ever holds the **user's own** Google token —
  never a provider key — so this is safe even in a public frontend bundle.
- The proxy accepts a Google token from **any** Google sign-in, so a hosted app
  may use its **own** Google client ID. `@pw.live` is still required.

---

## Human steps — just 2, one-time

1. **Register your app on the sheet.** Open the control sheet → `Whitelisted` tab
   → put your **exact APP_NAME** in the next empty cell of **row 1**, and list the
   allowed users' emails down that column.
2. **Hand it to your AI assistant** with the sentence at the top of this file.

That's all a human does. Everything below is for the AI.

---

## AI assistant — execute in order, do not skip a step

1. **Add the client.** Copy the right client (see "Client by app shape") into
   the app — `pw_access.py` (Python backend) or `pw_access.js` (Node backend or
   browser). Set `APP_NAME = "<exact name>"`; leave `PROXY_BASE_URL` at default.
2. **Verify registration.** `GET https://pw-apps-proxy.vercel.app/api/apps` and
   confirm `<APP_NAME>` is in the returned list. If it is **not** → STOP and tell
   the human to finish Human Step 1 (exact spelling matters).
3. **Locate the Google token.** Find where the app obtains the signed-in user's
   Google token (access token or id token). For a backend, that's its OAuth
   flow; for a **pure frontend**, use Google Identity Services ("Sign in with
   Google") in the browser and use the returned credential/id_token. If the app
   has no Google sign-in, add one (or ask the human) — a verified `@pw.live`
   token is required.
4. **Add the access gate.** Before *every* paid/main action:
   ```python
   import pw_access
   if not pw_access.check_allowed(google_token):
       raise PermissionError("Not authorized for this app.")
   ```
   Deny on `False`. (You may keep any existing whitelist ONLY as a fallback for
   when the proxy is unreachable.)
5. **Route AI calls through the proxy.** Replace every direct provider call:
   - Gemini `generateContent` → `pw_access.gemini_generate(token, model=..., request=<same body>, filename=, input_unit=, count=)`
   - Mathpix `/v3/text` → `pw_access.mathpix_ocr(token, request=<same body>, filename=, count=)`
   - Sarvam `/text-to-speech` → `pw_access.sarvam_tts(token, request=<same body>, filename=, count=)`

   (JS client `pw_access.js`: `geminiGenerate` / `mathpixOcr` / `sarvamTts`,
   same fields, e.g. `await geminiGenerate(token, { model, request, ... })`.)

   **Per-task logging (do this if a task makes MORE THAN ONE AI call):** create
   one `UsageSession` per task, pass `session=` to each call, and `flush()` at
   the end. This writes **one Usage Cost row per provider** (one Gemini row, one
   Sarvam row…) instead of one row per call.
   ```python
   s = pw_access.UsageSession(token, filename=fn, input_unit="No. of pages", count=n)
   pw_access.gemini_generate(token, model=..., request=..., session=s)
   pw_access.gemini_generate(token, model=..., request=..., session=s)  # more calls
   s.flush()   # -> ONE combined gemini row
   ```
   ```js
   const s = new UsageSession(token, { filename, input_unit, count });
   await geminiGenerate(token, { model, request, session: s });
   await s.flush();
   ```
   A task that makes only ONE call can skip the session (each call logs itself).

   Read `resp["result"]` for the raw provider response — existing parsing stays
   unchanged. If the app uses a provider **not** listed above → STOP and tell the
   human to ask the proxy owner to add it (a one-time proxy change).
6. **Remove keys.** Delete every provider API key (`GEMINI_API_KEY`,
   `MATHPIX_*`, `SARVAM_*`, etc.) from `.env`, code, and the build.
7. **Test** with a whitelisted `@pw.live` user's token:
   (a) `check_allowed` returns `True`, (b) one AI call returns a result,
   (c) a new row appears in the `Usage Cost` tab.
8. **Report** the completion checklist below.

---

## Completion checklist — nothing missed

- [ ] `pw_access.py` added, `APP_NAME` set to the exact sheet header
- [ ] `/api/apps` lists this `APP_NAME`
- [ ] `check_allowed()` gates every run and denies on failure
- [ ] all provider calls go through `pw_access` (no direct provider calls remain)
- [ ] no API keys left in `.env`, code, or build
- [ ] a real run logged a row to the `Usage Cost` tab
