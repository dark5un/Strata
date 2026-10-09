#!/usr/bin/env bash
# tools/test_harness.sh — the strata-harness gate: tests + linters, no GPU needed.
# Run from the repo root: ./tools/test_harness.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
echo "== unittest (serve/harness)"
"$PY" -m unittest discover -s serve/harness -t .
echo "== ruff"
ruff check serve/harness/
echo "== pyright"
pyright serve/harness/
if [ -f eslint.config.mjs ] && command -v npx >/dev/null 2>&1; then
  echo "== eslint (harness tab)"
  npx --no-install eslint serve/web/harness.js || npx eslint serve/web/harness.js
fi
echo "ALL GATES GREEN"
