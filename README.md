# Handwritten Notes Automation Complete

This project automates your manual workflow:

```text
Lecture PPT/PDF
→ slide/page extraction
→ Gemini notes generation using your subject prompts
→ handwritten-style DOCX formatting
→ automatic DTP slide-image insertion
→ PDF export
```

## 1. Install on Windows

Open PowerShell in this folder:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Or double-click:

```text
START_WINDOWS.bat
```

## 2. Create `.env`

**No Gemini API key is needed.** This app calls Gemini through the shared **PW
proxy** (`pw_access.py`); the proxy holds the key on its side, so nothing to
paste and nothing to leak. Copy `.env.example` to `.env` — it only selects the
model:

```env
MODEL_NAME=gemini-2.5-pro
IMAGE_MODEL_NAME=gemini-3.1-flash-image
# PW_PROXY_BASE_URL=https://pw-apps-proxy.vercel.app   # optional override
```

Access is granted per-user by the proxy: your `@pw.live` email must be listed
under the **Handwritten Notes Automation** column of the shared
[Whitelisted sheet](https://docs.google.com/spreadsheets/d/1aaF3y0VsgyB_YcyfDK33VWcCagzwxBPOwjoe2TvfbHE).
Sign in with your `@pw.live` Google account in the app.

## 2b. Install the Kalam handwritten font (for PW style)

The notes use the **Kalam** handwriting font to match the Physics Wallah style.
Install it once on Windows so the DOCX/PDF render handwritten:

1. Download from https://fonts.google.com/specimen/Kalam (click "Get font" → "Download all").
2. Unzip, select the `Kalam-*.ttf` files, right-click → **Install for all users**.
3. Re-run the app. If Kalam is missing the output still generates, but in a
   fallback font, and a warning is shown.

The output automatically reproduces the PW layout: a **Subject / Chapter title
block** (chapter taken from the file name), the **PW logo** in the header, a faint
**PW watermark** behind the text, and the **"Master NCERT with PW Books APP"**
footer with page numbers. The logo/watermark are extracted from the lecture slides.

**Best logo quality:** drop a high-resolution transparent `assets/pw_logo.png`
into the project. If present, it is used for the header and watermark instead of
the slide-extracted logo.

**AI-redraw diagrams (handwritten style):** every inserted diagram is redrawn by
default in both the app and CLI. Use `--no-ai-redraw-diagrams` on the CLI only
when you intentionally want to disable it. The Gemini image model uses a white
background, blue handwritten text/formulas,
original diagram-line colours preserved (light colours darkened so they show on
white). Uses the `gemini-3.1-flash-image` model, costs extra API quota, and falls
back to the original slide image if a redraw fails.

## 3. Test proxy access before generating notes

```powershell
python check_gemini.py --google-token "<your signed-in @pw.live token>"
```

This verifies the PW proxy allows your account and that a Gemini call succeeds.
If it fails, notes generation will fail too — confirm your email is in the
Whitelisted sheet and that you're signed in with an `@pw.live` account.

## 4. Run app

### Access control (PW proxy)

Access is controlled entirely by the **PW proxy** — there is **no local allowlist
sheet** in this app anymore. A user may use the app only if their `@pw.live`
email is listed under the **"Handwritten Notes Automation"** column of the
proxy's shared Whitelisted sheet:

```text
https://docs.google.com/spreadsheets/d/1aaF3y0VsgyB_YcyfDK33VWcCagzwxBPOwjoe2TvfbHE
```

Sign-in only needs a valid `@pw.live` Google account; the **proxy** decides
whether that account is authorized for this app, and the app **fails closed** if
the proxy is unreachable. Google login needs real OAuth credentials in
`.streamlit/secrets.toml` (create it from `.streamlit/secrets.toml.example`).
Configure the OAuth redirect URI in Google Cloud as:

```text
http://localhost:8501/oauth2callback
```

The proxy verifies a Google **id_token**, so the auth block must expose it:

```toml
[auth]
expose_tokens = ["access", "id"]
```

### Usage tracking

Usage is logged by the **PW proxy**, not by the app. Every AI call is routed
through the proxy, which writes one trusted **raw row per provider call** to the
shared **Raw Usage Ledger Export** tab (App Name, Email, Filename, Task ID,
tokens, cost, …). No sheet or service-account configuration is required in this
app.

Each file generated gets one **Task ID** (`handwritten-notes-…`), created before
the first AI call and attached to every Gemini, Mathpix, and image call that run
makes. To see what one file cost, group the ledger by
`Task ID + App Name + Email + Model`. The Task ID is written to the run's
`run_log.json` and shown in the UI in developer mode.

```powershell
python -m streamlit run app.py
```

In the app you can:

- **Upload multiple lecture files at once** — each is processed into its own DOCX/PDF with a per-file result section.
- **Preview** the generated notes rendered inline before downloading.
- **Inserted images** tab — see the slide/diagram images the pipeline placed into the notes.
- **Edit & rebuild** — tweak the notes text and regenerate the DOCX/PDF (slide images are re-inserted from the DTP notes) without calling the AI again.

### Batch mode from the command line

Point `--input` at a folder to process every supported file in it:

```powershell
python run_cli.py --input path\to\lecture_folder --subject biology --mode summary
```

A single file still works the same way (`--input lecture.pdf`).

### Gemini 429 / RESOURCE_EXHAUSTED

HTTP 429 means Vertex AI temporarily rate-limited the request or the configured
project has exhausted a quota. The application now sends Gemini requests
through one process-wide gate: one request at a time by default, with three
seconds between request starts. Transient 429/500/502/503/504 responses are
retried up to seven total attempts using exponential backoff and random jitter.
Authentication, permission, and malformed-request errors are not retried.

Defaults can be adjusted before starting the application:

```powershell
$env:GEMINI_MAX_CONCURRENCY="1"
$env:GEMINI_REQUEST_DELAY_SECONDS="3"
$env:GEMINI_MAX_RETRIES="7"
$env:GEMINI_RETRY_MAX_DELAY_SECONDS="60"
.\START_WINDOWS.bat
```

For the packaged build:

```powershell
.\dist\HandwrittenNotesAppTeam\HandwrittenNotesAppTeam.exe
```

If all retries still end in 429, check the Vertex AI quotas for the configured
Google Cloud project in Google Cloud Console and confirm that billing is active.
Repeated immediate 429 responses generally indicate a real project quota or
billing limit rather than a short traffic burst. The application does not
change models, regions, projects, or authentication automatically.

## 5. What is improved from MVP

- All Gemini calls go through the shared PW proxy (see `pw-app-kit/CONNECT-TO-PW-PROXY.md`); the app ships no Gemini key and no provider SDK.
- No silent fake/mock notes unless you enable mock mode.
- Handles long lectures in slide chunks and merges the result.
- Removes question blocks while keeping NCERT/instructional text on the same slide.
- Uses Word COM fallback for PDF export on Windows when LibreOffice is not installed.
- Tries PowerPoint COM fallback for PPT slide rendering on Windows.
- Inserts slide/page image below DTP notes and optionally hides the note.

## 6. Important settings

- **Send slide images to AI**: keep ON for scanned PDFs, handwritten annotations, and diagram labels.
- **Strict pre-filter**: removes obvious housekeeping slides and question blocks before AI.
- **DTP note handling**:
  - `keep_note_and_insert_image`: keep yellow DTP note and insert image below.
  - `hide_note_insert_image`: hide DTP note and insert image only.
  - `keep_note_only`: no automatic image insertion.
- **Inserted image mode**:
  - `smart_crop`: removes obvious slide margins.
  - `full_slide`: inserts full slide/page image.

## 7. PDF export

The app tries:

1. LibreOffice / soffice
2. Microsoft Word automation on Windows (`pywin32`)

If both are missing, DOCX is still created. Open DOCX in Word and save as PDF manually.

## 8. Output files

Each run creates:

```text
runs/run_.../
  input/
  rendered/
  output/
    *_Concise_Notes.docx
    *_Concise_Notes.pdf
  notes_raw.txt
  filter_report.json
  run_metadata.json
```
