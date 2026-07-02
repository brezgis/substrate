#!/usr/bin/env bash
# Build the researcher environment (/opt/labenv inside the sandbox).
# Installed as a plain --target directory, NOT a venv: a venv disables
# `pip install --user`, which authors rely on for extra packages.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf labenv && mkdir -p labenv
python3 -m pip install --quiet --no-cache-dir --target labenv \
  numpy pandas matplotlib scipy requests tqdm scikit-learn
python3 - <<'PY'
import sys; sys.path.insert(0, "labenv")
import numpy, pandas, matplotlib, scipy, requests, sklearn
print("labenv ready:", numpy.__version__)
PY
