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

load_dotenv()

GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")

app = FastAPI()


def verify_webhook_signature(
    payload_body: bytes,
    signature_header: str
) -> bool:
    """
    Verify that webhook request actually came from GitHub.
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

    return hmac.compare_digest(
        computed_signature,
        expected_signature
    )


async def process_pr(
    repo_full_name: str,
    pr_number: int
):
    """
    Background task:
    Fetch PR data and print it.

    LLM review logic will be added later.
    """
    from app.github.client import get_pr_context

    try:
        print(
            f"\nProcessing PR "
            f"#{pr_number} in {repo_full_name}..."
        )

        context = get_pr_context(
            repo_full_name,
            pr_number
        )

        print(f"\n{'=' * 50}")
        print("PR CONTEXT FETCHED SUCCESSFULLY")
        print(
            f"PR #{context['pr_number']}: "
            f"{context['title']}"
        )
        print(f"Author: {context['author']}")
        print(
            f"Files to review: "
            f"{len(context['files'])}"
        )

        for f in context["files"]:
            content_length = len(
                f["full_content"]
            )

            print(
                f"  - {f['filename']} "
                f"| {f['additions']}+ "
                f"{f['deletions']}- "
                f"| {content_length} chars"
            )

        print(f"{'=' * 50}\n")

    except Exception as e:
        print(
            f"ERROR processing "
            f"PR #{pr_number}: {e}"
        )


@app.get("/")
async def root():
    """
    Health check endpoint.
    """
    return {
        "status": "pr-review-agent is running"
    }


@app.post("/webhook")
async def handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks
):
    """
    Receive GitHub webhook events.
    """
    body = await request.body()

    signature = request.headers.get(
        "X-Hub-Signature-256",
        ""
    )

    # Verify webhook security
    if not verify_webhook_signature(
        body,
        signature
    ):
        print(
            "ERROR: Invalid webhook signature"
        )

        raise HTTPException(
            status_code=401,
            detail="Invalid signature"
        )

    # Parse JSON payload
    try:
        payload = json.loads(body)

    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON"
        )

    event_type = request.headers.get(
        "X-GitHub-Event",
        "unknown"
    )

    action = payload.get(
        "action",
        "unknown"
    )

    print(
        f"\nEVENT: "
        f"{event_type} / {action}"
    )

    # Handle PR events
    if (
        event_type == "pull_request"
        and action in [
            "opened",
            "synchronize",
            "reopened"
        ]
    ):
        repo_name = payload[
            "repository"
        ]["full_name"]

        pr_number = payload["number"]

        print(
            f"Starting background review "
            f"for PR #{pr_number}"
        )

        # Run PR processing in background
        background_tasks.add_task(
            process_pr,
            repo_name,
            pr_number
        )

        return {
            "status": "processing"
        }

    # GitHub webhook test event
    elif event_type == "ping":
        print(
            f"Ping: "
            f"{payload.get('zen', '')}"
        )

    else:
        print(
            f"Unhandled event: "
            f"{event_type}"
        )

    return {
        "status": "received"
    }