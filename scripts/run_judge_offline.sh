#!/usr/bin/env bash
set -euo pipefail

BOT_PID=""
cleanup() {
  if [[ -n "${BOT_PID}" ]]; then
    kill "${BOT_PID}" >/dev/null 2>&1 || true
    wait "${BOT_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

export BOT_HOST=127.0.0.1
export BOT_PORT="${BOT_PORT:-18080}"
python3 bot.py >/tmp/vera-bot.log 2>&1 &
BOT_PID="$!"

sleep 2
if ! kill -0 "${BOT_PID}" >/dev/null 2>&1; then
  echo "Bot failed to start. Recent log:"
  tail -n 100 /tmp/vera-bot.log || true
  exit 1
fi

export JUDGE_BOT_URL="http://${BOT_HOST}:${BOT_PORT}"
export JUDGE_LLM_PROVIDER=mock

export JUDGE_TEST_SCENARIO=all
python3 judge_simulator.py

curl -s -X POST "${JUDGE_BOT_URL}/v1/teardown" -H 'Content-Type: application/json' -d '{}' >/dev/null

export JUDGE_TEST_SCENARIO=full_evaluation
python3 judge_simulator.py

echo "Offline judge runs completed."
