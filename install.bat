@echo off
echo Installing dependencies (Python 3.13 compatible)...
pip install "playwright>=1.47.0" playwright-stealth "beautifulsoup4==4.12.3"
echo.
echo Installing Playwright Chromium browser...
python -m playwright install chromium
echo.
echo Done! Run the sample with: python main.py
pause
