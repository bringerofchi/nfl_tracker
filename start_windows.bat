@echo off
cd /d "%~dp0"
echo Installing/checking dependencies...
python -m pip install -r requirements.txt -q
echo Starting NFL Fantasy Data Tracker...
echo Coverage dashboard will be at http://127.0.0.1:5050/coverage
python app.py
pause
