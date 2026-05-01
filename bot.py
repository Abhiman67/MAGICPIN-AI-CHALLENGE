#!/usr/bin/env python3
"""Standalone Vera bot for the magicpin AI challenge.

This implementation intentionally avoids external dependencies so it can run
anywhere with a stock Python 3 interpreter. It exposes the 5 required judge
endpoints plus an optional /v1/teardown.

Behavior:
- Stores category / merchant / customer / trigger contexts with versioning
- Composes trigger-aware outbound messages
- Handles merchant replies with auto-reply, intent-transition, and hostile
  detection
- Returns deterministic, concise WhatsApp-ready messages
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse

HOST = os.getenv("BOT_HOST", "0.0.0.0")
PORT = int(os.getenv("BOT_PORT", "8080"))
TEAM_NAME = os.getenv("TEAM_NAME", "Team Vera")
TEAM_MEMBERS = [m.strip() for m in os.getenv("TEAM_MEMBERS", "Codex").split(",") if m.strip()]
MODEL_NAME = os.getenv("BOT_MODEL", "rules-based composer")
APPROACH = os.getenv(
    "BOT_APPROACH",
    "deterministic 4-context composer with trigger routing, dedup, and reply handling",
)
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "team@example.com")
VERSION = os.getenv("BOT_VERSION", "1.0.0")
DB_PATH = os.getenv("BOT_DB_PATH", "").strip()
RATE_LIMIT_RPS = max(1, int(os.getenv("BOT_RATE_LIMIT_RPS", "25")))
RATE_LIMIT_BURST = max(RATE_LIMIT_RPS, int(os.getenv("BOT_RATE_LIMIT_BURST", "100")))
RATE_LIMIT_WINDOW_SEC = 1.0


def log_event(event: str, **fields: Any) -> None:
    payload = {"ts": iso_now(), "event": event, **fields}
    try:
        print(json.dumps(payload, ensure_ascii=True), flush=True)
    except Exception:
        print(f'{{"ts":"{iso_now()}","event":"{event}","log_error":"serialization_failed"}}', flush=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return None


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def first(items: list[Any], default: Any = None) -> Any:
    return items[0] if items else default


def safe_get(data: dict[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def words(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z']+", text.lower())


def has_any(text: str, phrases: list[str]) -> bool:
    hay = normalize(text)
    return any(p in hay for p in phrases)


def format_date(value: str | None) -> str | None:
    dt = parse_iso(value)
    if dt is None:
        return value
    return dt.strftime("%-d %b %Y")


def format_time_label(value: str | None) -> str | None:
    dt = parse_iso(value)
    if dt is None:
        return value
    return dt.strftime("%a %-d %b, %-I%p").replace("AM", "am").replace("PM", "pm")


@dataclass
class ComposedMessage:
    body: str
    cta: str
    send_as: str
    suppression_key: str
    rationale: str
    template_name: str | None = None
    template_params: list[str] | None = None

    def to_action(self, conversation_id: str, merchant_id: str, customer_id: str | None, trigger_id: str) -> dict[str, Any]:
        action = {
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": self.send_as,
            "trigger_id": trigger_id,
            "body": self.body,
            "cta": self.cta,
            "suppression_key": self.suppression_key,
            "rationale": self.rationale,
        }
        if self.template_name:
            action["template_name"] = self.template_name
        if self.template_params:
            action["template_params"] = self.template_params
        return action


class BotState:
    def __init__(self) -> None:
        self.started_at = utc_now()
        self.lock = threading.RLock()
        self.contexts: dict[tuple[str, str], dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}
        self.sent_suppression_keys: set[str] = set()
        self.metrics = {
            "context_push_total": 0,
            "context_push_rejected_total": 0,
            "tick_total": 0,
            "actions_sent_total": 0,
            "reply_total": 0,
            "reply_action_send_total": 0,
            "reply_action_wait_total": 0,
            "reply_action_end_total": 0,
            "auto_reply_detected_total": 0,
            "opt_out_total": 0,
            "suppression_hits_total": 0,
            "rate_limited_total": 0,
        }
        self.db_path = DB_PATH
        if self.db_path:
            self._init_db()
            self._load_from_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kv_state (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

    def _serialize_state(self) -> dict[str, str]:
        contexts = {
            f"{scope}::{cid}": value for (scope, cid), value in self.contexts.items()
        }
        conversations = {}
        for cid, convo in self.conversations.items():
            cloned = dict(convo)
            if isinstance(cloned.get("sent_bodies"), set):
                cloned["sent_bodies"] = sorted(list(cloned["sent_bodies"]))
            conversations[cid] = cloned
        return {
            "started_at": self.started_at.isoformat(),
            "contexts": json.dumps(contexts, ensure_ascii=False),
            "conversations": json.dumps(conversations, ensure_ascii=False),
            "suppression_keys": json.dumps(sorted(list(self.sent_suppression_keys)), ensure_ascii=False),
            "metrics": json.dumps(self.metrics, ensure_ascii=False),
        }

    def _save_to_db(self) -> None:
        if not self.db_path:
            return
        state = self._serialize_state()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                "INSERT INTO kv_state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                [(k, v) for k, v in state.items()],
            )
            conn.commit()
        finally:
            conn.close()

    def _load_from_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = dict(conn.execute("SELECT k, v FROM kv_state").fetchall())
        finally:
            conn.close()
        if not rows:
            return
        try:
            started_raw = rows.get("started_at")
            if started_raw:
                self.started_at = parse_iso(started_raw) or self.started_at
            contexts_raw = json.loads(rows.get("contexts", "{}"))
            self.contexts = {}
            for key, value in contexts_raw.items():
                if "::" in key:
                    scope, cid = key.split("::", 1)
                    self.contexts[(scope, cid)] = value
            conversations_raw = json.loads(rows.get("conversations", "{}"))
            self.conversations = {}
            for cid, convo in conversations_raw.items():
                c = dict(convo)
                c["sent_bodies"] = set(c.get("sent_bodies", []))
                self.conversations[cid] = c
            self.sent_suppression_keys = set(json.loads(rows.get("suppression_keys", "[]")))
            metrics_raw = json.loads(rows.get("metrics", "{}"))
            if isinstance(metrics_raw, dict):
                for key in self.metrics:
                    if key in metrics_raw and isinstance(metrics_raw[key], int):
                        self.metrics[key] = metrics_raw[key]
            log_event(
                "state_loaded",
                db_path=self.db_path,
                contexts=len(self.contexts),
                conversations=len(self.conversations),
                suppression_keys=len(self.sent_suppression_keys),
            )
        except Exception as e:
            log_event("state_load_failed", db_path=self.db_path, error=str(e))

    def incr(self, metric: str, count: int = 1) -> None:
        with self.lock:
            if metric not in self.metrics:
                self.metrics[metric] = 0
            self.metrics[metric] += count
            self._save_to_db()

    def metrics_snapshot(self) -> dict[str, int]:
        with self.lock:
            return dict(self.metrics)

    def clear(self) -> None:
        with self.lock:
            self.contexts.clear()
            self.conversations.clear()
            self.sent_suppression_keys.clear()
            self.started_at = utc_now()
            for key in self.metrics:
                self.metrics[key] = 0
            self._save_to_db()

    def context_counts(self) -> dict[str, int]:
        counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        with self.lock:
            for (scope, _), _ctx in self.contexts.items():
                if scope in counts:
                    counts[scope] += 1
        return counts

    def store_context(self, scope: str, context_id: str, version: int, payload: dict[str, Any], delivered_at: str) -> tuple[bool, dict[str, Any]]:
        with self.lock:
            self.metrics["context_push_total"] += 1
            if scope not in {"category", "merchant", "customer", "trigger"}:
                self.metrics["context_push_rejected_total"] += 1
                self._save_to_db()
                return False, {"accepted": False, "reason": "invalid_scope", "details": scope}

            key = (scope, context_id)
            current = self.contexts.get(key)
            if current and current["version"] >= version:
                self.metrics["context_push_rejected_total"] += 1
                self._save_to_db()
                return False, {
                    "accepted": False,
                    "reason": "stale_version",
                    "current_version": current["version"],
                }

            self.contexts[key] = {
                "version": version,
                "payload": payload,
                "delivered_at": delivered_at,
            }
            self._save_to_db()
            return True, {
                "accepted": True,
                "ack_id": f"ack_{context_id}_v{version}",
                "stored_at": iso_now(),
            }

    def get_context(self, scope: str, context_id: str) -> dict[str, Any] | None:
        with self.lock:
            item = self.contexts.get((scope, context_id))
            if not item:
                return None
            return item["payload"]

    def get_trigger(self, trigger_id: str) -> dict[str, Any] | None:
        return self.get_context("trigger", trigger_id)

    def get_merchant(self, merchant_id: str) -> dict[str, Any] | None:
        return self.get_context("merchant", merchant_id)

    def get_customer(self, customer_id: str) -> dict[str, Any] | None:
        return self.get_context("customer", customer_id)

    def get_category(self, slug: str) -> dict[str, Any] | None:
        return self.get_context("category", slug)

    def record_conversation(self, conversation_id: str, merchant_id: str, customer_id: str | None, trigger_id: str | None, send_as: str) -> None:
        with self.lock:
            self.conversations.setdefault(conversation_id, {
                "conversation_id": conversation_id,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "trigger_id": trigger_id,
                "send_as": send_as,
                "messages": [],
                "status": "open",
                "turn_count": 0,
                "last_bot_body": "",
                "last_merchant_message": "",
                "last_merchant_at": None,
                "auto_reply_count": 0,
                "sent_bodies": set(),
            })
            self._save_to_db()

    def append_bot_message(self, conversation_id: str, body: str) -> None:
        with self.lock:
            convo = self.conversations.setdefault(conversation_id, {
                "conversation_id": conversation_id,
                "messages": [],
                "status": "open",
                "turn_count": 0,
                "last_bot_body": "",
                "last_merchant_message": "",
                "last_merchant_at": None,
                "auto_reply_count": 0,
                "sent_bodies": set(),
            })
            convo["messages"].append({"from": "bot", "body": body, "at": iso_now()})
            convo["turn_count"] += 1
            convo["last_bot_body"] = body
            convo["sent_bodies"].add(body)
            self._save_to_db()

    def append_merchant_message(self, conversation_id: str, message: str, received_at: str, from_role: str = "merchant") -> None:
        with self.lock:
            convo = self.conversations.setdefault(conversation_id, {
                "conversation_id": conversation_id,
                "messages": [],
                "status": "open",
                "turn_count": 0,
                "last_bot_body": "",
                "last_merchant_message": "",
                "last_merchant_at": None,
                "auto_reply_count": 0,
                "sent_bodies": set(),
            })
            convo["messages"].append({"from": from_role, "body": message, "at": received_at})
            convo["turn_count"] += 1
            convo["last_merchant_message"] = message
            convo["last_merchant_at"] = received_at
            self._save_to_db()

    def mark_status(self, conversation_id: str, status: str) -> None:
        with self.lock:
            if conversation_id in self.conversations:
                self.conversations[conversation_id]["status"] = status
                self._save_to_db()

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self.lock:
            return self.conversations.get(conversation_id)

    def suppress(self, suppression_key: str) -> bool:
        with self.lock:
            if suppression_key in self.sent_suppression_keys:
                self.metrics["suppression_hits_total"] += 1
                self._save_to_db()
                return True
            self.sent_suppression_keys.add(suppression_key)
            self._save_to_db()
            return False


STATE = BotState()


def merchant_owner_name(merchant: dict[str, Any]) -> str:
    identity = merchant.get("identity", {})
    owner = identity.get("owner_first_name")
    if owner:
        return owner
    name = identity.get("name") or merchant.get("merchant_id", "Merchant")
    if name.lower().startswith("dr."):
        return name
    return name.split(" ")[0]


def merchant_salutation(merchant: dict[str, Any], category_slug: str) -> str:
    identity = merchant.get("identity", {})
    name = identity.get("name", "Merchant")
    owner = merchant_owner_name(merchant)
    if category_slug == "dentists":
        return f"Dr. {owner}"
    if name.lower().startswith("dr."):
        return name
    return owner


def customer_salutation(customer: dict[str, Any]) -> str:
    return customer.get("identity", {}).get("name", "there")


def category_tone(slug: str) -> str:
    tones = {
        "dentists": "peer-clinical",
        "salons": "warm-practical",
        "restaurants": "operator-to-operator",
        "gyms": "coach-like",
        "pharmacies": "trustworthy-precise",
    }
    return tones.get(slug, "professional")


def preferred_language_mix(merchant: dict[str, Any], customer: dict[str, Any] | None = None) -> bool:
    if customer:
        pref = normalize(customer.get("identity", {}).get("language_pref", ""))
        return "hi" in pref
    langs = merchant.get("identity", {}).get("languages", [])
    return any(lang in {"hi", "hinglish"} for lang in langs)


def hinglish_phrase(text: str, enabled: bool) -> str:
    if not enabled:
        return text
    swaps = [
        ("Want me to", "Chahiye to main"),
        ("Want the", "Chahiye to"),
        ("Reply YES to book, or STOP to opt out.", "Reply YES to book, ya STOP for no reminders."),
        ("I can also draft", "Main draft bhi kar sakta hoon"),
    ]
    for old, new in swaps:
        text = text.replace(old, new)
    return text


def merchant_signal_snapshot(merchant: dict[str, Any]) -> str:
    signals = merchant.get("signals", [])
    if not isinstance(signals, list):
        return ""
    normalized = [str(s) for s in signals[:6]]
    if not normalized:
        return ""
    if any("stale_posts" in s for s in normalized):
        return "Posts look stale"
    if any("ctr_below_peer" in s for s in normalized):
        return "CTR is below peer median"
    if any("dormant" in s for s in normalized):
        return "Recent engagement looks dormant"
    return normalized[0]


def merchant_history_snapshot(merchant: dict[str, Any]) -> str:
    history = merchant.get("conversation_history", [])
    if not isinstance(history, list) or not history:
        return "no recent Vera conversation"
    last = history[-1]
    tag = str(last.get("engagement", "unknown"))
    if "replied" in tag:
        return "merchant replied recently"
    if "no_reply" in tag or "ignored" in tag:
        return "merchant ignored last nudge"
    return "recent conversation exists"


def persuasion_line(kind: str, merchant: dict[str, Any], trigger: dict[str, Any]) -> str:
    payload = trigger_payload(trigger)
    if kind in {"lead_missed_searches", "lead_followup_due"}:
        missed = payload.get("missed_searches") or payload.get("missed_leads")
        if missed is not None:
            return f"That may be costing roughly {missed} potential discovery events."
        return "There is likely a measurable discovery-to-lead loss."
    if kind in {"perf_dip", "seasonal_perf_dip"}:
        delta = safe_get(merchant, "performance", "delta_7d", default={}).get("calls_pct")
        if isinstance(delta, (int, float)) and delta < 0:
            return f"Call trend is down {delta:+.0%}; acting now prevents further drop."
        return "Early action now typically prevents next-week spillover."
    if kind in {"review_theme_emerged", "review_sentiment_alert"}:
        return "Quick response can prevent negative sentiment snowball."
    if kind in {"profile_incomplete", "profile_hours_missing", "profile_attributes_missing", "seo_visibility_gap"}:
        return "Completing these fields usually improves listing trust and click-through."
    if kind in {"festival_upcoming", "ipl_match_today"}:
        return "Timing this before the window closes usually performs better."
    return ""


def trigger_specificity_line(kind: str, merchant: dict[str, Any], category: dict[str, Any], trigger: dict[str, Any]) -> str:
    payload = trigger_payload(trigger)
    ctr = merchant_ctr(merchant)
    peer_ctr = category_peer_ctr(category)
    if kind in {"perf_dip", "seasonal_perf_dip", "perf_spike"}:
        delta_7d = safe_get(merchant, "performance", "delta_7d", default={}) or {}
        views_pct = delta_7d.get("views_pct")
        calls_pct = delta_7d.get("calls_pct")
        if isinstance(views_pct, (int, float)) and isinstance(calls_pct, (int, float)):
            return f"This week: views {views_pct:+.0%}, calls {calls_pct:+.0%}."
    if kind in {"lead_missed_searches", "lead_followup_due"}:
        missed = payload.get("missed_searches") or payload.get("missed_leads")
        if missed is not None:
            return f"Signal count right now: ~{missed} missed opportunities."
    if kind in {"profile_incomplete", "profile_hours_missing", "profile_attributes_missing", "seo_visibility_gap"}:
        missing_fields = payload.get("missing_fields") or []
        if isinstance(missing_fields, list) and missing_fields:
            return f"Priority fix count: {len(missing_fields[:4])} field(s)."
    if kind in {"review_sentiment_alert", "review_response_draft", "review_theme_emerged"}:
        review_count = payload.get("review_count") or payload.get("occurrences_30d")
        if review_count is not None:
            return f"Observed review volume in trigger: {review_count}."
    if kind == "renewal_due":
        days_remaining = merchant_days_remaining(merchant)
        if days_remaining is not None:
            return f"Renewal window remaining: {days_remaining} day(s)."
    if ctr is not None and peer_ctr is not None:
        return f"Reference: CTR {ctr:.1%} vs peer {peer_ctr:.1%}."
    return ""


def trigger_action_line(kind: str, merchant: dict[str, Any], trigger: dict[str, Any]) -> str:
    offer = active_offer(merchant)
    if kind in {"perf_dip", "seasonal_perf_dip", "perf_spike", "festival_upcoming", "ipl_match_today", "local_news_event"}:
        offer_title = offer.get("title") if offer else "your top offer"
        return f"If you reply DRAFT, I’ll send 1 post + 1 reply script for {offer_title}."
    if kind in {"review_sentiment_alert", "review_response_draft", "review_theme_emerged"}:
        return "If you reply DRAFT, I’ll send 3 response templates by tone: apology, neutral, and assertive."
    if kind in {"profile_incomplete", "profile_hours_missing", "profile_attributes_missing", "seo_visibility_gap"}:
        return "If you reply CHECKLIST, I’ll send the exact update order in 5 steps."
    if kind in {"lead_missed_searches", "lead_followup_due"}:
        return "If you reply PLAN, I’ll send a 7-day lead-capture plan with daily actions."
    if kind in {"photo_gap_detected", "content_pack_ready"}:
        return "If you reply PACK, I’ll send shot list + caption pack for this week."
    if kind in {"renewal_due"}:
        return "If you reply SEND, I’ll draft the renewal CTA for one-tap approval."
    if kind in {"active_planning_intent"}:
        topic = trigger_payload(trigger).get("intent_topic") or "this plan"
        return f"If you reply GO, I’ll send a ready-to-send draft for {topic}."
    return "If useful, reply DRAFT and I’ll prepare the exact next message."


def category_digest_item(category: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any] | None:
    payload = trigger.get("payload", {})
    item_id = payload.get("top_item_id")
    if item_id:
        for item in category.get("digest", []):
            if item.get("id") == item_id:
                return item
    top_item = payload.get("top_item")
    if isinstance(top_item, dict) and top_item.get("title"):
        return top_item
    digest = category.get("digest", [])
    return first(digest)


def active_offer(merchant: dict[str, Any]) -> dict[str, Any] | None:
    offers = merchant.get("offers", [])
    active = [o for o in offers if o.get("status") == "active"]
    return first(active)


def category_peer_ctr(category: dict[str, Any]) -> float | None:
    peer = category.get("peer_stats", {})
    return peer.get("avg_ctr")


def merchant_ctr(merchant: dict[str, Any]) -> float | None:
    return safe_get(merchant, "performance", "ctr")


def merchant_views(merchant: dict[str, Any]) -> Any:
    return safe_get(merchant, "performance", "views")


def merchant_calls(merchant: dict[str, Any]) -> Any:
    return safe_get(merchant, "performance", "calls")


def merchant_days_remaining(merchant: dict[str, Any]) -> Any:
    return safe_get(merchant, "subscription", "days_remaining")


def merchant_plan(merchant: dict[str, Any]) -> str:
    return safe_get(merchant, "subscription", "plan", default="Pro")


def merchant_city(merchant: dict[str, Any]) -> str:
    return merchant.get("identity", {}).get("city", "")


def merchant_locality(merchant: dict[str, Any]) -> str:
    return merchant.get("identity", {}).get("locality", "")


def trigger_kind(trigger: dict[str, Any]) -> str:
    return trigger.get("kind", "") or ""


def trigger_payload(trigger: dict[str, Any]) -> dict[str, Any]:
    payload = trigger.get("payload", {})
    return payload if isinstance(payload, dict) else {}


def trigger_scope(trigger: dict[str, Any]) -> str:
    return trigger.get("scope", "merchant")


def is_auto_reply(message: str) -> bool:
    text = normalize(message)
    patterns = [
        "thank you for contacting",
        "our team will respond shortly",
        "we will respond shortly",
        "thank you for reaching out",
        "we appreciate your message",
        "our team is currently unavailable",
        "out of office",
    ]
    return any(p in text for p in patterns)


def is_hard_no(message: str) -> bool:
    text = normalize(message)
    patterns = [
        "not interested",
        "stop messaging",
        "stop messaging me",
        "do not contact",
        "unsubscribe",
        "remove me",
        "leave me alone",
        "spam",
        "no thanks",
        "nah",
    ]
    return any(p in text for p in patterns)


def is_commitment(message: str) -> bool:
    text = normalize(message)
    patterns = [
        "yes please",
        "yes, please",
        "ok let's do it",
        "okay let's do it",
        "lets do it",
        "let's do it",
        "go ahead",
        "please do",
        "send it",
        "send me",
        "draft it",
        "do it",
        "sure",
        "proceed",
    ]
    return any(p in text for p in patterns)


def is_delay_request(message: str) -> bool:
    text = normalize(message)
    patterns = [
        "later",
        "tomorrow",
        "next week",
        "after sometime",
        "in a while",
        "busy right now",
        "call later",
        "remind me",
        "not now",
    ]
    return any(p in text for p in patterns)


def is_out_of_scope(message: str) -> bool:
    text = normalize(message)
    return "gst" in text or "tax" in text or "billing" in text and "gst" in text


def is_abusive(message: str) -> bool:
    text = normalize(message)
    abusive_terms = ["idiot", "stupid", "shut up", "f***", "fuck", "bs", "bastard", "madarchod", "chutiya"]
    return any(term in text for term in abusive_terms)


def is_ambiguous(message: str) -> bool:
    text = normalize(message)
    if text in {"ok", "hmm", "hmmm", "maybe", "not sure", "idk", "k", "fine"}:
        return True
    return len(words(text)) <= 2 and "?" not in text and not is_commitment(message) and not is_hard_no(message)


def build_template_name(kind: str, fallback: str = "generic") -> str:
    mapping = {
        "research_digest": "vera_research_digest_v1",
        "research_digest_release": "vera_research_digest_v1",
        "category_research_digest_release": "vera_research_digest_v1",
        "recall_due": "vera_recall_due_v1",
        "customer_lapsed_soft": "vera_recall_due_v1",
        "appointment_tomorrow": "vera_appointment_reminder_v1",
        "perf_dip": "vera_perf_dip_v1",
        "perf_spike": "vera_perf_spike_v1",
        "seasonal_perf_dip": "vera_perf_dip_v1",
        "renewal_due": "vera_renewal_due_v1",
        "curious_ask_due": "vera_curious_ask_v1",
        "review_theme_emerged": "vera_review_theme_v1",
        "competitor_opened": "vera_competitor_opened_v1",
        "festival_upcoming": "vera_festival_upcoming_v1",
        "ipl_match_today": "vera_ipl_day_v1",
        "active_planning_intent": "vera_action_plan_v1",
        "milestone_reached": "vera_milestone_v1",
        "trial_followup": "vera_trial_followup_v1",
        "chronic_refill_due": "vera_refill_due_v1",
        "weather_heatwave": "vera_weather_alert_v1",
        "review_sentiment_alert": "vera_review_alert_v1",
        "review_response_draft": "vera_review_response_v1",
        "profile_incomplete": "vera_profile_incomplete_v1",
        "profile_hours_missing": "vera_profile_hours_v1",
        "profile_attributes_missing": "vera_profile_attributes_v1",
        "seo_visibility_gap": "vera_seo_gap_v1",
        "lead_missed_searches": "vera_lead_capture_v1",
        "lead_followup_due": "vera_lead_followup_v1",
        "photo_gap_detected": "vera_photo_gap_v1",
        "content_pack_ready": "vera_content_pack_v1",
    }
    return mapping.get(kind, f"vera_{slugify(fallback)}_v1")


def build_rationale(kind: str, send_as: str, specificity: str, cta: str) -> str:
    return truncate(
        f"{'Customer-facing' if send_as == 'merchant_on_behalf' else 'Merchant-facing'} {kind} message; {specificity}; CTA is {cta}.",
        240,
    )


def cta_for_kind(kind: str, info_only: bool = False) -> str:
    if info_only:
        return "none"
    binary_kinds = {
        "recall_due",
        "customer_lapsed_soft",
        "customer_lapsed_hard",
        "appointment_tomorrow",
        "trial_followup",
        "chronic_refill_due",
        "renewal_due",
        "lead_followup_due",
        "profile_hours_missing",
        "profile_attributes_missing",
        "profile_incomplete",
        "photo_gap_detected",
    }
    if kind in binary_kinds:
        return "binary_yes_no"
    return "open_ended"


def has_numeric_anchor(text: str) -> bool:
    return bool(re.search(r"\d", text)) or "%" in text or "₹" in text


def enforce_first_touch_quality(
    body: str,
    kind: str,
    scope: str,
    merchant: dict[str, Any],
    category: dict[str, Any],
    trigger: dict[str, Any],
) -> str:
    """Deterministic quality guardrail for first-touch content."""
    hardened = body
    specificity = trigger_specificity_line(kind, merchant, category, trigger)
    action_line = trigger_action_line(kind, merchant, trigger)
    action_needed = scope != "customer" and kind not in {"milestone_reached"}

    if not has_numeric_anchor(hardened) and specificity:
        hardened += f" {specificity}"
    if action_needed:
        normalized = normalize(hardened)
        if "reply " not in normalized and "want me to" in normalized and action_line:
            hardened += f" {action_line}"
        elif "reply " not in normalized and action_line:
            hardened += f" {action_line}"

    if "quick update from vera." in normalize(hardened):
        hardened = hardened.replace("quick update from Vera.", "here is the highest-impact update for today.")

    return truncate(hardened, 420)


def build_first_touch(
    category: dict[str, Any],
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: dict[str, Any] | None = None,
) -> ComposedMessage:
    kind = trigger_kind(trigger)
    scope = trigger_scope(trigger)
    send_as = "merchant_on_behalf" if customer else "vera"
    category_slug = category.get("slug", merchant.get("category_slug", "generic"))
    salutation = customer_salutation(customer) if customer else merchant_salutation(merchant, category_slug)
    language_mix = preferred_language_mix(merchant, customer)
    hook = ""
    cta_text = ""
    body = ""
    template_name = build_template_name(kind)
    template_params: list[str] = []
    sup_key = trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id', 'merchant')}"

    item = category_digest_item(category, trigger)
    offer = active_offer(merchant)
    peer_ctr = category_peer_ctr(category)
    ctr = merchant_ctr(merchant)
    city = merchant_city(merchant)
    locality = merchant_locality(merchant)
    signal_line = merchant_signal_snapshot(merchant)
    history_line = merchant_history_snapshot(merchant)

    if kind in {"research_digest", "research_digest_release", "category_research_digest_release"}:
        if item:
            title = item.get("title") or item.get("name") or "new item"
            source = item.get("source")
            summary = item.get("summary") or item.get("actionable") or ""
            trial_n = item.get("trial_n")
            patient_segment = item.get("patient_segment")
            pieces = []
            if trial_n:
                pieces.append(f"{int(trial_n):,}-patient trial")
            if patient_segment:
                pieces.append(patient_segment.replace("_", " "))
            descriptor = ", ".join(pieces)
            hook = f"{title}"
            issue_label = "your category digest"
            if source:
                issue_label = "JIDA's Oct issue" if "JIDA" in source else source
            body = f"{salutation}, {issue_label} landed."
            if descriptor:
                body += f" One item relevant to {descriptor} — "
            else:
                body += " One item worth a look — "
            body += f"{summary or title}"
            if source:
                body += f" — {source}"
            cta_text = "Want me to pull the abstract and draft a patient-ed WhatsApp you can share?"
            template_params = [
                salutation,
                truncate(f"{title}. {summary or ''}".strip(), 120),
                cta_text,
            ]
        else:
            hook = "new research digest"
            body = f"{salutation}, your category digest is in."
            if ctr is not None and peer_ctr is not None:
                body += f" Your CTR is {ctr:.1%} vs peer {peer_ctr:.1%}."
            cta_text = "Want me to pull the top item and draft a post?"
            template_params = [salutation, hook, cta_text]

    elif kind in {"recall_due", "customer_lapsed_soft", "customer_lapsed_hard", "appointment_tomorrow", "trial_followup", "chronic_refill_due"} and customer:
        customer_name = customer_salutation(customer)
        relationship = customer.get("relationship", {})
        last_visit = format_date(relationship.get("last_visit"))
        due_date = format_date(trigger_payload(trigger).get("due_date") or trigger_payload(trigger).get("next_due_date"))
        slots = trigger_payload(trigger).get("available_slots") or trigger_payload(trigger).get("next_session_options") or []
        slot_labels = [s.get("label") or format_time_label(s.get("iso")) for s in slots if isinstance(s, dict)]
        offer_title = offer.get("title") if offer else None
        body = f"Hi {customer_name}, {merchant_salutation(merchant, category_slug)} here."
        if kind == "recall_due":
            body += " It's time for your recall visit"
            if last_visit:
                body += f" — last visit was {last_visit}."
            if due_date:
                body += f" Due date: {due_date}."
        elif kind == "appointment_tomorrow":
            body += " Your appointment is coming up tomorrow."
        elif kind == "customer_lapsed_hard":
            body += " We have not seen you in a while and your follow-up is pending."
        elif kind == "trial_followup":
            body += " Following up on your trial."
        elif kind == "chronic_refill_due":
            body += " Your refill window is open."
        else:
            body += " Your next check-in is due."

        if slot_labels:
            body += " Available slots: " + " / ".join(slot_labels[:2]) + "."
        if offer_title:
            body += f" {offer_title}."
        if kind == "trial_followup":
            body += " Want to book your next step?"
        else:
            body += " Reply YES to book, or STOP if you want no more reminders."

        cta_text = "Reply YES to book, or STOP to opt out."
        template_params = [
            customer_name,
            truncate(body, 120),
            cta_text,
        ]
        if language_mix and "hi" in normalize(customer.get("identity", {}).get("language_pref", "")):
            body = body.replace("Reply YES to book, or STOP if you want no more reminders.", "Reply YES to book, ya STOP if you want no more reminders.")

    elif kind in {"perf_dip", "seasonal_perf_dip"}:
        delta_7d = safe_get(merchant, "performance", "delta_7d", default={}) or {}
        views_pct = delta_7d.get("views_pct")
        calls_pct = delta_7d.get("calls_pct")
        body = f"{salutation}, your {merchant_locality(merchant) or 'local'} profile needs a quick look."
        if views_pct is not None:
            body += f" Views moved {views_pct:+.0%} this week"
        if calls_pct is not None:
            body += f" and calls moved {calls_pct:+.0%}."
        if ctr is not None and peer_ctr is not None:
            body += f" CTR is {ctr:.1%} vs peer {peer_ctr:.1%}."
        if offer:
            body += f" Your active offer is {offer.get('title')}."
        if signal_line:
            body += f" {signal_line}."
        body += f" Last touch status: {history_line}."
        lever = persuasion_line(kind, merchant, trigger)
        if lever:
            body += f" {lever}"
        body += " Want me to draft a tighter post and a 1-line reply you can reuse?"
        cta_text = "Want me to draft the fix?"
        template_params = [salutation, truncate(body, 120), cta_text]

    elif kind == "perf_spike":
        delta_7d = safe_get(merchant, "performance", "delta_7d", default={}) or {}
        views_pct = delta_7d.get("views_pct")
        calls_pct = delta_7d.get("calls_pct")
        body = f"{salutation}, you have momentum right now."
        if views_pct is not None:
            body += f" Views are up {views_pct:+.0%} this week."
        if calls_pct is not None:
            body += f" Calls are up {calls_pct:+.0%}."
        if offer:
            body += f" Your active offer {offer.get('title')} is a good candidate to push."
        body += " Want me to turn this into a post while the spike is live?"
        cta_text = "Want me to turn it into a post?"
        template_params = [salutation, truncate(body, 120), cta_text]

    elif kind == "renewal_due":
        days_remaining = merchant_days_remaining(merchant)
        plan = merchant_plan(merchant)
        amount = trigger_payload(trigger).get("renewal_amount")
        body = f"{salutation}, your {plan} plan renewal is coming up"
        if days_remaining is not None:
            body += f" — {days_remaining} day(s) left."
        if amount is not None:
            body += f" Renewal amount: ₹{amount}."
        body += " Want me to send the renewal CTA you can approve in one tap?"
        cta_text = "Want me to prepare the renewal CTA?"
        template_params = [salutation, truncate(body, 120), cta_text]

    elif kind == "curious_ask_due":
        body = f"{salutation}, quick question: what service is most asked for this week at {merchant.get('identity', {}).get('name', 'your business')}?"
        if offer:
            body += f" I can turn {offer.get('title')} into a Google post and a 4-line WhatsApp reply."
        body += " Takes 5 min."
        cta_text = "What’s in demand this week?"
        template_params = [salutation, truncate(body, 120), cta_text]

    elif kind == "review_theme_emerged":
        theme = trigger_payload(trigger).get("theme") or first(merchant.get("review_themes", []), {}).get("theme", "a review theme")
        count = trigger_payload(trigger).get("occurrences_30d")
        body = f"{salutation}, {count or 'several'} reviews are pointing at {str(theme).replace('_', ' ')}."
        body += " Pick 1 for a response draft, or 2 for a fix-it post."
        lever = persuasion_line(kind, merchant, trigger)
        if lever:
            body += f" {lever}"
        body += " Reply 1 or 2."
        cta_text = "Reply 1 or 2."
        template_params = [salutation, truncate(body, 120), cta_text]

    elif kind == "competitor_opened":
        distance = trigger_payload(trigger).get("distance_km") or trigger_payload(trigger).get("distance")
        competitor = trigger_payload(trigger).get("competitor_name") or "a new competitor"
        body = f"{salutation}, {competitor} just opened nearby"
        if distance is not None:
            body += f" — about {distance} km away."
        body += " Want me to compare their positioning with yours and draft a response?"
        cta_text = "Want the comparison?"
        template_params = [salutation, truncate(body, 120), cta_text]

    elif kind in {"festival_upcoming", "ipl_match_today", "weather_heatwave", "local_news_event"}:
        topic = trigger_payload(trigger).get("festival") or trigger_payload(trigger).get("match") or trigger_payload(trigger).get("headline") or kind.replace("_", " ")
        body = f"{salutation}, quick heads-up on {topic}."
        if offer:
            body += f" Your active offer {offer.get('title')} is a good fit for this window."
        else:
            body += f" {merchant.get('identity', {}).get('name', 'Your business')} has room to ride this event."
        lever = persuasion_line(kind, merchant, trigger)
        if lever:
            body += f" {lever}"
        body += " Want me to draft a post right now?"
        cta_text = "Want me to draft the post?"
        template_params = [salutation, truncate(body, 120), cta_text]

    elif kind in {"review_sentiment_alert", "review_response_draft"}:
        sentiment = trigger_payload(trigger).get("sentiment") or "mixed"
        review_count = trigger_payload(trigger).get("review_count") or trigger_payload(trigger).get("occurrences_30d") or "recent"
        body = f"{salutation}, I spotted {review_count} {sentiment} review signal(s) for {merchant.get('identity', {}).get('name', 'your profile')}."
        if sentiment == "negative":
            body += " Pick 1 for apology-first responses, or 2 for escalation checklist."
        else:
            body += " Pick 1 for response templates, or 2 for a highlight post."
        body += f" Recent account state: {history_line}."
        lever = persuasion_line(kind, merchant, trigger)
        if lever:
            body += f" {lever}"
        body += " Reply 1 or 2."
        cta_text = "Reply 1 or 2."
        template_params = [salutation, truncate(body, 120), cta_text]

    elif kind in {"profile_incomplete", "profile_hours_missing", "profile_attributes_missing", "seo_visibility_gap"}:
        missing_fields = trigger_payload(trigger).get("missing_fields") or []
        if isinstance(missing_fields, list) and missing_fields:
            missing_text = ", ".join(str(x) for x in missing_fields[:4])
        else:
            missing_text = "key GBP fields"
        body = f"{salutation}, your Google profile can improve discoverability with quick fixes."
        body += f" Missing or weak: {missing_text}."
        ctr = merchant_ctr(merchant)
        peer_ctr = category_peer_ctr(category)
        if ctr is not None and peer_ctr is not None:
            body += f" Current CTR {ctr:.1%} vs peer {peer_ctr:.1%}."
        lever = persuasion_line(kind, merchant, trigger)
        if lever:
            body += f" {lever}"
        body += " Pick 1 for top-3 quick fixes, or 2 for full checklist."
        body += " Reply 1 or 2."
        cta_text = "Reply 1 or 2."
        template_params = [salutation, truncate(body, 120), cta_text]

    elif kind in {"lead_missed_searches", "lead_followup_due"}:
        missed = trigger_payload(trigger).get("missed_searches") or trigger_payload(trigger).get("missed_leads")
        body = f"{salutation}, there is a lead-capture opportunity on your listing."
        if missed is not None:
            body += f" Estimated missed searches/leads: {missed}."
        if offer:
            body += f" I can route traffic to {offer.get('title')}."
        lever = persuasion_line(kind, merchant, trigger)
        if lever:
            body += f" {lever}"
        body += " Pick 1 for quick 3-step patch, or 2 for full 7-day plan."
        body += " Reply 1 or 2."
        cta_text = "Reply 1 or 2."
        template_params = [salutation, truncate(body, 120), cta_text]

    elif kind in {"photo_gap_detected", "content_pack_ready"}:
        missing_photos = trigger_payload(trigger).get("missing_photos")
        body = f"{salutation}, your profile can benefit from a fresh content pack."
        if missing_photos is not None:
            body += f" Photo gap detected: {missing_photos} slot(s)."
        if signal_line:
            body += f" {signal_line}."
        body += " Pick 1 for 5-shot list, or 2 for full content pack with captions."
        body += " Reply 1 or 2."
        cta_text = "Reply 1 or 2."
        template_params = [salutation, truncate(body, 120), cta_text]

    elif kind == "active_planning_intent":
        topic = trigger_payload(trigger).get("intent_topic") or "your plan"
        merchant_last_message = trigger_payload(trigger).get("merchant_last_message", "")
        body = f"{salutation}, here’s a starter version for {topic}."
        if merchant_last_message:
            body += f" You said: “{truncate(merchant_last_message, 80)}”."
        if offer:
            body += f" I’ve anchored it to {offer.get('title')}."
        body += " Pick 1 for short version, or 2 for detailed version."
        body += " Reply 1 or 2 and I’ll send it ready-to-use."
        cta_text = "Reply 1 or 2."
        template_params = [salutation, truncate(body, 120), cta_text]

    elif kind == "milestone_reached":
        metric = trigger_payload(trigger).get("metric") or "a milestone"
        value_now = trigger_payload(trigger).get("value_now")
        milestone_value = trigger_payload(trigger).get("milestone_value")
        body = f"{salutation}, nice milestone on {metric}"
        if value_now is not None and milestone_value is not None:
            body += f" — {value_now} toward {milestone_value}."
        body += " Want me to turn this into a celebratory post?"
        cta_text = "Want the celebratory post?"
        template_params = [salutation, truncate(body, 120), cta_text]

    else:
        body = f"{salutation}, quick update from Vera."
        if ctr is not None and peer_ctr is not None:
            body += f" Your CTR is {ctr:.1%} vs peer {peer_ctr:.1%}."
        if offer:
            body += f" Your active offer is {offer.get('title')}."
        body += " Want me to draft the next step?"
        cta_text = "Want me to draft the next step?"
        template_params = [salutation, truncate(body, 120), cta_text]

    specificity_line = trigger_specificity_line(kind, merchant, category, trigger)
    if specificity_line and specificity_line not in body:
        body += f" {specificity_line}"
    if customer is None:
        action_line = trigger_action_line(kind, merchant, trigger)
        if action_line and action_line not in body:
            body += f" {action_line}"

    if language_mix and customer is None and category_slug in {"salons", "restaurants", "gyms", "pharmacies"}:
        body = hinglish_phrase(body, True)

    body = enforce_first_touch_quality(body, kind, scope, merchant, category, trigger)
    info_only = kind in {"milestone_reached"}
    return ComposedMessage(
        body=body,
        cta=cta_for_kind(kind, info_only=info_only),
        send_as=send_as,
        suppression_key=sup_key,
        rationale=build_rationale(kind, send_as, f"{category_tone(category_slug)} / {merchant_locality(merchant) or merchant_city(merchant) or 'local'}", cta_for_kind(kind, info_only=info_only)),
        template_name=template_name,
        template_params=template_params,
    )


def compose_follow_up(
    conversation: dict[str, Any],
    merchant: dict[str, Any],
    trigger: dict[str, Any] | None,
    customer: dict[str, Any] | None,
    merchant_message: str,
) -> dict[str, Any]:
    category_slug = merchant.get("category_slug", safe_get(trigger or {}, "payload", "category", default="generic"))
    salutation = merchant_salutation(merchant, category_slug)
    kind = trigger_kind(trigger or {})
    language_mix = preferred_language_mix(merchant, customer)
    convo_id = conversation.get("conversation_id", "conversation")

    merchant_turns = [
        m for m in conversation.get("messages", [])
        if m.get("from") == "merchant" and isinstance(m.get("body"), str)
    ]
    same_message_count = sum(1 for m in merchant_turns if normalize(m.get("body", "")) == normalize(merchant_message))

    if is_auto_reply(merchant_message):
        count = conversation.get("auto_reply_count", 0) + 1
        conversation["auto_reply_count"] = count
        if count >= 3:
            conversation["status"] = "ended"
            return {
                "action": "end",
                "rationale": "Detected repeated merchant auto-reply pattern; backing off to avoid wasting turns.",
            }
        return {
            "action": "wait",
            "wait_seconds": 14400,
            "rationale": "Detected merchant auto-reply pattern; backing off to wait for a real owner response.",
        }

    conversation["auto_reply_count"] = 0

    if is_hard_no(merchant_message):
        conversation["status"] = "ended"
        return {
            "action": "end",
            "rationale": "Merchant explicitly declined or asked to stop; conversation closed.",
        }

    if is_abusive(merchant_message):
        conversation["status"] = "ended"
        return {
            "action": "send",
            "body": "I hear your frustration. I’ll stop here and won’t continue this thread.",
            "cta": "none",
            "rationale": "Detected abusive language; de-escalated and closed the thread.",
        }

    if same_message_count >= 4:
        conversation["status"] = "ended"
        return {
            "action": "end",
            "rationale": "Repeated low-signal merchant responses detected; ending to avoid spammy looping.",
        }
    if same_message_count >= 2 and not is_commitment(merchant_message):
        return {
            "action": "wait",
            "wait_seconds": 10800,
            "rationale": "Repeated same reply detected; backing off before next follow-up.",
        }

    if is_delay_request(merchant_message):
        return {
            "action": "wait",
            "wait_seconds": 7200,
            "rationale": "Merchant asked for more time; backing off before trying again.",
        }

    if is_out_of_scope(merchant_message):
        body = f"I’ll leave GST filing to your CA. Coming back to {kind or 'this thread'} — want me to keep the draft short and practical?"
        if language_mix:
            body = "GST wali help main directly nahi kar paunga, but original thread par wapas aate hain — draft short rakhun?"
        conversation["last_bot_body"] = body
        return {
            "action": "send",
            "body": body,
            "cta": "open_ended",
            "rationale": "Merchant asked for an out-of-scope task; politely declined and redirected to the original conversation.",
        }

    if is_ambiguous(merchant_message):
        offer = active_offer(merchant)
        suggestion = offer.get("title") if offer else "a short action draft"
        body = f"{salutation}, quick one: should I send (1) the short summary or (2) the ready-to-send draft for {suggestion}?"
        if language_mix:
            body = f"{salutation}, quick check: (1) short summary bheju ya (2) ready draft bheju for {suggestion}?"
        return {
            "action": "send",
            "body": body,
            "cta": "open_ended",
            "rationale": "Merchant reply was ambiguous; narrowed choice to two clear options.",
        }

    if is_commitment(merchant_message):
        if kind in {"research_digest", "research_digest_release", "category_research_digest_release"}:
            item = category_digest_item(STATE.get_category(category_slug) or {}, trigger or {}) if trigger else None
            source = item.get("source") if item else None
            summary = item.get("summary") or item.get("actionable") or "" if item else ""
            body = "Sending the abstract now."
            if source:
                body += f" {source}."
            if summary:
                body += f" Short version: {truncate(summary, 140)}"
            if language_mix:
                body += " Aur main patient-ed WhatsApp draft bhi kar sakta hoon."
            else:
                body += " I can also draft the patient-ed WhatsApp if you want."
            return {
                "action": "send",
                "body": body,
                "cta": "binary_yes_no",
                "rationale": "Merchant committed; switching immediately from pitch mode to action mode with a concrete follow-on.",
            }

        if kind in {"recall_due", "customer_lapsed_soft", "appointment_tomorrow", "trial_followup", "chronic_refill_due"} and customer:
            cust_name = customer_salutation(customer)
            slots = trigger_payload(trigger or {}).get("available_slots") or trigger_payload(trigger or {}).get("next_session_options") or []
            slot_labels = [s.get("label") or format_time_label(s.get("iso")) for s in slots if isinstance(s, dict)]
            offer = active_offer(merchant)
            body = f"Done — here’s the ready-to-send note for {cust_name}:"
            if slot_labels:
                body += f" {slot_labels[0]}"
                if len(slot_labels) > 1:
                    body += f" or {slot_labels[1]}"
            if offer:
                body += f". {offer.get('title')}."
            body += " If you want, I can trim it further for WhatsApp."
            return {
                "action": "send",
                "body": body,
                "cta": "binary_yes_no",
                "rationale": "Merchant accepted the reminder flow; providing a concrete customer-facing draft and next step.",
            }

        if kind == "active_planning_intent":
            topic = trigger_payload(trigger or {}).get("intent_topic", "your plan")
            offer = active_offer(merchant)
            body = f"Done — here’s a draft for {topic}."
            if offer:
                body += f" I anchored it to {offer.get('title')}."
            body += " Want me to make it shorter and more WhatsApp-friendly?"
            return {
                "action": "send",
                "body": body,
                "cta": "binary_yes_no",
                "rationale": "Merchant explicitly asked to proceed; delivering the draft instead of asking another qualifying question.",
            }

        if kind in {"renewal_due"}:
            days = merchant_days_remaining(merchant)
            body = f"Done — I’ve prepared the renewal reminder."
            if days is not None:
                body += f" With {days} day(s) left, this is a good time to send it."
            return {
                "action": "send",
                "body": body,
                "cta": "binary_yes_no",
                "rationale": "Merchant confirmed renewal handling; moving straight to the draft/next step.",
            }

        body = f"Done — I’ll take that forward now."
        return {
            "action": "send",
            "body": body,
            "cta": "open_ended",
            "rationale": "Merchant gave clear go-ahead; advancing the conversation without re-qualifying.",
        }

    if "abstract" in normalize(merchant_message) or "send me" in normalize(merchant_message) or "draft" in normalize(merchant_message):
        item = category_digest_item(STATE.get_category(category_slug) or {}, trigger or {}) if trigger else None
        title = item.get("title") if item else "the item"
        source = item.get("source") if item else None
        summary = item.get("summary") or item.get("actionable") or "" if item else ""
        body = f"Absolutely — here’s the short version of {title}."
        if source:
            body += f" Source: {source}."
        if summary:
            body += f" {truncate(summary, 160)}"
        if customer:
            body += " I can turn this into a customer note too."
        else:
            body += " I can also draft the patient-ed WhatsApp if you want."
        return {
            "action": "send",
            "body": body,
            "cta": "binary_yes_no",
            "rationale": "Merchant asked for the concrete asset directly; answering with the payload and one low-friction follow-up.",
        }

    if has_any(merchant_message, ["help", "show", "what is", "how does", "explain", "details", "more info"]):
        offer = active_offer(merchant)
        body = f"Sure — I’m looking at {merchant.get('identity', {}).get('name', 'your business')}."
        if offer:
            body += f" Your active offer is {offer.get('title')}."
        ctr = merchant_ctr(merchant)
        peer_ctr = category_peer_ctr(STATE.get_category(category_slug) or {})
        if ctr is not None and peer_ctr is not None:
            body += f" CTR is {ctr:.1%} vs peer {peer_ctr:.1%}."
        body += " Want me to keep going with the highest-impact next step?"
        if language_mix:
            body = body.replace("Want me to", "Chahiye to main")
        return {
            "action": "send",
            "body": body,
            "cta": "open_ended",
            "rationale": "Merchant is asking for clarity; answering briefly and steering back to the highest-value next step.",
        }

    # Default reply: keep it short, helpful, and on mission.
    body = f"{salutation}, I can keep this practical."
    if kind in {"research_digest", "research_digest_release"}:
        body += " Want the 2-minute abstract or the patient draft?"
    elif kind in {"recall_due", "appointment_tomorrow"} and customer:
        body += f" Want me to send the note to {customer_salutation(customer)} now?"
    else:
        body += " Want the draft or the summary first?"
    if language_mix:
        body = body.replace("Want", "Chahiye")
    return {
        "action": "send",
        "body": body,
        "cta": "open_ended",
        "rationale": "General merchant reply; responding concisely and preserving the original thread.",
    }


def choose_triggers(active_trigger_ids: list[str]) -> list[dict[str, Any]]:
    triggers = []
    for trigger_id in active_trigger_ids:
        trigger = STATE.get_trigger(trigger_id)
        if trigger:
            triggers.append(trigger)
    triggers.sort(key=lambda t: (-int(t.get("urgency", 0) or 0), t.get("expires_at", "")))
    return triggers[:20]


def merchant_and_category_for_trigger(trigger: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    merchant_id = trigger.get("merchant_id")
    merchant = STATE.get_merchant(merchant_id) if merchant_id else None
    if not merchant:
        return None, None
    category_slug = merchant.get("category_slug") or trigger_payload(trigger).get("category") or merchant.get("identity", {}).get("category_slug")
    category = STATE.get_category(category_slug) if category_slug else None
    if not category:
        # Fallback to any category listed in the trigger payload.
        category = STATE.get_category(trigger_payload(trigger).get("category")) if trigger_payload(trigger).get("category") else None
    return merchant, category


def build_tick_actions(now: str, active_trigger_ids: list[str]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for trigger in choose_triggers(active_trigger_ids):
        merchant, category = merchant_and_category_for_trigger(trigger)
        if not merchant or not category:
            continue

        customer = None
        customer_id = trigger.get("customer_id")
        if customer_id:
            customer = STATE.get_customer(customer_id)

        suppression_key = trigger.get("suppression_key") or f"{trigger_kind(trigger)}:{merchant.get('merchant_id')}"
        if STATE.suppress(suppression_key):
            continue

        conversation_id = f"conv_{slugify(merchant.get('merchant_id', 'merchant'))}_{slugify(trigger.get('id', 'trigger'))}"
        STATE.record_conversation(conversation_id, merchant.get("merchant_id", "merchant"), customer_id, trigger.get("id"), "merchant_on_behalf" if customer else "vera")
        composed = build_first_touch(category, merchant, trigger, customer)
        composed.suppression_key = suppression_key
        action = composed.to_action(conversation_id, merchant.get("merchant_id", "merchant"), customer_id, trigger.get("id"))
        actions.append(action)
        STATE.append_bot_message(conversation_id, composed.body)
        if len(actions) >= 20:
            break
    return actions


class VeraRequestHandler(BaseHTTPRequestHandler):
    server_version = "VeraBot/1.0"
    _rate_lock = threading.Lock()
    _rate_buckets: dict[str, dict[str, float]] = {}

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        log_event(
            "http_response",
            method=self.command,
            path=urlparse(self.path).path,
            status=status,
            client_ip=self._client_ip(),
            payload_keys=sorted(list(payload.keys())),
        )

    def _read_json(self) -> tuple[dict[str, Any] | None, str | None]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                return None, "JSON body must be an object"
            return data, None
        except Exception as e:
            return None, str(e)

    def _client_ip(self) -> str:
        if self.client_address and len(self.client_address) > 0:
            return str(self.client_address[0])
        return "unknown"

    def _rate_limited(self) -> bool:
        now = time.monotonic()
        client = self._client_ip()
        with VeraRequestHandler._rate_lock:
            bucket = VeraRequestHandler._rate_buckets.get(client)
            if not bucket:
                VeraRequestHandler._rate_buckets[client] = {
                    "tokens": float(RATE_LIMIT_BURST - 1),
                    "last": now,
                }
                return False
            elapsed = max(0.0, now - bucket["last"])
            refill = elapsed * RATE_LIMIT_RPS / RATE_LIMIT_WINDOW_SEC
            bucket["tokens"] = min(float(RATE_LIMIT_BURST), bucket["tokens"] + refill)
            bucket["last"] = now
            if bucket["tokens"] < 1.0:
                return True
            bucket["tokens"] -= 1.0
            return False

    def _not_found(self) -> None:
        self._send_json(404, {"error": "not_found"})

    def do_GET(self) -> None:  # noqa: N802
        if self._rate_limited():
            STATE.incr("rate_limited_total")
            self._send_json(429, {"error": "rate_limited", "details": "too_many_requests"})
            return
        path = urlparse(self.path).path
        if path == "/v1/healthz":
            uptime = int((utc_now() - STATE.started_at).total_seconds())
            self._send_json(200, {
                "status": "ok",
                "uptime_seconds": uptime,
                "contexts_loaded": STATE.context_counts(),
            })
            return
        if path == "/v1/readiness":
            # Readiness is separate from liveness: bot is ready only when core
            # baseline contexts are available.
            counts = STATE.context_counts()
            ready = counts["category"] > 0 and counts["merchant"] > 0
            self._send_json(200, {
                "status": "ready" if ready else "warming",
                "ready": ready,
                "contexts_loaded": counts,
            })
            return
        if path == "/v1/metadata":
            self._send_json(200, {
                "team_name": TEAM_NAME,
                "team_members": TEAM_MEMBERS,
                "model": MODEL_NAME,
                "approach": APPROACH,
                "contact_email": CONTACT_EMAIL,
                "version": VERSION,
                "submitted_at": iso_now(),
            })
            return
        if path == "/v1/metrics":
            self._send_json(200, {
                "metrics": STATE.metrics_snapshot(),
                "contexts_loaded": STATE.context_counts(),
            })
            return
        self._not_found()

    def do_POST(self) -> None:  # noqa: N802
        if self._rate_limited():
            STATE.incr("rate_limited_total")
            self._send_json(429, {"error": "rate_limited", "details": "too_many_requests"})
            return
        path = urlparse(self.path).path
        data, err = self._read_json()
        if err is not None or data is None:
            self._send_json(400, {"error": "invalid_json", "details": err})
            return

        if path == "/v1/context":
            scope = data.get("scope")
            context_id = data.get("context_id")
            version = data.get("version")
            payload = data.get("payload")
            delivered_at = data.get("delivered_at", iso_now())

            if not isinstance(scope, str) or not isinstance(context_id, str) or not isinstance(version, int) or not isinstance(payload, dict):
                self._send_json(400, {"accepted": False, "reason": "invalid_payload", "details": "scope/context_id/version/payload are required"})
                return

            accepted, response = STATE.store_context(scope, context_id, version, payload, delivered_at)
            status = 200 if accepted else (409 if response.get("reason") == "stale_version" else 400)
            self._send_json(status, response)
            return

        if path == "/v1/tick":
            STATE.incr("tick_total")
            now = data.get("now", iso_now())
            active_trigger_ids = data.get("available_triggers", [])
            if not isinstance(active_trigger_ids, list):
                self._send_json(400, {"error": "invalid_payload", "details": "available_triggers must be a list"})
                return
            trigger_ids = [t for t in active_trigger_ids if isinstance(t, str)]
            actions = build_tick_actions(now, trigger_ids)
            if actions:
                STATE.incr("actions_sent_total", len(actions))
            self._send_json(200, {"actions": actions})
            return

        if path == "/v1/reply":
            STATE.incr("reply_total")
            conversation_id = data.get("conversation_id")
            merchant_id = data.get("merchant_id")
            customer_id = data.get("customer_id")
            from_role = data.get("from_role")
            message = data.get("message")
            received_at = data.get("received_at", iso_now())
            turn_number = data.get("turn_number")

            if not isinstance(conversation_id, str) or not isinstance(merchant_id, str) or not isinstance(from_role, str) or not isinstance(message, str) or not isinstance(turn_number, int):
                self._send_json(400, {"error": "invalid_payload", "details": "conversation_id, merchant_id, from_role, message, turn_number are required"})
                return

            merchant = STATE.get_merchant(merchant_id) or {}
            trigger_id = None
            convo = STATE.conversation(conversation_id)
            if convo:
                trigger_id = convo.get("trigger_id")

            customer = STATE.get_customer(customer_id) if isinstance(customer_id, str) else None
            trigger = STATE.get_trigger(trigger_id) if trigger_id else None

            if is_auto_reply(message):
                STATE.incr("auto_reply_detected_total")
            if is_hard_no(message):
                STATE.incr("opt_out_total")

            STATE.append_merchant_message(conversation_id, message, received_at, from_role=from_role)
            convo = STATE.conversation(conversation_id) or {}
            if convo.get("status") == "ended":
                self._send_json(200, {"action": "end", "rationale": "Conversation already ended."})
                return

            if merchant and trigger:
                result = compose_follow_up(convo, merchant, trigger, customer, message)
            else:
                # Minimal safe fallback when conversation metadata is missing.
                if is_auto_reply(message):
                    result = {
                        "action": "wait",
                        "wait_seconds": 14400,
                        "rationale": "Detected canned auto-reply; backing off.",
                    }
                elif is_hard_no(message):
                    result = {
                        "action": "end",
                        "rationale": "Merchant explicitly asked to stop.",
                    }
                elif is_commitment(message):
                    result = {
                        "action": "send",
                        "body": "Done — I’ll take that forward now.",
                        "cta": "open_ended",
                        "rationale": "Merchant gave a clear go-ahead; advancing the conversation.",
                    }
                else:
                    result = {
                        "action": "send",
                        "body": "Got it — want the short draft or the summary first?",
                        "cta": "open_ended",
                        "rationale": "Fallback response keeps the thread moving while staying concise.",
                    }

            if result.get("action") == "send":
                STATE.incr("reply_action_send_total")
                STATE.append_bot_message(conversation_id, result.get("body", ""))
            if result.get("action") == "end":
                STATE.incr("reply_action_end_total")
                STATE.mark_status(conversation_id, "ended")
            if result.get("action") == "wait":
                STATE.incr("reply_action_wait_total")

            self._send_json(200, result)
            return

        if path == "/v1/teardown":
            STATE.clear()
            self._send_json(200, {"ok": True})
            return

        self._not_found()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # Keep the server quiet; the judge only cares about JSON responses.
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), VeraRequestHandler)
    log_event(
        "server_start",
        host=HOST,
        port=PORT,
        db_path=DB_PATH or None,
        rate_limit_rps=RATE_LIMIT_RPS,
        rate_limit_burst=RATE_LIMIT_BURST,
    )

    def _shutdown_handler(signum: int, _frame: Any) -> None:
        log_event("shutdown_signal", signal=signum)
        try:
            server.shutdown()
        except Exception as e:
            log_event("shutdown_error", error=str(e))

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_event("keyboard_interrupt")
    finally:
        STATE._save_to_db()
        server.server_close()
        log_event("server_stop")


if __name__ == "__main__":
    main()
