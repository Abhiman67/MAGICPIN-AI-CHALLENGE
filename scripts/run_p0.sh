#!/usr/bin/env bash
set -euo pipefail

python3 -m py_compile bot.py tests/test_contract.py scripts/p0_readiness.py
python3 -m unittest discover -s tests -p 'test_*.py' -q
python3 scripts/p0_readiness.py
./scripts/docker_smoke.sh

echo "P0 local readiness checks passed."
