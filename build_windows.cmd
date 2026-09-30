@echo off
setlocal
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "VOX Tile Mixer" --paths "%CD%" run.py
if errorlevel 1 exit /b 1
echo Built: dist\VOX Tile Mixer.exe
