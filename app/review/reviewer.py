"""
LLM-powered code reviewer.

Takes a parsed diff + full file content, sends it to Groq,
and returns structured review comments ready for DB storage.

Flow: build_prompt → call_groq → parse + validate → return comments
"""
import json
import os
import re

from groq import Groq 
from dotenv import load_dotenv

from app.review.parser import ParsedFile, build_file_context

load_dotenv()

# The model we use for reviews.
# Sonnet: best balance of quality and cost for code review.
REVIEW_MODEL = "llama-3.3-70b-versatile"

# Valid values for our schema — used for validation after parsing.
VALID_ISSUE_TYPES = {"security", "logic", "quality", "test_gap"}
VALID_SEVERITIES = {"critical", "major", "minor"}

# System prompt — sets Groq's role and behavior.
# Kept separate from the user prompt so it's easy to tune.
SYSTEM_PROMPT = """You are a senior Python engineer doing a pull request review.

Your job is to find real bugs — not style issues, not opinions.

## What to report

Only report issues in these four categories:

1. SECURITY — user input reaching sensitive operations without validation,
   hardcoded secrets, auth bypass, exposed internal state, injection risks

2. BUGS — logic that will produce wrong results or crash under a plausible
   input (None access, wrong operator, missed branch, off-by-one in a
   critical path, incorrect condition)

3. MISSING ERROR HANDLING — network calls, file I/O, or DB operations
   that have no error path and will silently fail or leak resources under
   real-world conditions

4. RESOURCE LEAK — connections, file handles, or memory that accumulate
   under load because they are never closed or released

## What NOT to report

- Missing docstrings or comments
- Naming style (snake_case, variable length, etc.)
- Formatting, whitespace, blank lines
- Refactoring suggestions that don't fix a real bug
- Type hint coverage
- "Could be more Pythonic" observations
- Anything you are not confident is a real problem

If you are unsure whether something is a real issue, do not report it.
Silence is better than a false positive.

## Severity definitions — be conservative

critical: This code WILL cause a security breach, data loss, or crash in
          production today. The bug is reachable and exploitable without
          unusual conditions.

major:    This code WILL produce wrong output or raise an unhandled exception
          under a plausible, realistic input (empty string, None, concurrent
          request, network timeout). A real bug, not a theoretical one.

minor:    This COULD be a problem under unusual conditions. Use sparingly.
          When choosing between major and minor, prefer minor.
          When choosing between minor and omitting, prefer omitting.

## Comment format — mandatory

Each comment_body must follow this structure exactly:
1. One sentence: what will go wrong (specific, not dramatic)
2. One sentence: why it matters in this context
3. One sentence: the exact fix, with a code snippet if possible

Maximum 3 sentences. No preamble. No "it's worth noting that...".
No "significant risk". No "malicious actors". No "could potentially".

Good example:
"`db.query(user_id)` will raise `AttributeError` if `get_session()` returns
None on a failed connection. This will crash the request with no error logged.
Wrap in try/except and return a 503 on failure."

Bad example:
"This code presents a significant security vulnerability where the database
query could potentially be manipulated to cause unexpected behavior in
certain edge cases."

## Output format

Respond with ONLY a valid JSON array — no markdown, no explanation.

Each object must match this schema exactly:
{
  "file_path": "filename",
  "line_number": <integer or null>,
  "issue_type": "security" | "logic" | "quality" | "test_gap",
  "severity": "critical" | "major" | "minor",
  "comment_body": "..."
}

Rules:
- Only use line numbers from the REVIEWABLE LINES list provided
- Never invent line numbers — use null if unsure
- If no real issues exist, return []
- Prefer fewer high-confidence findings over many weak ones
"""


def build_prompt(parsed_file: ParsedFile, full_content: str, pr_context: dict) -> str:
    """
    Build the user-facing prompt for a single file review.
    
    Includes:
    - PR context (title, description) for intent understanding
    - The file diff with line numbers
    - The full file content for surrounding context
    - The explicit list of valid line numbers Groq can reference
    """
    reviewable_lines = parsed_file.get_reviewable_line_numbers()
    added_lines = parsed_file.get_added_line_numbers()
    
    file_context = build_file_context(parsed_file, full_content)
    
    prompt_parts = [
        f"PR Title: {pr_context.get('title', 'N/A')}",
        f"PR Description: {pr_context.get('description', 'No description provided.')}",
        f"Author: {pr_context.get('author', 'N/A')}",
        "",
        "Focus your review on the ADDED lines (actual changes), but use context lines",
        "to understand the surrounding code.",
        "",
        f"Newly added lines: {added_lines}",
        f"All reviewable line numbers (lines you MAY reference): {reviewable_lines}",
        "",
        "IMPORTANT: You may ONLY use line numbers from the reviewable list above.",
        "Any line number not in that list will be rejected. Use null if unsure.",
        "",
        "=== CODE TO REVIEW ===",
        "",
        file_context,
        "",
        "Return your review as a JSON array. If no issues found, return []",
    ]
    
    return "\n".join(prompt_parts)


