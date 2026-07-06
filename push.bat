@echo off
cd /d "%~dp0"
echo ===================================
echo   Song Library Sync and Push
echo ===================================
echo.

echo [1/2] Run sync.py (scan CSV + copy files + rebuild JSON)...
python3 sync.py
if %errorlevel% neq 0 (
    echo FAILED at sync step
    pause
    exit /b
)

echo.
echo [2/2] Commit and push to GitHub...
echo   - Adding files...
git add data/songs.json covers/ previews/
echo   - Committing...
git commit -m "Library update" 2>nul
if %errorlevel% neq 0 (
    echo   (no changes to commit or commit failed)
)
echo   - Pushing to GitHub...
git push origin master

echo.
echo ===================================
echo   Done. All synced.
echo ===================================
pause
