#!/usr/bin/env bash
set -euo pipefail

python_bin="${PYTHON_BIN:-python}"

"$python_bin" -m pytest -q
"$python_bin" -m compileall -q src
"$python_bin" -m project_ensemble --help >/dev/null

echo "Project_ENSEMBLE smoke test: OK"
