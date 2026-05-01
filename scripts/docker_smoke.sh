#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker not available on this machine; skipping Docker smoke test."
  exit 0
fi

IMAGE_NAME="vera-bot-p0-smoke"
CONTAINER_NAME="vera-bot-p0-smoke-run"

docker build -t "${IMAGE_NAME}" .
docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
docker run -d --name "${CONTAINER_NAME}" -p 18080:8080 "${IMAGE_NAME}" >/dev/null

cleanup() {
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

sleep 2
curl -sf http://127.0.0.1:18080/v1/healthz >/dev/null
curl -sf http://127.0.0.1:18080/v1/readiness >/dev/null
curl -sf http://127.0.0.1:18080/v1/metadata >/dev/null

echo "Docker smoke test passed."
