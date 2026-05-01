#!/usr/bin/env bash
set -euo pipefail

python3 -m py_compile bot.py
python3 -m unittest discover -s tests -p 'test_*.py' -q

echo "Local checks passed."
