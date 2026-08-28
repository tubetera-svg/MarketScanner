@echo off

taskkill /FI "WINDOWTITLE eq Market Scanner API*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Market Scanner Frontend*" /T /F >nul 2>&1

echo Market Scanner services stopped.
pause
