# BracketWise — Reviews Worth Reading

> An AI-powered autonomous agent that reviews GitHub pull requests like a thoughtful senior engineer.  

---

## What it does

BracketWise listens for GitHub PR events via webhook, fetches the diff and full file content, runs each changed file through an LLM with a strict prompt, and posts a structured code review — inline comments on specific lines, plus a summary — all in a single GitHub Review API call.

It flags **security issues, logic bugs, missing error handling, and resource leaks**. It ignores naming conventions, docstrings, and formatting. If it's not a real bug, it stays quiet.

---

## Features

- **Autonomous webhook-driven pipeline** — opens → reviews → posts, zero manual steps
- **Diff-aware LLM prompting** — feeds the agent only reviewable lines + surrounding context
- **Inline GitHub comments** — maps LLM line numbers to exact diff positions
- **Severity-labeled callouts** — GitHub renders `[!CAUTION]` / `[!WARNING]` natively
- **Idempotent** — same commit SHA never gets reviewed twice
- **Full DB audit trail** — every review and comment stored in SQLite & Postgres

---

## Architecture

```
GitHub PR opened/updated
        │
        ▼
   FastAPI Webhook  ──── signature verification (HMAC-SHA256)
        │
        ▼
  Background Task
        │
        ├──▶  GitHub Client  ──── fetch diff + full file content
        │
        ├──▶  Diff Parser  ──── parse hunks → DiffLine objects (positions + line numbers)
        │
        ├──▶  LLM Reviewer (Groq)  ──── prompt per file → raw JSON comments
        │
        ├──▶  Validator  ──── check line numbers, deduplicate, enforce schema
        │
        ├──▶  DB Storage  ──── save review + comments (SQLAlchemy)
        │
        └──▶  GitHub Poster  ──── POST /pulls/{pr}/reviews  (one API call)
```

---

## Tech Stack

| Layer | Tech |
|-------|------|
| API server | FastAPI |
| LLM | Groq (`llama-3.3-70b-versatile`) |
| GitHub integration | PyGithub + GitHub Apps (JWT auth) |
| Database | SQLAlchemy + SQLite (dev) / Postgres (prod) |
| Deployment |  |
| Testing | pytest + unittest.mock |

---

## Quick Setup

```bash
git clone https://github.com/yash-ladda/BracketWise
cd BracketWise
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file:

```env
GITHUB_APP_ID=your_app_id
GITHUB_PRIVATE_KEY_PATH=./private-key.pem
GITHUB_WEBHOOK_SECRET=your_webhook_secret
GROQ_API_KEY=your_groq_api_key
DATABASE_URL=sqlite:///./pr_reviews.db   # optional, defaults to SQLite
```

Start the server:

```bash
uvicorn app.main:app --reload
```

Expose locally with [ngrok](https://ngrok.com) and point your GitHub App webhook at `https://<your-ngrok>.ngrok.io/webhook`.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_APP_ID` | ✅ | Your GitHub App's numeric ID |
| `GITHUB_PRIVATE_KEY_PATH` | ✅ | Path to the `.pem` private key file |
| `GITHUB_WEBHOOK_SECRET` | ✅ | Secret set in your GitHub App settings |
| `GROQ_API_KEY` | ✅ | API key from [console.groq.com](https://console.groq.com) |
| `DATABASE_URL` | ❌ | Defaults to local SQLite; set Postgres URL for production |

---

## Testing

```bash
# All tests (no API keys needed — Groq is mocked)
pytest tests/ -v

# Individual suites
pytest tests/test_parser.py -v      # diff parsing
pytest tests/test_reviewer.py -v    # LLM prompt + response handling
pytest tests/test_poster.py -v      # GitHub comment formatting
pytest tests/test_storage.py -v     # DB operations
```

Tests use an in-memory SQLite database — no setup required.

---

## Example Review Output

When BracketWise reviews a PR, GitHub renders something like this:

**Inline comment on `auth.py` line 14:**

> [!CAUTION]
> **SECURITY**
>
> `jwt.decode(token, SECRET)` is called without specifying `algorithms`, which defaults to accepting any algorithm including `none`. An attacker can craft a token with `alg: none` and bypass verification entirely. Pass `algorithms=["HS256"]` explicitly.

**PR-level summary:**

```
## BracketWise Review Summary

Found **3 issue(s)** across the changed files.

### Breakdown
- 🔴 Critical: 1
- 🟠 Major: 1
- 🟡 Minor: 1

### By Category
- Security: 1
- Logic: 1
- Code Quality: 1
```

---

## Current Capabilities

- Reviews Python, JavaScript, TypeScript, Go, Java, Ruby, Rust, and more
- Handles PRs up to 10 files (configurable)
- Truncates files >6,000 characters to keep context focused
- Retries diff fetching when GitHub hasn't generated it yet (race condition handling)
- Skips binary files, removed files, and non-code extensions automatically

---

## Limitations

- One LLM call per file — large PRs with many files are slower and costlier
- No memory across reviews — each PR is reviewed in isolation
- No re-review on comment reply (yet)
- Groq rate limits can cause delays on burst traffic

---

## Roadmap

- [ ] Re-review trigger on `/bracketwise review` comment command
- [ ] Per-repo configuration (`.bracketwise.yml`) — tune severity thresholds
- [ ] Analytics dashboard — most common issue types, per-repo trends
- [ ] Support for multi-turn review conversations

---

## Contact

Built by [Yash Ladda](https://github.com/yash-ladda) — feel free to open an issue or reach out.
