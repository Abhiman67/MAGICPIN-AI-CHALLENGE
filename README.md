# magicpin AI Challenge Bot

This repository now contains a standalone implementation of the Vera challenge bot.

## What it does

- Exposes the required HTTP endpoints:
  - `GET /v1/healthz`
  - `GET /v1/readiness`
  - `GET /v1/metadata`
  - `GET /v1/metrics`
  - `POST /v1/context`
  - `POST /v1/tick`
  - `POST /v1/reply`
  - optional `POST /v1/teardown`
- Stores category, merchant, customer, and trigger contexts with versioning and optional SQLite persistence
- Composes outbound messages from the 4-context model
- Handles multi-turn merchant replies
- Adds request rate limiting and structured request logs
- Detects:
  - canned auto-replies
  - explicit intent transitions
  - hostile / opt-out replies
  - out-of-scope questions

## Design approach

The bot is deterministic and dependency-free. Instead of relying on an LLM at runtime, it uses a trigger router plus category-aware templates to produce concise, specific WhatsApp-ready messages.

That keeps the service:

- fast
- reproducible
- easy to run locally
- safer for the judge time budget

The message composer uses:

- category voice
- merchant performance
- active offers
- trigger payloads
- customer context when present

## Run locally

```bash
python3 bot.py
```

By default it listens on `http://0.0.0.0:8080`.

You can override settings with environment variables:

- `BOT_HOST`
- `BOT_PORT`
- `BOT_DB_PATH` (optional SQLite file path for persistence across restarts)
- `BOT_RATE_LIMIT_RPS` (default `25`)
- `BOT_RATE_LIMIT_BURST` (default `100`)
- `TEAM_NAME`
- `TEAM_MEMBERS`
- `BOT_MODEL`
- `BOT_APPROACH`
- `CONTACT_EMAIL`
- `BOT_VERSION`

## Run with Docker

```bash
docker build -t vera-bot .
docker run --rm -p 8080:8080 vera-bot
```

## Test it

Use contract tests, the judge simulator, or hit endpoints directly.

```bash
./scripts/run_local_checks.sh
./scripts/run_p0.sh
./scripts/run_score_loop.sh
```

Example:

```bash
curl http://localhost:8080/v1/healthz
curl http://localhost:8080/v1/readiness
curl http://localhost:8080/v1/metadata
curl http://localhost:8080/v1/metrics
```

## Notes

- Context versioning is enforced.
- Repeated or stale versions are rejected.
- Conversation and context state stay in memory by default.
- If `BOT_DB_PATH` is set, state is persisted and restored on restart.
- `POST /v1/teardown` clears all stored state if the judge calls it.
- `GET /v1/metrics` exposes counters for actions, replies, suppression hits, and rate-limited requests.

## Score optimization workflow

Use the score loop runner for deterministic evaluation artifacts:

```bash
export JUDGE_BOT_URL='https://<your-bot-url>'
export JUDGE_LLM_PROVIDER='openrouter'
export JUDGE_LLM_API_KEY='<your-key>'
export JUDGE_LLM_MODEL='openai/gpt-4o-mini'
./scripts/run_score_loop.sh
```

What it does:

- Calls `/v1/teardown` and verifies clean readiness state before scoring
- Runs `full_evaluation`
- Saves timestamped report to `reports/run_<timestamp>.txt`
- Validates report completeness (fails on incomplete runs, e.g. `0 actions`)
- Writes structured summary metrics JSON to `reports/run_<timestamp>.json`

## What’s covered from the challenge spec

- Merchant-facing messaging
- Customer-facing messaging
- Research digests
- Recall reminders
- Performance dips / spikes
- Renewal nudges
- Curious-ask cadence
- Review themes
- Competitor openings
- Festivals / events
- Intent transition handling
- Auto-reply detection
- Graceful exit on opt-out
