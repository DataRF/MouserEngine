@echo off
rem Ejecuta MouserEngine desde el codigo fuente (requiere Python 3.10 o superior).
rem La primera vez crea un entorno virtual e instala las dependencias.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Preparando MouserEngine por primera vez, espere unos minutos...
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
    if errorlevel 1 (
        echo No se encontro Python. Instalelo desde https://www.python.org/downloads/
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Error instalando dependencias.
        pause
        exit /b 1
    )
)

start "" ".venv\Scripts\pythonw.exe" -m mouser_engine %*
