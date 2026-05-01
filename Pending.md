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

- Re-run `full_evaluation` and capture updated score delta after this pass

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
  - track per-dimension deltas vs baseline `34/50`

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
