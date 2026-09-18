@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -m venv .venv
  if errorlevel 1 exit /b 1
)
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
