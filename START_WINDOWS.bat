@echo off
cd /d %~dp0
if not exist .venv (
  py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
REM Host and port MUST match `redirect_uri` in .streamlit\secrets.toml
REM (http://localhost:8501/oauth2callback). Opening the app on 127.0.0.1 is a
REM DIFFERENT cookie origin to localhost: Streamlit rejects the login cookie and
REM the Google sign-in loses its OAuth state, forcing a fresh login every start.
python -m streamlit run app.py --server.port 8501 --browser.serverAddress localhost
pause
