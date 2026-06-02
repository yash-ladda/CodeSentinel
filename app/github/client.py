import base64
import json
import os
import time

from github import Github, GithubException
from app.github.auth import get_installation_token


# File extensions we care about reviewing.
# Everything else gets skipped — no point sending config/docs to the LLM.
REVIEWABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".go", ".rb", ".cpp", ".c",
    ".cs", ".php", ".swift", ".kt", ".rs"
}

# Config constants
MAX_REVIEWABLE_FILES = 10
MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 2


def get_github_client() -> Github:
    """
    Create an authenticated GitHub client using our installation token.
    Called fresh for each PR review — tokens expire after 1 hour.
    """
    token = get_installation_token()
    return Github(token)


def get_pr_files(
    repo_full_name: str,
    pr_number: int
) -> list[dict]:
    """
    Fetch all changed files in a PR.

    Includes retry logic to handle GitHub race condition where
    webhook fires before diff generation completes.

    Returns a list of dicts, one per reviewable file:
    {
        "filename": "src/auth.py",
        "status": "modified",
        "additions": 12,
        "deletions": 3,
        "patch": "@@ -1,4 +1,6 @@\n ..."
    }
    """
    client = get_github_client()

    try:
        repo = client.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)

    except GithubException as e:
        print(
            f"ERROR fetching PR files: "
            f"{e.status} — {e.data}"
        )
        raise

    delay = INITIAL_RETRY_DELAY
    files = []

    # Retry for GitHub async diff generation
    for attempt in range(MAX_RETRIES + 1):

        files = list(pr.get_files())

        # Success condition:
        # at least one file has a patch
        if files and any(f.patch for f in files):
            break

        # Valid empty PR
        if pr.changed_files == 0:
            print("  Empty PR detected")
            break

        if attempt < MAX_RETRIES:
            print(
                f"  Diff not ready yet "
                f"(attempt {attempt + 1}/"
                f"{MAX_RETRIES + 1}) "
                f"Retrying in {delay}s..."
            )

            time.sleep(delay)
            delay *= 2

            # Refresh PR object
            pr.update()

        else:
            print(
                "  Max retries reached. "
                "Proceeding with current data."
            )

    reviewable = []

    for f in files:

        # Skip deleted files
        if f.status == "removed":
            print(
                f"  Skipping {f.filename} "
                f"(removed file)"
            )
            continue

        # Skip binary / huge files
        if not f.patch:
            print(
                f"  Skipping {f.filename} "
                f"(no patch available)"
            )
            continue

        # Check extension
        extension = (
            "." + f.filename.split(".")[-1]
            if "." in f.filename
            else ""
        )

        if extension not in REVIEWABLE_EXTENSIONS:
            print(
                f"  Skipping {f.filename} "
                f"(not a reviewable file type)"
            )
            continue

        reviewable.append({
            "filename": f.filename,
            "status": f.status,
            "additions": f.additions,
            "deletions": f.deletions,
            "patch": f.patch,
        })

    print(
        f"Found {len(reviewable)} "
        f"reviewable file(s) out of "
        f"{pr.changed_files} total changed"
    )

    return reviewable


def get_file_content(
    repo_full_name: str,
    filepath: str,
    ref: str
) -> str:
    """
    Fetch the full content of a file
    at a specific commit.

    Uses `ref` to fetch file from
    PR branch instead of main branch.
    """
    client = get_github_client()

    try:
        repo = client.get_repo(repo_full_name)

        file_obj = repo.get_contents(
            filepath,
            ref=ref
        )

    except GithubException as e:

        # File doesn't exist
        if e.status == 404:
            print(
                f"  File not found "
                f"at ref {ref}: "
                f"{filepath}"
            )
            return ""

        print(
            f"  ERROR fetching "
            f"{filepath}: "
            f"{e.status} — {e.data}"
        )
        raise

    try:
        content = (
            file_obj
            .decoded_content
            .decode("utf-8")
        )

    except UnicodeDecodeError:
        print(
            f"  Skipping {filepath}: "
            f"binary content"
        )
        return ""

    return content


def get_pr_context(
    repo_full_name: str,
    pr_number: int
) -> dict:
    """
    Main function:
    fetch everything needed
    to review a PR.

    Returns:
    - PR metadata
    - changed files
    - patch
    - full file content
    """
    client = get_github_client()

    try:
        repo = client.get_repo(
            repo_full_name
        )

        pr = repo.get_pull(
            pr_number
        )

    except GithubException as e:
        print(
            f"ERROR fetching PR: "
            f"{e.status} — {e.data}"
        )
        raise

    head_sha = pr.head.sha

    print(
        f"\nFetching PR "
        f"#{pr_number}: "
        f"'{pr.title}'"
    )

    print(
        f"  Author: "
        f"{pr.user.login}"
    )

    print(
        f"  Head SHA: "
        f"{head_sha[:8]}..."
    )

    # Get changed files
    files = get_pr_files(
        repo_full_name,
        pr_number
    )

    # Cap large PRs
    if len(files) > MAX_REVIEWABLE_FILES:
        print(
            f"  PR has "
            f"{len(files)} "
            f"reviewable files — "
            f"capping at "
            f"{MAX_REVIEWABLE_FILES}"
        )

        files = files[
            :MAX_REVIEWABLE_FILES
        ]

    enriched_files = []

    for f in files:

        print(
            f"  Fetching content: "
            f"{f['filename']} "
            f"({f['status']}) "
            f"| "
            f"{f['additions']}+ "
            f"{f['deletions']}-"
        )

        content = get_file_content(
            repo_full_name,
            f["filename"],
            head_sha
        )

        enriched_files.append({
            **f,
            "full_content": content,
        })

    return {
        "pr_number": pr_number,
        "title": pr.title,
        "author": pr.user.login,
        "description": pr.body or "",
        "head_sha": head_sha,
        "repo": repo_full_name,
        "files": enriched_files,
    }