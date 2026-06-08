"""
Tests for the GitHub review poster.

All tests mock the requests.post call so no real API calls are made.

Run with: pytest tests/test_poster.py -v
"""
import pytest
from unittest.mock import patch, MagicMock

from app.review.parser import parse_patch
from app.github.poster import (
    build_review_comments,
    build_review_summary,
)

SAMPLE_PATCH = """\
@@ -1,5 +1,8 @@
 def authenticate(token: str):
-    pass
+    if not token:
+        raise ValueError("Token required")
+    decoded = jwt.decode(token, SECRET)
+    return decoded
 
 def logout():
     session.clear()"""


@pytest.fixture
def parsed_file():
    return parse_patch(SAMPLE_PATCH, "auth.py", "modified")


@pytest.fixture
def parsed_files_map(parsed_file):
    return {"auth.py": parsed_file}


class TestBuildReviewComments:

    def test_valid_line_number_maps_to_position(self, parsed_file, parsed_files_map):
        reviewable = parsed_file.get_reviewable_line_numbers()
        line = reviewable[0]
        comments = [{
            "file_path": "auth.py",
            "line_number": line,
            "issue_type": "security",
            "severity": "critical",
            "comment_body": "JWT decoded without verification",
        }]
        inline, fallback = build_review_comments(comments, parsed_files_map)
        assert len(inline) == 1
        assert inline[0]["path"] == "auth.py"
        assert isinstance(inline[0]["position"], int)
        assert len(fallback) == 0

    def test_null_line_number_becomes_fallback(self, parsed_files_map):
        comments = [{
            "file_path": "auth.py",
            "line_number": None,
            "issue_type": "test_gap",
            "severity": "minor",
            "comment_body": "No tests for this function",
        }]
        inline, fallback = build_review_comments(comments, parsed_files_map)
        assert len(inline) == 0
        assert len(fallback) == 1

    def test_hallucinated_line_becomes_fallback(self, parsed_files_map):
        comments = [{
            "file_path": "auth.py",
            "line_number": 9999,
            "issue_type": "logic",
            "severity": "major",
            "comment_body": "Issue here",
        }]
        inline, fallback = build_review_comments(comments, parsed_files_map)
        assert len(inline) == 0
        assert len(fallback) == 1

    def test_body_includes_severity_and_type_badge(self, parsed_file, parsed_files_map):
        reviewable = parsed_file.get_reviewable_line_numbers()
        comments = [{
            "file_path": "auth.py",
            "line_number": reviewable[0],
            "issue_type": "security",
            "severity": "critical",
            "comment_body": "Dangerous call",
        }]
        inline, _ = build_review_comments(comments, parsed_files_map)
        assert "SECURITY" in inline[0]["body"]
        assert "Dangerous call" in inline[0]["body"]
        assert "[!CAUTION]" in inline[0]["body"]

    def test_unknown_file_becomes_fallback(self, parsed_files_map):
        comments = [{
            "file_path": "nonexistent.py",
            "line_number": 5,
            "issue_type": "quality",
            "severity": "minor",
            "comment_body": "Some note",
        }]
        inline, fallback = build_review_comments(comments, parsed_files_map)
        assert len(inline) == 0
        assert len(fallback) == 1

    def test_mixed_comments_split_correctly(self, parsed_file, parsed_files_map):
        reviewable = parsed_file.get_reviewable_line_numbers()
        comments = [
            {
                "file_path": "auth.py",
                "line_number": reviewable[0],
                "issue_type": "security",
                "severity": "critical",
                "comment_body": "Inline issue",
            },
            {
                "file_path": "auth.py",
                "line_number": None,
                "issue_type": "test_gap",
                "severity": "minor",
                "comment_body": "File level note",
            },
        ]
        inline, fallback = build_review_comments(comments, parsed_files_map)
        assert len(inline) == 1
        assert len(fallback) == 1


class TestBuildReviewSummary:

    def test_no_issues_returns_clean_message(self):
        summary = build_review_summary([], [], "org/repo", 42)
        assert "No Issues Found" in summary
        assert "automatically" in summary.lower()

    def test_summary_includes_total_count(self):
        comments = [
            {"severity": "critical", "issue_type": "security", "comment_body": "x"},
            {"severity": "minor", "issue_type": "quality", "comment_body": "y"},
        ]
        summary = build_review_summary(comments, [], "org/repo", 42)
        assert "2 issue" in summary

    def test_summary_includes_severity_breakdown(self):
        comments = [
            {"severity": "critical", "issue_type": "security", "comment_body": "x"},
            {"severity": "major", "issue_type": "logic", "comment_body": "y"},
            {"severity": "minor", "issue_type": "quality", "comment_body": "z"},
        ]
        summary = build_review_summary(comments, [], "org/repo", 42)
        assert "Critical: 1" in summary
        assert "Major: 1" in summary
        assert "Minor: 1" in summary

    def test_fallback_comments_appear_in_summary(self):
        fallbacks = [{"file_path": "utils.py", "body": "**[MINOR | quality]** No docstring"}]
        summary = build_review_summary([], fallbacks, "org/repo", 42)
        assert "utils.py" in summary

    def test_summary_has_agent_attribution(self):
        summary = build_review_summary([], [], "org/repo", 42)
        assert "automatically" in summary.lower()