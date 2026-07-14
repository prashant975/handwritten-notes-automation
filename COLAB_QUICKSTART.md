# Colab Quickstart

Use this when you want to run the MVP in Google Colab.

```python
!apt-get update -qq
!apt-get install -y -qq libreoffice
!pip install -q streamlit python-dotenv python-pptx PyMuPDF Pillow python-docx requests
```

Upload or clone this project folder. **No Gemini key is needed** — the app calls
Gemini through the PW proxy, which holds the key. You pass a signed-in `@pw.live`
Google token instead:

```python
import os
os.environ["MODEL_NAME"] = "gemini-2.5-pro"
os.environ["PW_GOOGLE_TOKEN"] = "YOUR_SIGNED_IN_PW_LIVE_TOKEN"
```

Run from CLI inside Colab (`--google-token` reads `PW_GOOGLE_TOKEN` by default):

```python
!python run_cli.py "/content/lecture.pdf" --subject biology --language English --mode summary
```

The output will be inside the latest `runs/.../output/` folder.

For Google Drive input/output:

```python
from google.colab import drive
drive.mount('/content/drive')

!python run_cli.py "/content/drive/MyDrive/your_folder/lecture.pdf" --subject biology --language English --mode summary
```

Then copy the latest run output back to your Drive folder if needed.
