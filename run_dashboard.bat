@echo off
cd /d "c:\Users\Mattia\munichScraper"
echo =======================================================
echo   Launching Munich Job Matcher & Application Tracker
echo   Local Dashboard: http://127.0.0.1:8000
echo =======================================================
start "" "http://127.0.0.1:8000"
python main.py --web
pause
