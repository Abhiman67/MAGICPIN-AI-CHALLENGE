#!/usr/bin/env python3
"""Integration tests for the challenge HTTP contract."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BOT_FILE = ROOT / "bot.py"


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class BotProcess:
    def __init__(self, port: int, db_path: str = "") -> None:
        self.port = port
        self.db_path = db_path
        self.proc: subprocess.Popen[str] | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        env = os.environ.copy()
        env["BOT_HOST"] = "127.0.0.1"
        env["BOT_PORT"] = str(self.port)
        if self.db_path:
            env["BOT_DB_PATH"] = self.db_path
        self.proc = subprocess.Popen(
            ["python3", str(BOT_FILE)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._wait_ready()

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

    def _wait_ready(self) -> None:
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                data = self.get("/v1/healthz")
                if data.get("status") == "ok":
                    return
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("bot did not become ready")

    def get(self, path: str) -> dict:
        req = Request(self.base + path, method="GET")
        with urlopen(req, timeout=5) as resp:
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
            with urlopen(req, timeout=5) as resp:
                return int(resp.status), json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            return int(e.code), json.loads(e.read().decode("utf-8"))


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.port = free_port()
        self.bot = BotProcess(self.port)
        self.bot.start()

    def tearDown(self) -> None:
        self.bot.stop()

    def test_health_and_metadata(self) -> None:
        health = self.bot.get("/v1/healthz")
        self.assertEqual(health["status"], "ok")
        self.assertIn("contexts_loaded", health)

        metadata = self.bot.get("/v1/metadata")
        self.assertIn("team_name", metadata)
        self.assertIn("model", metadata)

    def test_context_idempotency(self) -> None:
        category = {"slug": "dentists", "peer_stats": {"avg_ctr": 0.03}, "digest": []}
        status, body = self.bot.post(
            "/v1/context",
            {
                "scope": "category",
                "context_id": "dentists",
                "version": 1,
                "payload": category,
                "delivered_at": "2026-05-01T00:00:00Z",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["accepted"])

        status, body = self.bot.post(
            "/v1/context",
            {
                "scope": "category",
                "context_id": "dentists",
                "version": 1,
                "payload": category,
                "delivered_at": "2026-05-01T00:00:00Z",
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["reason"], "stale_version")

    def test_tick_and_reply_flow(self) -> None:
        self._seed_minimal_contexts()
        status, tick = self.bot.post(
            "/v1/tick",
            {"now": "2026-05-01T10:00:00Z", "available_triggers": ["trg_1"]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(len(tick["actions"]) >= 1)
        action = tick["actions"][0]
        self.assertEqual(action["merchant_id"], "m_1")

        status, reply = self.bot.post(
            "/v1/reply",
            {
                "conversation_id": action["conversation_id"],
                "merchant_id": "m_1",
                "customer_id": None,
                "from_role": "merchant",
                "message": "Yes please do it",
                "received_at": "2026-05-01T10:01:00Z",
                "turn_number": 2,
            },
        )
        self.assertEqual(status, 200)
        self.assertIn(reply["action"], {"send", "wait", "end"})

    def test_persistence_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "state.db")
            self.bot.stop()
            self.bot = BotProcess(self.port, db_path=db_path)
            self.bot.start()
            self._seed_minimal_contexts()
            health = self.bot.get("/v1/healthz")
            self.assertEqual(health["contexts_loaded"]["category"], 1)
            self.assertEqual(health["contexts_loaded"]["merchant"], 1)

            self.bot.stop()
            self.bot = BotProcess(self.port, db_path=db_path)
            self.bot.start()
            health = self.bot.get("/v1/healthz")
            self.assertEqual(health["contexts_loaded"]["category"], 1)
            self.assertEqual(health["contexts_loaded"]["merchant"], 1)
            self.assertEqual(health["contexts_loaded"]["trigger"], 1)

    def test_site_workflow_triggers_generate_actions(self) -> None:
        self._seed_minimal_contexts()
        merchant_id = "m_1"
        trigger_payloads = [
            ("trg_review_1", "review_sentiment_alert", {"sentiment": "negative", "review_count": 3}),
            ("trg_profile_1", "profile_incomplete", {"missing_fields": ["hours", "description", "photos"]}),
            ("trg_lead_1", "lead_missed_searches", {"missed_searches": 6777}),
            ("trg_photo_1", "photo_gap_detected", {"missing_photos": 6}),
        ]
        for idx, (tid, kind, payload) in enumerate(trigger_payloads, start=2):
            status, _ = self.bot.post(
                "/v1/context",
                {
                    "scope": "trigger",
                    "context_id": tid,
                    "version": 1,
                    "payload": {
                        "id": tid,
                        "scope": "merchant",
                        "kind": kind,
                        "source": "internal",
                        "merchant_id": merchant_id,
                        "customer_id": None,
                        "payload": payload,
                        "urgency": 2,
                        "suppression_key": f"{kind}:{idx}",
                        "expires_at": "2026-05-10T00:00:00Z",
                    },
                    "delivered_at": "2026-05-01T00:00:00Z",
                },
            )
            self.assertEqual(status, 200)

            status, tick = self.bot.post(
                "/v1/tick",
                {"now": "2026-05-01T10:00:00Z", "available_triggers": [tid]},
            )
            self.assertEqual(status, 200)
            self.assertTrue(tick["actions"])
            action = tick["actions"][0]
            self.assertIn(action["cta"], {"open_ended", "binary_yes_no", "none"})
            self.assertEqual(action["trigger_id"], tid)

    def test_repeated_low_signal_reply_backoff_then_end(self) -> None:
        self._seed_minimal_contexts()
        status, tick = self.bot.post(
            "/v1/tick",
            {"now": "2026-05-01T10:00:00Z", "available_triggers": ["trg_1"]},
        )
        self.assertEqual(status, 200)
        conv = tick["actions"][0]["conversation_id"]

        repeated_msg = "ok"
        reply_action = None
        for i in range(1, 6):
            status, reply = self.bot.post(
                "/v1/reply",
                {
                    "conversation_id": conv,
                    "merchant_id": "m_1",
                    "customer_id": None,
                    "from_role": "merchant",
                    "message": repeated_msg,
                    "received_at": "2026-05-01T10:01:00Z",
                    "turn_number": i + 1,
                },
            )
            self.assertEqual(status, 200)
            reply_action = reply["action"]
            if i == 2:
                self.assertEqual(reply_action, "wait")
            if i >= 4:
                self.assertEqual(reply_action, "end")
                break
        self.assertEqual(reply_action, "end")

    def test_ambiguous_reply_gets_two_option_clarifier(self) -> None:
        self._seed_minimal_contexts()
        status, tick = self.bot.post(
            "/v1/tick",
            {"now": "2026-05-01T10:00:00Z", "available_triggers": ["trg_1"]},
        )
        self.assertEqual(status, 200)
        conv = tick["actions"][0]["conversation_id"]

        status, reply = self.bot.post(
            "/v1/reply",
            {
                "conversation_id": conv,
                "merchant_id": "m_1",
                "customer_id": None,
                "from_role": "merchant",
                "message": "hmm",
                "received_at": "2026-05-01T10:01:00Z",
                "turn_number": 2,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(reply["action"], "send")
        self.assertIn("(1)", reply["body"])
        self.assertIn("(2)", reply["body"])

    def test_abusive_reply_deescalates_and_stops(self) -> None:
        self._seed_minimal_contexts()
        status, tick = self.bot.post(
            "/v1/tick",
            {"now": "2026-05-01T10:00:00Z", "available_triggers": ["trg_1"]},
        )
        self.assertEqual(status, 200)
        conv = tick["actions"][0]["conversation_id"]

        status, reply = self.bot.post(
            "/v1/reply",
            {
                "conversation_id": conv,
                "merchant_id": "m_1",
                "customer_id": None,
                "from_role": "merchant",
                "message": "shut up this is spam",
                "received_at": "2026-05-01T10:01:00Z",
                "turn_number": 2,
            },
        )
        self.assertEqual(status, 200)
        self.assertIn(reply["action"], {"send", "end"})
        if reply["action"] == "send":
            self.assertEqual(reply["cta"], "none")

    def _seed_minimal_contexts(self) -> None:
        category = {
            "slug": "dentists",
            "voice": {"tone": "peer_clinical"},
            "peer_stats": {"avg_ctr": 0.03},
            "digest": [
                {
                    "id": "d_1",
                    "title": "3-month recall better",
                    "source": "JIDA Oct 2026, p.14",
                    "trial_n": 2100,
                    "patient_segment": "high_risk_adults",
                    "summary": "38% lower recurrence for high-risk adults.",
                }
            ],
            "offer_catalog": [],
            "seasonal_beats": [],
            "trend_signals": [],
            "patient_content_library": [],
        }
        merchant = {
            "merchant_id": "m_1",
            "category_slug": "dentists",
            "identity": {
                "name": "Dr. Test Clinic",
                "owner_first_name": "Test",
                "city": "Delhi",
                "locality": "Saket",
                "languages": ["en", "hi"],
            },
            "subscription": {"status": "active", "plan": "Pro", "days_remaining": 100},
            "performance": {"views": 1000, "calls": 10, "ctr": 0.021, "delta_7d": {"views_pct": 0.1, "calls_pct": 0.0}},
            "offers": [{"id": "o1", "title": "Dental Cleaning @ ₹299", "status": "active"}],
            "conversation_history": [],
            "customer_aggregate": {"total_unique_ytd": 100},
            "signals": [],
        }
        trigger = {
            "id": "trg_1",
            "scope": "merchant",
            "kind": "research_digest",
            "source": "external",
            "merchant_id": "m_1",
            "customer_id": None,
            "payload": {"category": "dentists", "top_item_id": "d_1"},
            "urgency": 2,
            "suppression_key": "research:dentists:w18",
            "expires_at": "2026-05-10T00:00:00Z",
        }

        for scope, context_id, payload in [
            ("category", "dentists", category),
            ("merchant", "m_1", merchant),
            ("trigger", "trg_1", trigger),
        ]:
            status, _ = self.bot.post(
                "/v1/context",
                {
                    "scope": scope,
                    "context_id": context_id,
                    "version": 1,
                    "payload": payload,
                    "delivered_at": "2026-05-01T00:00:00Z",
                },
            )
            self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