def call_groq(prompt: str) -> str:
    """
    Make the API call to Groq and return the raw response text.
    
    Separated from parsing so we can log/debug the raw response
    independently of the parsing logic.
    """
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    completion = client.chat.completions.create(
        model=REVIEW_MODEL,
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.1,
        max_tokens=2048,
    )
    
    return completion.choices[0].message.content


def parse_groq_response(raw_response: str, filename: str) -> list[dict]:
    """
    Parse Groq's raw text response into a list of comment dicts.
    
    Handles the common ways Groq wraps its output:
    - Raw JSON array
    - JSON wrapped in ```json ... ``` fences
    - JSON wrapped in ``` ... ``` fences
    
    Returns empty list if parsing fails — we never crash the pipeline
    because Groq returned something unexpected.
    """
    text = raw_response.strip()
    
    # Strip markdown code fences if present
    # Pattern: optional ```json or ``` at start, ``` at end
    fence_pattern = r"^```(?:json)?\s*\n?(.*?)\n?```$"
    match = re.match(fence_pattern, text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  WARNING: Could not parse Groq response as JSON: {e}")
        print(f"  Raw response was: {raw_response[:200]}")
        return []
    
    if not isinstance(parsed, list):
        print(f"  WARNING: Groq returned non-list JSON: {type(parsed)}")
        return []
    
    # Ensure file_path is set correctly — Groq sometimes omits it or gets it wrong
    for item in parsed:
        if "file_path" not in item or not item["file_path"]:
            item["file_path"] = filename
    
    return parsed


def validate_comments(
    raw_comments: list[dict],
    parsed_file: ParsedFile,
) -> list[dict]:
    """
    Validate, clean, and deduplicate each comment from the LLM.

    Checks:
    1. Required fields are present
    2. issue_type and severity are valid enum values
    3. line_number actually exists in the diff (if provided)
       — falls back to null if the LLM hallucinated a line number
    4. Deduplicates comments with the same file + line + body prefix
       to prevent identical findings being posted twice
    """
    valid_comments = []
    reviewable_set = set(parsed_file.get_reviewable_line_numbers())
    seen: set[tuple] = set()

    for i, comment in enumerate(raw_comments):
        # Check required fields exist
        required = ["file_path", "issue_type", "severity", "comment_body"]
        missing = [f for f in required if f not in comment]
        if missing:
            print(f"  WARNING: Comment {i} missing fields {missing}, skipping")
            continue

        # Validate enum values
        if comment["issue_type"] not in VALID_ISSUE_TYPES:
            print(f"  WARNING: Invalid issue_type '{comment['issue_type']}', defaulting to 'quality'")
            comment["issue_type"] = "quality"

        if comment["severity"] not in VALID_SEVERITIES:
            print(f"  WARNING: Invalid severity '{comment['severity']}', defaulting to 'minor'")
            comment["severity"] = "minor"

        # Validate line number
        line_num = comment.get("line_number")
        if line_num is not None:
            if not isinstance(line_num, int) or line_num not in reviewable_set:
                print(
                    f"  WARNING: Line {line_num} not in diff for {parsed_file.filename}. "
                    f"Falling back to file-level comment."
                )
                comment["line_number"] = None

        # Deduplicate — key on file + line + first 80 chars of body
        # This catches exact duplicates and near-duplicates from the same finding
        dedup_key = (
            comment["file_path"],
            comment.get("line_number"),
            comment["comment_body"][:80].strip(),
        )
        if dedup_key in seen:
            print(f"  INFO: Duplicate comment skipped for {comment['file_path']} line {comment.get('line_number')}")
            continue
        seen.add(dedup_key)

        valid_comments.append(comment)

    return valid_comments


def review_file(
    parsed_file: ParsedFile,
    full_content: str,
    pr_context: dict,
) -> list[dict]:
    """
    Main entry point: review a single file and return validated comments.
    
    Returns a list of dicts ready to pass directly to save_comments_bulk().
    Each dict has: file_path, line_number, issue_type, severity, comment_body
    
    Never raises — errors are caught and logged, returning empty list.
    This ensures one bad file doesn't abort the entire PR review.
    """
    print(f"  Reviewing {parsed_file.filename} with Groq...")
    
    try:
        prompt = build_prompt(parsed_file, full_content, pr_context)
        raw_response = call_groq(prompt)

        print("\nRAW LLM RESPONSE:")
        print(raw_response)
        print()
        
        print(f"  Groq responded ({len(raw_response)} chars)")
        
        raw_comments = parse_groq_response(raw_response, parsed_file.filename)
        validated = validate_comments(raw_comments, parsed_file)
        
        print(f"  Found {len(validated)} issue(s) in {parsed_file.filename}")
        return validated
    
    except Exception as e:
        print(f"  ERROR reviewing {parsed_file.filename}: {e}")
        return []