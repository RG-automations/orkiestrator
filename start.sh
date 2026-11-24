#!/usr/bin/env bash
# Linux/macOS equivalent of start.bat. Keys live in local.env (see local.env.example), never in this file.
# Run this inside a virtualenv (python3 -m venv .venv && source .venv/bin/activate) on systems where
# the system Python refuses `pip install` outside one (PEP 668, "externally-managed-environment").
set -euo pipefail
cd "$(dirname "$0")"

python3 -m pip install -e . -q

if [ -f local.env ]; then
  set -a
  # shellcheck disable=SC1091
  source <(grep -v '^\s*#' local.env | grep -v '^\s*$')
  set +a
fi

if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${GOOGLE_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ] && [ "${ORKI_PROVIDER:-}" != "demo" ]; then
  echo "UWAGA: brak klucza AI. Skopiuj local.env.example do local.env i wpisz GEMINI_API_KEY," >&2
  echo "albo ustaw ORKI_PROVIDER=demo, żeby zobaczyć panel bez klucza (dane fikcyjne, odpowiedzi stałe)." >&2
fi

python3 -m orkiestrator serve
