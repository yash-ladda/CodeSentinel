import json
import hmac
import hashlib
import os

from fastapi import (
    FastAPI,
    Request,
    HTTPException,
    BackgroundTasks
)
from dotenv import load_dotenv

from app.db.models import create_tables

load_dotenv()

GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")

app = FastAPI()

# Create DB tables on startup — safe to call every time,
# SQLAlchemy skips tables that already exist
create_tables()


def verify_webhook_signature(
    payload_body: bytes,
    signature_header: str
) -> bool:
    """
    Verify that the webhook request actually came from GitHub.
    Uses HMAC-SHA256 with our webhook secret.
    """
    if (
        not signature_header
        or not signature_header.startswith("sha256=")
    ):
        return False

    expected_signature = signature_header[7:]

    computed_signature = hmac.new(
        key=GITHUB_WEBHOOK_SECRET.encode("utf-8"),
        msg=payload_body,
        digestmod=hashlib.sha256
    ).hexdigest()

    # compare_digest prevents timing attacks
    return hmac.compare_digest(
        computed_signature,
        expected_signature
    )


async def process_pr(
    repo_full_name: str,
    pr_number: int,
    commit_sha: str
):
    """
    Background task: full review pipeline with DB tracking.

    Current shape:
      idempotency check → create DB record → fetch PR data
      → parse diffs → [LLM review — Day 6+] → save results → mark complete

    Status transitions: queued → processing → completed | failed
    """
    from app.github.client import get_pr_context
    from app.review.parser import parse_pr_files
    from app.db.storage import (
        is_already_reviewed,
        create_review,
        update_review_status,
        get_review_summary,
    )

    # ── Idempotency check ──────────────────────────────────────────────
    # If we already completed a review for this exact commit SHA, skip.
    # Prevents duplicate comments when GitHub redelivers a webhook.
    if is_already_reviewed(commit_sha, repo_full_name):
        print(
            f"  Skipping PR #{pr_number} — "
            f"commit {commit_sha[:8]} already reviewed"
        )
        return

    # ── Create review record ───────────────────────────────────────────
    review_id = create_review(
        repo=repo_full_name,
        pr_number=pr_number,
        commit_sha=commit_sha,
        pr_title="",   # filled after fetch below
        author="",
    )
    update_review_status(review_id, "processing")

    try:
        # ── Fetch PR data from GitHub ──────────────────────────────────
        context = get_pr_context(repo_full_name, pr_number)
        parsed_files = parse_pr_files(context["files"])

        print(f"\n  PR:    #{context['pr_number']} — {context['title']}")
        print(f"  Author: {context['author']}")
        print(f"  Files to review: {len(parsed_files)}")

        # ── [Day 6+] LLM review goes here ─────────────────────────────
        print(f"\n  [Day 6+] LLM review slots in here")

        # ── Mark complete ──────────────────────────────────────────────
        update_review_status(review_id, "completed")

        summary = get_review_summary(review_id)
        print(f"\n  Review #{review_id} complete: {summary}")

    except Exception as e:
        import traceback
        update_review_status(
            review_id,
            "failed",
            error_message=str(e)
        )
        print(f"  ERROR in review #{review_id}: {e}")
        traceback.print_exc()


@app.get("/")
async def root():
    """Health check endpoint."""
    return {"status": "pr-review-agent is running"}


@app.post("/webhook")
async def handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks
):
    """
    Receive and route GitHub webhook events.

    Must respond with 200 within 10 seconds — heavy work
    runs in a background task after we return.
    """
    body = await request.body()

    signature = request.headers.get(
        "X-Hub-Signature-256",
        ""
    )

    # ── Security: verify the request is from GitHub ────────────────────
    if not verify_webhook_signature(body, signature):
        print("ERROR: Invalid webhook signature — request rejected")
        raise HTTPException(
            status_code=401,
            detail="Invalid signature"
        )

    # ── Parse payload ──────────────────────────────────────────────────
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON"
        )

    event_type = request.headers.get("X-GitHub-Event", "unknown")
    action = payload.get("action", "unknown")

    print(f"\nEVENT: {event_type} / {action}")

    # ── Route events ───────────────────────────────────────────────────
    if (
        event_type == "pull_request"
        and action in ["opened", "synchronize", "reopened"]
    ):
        repo_name = payload["repository"]["full_name"]
        pr_number = payload["number"]
        commit_sha = payload["pull_request"]["head"]["sha"]

        print(
            f"  Queuing review for PR #{pr_number} "
            f"({commit_sha[:8]}...) in {repo_name}"
        )

        background_tasks.add_task(
            process_pr,
            repo_name,
            pr_number,
            commit_sha
        )

        return {"status": "processing"}

    elif event_type == "ping":
        print(f"  Ping received: {payload.get('zen', '')}")

    else:
        print(f"  Unhandled event: {event_type}")

    return {"status": "received"}