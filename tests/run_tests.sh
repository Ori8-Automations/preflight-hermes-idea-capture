#!/usr/bin/env bash
# Static checks + API smoke test for the Preflight Idea Capture plugin.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DASH="$ROOT/preflight-idea-capture/dashboard"

echo "== py_compile =="
PYTHON_BIN="${PYTHON:-python3}"
"$PYTHON_BIN" -m py_compile "$DASH/plugin_api.py"
echo "PASS python compile"

echo "== node --check =="
if command -v node >/dev/null 2>&1; then
  node --check "$DASH/dist/index.js"
  echo "PASS js syntax"
else
  echo "SKIP js syntax (node not installed)"
fi

echo "== API smoke test =="
"$PYTHON_BIN" "$ROOT/tests/smoke_test.py"
