@echo off
rem Compila dist\MouserEngine.exe (un solo archivo, sin consola) con PyInstaller.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt || goto :error
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean MouserEngine.spec || goto :error

echo.
echo Listo: dist\MouserEngine.exe
echo Copielo al escritorio o use Herramientas ^> Crear acceso directo en el escritorio.
pause
exit /b 0

:error
echo La compilacion fallo.
pause
exit /b 1
