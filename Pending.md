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

- Deploy to a public URL and perform one full end-to-end external reachability check
- Run `judge_simulator.py` with configured real LLM provider/API key and collect score report artifacts

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
