#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${JUDGE_BOT_URL:-}" ]]; then
  echo "JUDGE_BOT_URL is required"
  exit 1
fi
if [[ -z "${JUDGE_LLM_PROVIDER:-}" ]]; then
  echo "JUDGE_LLM_PROVIDER is required"
  exit 1
fi
if [[ "${JUDGE_LLM_PROVIDER}" != "mock" ]] && [[ -z "${JUDGE_LLM_API_KEY:-}" ]]; then
  echo "JUDGE_LLM_API_KEY is required for provider ${JUDGE_LLM_PROVIDER}"
  exit 1
fi

mkdir -p reports
ts="$(date +%Y%m%d_%H%M%S)"
report="reports/run_${ts}.txt"
metrics="reports/run_${ts}.json"

echo "[INFO] Resetting bot state via teardown..."
curl -sS -X POST "${JUDGE_BOT_URL}/v1/teardown" -H "Content-Type: application/json" -d '{}' >/dev/null
python3 - <<'PY'
import json
import os
from urllib import request

url = os.environ["JUDGE_BOT_URL"].rstrip("/") + "/v1/readiness"
data = json.loads(request.urlopen(url, timeout=20).read().decode("utf-8"))
counts = data.get("contexts_loaded", {})
if any(int(counts.get(k, 0)) != 0 for k in ("category", "merchant", "customer", "trigger")):
    raise SystemExit(f"Clean-state gate failed: contexts not zero -> {counts}")
print("[INFO] Clean-state gate passed:", counts)
PY

echo "[INFO] Running full evaluation..."
export JUDGE_TEST_SCENARIO="full_evaluation"
python3 judge_simulator.py | tee "${report}"

echo "[INFO] Validating report..."
python3 scripts/judge_report_tools.py --report "${report}" --out-json "${metrics}"

echo "[PASS] Score loop complete"
echo "[INFO] Report: ${report}"
echo "[INFO] Metrics: ${metrics}"
