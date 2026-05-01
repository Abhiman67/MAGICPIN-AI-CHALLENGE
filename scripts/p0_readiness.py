#!/usr/bin/env python3
"""P0 readiness harness for local submission confidence.

This script intentionally does not require external LLM API keys. It validates
the bot against challenge-like lifecycle checks:
- warmup: health/metadata + context push
- scenario checks: auto-reply, intent transition, hostile handling
- full-like pass: trigger batches through /v1/tick
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "dataset"
BOT_FILE = ROOT / "bot.py"


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class BotRunner:
    def __init__(self, port: int):
        self.port = port
        self.proc: subprocess.Popen[str] | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        env = os.environ.copy()
        env["BOT_HOST"] = "127.0.0.1"
        env["BOT_PORT"] = str(self.port)
        env["BOT_DB_PATH"] = str(ROOT / "p0_state.db")
        self.proc = subprocess.Popen(
            ["python3", str(BOT_FILE)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.wait_health()

    def stop(self) -> None:
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.proc.stdout:
            self.proc.stdout.close()
        self.proc = None

    def wait_health(self) -> None:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                data = self.get("/v1/healthz")
                if data.get("status") == "ok":
                    return
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("bot failed to come up")

    def get(self, path: str) -> dict:
        req = Request(self.base + path, method="GET")
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8")
        req = Request(
            self.base + path,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=15) as resp:
                return int(resp.status), json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            return int(e.code), json.loads(e.read().decode("utf-8"))


def load_dataset() -> tuple[dict, list[dict], list[dict], list[dict]]:
    categories = {}
    for file in (DATASET / "categories").glob("*.json"):
        item = json.loads(file.read_text())
        categories[item["slug"]] = item
    merchants = json.loads((DATASET / "merchants_seed.json").read_text())["merchants"]
    customers = json.loads((DATASET / "customers_seed.json").read_text())["customers"]
    triggers = json.loads((DATASET / "triggers_seed.json").read_text())["triggers"]
    return categories, merchants, customers, triggers


def push_contexts(bot: BotRunner, categories: dict, merchants: list[dict], customers: list[dict], triggers: list[dict]) -> None:
    for slug, payload in categories.items():
        status, body = bot.post(
            "/v1/context",
            {"scope": "category", "context_id": slug, "version": 1, "payload": payload, "delivered_at": "2026-05-01T00:00:00Z"},
        )
        assert status == 200 and body.get("accepted"), f"category push failed: {slug}"

    for payload in merchants:
        mid = payload["merchant_id"]
        status, body = bot.post(
            "/v1/context",
            {"scope": "merchant", "context_id": mid, "version": 1, "payload": payload, "delivered_at": "2026-05-01T00:00:00Z"},
        )
        assert status == 200 and body.get("accepted"), f"merchant push failed: {mid}"

    for payload in customers:
        cid = payload["customer_id"]
        status, body = bot.post(
            "/v1/context",
            {"scope": "customer", "context_id": cid, "version": 1, "payload": payload, "delivered_at": "2026-05-01T00:00:00Z"},
        )
        assert status == 200 and body.get("accepted"), f"customer push failed: {cid}"

    for payload in triggers:
        tid = payload["id"]
        status, body = bot.post(
            "/v1/context",
            {"scope": "trigger", "context_id": tid, "version": 1, "payload": payload, "delivered_at": "2026-05-01T00:00:00Z"},
        )
        assert status == 200 and body.get("accepted"), f"trigger push failed: {tid}"


def run_scenarios(bot: BotRunner, merchants: list[dict], triggers: list[dict]) -> None:
    mid = merchants[0]["merchant_id"]

    # Auto-reply scenario
    for i in range(1, 4):
        status, body = bot.post(
            "/v1/reply",
            {
                "conversation_id": "p0_auto_conv",
                "merchant_id": mid,
                "customer_id": None,
                "from_role": "merchant",
                "message": "Thank you for contacting us! Our team will respond shortly.",
                "received_at": "2026-05-01T10:00:00Z",
                "turn_number": i + 1,
            },
        )
        assert status == 200
        assert body.get("action") in {"wait", "end"}

    # Intent scenario
    status, body = bot.post(
        "/v1/reply",
        {
            "conversation_id": "p0_intent_conv",
            "merchant_id": mid,
            "customer_id": None,
            "from_role": "merchant",
            "message": "Ok lets do it. Whats next?",
            "received_at": "2026-05-01T10:05:00Z",
            "turn_number": 2,
        },
    )
    assert status == 200
    assert body.get("action") in {"send", "wait", "end"}

    # Hostile scenario
    status, body = bot.post(
        "/v1/reply",
        {
            "conversation_id": "p0_hostile_conv",
            "merchant_id": mid,
            "customer_id": None,
            "from_role": "merchant",
            "message": "Stop messaging me. This is useless spam.",
            "received_at": "2026-05-01T10:06:00Z",
            "turn_number": 2,
        },
    )
    assert status == 200
    assert body.get("action") in {"send", "end"}

    # Full-like batch ticks
    trigger_ids = [t["id"] for t in triggers]
    for i in range(0, len(trigger_ids), 5):
        batch = trigger_ids[i : i + 5]
        status, body = bot.post(
            "/v1/tick",
            {"now": "2026-05-01T10:10:00Z", "available_triggers": batch},
        )
        assert status == 200
        assert "actions" in body
        for action in body["actions"]:
            assert "merchant_id" in action
            assert "trigger_id" in action
            assert "body" in action
            assert "cta" in action


def main() -> int:
    categories, merchants, customers, triggers = load_dataset()
    port = free_port()
    bot = BotRunner(port)
    print("[P0] Starting bot...")
    bot.start()
    try:
        print("[P0] Warmup checks...")
        health = bot.get("/v1/healthz")
        metadata = bot.get("/v1/metadata")
        assert health.get("status") == "ok"
        assert "team_name" in metadata

        print("[P0] Pushing contexts...")
        push_contexts(bot, categories, merchants, customers, triggers)

        post_health = bot.get("/v1/healthz")
        counts = post_health.get("contexts_loaded", {})
        assert counts.get("category", 0) >= len(categories)
        assert counts.get("merchant", 0) >= len(merchants)
        assert counts.get("customer", 0) >= len(customers)
        assert counts.get("trigger", 0) >= len(triggers)

        print("[P0] Running scenarios and full-like tick checks...")
        run_scenarios(bot, merchants, triggers)

        print("[P0] Checking readiness and metrics...")
        readiness = bot.get("/v1/readiness")
        metrics = bot.get("/v1/metrics")
        assert readiness.get("ready") is True
        assert "metrics" in metrics
        assert metrics["metrics"].get("tick_total", 0) >= 1

        print("[P0] PASS")
        return 0
    except AssertionError as e:
        print(f"[P0] FAIL: {e}")
        return 1
    finally:
        bot.stop()
        db_file = ROOT / "p0_state.db"
        if db_file.exists():
            db_file.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
