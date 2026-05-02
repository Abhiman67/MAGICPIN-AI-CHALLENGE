#!/usr/bin/env python3
"""Unit tests for deterministic message quality policy."""

from __future__ import annotations

import unittest

import bot


def sample_merchant() -> dict:
    return {
        "merchant_id": "merchant_001",
        "category_slug": "salons",
        "identity": {
            "name": "Glow Studio",
            "owner_first_name": "Anjali",
            "city": "Bengaluru",
            "locality": "HSR Layout",
            "languages": ["en"],
        },
        "performance": {"ctr": 0.032, "delta_7d": {"views_pct": -0.12, "calls_pct": -0.18}},
        "offers": [{"title": "Hair Spa Combo", "status": "active"}],
        "conversation_history": [{"engagement": "ignored_last"}],
        "signals": ["ctr_below_peer", "stale_posts"],
    }


def sample_category() -> dict:
    return {
        "slug": "salons",
        "peer_stats": {"avg_ctr": 0.045},
        "digest": [{"id": "d1", "title": "Local style trend", "summary": "Short-form reels are converting."}],
    }


class QualityPolicyTests(unittest.TestCase):
    def test_action_trigger_has_numeric_anchor_and_decision_token(self) -> None:
        msg = bot.build_first_touch(
            sample_category(),
            sample_merchant(),
            {
                "id": "t1",
                "kind": "profile_incomplete",
                "scope": "merchant",
                "payload": {"missing_fields": ["hours", "services", "photos"]},
            },
        )
        self.assertRegex(msg.body, r"\d|%|₹")
        self.assertIn("Reply", msg.body)

    def test_info_trigger_not_forced_to_binary(self) -> None:
        msg = bot.build_first_touch(
            sample_category(),
            sample_merchant(),
            {"id": "t2", "kind": "milestone_reached", "scope": "merchant", "payload": {"metric": "views", "value_now": 120, "milestone_value": 100}},
        )
        self.assertEqual(msg.cta, "none")
        self.assertNotIn("Reply 1 or 2.", msg.body)

    def test_single_primary_cta_only(self) -> None:
        msg = bot.build_first_touch(
            sample_category(),
            sample_merchant(),
            {"id": "t3", "kind": "review_sentiment_alert", "scope": "merchant", "payload": {"sentiment": "negative", "review_count": 4}},
        )
        count = sum(1 for token in ["Reply 1 or 2.", "Reply DRAFT.", "Reply PLAN.", "Reply CHECKLIST.", "Reply PACK.", "Reply SEND.", "Reply GO."] if token in msg.body)
        self.assertLessEqual(count, 1)

    def test_avoids_generic_opening(self) -> None:
        msg = bot.build_first_touch(
            sample_category(),
            sample_merchant(),
            {"id": "t4", "kind": "unknown_kind", "scope": "merchant", "payload": {}},
        )
        self.assertNotIn("quick update from Vera", msg.body)


if __name__ == "__main__":
    unittest.main()

