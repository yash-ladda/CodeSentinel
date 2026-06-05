"""
Tests for the LLM reviewer module.

These tests cover prompt building, response parsing, and comment validation
WITHOUT making real API calls. The actual Groq call is tested manually.

Run with: pytest tests/test_reviewer.py -v
"""
import pytest
from unittest.mock import patch
from app.review.parser import parse_patch
from app.review.reviewer import (
    build_prompt,
    parse_groq_response,
    validate_comments,
    review_file,
)

SAMPLE_PATCH = """\
@@ -1,6 +1,9 @@
 def get_user(user_id: int):
-    return db.query(user_id)
+    if user_id <= 0:
+        raise ValueError("user_id must be positive")
+    user = db.query(user_id)
+    if user is None:
+        return None
+    return user
 
 def delete_user(user_id: int):
     db.delete(user_id)"""

PR_CONTEXT = {
    "title": "Add null check to get_user",
    "description": "Fixes crash when user_id is invalid",
    "author": "dev",
}


@pytest.fixture
def parsed_file():
    return parse_patch(SAMPLE_PATCH, "users.py", "modified")


class TestBuildPrompt:

    def test_prompt_contains_filename(self, parsed_file):
        prompt = build_prompt(parsed_file, "", PR_CONTEXT)
        assert "users.py" in prompt

    def test_prompt_contains_pr_title(self, parsed_file):
        prompt = build_prompt(parsed_file, "", PR_CONTEXT)
        assert "Add null check to get_user" in prompt

    def test_prompt_contains_reviewable_lines(self, parsed_file):
        prompt = build_prompt(parsed_file, "", PR_CONTEXT)
        assert "reviewable" in prompt.lower()

    def test_prompt_contains_added_lines(self, parsed_file):
        prompt = build_prompt(parsed_file, "", PR_CONTEXT)
        # Our patch adds lines 2-6
        assert "added lines" in prompt.lower()

    def test_prompt_includes_full_content_when_provided(self, parsed_file):
        prompt = build_prompt(parsed_file, "full file content here", PR_CONTEXT)
        assert "full file content here" in prompt


class TestParseGroqResponse:

    def test_parses_valid_json_array(self):
        raw = '[{"file_path": "a.py", "line_number": 1, "issue_type": "security", "severity": "critical", "comment_body": "Bad"}]'
        result = parse_groq_response(raw, "a.py")
        assert len(result) == 1
        assert result[0]["issue_type"] == "security"

    def test_parses_json_with_markdown_fences(self):
        raw = '```json\n[{"file_path": "a.py", "line_number": null, "issue_type": "logic", "severity": "major", "comment_body": "Issue"}]\n```'
        result = parse_groq_response(raw, "a.py")
        assert len(result) == 1

    def test_parses_json_with_plain_fences(self):
        raw = '```\n[{"file_path": "a.py", "line_number": null, "issue_type": "quality", "severity": "minor", "comment_body": "Nit"}]\n```'
        result = parse_groq_response(raw, "a.py")
        assert len(result) == 1

    def test_returns_empty_list_for_invalid_json(self):
        result = parse_groq_response("this is not json", "a.py")
        assert result == []

    def test_returns_empty_list_for_empty_array(self):
        result = parse_groq_response("[]", "a.py")
        assert result == []

    def test_sets_file_path_if_missing(self):
        raw = '[{"line_number": null, "issue_type": "quality", "severity": "minor", "comment_body": "Note"}]'
        result = parse_groq_response(raw, "target.py")
        assert result[0]["file_path"] == "target.py"


class TestValidateComments:

    def test_valid_comment_passes_through(self, parsed_file):
        reviewable = parsed_file.get_reviewable_line_numbers()
        valid_line = reviewable[0]
        comments = [{
            "file_path": "users.py",
            "line_number": valid_line,
            "issue_type": "security",
            "severity": "critical",
            "comment_body": "Issue here",
        }]
        result = validate_comments(comments, parsed_file)
        assert len(result) == 1
        assert result[0]["line_number"] == valid_line

    def test_hallucinated_line_number_becomes_null(self, parsed_file):
        comments = [{
            "file_path": "users.py",
            "line_number": 9999,  # doesn't exist in diff
            "issue_type": "logic",
            "severity": "major",
            "comment_body": "Some issue",
        }]
        result = validate_comments(comments, parsed_file)
        assert result[0]["line_number"] is None

    def test_invalid_issue_type_defaults_to_quality(self, parsed_file):
        comments = [{
            "file_path": "users.py",
            "line_number": None,
            "issue_type": "made_up_type",
            "severity": "minor",
            "comment_body": "Issue",
        }]
        result = validate_comments(comments, parsed_file)
        assert result[0]["issue_type"] == "quality"

    def test_invalid_severity_defaults_to_minor(self, parsed_file):
        comments = [{
            "file_path": "users.py",
            "line_number": None,
            "issue_type": "logic",
            "severity": "extreme",
            "comment_body": "Issue",
        }]
        result = validate_comments(comments, parsed_file)
        assert result[0]["severity"] == "minor"

    def test_comment_missing_required_field_is_skipped(self, parsed_file):
        comments = [{
            "file_path": "users.py",
            # missing issue_type, severity, comment_body
            "line_number": None,
        }]
        result = validate_comments(comments, parsed_file)
        assert result == []

    def test_null_line_number_is_accepted(self, parsed_file):
        comments = [{
            "file_path": "users.py",
            "line_number": None,
            "issue_type": "test_gap",
            "severity": "minor",
            "comment_body": "No tests for this function",
        }]
        result = validate_comments(comments, parsed_file)
        assert len(result) == 1
        assert result[0]["line_number"] is None


class TestReviewFileIntegration:
    """
    Tests review_file() with a mocked Groq call.
    This lets us test the full orchestration without API costs.
    """

    def test_review_file_returns_validated_comments(self, parsed_file):
        mock_response = '[{"file_path": "users.py", "line_number": null, "issue_type": "test_gap", "severity": "minor", "comment_body": "No tests cover the None return path."}]'
        
        with patch("app.review.reviewer.call_groq", return_value=mock_response):
            result = review_file(parsed_file, "", PR_CONTEXT)
        
        assert len(result) == 1
        assert result[0]["issue_type"] == "test_gap"

    def test_review_file_returns_empty_on_api_error(self, parsed_file):
        with patch("app.review.reviewer.call_groq", side_effect=Exception("API timeout")):
            result = review_file(parsed_file, "", PR_CONTEXT)
        
        assert result == []

    def test_review_file_handles_empty_response(self, parsed_file):
        with patch("app.review.reviewer.call_groq", return_value="[]"):
            result = review_file(parsed_file, "", PR_CONTEXT)
        
        assert result == []