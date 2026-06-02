"""
Run this once to save a real PR's data as a test fixture.
Usage: python tests/fixtures/save_fixture.py

Replace REPO and PR_NUMBER with a real repo/PR you have access to.
"""
import sys
import json
import os

# Allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from app.github.client import get_pr_context

# Use your webhook-test repo and any PR number you created on Day 1
REPO = "yash-ladda/codesentinel-test"   # ← change this
PR_NUMBER = 2                          # ← change this to your PR number

if __name__ == "__main__":
    print(f"Fetching PR #{PR_NUMBER} from {REPO}...")
    
    context = get_pr_context(REPO, PR_NUMBER)
    
    # Save to fixture file
    fixture_path = os.path.join(os.path.dirname(__file__), "sample_pr_files.json")
    with open(fixture_path, "w") as f:
        json.dump(context, f, indent=2)
    
    print(f"\nSaved fixture to {fixture_path}")
    print(f"Files captured: {len(context['files'])}")
    for file in context["files"]:
        print(f"  - {file['filename']} ({file['additions']}+ {file['deletions']}-)")