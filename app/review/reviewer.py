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
SYSTEM_PROMPT = """You are a senior software engineer performing a pull request code review.
Your job is to identify real, actionable issues in the code changes shown to you.

You must respond with ONLY a valid JSON array. No explanation, no markdown, no preamble.
Just the raw JSON array.

Each element in the array must have exactly these fields:
{
  "file_path": "the filename",
  "line_number": <integer or null>,
  "issue_type": "security" | "logic" | "quality" | "test_gap",
  "severity": "critical" | "major" | "minor",
  "comment_body": "Clear explanation of the issue and how to fix it"
}

Rules:
- Only comment on line numbers that appear in the REVIEWABLE LINES list provided.
- If you cannot map an issue to a specific line, use null for line_number.
- Do not invent line numbers. If unsure, use null.
- Only report real issues. Do not pad with style nitpicks.
- If there are no issues, return an empty array: []
- comment_body should be 1-3 sentences: what the issue is, why it matters, how to fix it."""


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
    Validate and clean each comment from Groq.
    
    Checks:
    1. Required fields are present
    2. issue_type and severity are valid enum values
    3. line_number actually exists in the diff (if provided)
       — falls back to null if Groq hallucinated a line number
    
    This is your safety net. Never trust LLM output directly.
    """
    valid_comments = []
    reviewable_set = set(parsed_file.get_reviewable_line_numbers())
    
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
        
        print(f"  Groq responded ({len(raw_response)} chars)")
        
        raw_comments = parse_groq_response(raw_response, parsed_file.filename)
        validated = validate_comments(raw_comments, parsed_file)
        
        print(f"  Found {len(validated)} issue(s) in {parsed_file.filename}")
        return validated
    
    except Exception as e:
        print(f"  ERROR reviewing {parsed_file.filename}: {e}")
        return []