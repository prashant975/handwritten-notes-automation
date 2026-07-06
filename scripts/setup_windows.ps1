Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
Set-Location $PSScriptRoot\..
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python check_gemini.py
python -m streamlit run app.py
