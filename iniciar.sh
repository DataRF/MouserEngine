#!/usr/bin/env bash
# Ejecuta MouserEngine desde el código fuente en Linux o macOS (requiere Python 3.10+).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "Preparando MouserEngine por primera vez..."
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.txt
fi

exec .venv/bin/python -m mouser_engine "$@"
