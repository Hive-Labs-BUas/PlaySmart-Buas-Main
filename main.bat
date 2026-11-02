@echo off
rem PlaySmart capture launcher. Modified 2026-09 (iteration 5): runs the pre-flight
rem check first, then the orchestrator. Passwords and switches come from .env
rem (see .env.example); nothing needs to be typed into this window.
cd /d "%~dp0"

echo == PlaySmart capture: rig setup ==
poetry run python src\rig_setup.py --quiet
echo.
echo == PlaySmart capture: pre-flight ==
poetry run python src\preflight.py --no-ble
if errorlevel 1 (
    echo.
    echo Pre-flight found blocking problems ^(see FAIL lines^).
    echo Fix them and run main.bat again, or press any key to start anyway.
    pause >nul
)

echo.
echo == Capture orchestrator: F7 arms, the game starts and stops the recording, F12 aborts ==
poetry run python src\key_listener.py
pause
