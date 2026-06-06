"""
GitHub PR Review Poster.

Takes validated review comments and posts them to GitHub as a
single Pull Request Review using the batch Review API.

One API call posts all inline comments + summary together.
This is how professional review tools (Reviewdog, Danger) work.
"""
import os
import requests
from dotenv import load_dotenv

from app.github.auth import get_installation_token
from app.review.parser import ParsedFile

load_dotenv()

GITHUB_API_BASE = "https://api.github.com"


def build_review_comments(
    comments: list[dict],
    parsed_files: dict[str, ParsedFile],
) -> tuple[list[dict], list[dict]]:
    """
    Convert internal comment dicts into GitHub Review API comment format.

    GitHub requires 'position' (diff offset), not raw line numbers.
    We use get_position_for_line() from the parser to do this mapping.

    Returns:
        inline_comments: comments with valid diff positions (go in the review)
        fallback_comments: comments with no position (folded into summary body)
    """
    inline_comments = []
    fallback_comments = []

    for comment in comments:
        file_path = comment["file_path"]
        line_number = comment.get("line_number")
        body = comment["comment_body"]

        # Prefix body with issue type and severity badge
        issue_type = comment.get("issue_type", "quality")
        severity = comment.get("severity", "minor")
        formatted_body = f"**[{severity.upper()} | {issue_type}]** {body}"

        if line_number is None:
            # File-level comment — no position, goes into summary
            fallback_comments.append({
                "file_path": file_path,
                "body": formatted_body,
            })
            continue

        parsed_file = parsed_files.get(file_path)
        if parsed_file is None:
            # We have a comment for a file we didn't parse — shouldn't happen
            # but guard against it
            print(f"  WARNING: No parsed file found for {file_path}, skipping inline comment")
            fallback_comments.append({"file_path": file_path, "body": formatted_body})
            continue

        position = parsed_file.get_position_for_line(line_number)
        if position is None:
            # Line number didn't map to a diff position
            # This can happen if the line is in full_content but not in the diff
            print(f"  WARNING: Line {line_number} in {file_path} has no diff position, falling back")
            fallback_comments.append({"file_path": file_path, "body": formatted_body})
            continue

        inline_comments.append({
            "path": file_path,
            "position": position,
            "body": formatted_body,
        })

    return inline_comments, fallback_comments


def build_review_summary(
    all_comments: list[dict],
    fallback_comments: list[dict],
    repo: str,
    pr_number: int,
) -> str:
    """
    Build the top-level review body text.

    Includes:
    - A summary header with issue counts by severity
    - File-level (fallback) comments that couldn't be posted inline
    - A closing note
    """
    total = len(all_comments)

    if total == 0 and not fallback_comments:
        return (
            "## CodeSentinel Review — No Issues Found\n\n"
            "Reviewed all changed files. No significant issues detected.\n\n"
            "_This review was generated automatically by CodeSentinel._"
        )

    # Count by severity
    severity_counts: dict[str, int] = {"critical": 0, "major": 0, "minor": 0}
    for c in all_comments:
        sev = c.get("severity", "minor")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    # Count by issue type
    type_counts: dict[str, int] = {}
    for c in all_comments:
        issue_type = c.get("issue_type", "quality")
        type_counts[issue_type] = type_counts.get(issue_type, 0) + 1

    lines = [
        "## CodeSentinel Review Summary",
        "",
        f"Found **{total} issue(s)** across the changed files.",
        "",
        "### Breakdown",
        f"- 🔴 Critical: {severity_counts['critical']}",
        f"- 🟠 Major: {severity_counts['major']}",
        f"- 🟡 Minor: {severity_counts['minor']}",
        "",
    ]

    if type_counts:
        lines.append("### By Category")
        type_labels = {
            "security": "Security",
            "logic": "Logic",
            "quality": "Code Quality",
            "test_gap": "Test Coverage",
        }
        for issue_type, count in sorted(type_counts.items()):
            label = type_labels.get(issue_type, issue_type)
            lines.append(f"- {label}: {count}")
        lines.append("")

    # Fold in file-level comments that couldn't be posted inline
    if fallback_comments:
        lines.append("### File-level Notes")
        for fc in fallback_comments:
            lines.append(f"\n**`{fc['file_path']}`**")
            lines.append(fc["body"])
        lines.append("")

    lines.append("_This review was generated automatically by the CodeSentinel._")

    return "\n".join(lines)


def post_review(
    repo: str,
    pr_number: int,
    commit_sha: str,
    installation_id: int,
    all_comments: list[dict],
    parsed_files: dict[str, ParsedFile],
) -> dict:
    """
    Post a complete PR review to GitHub in a single API call.

    Uses the Pull Request Reviews API (batch endpoint) which posts
    all inline comments + summary atomically — the correct approach
    for review bots.

    Returns the GitHub API response dict on success.
    Raises on HTTP errors so the caller can update review status.
    """
    token = get_installation_token(installation_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    owner, repo_name = repo.split("/")

    # Map comments to GitHub format, separating inline vs fallback
    inline_comments, fallback_comments = build_review_comments(
        all_comments, parsed_files
    )

    summary_body = build_review_summary(all_comments, fallback_comments, repo, pr_number)

    payload = {
        "commit_id": commit_sha,
        "body": summary_body,
        "event": "COMMENT",  # Never APPROVE or REQUEST_CHANGES
        "comments": inline_comments,
    }

    print(f"\n  Posting review to GitHub PR #{pr_number}...")
    print(f"  Inline comments: {len(inline_comments)}")
    print(f"  Fallback (summary only): {len(fallback_comments)}")

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo_name}/pulls/{pr_number}/reviews"
    response = requests.post(url, json=payload, headers=headers)

    if response.status_code not in (200, 201):
        print(f"  ERROR posting review: {response.status_code} {response.text[:300]}")
        response.raise_for_status()

    data = response.json()
    print(f"  ✅ Review posted! Review ID: {data.get('id')}, State: {data.get('state')}")
    return data