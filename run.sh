#!/usr/bin/env bash
# Spuštění Novotného Sound Studia.
# Pokud .venv chybí nebo nefunguje (např. jiná verze Pythonu), vytvoří ho znovu
# a nainstaluje závislosti.
set -e
cd "$(dirname "$0")"

# systémový Python (ne ten z aktivovaného .venv, který se může mazat)
PY="${PYTHON:-/usr/bin/python3}"
[ -x "$PY" ] || PY="$(command -v python3)"

venv_ok() {
    [ -x .venv/bin/python ] && .venv/bin/python -c "import numpy, soundfile, PySide6" 2>/dev/null
}

if ! venv_ok; then
    echo ">> Připravuji prostředí .venv pro $("$PY" --version) (jednorázově, chvíli to trvá)…"
    rm -rf .venv
    "$PY" -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.txt
    venv_ok || { echo "!! Instalace závislostí selhala, viz výpis výše."; exit 1; }
fi

exec .venv/bin/python -m studio "$@"
