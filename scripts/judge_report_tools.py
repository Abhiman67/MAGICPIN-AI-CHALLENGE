#!/usr/bin/env python3
"""Validate and summarize judge report outputs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def extract(lines: list[str]) -> dict[str, Any]:
    row_re = re.compile(r"\[INFO\]\s+Batch\s+(\d+):\s+(\d+)\s+actions")
    msg_re = re.compile(r'Message:\s+"(.*)"')
    score_re = {
        "specificity": re.compile(r"Specificity.*?([0-9]+)/10"),
        "category_fit": re.compile(r"Category Fit.*?([0-9]+)/10"),
        "merchant_fit": re.compile(r"Merchant Fit.*?([0-9]+)/10"),
        "decision_quality": re.compile(r"Decision Quality.*?([0-9]+)/10"),
        "engagement": re.compile(r"Engagement.*?([0-9]+)/10"),
    }
    total_re = re.compile(r"TOTAL:\s*([0-9]+)/50")
    avg_re = re.compile(r"AVERAGE SCORE:\s*([0-9]+)/50\s+\(([0-9]+)%\)")

    batches: list[dict[str, int]] = []
    messages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in lines:
        m = row_re.search(line)
        if m:
            batches.append({"batch": int(m.group(1)), "actions": int(m.group(2))})
            continue
        m = msg_re.search(line)
        if m:
            current = {"message": m.group(1)}
            continue
        if current is not None:
            for key, rx in score_re.items():
                s = rx.search(line)
                if s:
                    current[key] = int(s.group(1))
            t = total_re.search(line)
            if t:
                current["total"] = int(t.group(1))
                if all(k in current for k in ["specificity", "category_fit", "merchant_fit", "decision_quality", "engagement"]):
                    messages.append(current)
                current = None

    avg_score = None
    avg_percent = None
    for line in lines:
        m = avg_re.search(line)
        if m:
            avg_score = int(m.group(1))
            avg_percent = int(m.group(2))
            break

    metric_avgs: dict[str, float] = {}
    if messages:
        for key in ["specificity", "category_fit", "merchant_fit", "decision_quality", "engagement", "total"]:
            metric_avgs[key] = round(mean([m[key] for m in messages]), 2)

    return {
        "batches": batches,
        "messages": messages,
        "average_score": avg_score,
        "average_percent": avg_percent,
        "metric_averages": metric_avgs,
    }


def family_for_message(message: str) -> str:
    text = message.lower()
    if "review" in text:
        return "review"
    if "profile" in text or "ctr is" in text:
        return "profile_perf"
    if "lead" in text or "missed" in text:
        return "lead"
    if "draft" in text or "starter version" in text or "plan" in text:
        return "planning"
    if "offer" in text or "festival" in text or "match" in text:
        return "campaign"
    return "other"


def validate(parsed: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    batches = parsed["batches"]
    if not batches:
        errors.append("No batch lines found in report.")
    if any(b["actions"] == 0 for b in batches):
        errors.append("One or more batches had 0 actions.")
    if parsed["average_score"] is None:
        errors.append("Final average score line missing.")
    if not parsed["messages"]:
        errors.append("No scored messages parsed.")
    return errors


def summary(parsed: dict[str, Any]) -> dict[str, Any]:
    rows = parsed["messages"]
    family_scores: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        family_scores[family_for_message(row["message"])].append(row["total"])
    family_avg = {k: round(mean(v), 2) for k, v in family_scores.items() if v}
    bottom = sorted(rows, key=lambda r: r["total"])[:5]
    return {
        "average_score": parsed["average_score"],
        "average_percent": parsed["average_percent"],
        "metric_averages": parsed["metric_averages"],
        "family_average_total": dict(sorted(family_avg.items(), key=lambda x: x[1])),
        "bottom_5_messages": [
            {
                "total": r["total"],
                "specificity": r["specificity"],
                "decision_quality": r["decision_quality"],
                "engagement": r["engagement"],
                "message": r["message"],
            }
            for r in bottom
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, help="Path to judge report text file.")
    parser.add_argument("--out-json", default="", help="Optional output JSON summary path.")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    report_path = Path(args.report)
    raw = report_path.read_text(encoding="utf-8", errors="ignore")
    lines = strip_ansi(raw).splitlines()
    parsed = extract(lines)
    errors = validate(parsed)

    if errors:
        for err in errors:
            print(f"[FAIL] {err}")
        return 1

    data = summary(parsed)
    print(f"[PASS] Report valid. Score: {data['average_score']}/50 ({data['average_percent']}%)")
    print("[INFO] Metric averages:", json.dumps(data["metric_averages"], ensure_ascii=True))
    print("[INFO] Family averages:", json.dumps(data["family_average_total"], ensure_ascii=True))
    print("[INFO] Bottom 5 totals:", ", ".join(str(x["total"]) for x in data["bottom_5_messages"]))

    if args.out_json:
        out = Path(args.out_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        print(f"[INFO] Wrote summary: {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

