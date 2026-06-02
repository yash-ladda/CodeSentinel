import json
import hmac
import hashlib
import os
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

load_dotenv()

GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")

app = FastAPI()


def verify_webhook_signature(payload_body: bytes, signature_header: str) -> bool:
    """
    Verify that the webhook came from GitHub and not someone else.
    
    GitHub signs every webhook payload with our secret using HMAC-SHA256.
    We compute the same signature ourselves and compare.
    If they match — it's really from GitHub.
    If they don't — reject it.
    
    This is why the webhook secret matters: without it, anyone who knows
    your webhook URL could send fake events.
    """
    if not signature_header:
        return False
    
    # GitHub sends the signature as "sha256=<hex_digest>"
    # We need just the hex digest part
    if not signature_header.startswith("sha256="):
        return False
    
    expected_signature = signature_header[7:]  # strip "sha256=" prefix
    
    # Compute HMAC-SHA256 of the payload using our secret
    computed_signature = hmac.new(
        key=GITHUB_WEBHOOK_SECRET.encode("utf-8"),
        msg=payload_body,
        digestmod=hashlib.sha256
    ).hexdigest()
    
    # Use hmac.compare_digest to prevent timing attacks
    # (don't use == for comparing secrets)
    return hmac.compare_digest(computed_signature, expected_signature)

@app.get("/")
async def root():
    return {"status": "pr-review-agent is running"}

@app.post("/webhook")
async def handle_webhook(request: Request):
    # Read raw bytes — must be before any parsing
    body = await request.body()
    
    # Get the signature GitHub attached to this request
    signature = request.headers.get("X-Hub-Signature-256", "")
    
    # Verify it's really from GitHub
    if not verify_webhook_signature(body, signature):
        print("ERROR: Invalid webhook signature — request rejected")
        raise HTTPException(status_code=401, detail="Invalid signature")
    
    # Parse the JSON payload
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    # Identify what type of event this is
    event_type = request.headers.get("X-GitHub-Event", "unknown")
    action = payload.get("action", "unknown")
    
    print(f"\n{'='*50}")
    print(f"EVENT: {event_type} / action: {action}")
    
    # Handle pull_request events specifically
    if event_type == "pull_request" and action in ["opened", "synchronize"]:
        pr_number = payload["number"]
        repo_name = payload["repository"]["full_name"]
        pr_title = payload["pull_request"]["title"]
        sender = payload["sender"]["login"]
        
        print(f"PR #{pr_number}: '{pr_title}'")
        print(f"Repo:   {repo_name}")
        print(f"By:     {sender}")
        print(f"Action: {action}")
    
    elif event_type == "ping":
        print(f"Ping received! GitHub says: {payload.get('zen', '')}")
    
    else:
        print(f"Unhandled event: {event_type}")
    
    print(f"{'='*50}\n")
    
    # Always return 200 quickly
    return {"status": "received"}