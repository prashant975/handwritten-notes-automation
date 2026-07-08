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

Copy `.env.example` to `.env` and paste your **new** Gemini key:

```env
GEMINI_API_KEY=PASTE_ONLY_THE_KEY_AFTER_key=_HERE
GEMINI_MODEL=gemini-2.5-pro
GEMINI_PROVIDER=auto
```

You can also paste your full curl/URL into the Streamlit sidebar. The app extracts only the value after `?key=`.

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

**AI-redraw diagrams (handwritten style):** enable *"AI-redraw diagrams"* in the
app (or `--ai-redraw-diagrams` on the CLI) to redraw each inserted diagram with
the Gemini image model — white background, blue handwritten text/formulas,
original diagram-line colours preserved (light colours darkened so they show on
white). Uses the `gemini-2.5-flash-image` model, costs extra API quota, and falls
back to the original slide image if a redraw fails.

## 3. Test Gemini before generating notes

```powershell
python check_gemini.py
```

If this fails, notes generation will fail too. Create a fresh key in AI Studio, paste only the key, and test again.

## 4. Run app

### Google login allowlist

The Streamlit app now requires an allowlisted email. The primary allowlist is
this Google Sheet:

```text
https://docs.google.com/spreadsheets/d/1ZpHOOYUVL_uz6MZ_yitrdUuSscLUNygCvRsh9-xhwa0/edit?usp=sharing
```

The app reads the sheet through Google Sheets CSV export and scans it for email
addresses, so adding a user to the sheet is enough to allow that account. The
sheet must be shared so the app can view it, for example "Anyone with the link
can view".

You can override the sheet URL with an environment variable:

```powershell
$env:ALLOWED_EMAILS_GOOGLE_SHEET_URL="https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit"
```

Or with a top-level Streamlit secret:

```toml
allowed_emails_google_sheet_url = "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit"
```

If the Google Sheet is unavailable, the app falls back to the local workbook.
For local workbook fallback, the app scans every sheet in the workbook.

By default, the app looks for the workbook at:

```text
C:\Users\<your-user>\Downloads\App Allowed Emails.xlsx
```

You can override this with an environment variable:

```powershell
$env:ALLOWED_EMAILS_WORKBOOK="C:\path\to\App Allowed Emails.xlsx"
```

If the workbook is also unavailable, the app falls back to `assets/allowed_emails.txt`.

Users sign in with Google and are then checked against the Google Sheet
allowlist. Google login requires real OAuth credentials in
`.streamlit/secrets.toml`; the placeholder `client_id` and `client_secret`
values will not work.

To enable real Google login, create `.streamlit/secrets.toml` from
`.streamlit/secrets.toml.example`, then add your Google OAuth client ID and
secret. These credentials are for the app, not for each user. For local
development, configure the OAuth redirect URI in Google Cloud as:

```text
http://localhost:8501/oauth2callback
```

To let the app write usage rows directly to Google Sheets as the signed-in user,
the auth block must expose the Google access token and request the Sheets scope:

```toml
[auth]
expose_tokens = ["access"]

[auth.google]
client_kwargs = { scope = "openid email profile https://www.googleapis.com/auth/spreadsheets" }
```

Each signed-in user must have Editor access to the usage sheets.

### Usage tracking

After a file is generated, the app records one usage row with:

```text
Timestamp, App Name, Email, Filename, Input Unit, Count, Tokens Input, Tokens Output, Model, Cost (INR)
```

By default, rows are written to `outputs/usage_tracking.csv`. To append directly
to Google Sheets, configure one of these write methods:

1. Apps Script webhook:
   - Open the tracking Google Sheet.
   - Go to Extensions -> Apps Script.
   - Use `scripts/google_sheets_usage_webhook.gs`.
   - Deploy as a Web app and set access to allow the app to post.
   - Add the deployment URL to `.streamlit/secrets.toml`:

```toml
usage_tracking_webhook_url = "https://script.google.com/macros/s/YOUR_DEPLOYMENT_ID/exec"
```

2. Google service account:
   - Share the tracking sheet with the service account `client_email` as Editor.
   - Add the JSON to `.streamlit/secrets.toml`:

```toml
google_service_account_json = '{"type":"service_account","project_id":"..."}'
usage_tracking_google_sheet_url = "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit?gid=YOUR_TAB_GID#gid=YOUR_TAB_GID"
usage_tracking_gid = "YOUR_TAB_GID"
usage_tracking_range = "A:J"
```

To append each usage row to more than one sheet, list the targets in order:

```toml
usage_tracking_google_sheet_urls = [
  "https://docs.google.com/spreadsheets/d/FIRST_SHEET_ID/edit?gid=FIRST_TAB_GID#gid=FIRST_TAB_GID",
  "https://docs.google.com/spreadsheets/d/SECOND_SHEET_ID/edit?gid=SECOND_TAB_GID#gid=SECOND_TAB_GID",
]
usage_tracking_gids = ["FIRST_TAB_GID", "SECOND_TAB_GID"]
```

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

## 5. What is improved from MVP

- Gemini API test button before running a file.
- Accepts raw key, full cURL, or URL containing `?key=`.
- Tries three Gemini routes: `google-genai` SDK, Developer REST, and the `aiplatform.googleapis.com` REST style.
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
    *_handwritten_notes.docx
    *_handwritten_notes.pdf
  notes_raw.txt
  filter_report.json
  run_metadata.json
```
