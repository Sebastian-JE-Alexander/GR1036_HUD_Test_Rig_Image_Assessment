@echo off
:: ==============================================================================
:: IMAGE ASSESSMENT TOOL 
:: ==============================================================================
title Image Assessment Tool

:: 1. Navigate to the folder where the script lives on the machine PC
cd /d "%~dp0"

:: 2. Launch the script silently using 'pythonw' (hides the ugly black command box)
echo Launching Quality Assurance Interface...
start "" pythonw "script.py"

exit