# Pending Work (Prioritized)

## Priority Legend

- `P0` = should do before final submission run for best confidence
- `P1` = improves score quality and robustness
- `P2` = broader Vera product scope beyond strict judge contract

---

## Recommended Execution Order

1. Finish `P0` external submission and dry-run confidence checks
2. Decide whether to add optional intelligence upgrades (LLM/retrieval)
3. Expand into `P2` product workflows where integrations are available

---

## P0 — Submission Confidence

P0 is complete:

- Public deployment URL validation done (`healthz`/`metadata`/`readiness` behavior checked)
- Real-provider judge run done (OpenRouter) for:
  - `all` scenario
  - `full_evaluation` with saved report artifact (`judge-report.txt`)

P0 local checks are completed via `./scripts/run_p0.sh`:

- compile + integration suite
- warmup/scenario/full-like lifecycle harness
- Docker smoke path (auto-skips where Docker is unavailable)
- Offline official-judge flow also completed via `./scripts/run_judge_offline.sh` using `mock` judge provider (covers `all` + `full_evaluation`)

---

## P1 — Completed

- Category-specific copy upgrades done
- Trigger-level persuasion levers added
- CTA policy tightened by action type
- Personalization depth improved using history/signals/offers/customer state
- Ambiguity resolution and abuse de-escalation added
- Multilingual (Hinglish) quality improved
- Trigger-level specificity/actionability pass added:
  - numeric evidence lines per trigger family
  - concrete reply-keyword next steps (`DRAFT`/`PLAN`/`CHECKLIST`/`PACK`/`SEND`/`GO`)

Remaining quality step:

- Re-run `full_evaluation` and capture updated score delta after each perf/profile patch

Active optimization plan (in progress):

- Implement deterministic quality guard in first-touch composition:
  - require at least one hard specificity anchor (metric/count/comparison)
  - require one explicit micro-commitment action for action triggers
  - normalize CTA phrasing away from vague asks
- Apply targeted copy upgrades to lowest-scoring families:
  - `active_planning_intent`, `profile_*`, `review_*`, `lead_*`, `perf_dip`
- Validation loop:
  - run tests
  - redeploy
  - run `full_evaluation`
  - track per-dimension deltas vs stable baseline band `34-35/50 (68-70%)`
- Completed foundation for rapid loops:
  - `scripts/run_score_loop.sh` clean-state + timestamped report pipeline
  - `scripts/judge_report_tools.py` report validator + structured score diagnostics
  - quality-policy unit tests in `tests/test_quality_policy.py`

Current priority within optimization:

- Lift `profile_perf` family (current lowest)
- Remove remaining weak low-fit perf/profile variants while keeping natural merchant tone
- Raise decision quality and engagement while preserving merchant/category fit

Current score state (for decision making):

- Best recent: `35/50 (70%)`
- Stable operating band: `34-35/50 (68-70%)`
- Latest low outlier: `30/50 (60%)` from `reports/run_20260503_185506.json`
- Outlier metrics:
  - specificity `6.52`
  - category fit `7.92`
  - merchant fit `7.24`
  - decision quality `5.72`
  - engagement `5.68`
  - bottom-5 totals `22, 24, 27, 27, 28`

Execution plan to push toward 80:

1. Recovery and stability:
- Re-establish `>=34` after latest recovery patch deploy
- Run 3 consecutive loops and optimize against average, not a single run

2. Precision optimization:
- Patch only bottom-5 messages each loop
- Prioritize rows with engagement `<=5` and decision quality `<=5` first
- Keep one clear CTA + one concrete payoff + one metric anchor

3. Milestone progression:
- Milestone A: stabilize `>=36/50 (72%)`
- Milestone B: stabilize `>=38/50 (76%)`
- Milestone C: stretch to `>=40/50 (80%)`

---

## P2 — Product Expansion Beyond Judge Contract

- Review management:
  - connect response drafts to real review payload sources
  - add explicit escalation policy and merchant approval path
- Profile optimization:
  - connect checklist output to real profile update execution flow
- Lead generation:
  - add measurable conversion tracking loop
- Photo/content enhancement:
  - connect content suggestions to concrete media-generation/edit pipelines

---

## Explicitly Done Already (Not Pending)

- Required challenge endpoints implemented
- Stateful context + conversation tracking implemented
- Optional SQLite persistence implemented
- Rate limiting implemented
- Structured logs + metrics endpoint implemented
- Docker packaging added
- HTTP contract integration tests added and passing
- Site-aligned workflow trigger families implemented (reviews/profile/leads/photos)
- Repeated low-signal reply backoff/termination implemented
- P1 score-and-quality optimization pass completed
- P0 local readiness automation completed
