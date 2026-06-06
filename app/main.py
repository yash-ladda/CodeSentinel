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
    commit_sha: str,
    installation_id: int,        # ← add this parameter
):
    from app.github.client import get_pr_context
    from app.review.parser import parse_pr_files
    from app.review.reviewer import review_file
    from app.github.poster import post_review
    from app.db.storage import (
        is_already_reviewed,
        create_review,
        update_review_status,
        save_comments_bulk,
        get_review_summary,
        #get_comments_for_review,   # ← new helper, see below
    )

    if is_already_reviewed(commit_sha, repo_full_name):
        print(f"  Skipping PR #{pr_number} — commit {commit_sha[:8]} already reviewed")
        return

    review_id = create_review(
        repo=repo_full_name,
        pr_number=pr_number,
        commit_sha=commit_sha,
        pr_title="",
        author="",
    )
    update_review_status(review_id, "processing")

    try:
        context = get_pr_context(repo_full_name, pr_number, installation_id)
        parsed_files_list = parse_pr_files(context["files"])

        # Build a map of filename → ParsedFile for poster lookups
        parsed_files_map = {pf.filename: pf for pf in parsed_files_list}

        print(f"\n  PR:    #{context['pr_number']} — {context['title']}")
        print(f"  Author: {context['author']}")
        print(f"  Files to review: {len(parsed_files_list)}")

        # ── LLM review ─────────────────────────────────────────────
        all_comments = []
        pr_context = {
            "title": context["title"],
            "description": context["description"],
            "author": context["author"],
        }
        content_map = {
            f["filename"]: f.get("full_content", "")
            for f in context["files"]
        }

        for parsed_file in parsed_files_list:
            full_content = content_map.get(parsed_file.filename, "")
            comments = review_file(parsed_file, full_content, pr_context)
            all_comments.extend(comments)

        if all_comments:
            saved = save_comments_bulk(review_id, all_comments)
            print(f"\n  Saved {saved} comment(s) to database")
        else:
            print("\n  No issues found across all files")
        # ── End LLM review ──────────────────────────────────────────

        # ── Post review to GitHub ───────────────────────────────────
        update_review_status(review_id, "posting")

        print("\nPosting GitHub Review...")
        print(f"Comments to post: {len(all_comments)}")

        post_review(
            repo=repo_full_name,
            pr_number=pr_number,
            commit_sha=commit_sha,
            installation_id=installation_id,
            all_comments=all_comments,
            parsed_files=parsed_files_map,
        )

        update_review_status(review_id, "completed")
        # ── End post ────────────────────────────────────────────────

        summary = get_review_summary(review_id)
        print(f"\n  Review #{review_id} complete: {summary}")

    except Exception as e:
        import traceback
        update_review_status(review_id, "failed", error_message=str(e))
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

        installation_id = payload.get("installation", {}).get("id")

        background_tasks.add_task(
            process_pr,
            repo_name,
            pr_number,
            commit_sha,
            installation_id
        )

        return {"status": "processing"}

    elif event_type == "ping":
        print(f"  Ping received: {payload.get('zen', '')}")

    else:
        print(f"  Unhandled event: {event_type}")

    return {"status": "received"}