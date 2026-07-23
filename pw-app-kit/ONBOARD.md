# Add your app to the PW shared proxy — 3 steps (~15 min)

You never touch a service-account key or any provider key (Gemini / Mathpix /
Sarvam / ElevenLabs). They live on the proxy. Your app only ever calls the
proxy. That's the whole point.

**What you need first:** the proxy base URL (e.g. `https://pw-apps-proxy.vercel.app`)
and your exact `APP_NAME` — the header in row 1 of the `Whitelisted` tab.

---

## Step 1 — Register your app (once)

Your app needs its own column in the `Whitelisted` tab. Either:

- Ask the proxy admin to run:
  `node scripts/setup-sheet.js "Your App Name"`  (adds the column), **or**
- Add the column by hand: put `Your App Name` in the next empty cell of row 1
  of the `Whitelisted` tab.

Then list the allowed users' emails down that column. Confirm the exact name:
```
curl https://pw-apps-proxy.vercel.app/api/apps
```

## Step 2 — Drop in the client

Copy [`pw_access.py`](pw_access.py) into your backend and set two things at the top:
```python
APP_NAME = "Your App Name"                       # EXACT Whitelisted header
# PROXY_BASE_URL — defaults to the shared proxy; override via env if needed
```
Install the one dependency if you don't have it: `pip install requests`.

## Step 3 — Wire it into your app

`google_token` = the signed-in user's Google token your app already has.
⚠️ Google tokens expire after **~1 hour** — for anything long-running, pass a
**function** that returns a fresh token instead of the string (every
`pw_access` helper accepts either; details in the `pw_access.py` header).

**Before every paid/main run — gate on the whitelist (fail closed):**
```python
import pw_access

if not pw_access.check_allowed(google_token):
    raise PermissionError("Not authorized for this app.")
```

**Replace direct Gemini/Mathpix calls with the proxy helpers (they auto-log):**
```python
resp = pw_access.gemini_generate(
    google_token,
    model="gemini-2.5-flash",
    request=generate_content_body,   # your existing generateContent payload
    filename="chapter1.pdf",
    input_unit="No. of pages",
    count=20,
)
data = resp["result"]                # raw Gemini response — parse as before
```

**If you still call a paid API directly for now, log it yourself:**
```python
pw_access.log_usage(
    google_token,
    filename="chapter1.pdf", input_unit="No. of pages", count=20,
    items=[{"model": "gemini-2.5-flash", "tokens_in": 14500,
            "tokens_out": 2300, "cost_inr": 12.45}],
)
```

---

## Done. What you get automatically
- ✅ Per-app access control (a user allowed for one app isn't allowed for another)
- ✅ Every paid call logged to `Usage Cost`, split by app name + email
- ✅ **No API keys in your app** — nothing to leak, nothing to rotate on your side

## Rules (from the skill file)
- Check `check_allowed()` **before every run**, not just at login.
- Deny on any network/proxy failure.
- Never put a service-account key or provider key in the app, `.env`, or build.
- If you add a brand-new provider (Sarvam, etc.), the **proxy** gets one new
  endpoint + one env var — apps still ship zero keys.
