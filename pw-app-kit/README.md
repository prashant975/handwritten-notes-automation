# PW App Onboarding Kit — Member Guide (START HERE)

Connect your app to the **shared PW proxy** so it gets, with **zero API keys in
your app**:

- ✅ per-user access control (app-wise whitelist)
- ✅ automatic usage + cost logging
- ✅ AI provider calls — **Gemini, Mathpix, Sarvam TTS** — via the proxy

The proxy holds all the keys. Your app just calls it with the signed-in user's
Google token. Works for **any app**: local desktop, your own Vercel/Render, or a
pure frontend/SPA.

---

## What's in this folder

| File | For | What it is |
|---|---|---|
| **README.md** | you (human) | this guide — start here |
| **CONNECT-TO-PW-PROXY.md** | your AI assistant | the instruction file the AI follows to wire everything |
| **pw_access.py** | Python backends | the client to copy in |
| **pw_access.js** | Node backends + browsers/frontends | the client to copy in |
| **verify_onboarding.py** | Python apps | one-command self-check |
| **ONBOARD.md** | optional | a shorter TL;DR of this guide |

---

## Fixed facts (already set up — don't change)

| Thing | Value |
|---|---|
| Proxy URL | `https://pw-apps-proxy.vercel.app` (already inside the client files) |
| Control sheet | `https://docs.google.com/spreadsheets/d/1aaF3y0VsgyB_YcyfDK33VWcCagzwxBPOwjoe2TvfbHE` |
| Allowed sign-in domain | `pw.live` |
| Providers available | Gemini · Mathpix · Sarvam TTS |

## Before you start
- Your app has (or will add) **Google Sign-in** for `@pw.live` users.
- You can edit the **control sheet** (link above) to register your app.
- Your app uses **Gemini / Mathpix / Sarvam TTS**. Need another provider
  (OpenAI, etc.)? Ping the proxy owner — it's a one-time add on the proxy.

---

## The 4 steps

### Step 1 — Register your app on the sheet
Open the control sheet → **`Whitelisted`** tab → in **row 1**, add your app's
**exact name** as a new column header → list your allowed users' emails down
that column.

Confirm it registered (open in a browser or curl):
```
https://pw-apps-proxy.vercel.app/api/apps
```
Your app name should appear in the list.

### Step 2 — Add this kit to your app
Copy the whole **`pw-app-kit`** folder into your app's project.

### Step 3 — Tell your AI assistant (copy-paste this prompt)
Open your AI assistant (Anti-Gravity) **in your app's project** and paste:

> **Onboard this app to the PW proxy following `pw-app-kit/CONNECT-TO-PW-PROXY.md`. My APP_NAME is "PUT-YOUR-EXACT-APP-NAME-HERE". Add the client (`pw_access.py` for a Python backend, or `pw_access.js` for a Node backend / frontend), set APP_NAME, add the access check before every run, route ALL AI calls (Gemini/Mathpix/Sarvam) through the proxy, and — if a task makes more than one AI call — use a `UsageSession` so each provider logs ONE combined row per task. Remove any local provider API keys from the code and .env, then run the verification and show me the result.**

The AI does the wiring. It will **stop and ask you** only if your app name
isn't on the sheet yet, or if it uses a provider the proxy doesn't have.

### Step 4 — Verify + test
- **Python app:** from your backend folder, run
  `python pw-app-kit/verify_onboarding.py .env` → should print **ALL GOOD**.
- **JS app:** confirm `/api/apps` lists your app and no API keys remain in your
  code/`.env`/bundle.
- **Live test:** sign in as a whitelisted user, do something that uses AI, and
  check the sheet's **`Usage Cost`** tab for a new row.

Done. Your app now ships no keys and logs every AI call centrally.

---

## Which client + where it calls (auto-handled by the AI)

| Your app | Client | Proxy is called from |
|---|---|---|
| Local desktop | `pw_access.py` | the local backend |
| Hosted app with a backend (Vercel/Render/Node/FastAPI) | `pw_access.py` or `pw_access.js` | your hosted backend |
| Pure frontend / static SPA | `pw_access.js` | the browser, directly |

The proxy has open CORS + Bearer-token auth, so a browser can call it directly
and safely — it only ever holds the user's *own* Google token, never a key.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `403 not authorized for <app>` | Your email isn't in your app's column on the `Whitelisted` tab. Add it. |
| Your app isn't in `/api/apps` | App-name mismatch — it must match the sheet header **exactly** (spaces, case). |
| Need OpenAI / another provider | Ask the proxy owner — one-time add on the proxy; then all apps can use it. |
| App is Node / pure frontend | Use `pw_access.js` (same 4 calls: `checkAllowed`, `geminiGenerate`, `mathpixOcr`, `sarvamTts`). |
| Hosted on Vercel/Render/etc. | Works identically — see "Which client + where it calls" above. |

## The one rule
**Never put a provider API key in your app.** The proxy holds them all — that's
what makes this safe, even for a public frontend.
