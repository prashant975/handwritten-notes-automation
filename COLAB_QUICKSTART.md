# Colab Quickstart

Use this when you want to run the MVP in Google Colab.

```python
!apt-get update -qq
!apt-get install -y -qq libreoffice
!pip install -q streamlit python-dotenv python-pptx PyMuPDF Pillow python-docx google-genai pydantic
```

Upload or clone this project folder, then set your API key:

```python
import os
os.environ["GEMINI_API_KEY"] = "YOUR_KEY_HERE"
os.environ["GEMINI_MODEL"] = "gemini-3.5-flash"
```

Run from CLI inside Colab:

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
