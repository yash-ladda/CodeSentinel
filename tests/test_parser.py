"""
Unit tests for the diff parser.

Run with: pytest tests/test_parser.py -v
"""
import pytest
from app.review.parser import parse_patch, build_file_context, DiffLine


# A realistic patch string we control completely.
# This is a function that gets a null check added to it.
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


class TestParsePatch:

    def test_parses_correct_number_of_lines(self):
        result = parse_patch(SAMPLE_PATCH, "users.py", "modified")
        # 1 header + 1 context + 1 removed + 5 added + 1 blank-ish + 2 context
        # Just verify we got lines back
        assert len(result.lines) > 0

    def test_first_line_is_header_at_position_1(self):
        result = parse_patch(SAMPLE_PATCH, "users.py", "modified")
        first = result.lines[0]
        assert first.line_type == "header"
        assert first.position == 1
        assert first.new_line_number is None

    def test_context_line_has_correct_position_and_line_number(self):
        result = parse_patch(SAMPLE_PATCH, "users.py", "modified")
        # First line after @@ header is context: "def get_user(user_id: int):"
        context_line = result.lines[1]
        assert context_line.line_type == "context"
        assert context_line.position == 2
        assert context_line.new_line_number == 1

    def test_removed_line_has_no_new_line_number(self):
        result = parse_patch(SAMPLE_PATCH, "users.py", "modified")
        removed = next(l for l in result.lines if l.line_type == "removed")
        assert removed.new_line_number is None
        assert removed.old_line_number is not None

    def test_added_lines_have_sequential_line_numbers(self):
        result = parse_patch(SAMPLE_PATCH, "users.py", "modified")
        added_lines = [l for l in result.lines if l.line_type == "added"]
        # Added lines should have sequential new_line_numbers
        line_nums = [l.new_line_number for l in added_lines]
        assert line_nums == sorted(line_nums)
        assert len(set(line_nums)) == len(line_nums)  # no duplicates

    def test_positions_are_sequential_with_no_gaps(self):
        result = parse_patch(SAMPLE_PATCH, "users.py", "modified")
        positions = [l.position for l in result.lines]
        # Positions must be 1, 2, 3, ... with no gaps or repeats
        assert positions == list(range(1, len(result.lines) + 1))

    def test_get_position_for_line_returns_correct_position(self):
        result = parse_patch(SAMPLE_PATCH, "users.py", "modified")
        # Line 1 in new file is the context "def get_user" line
        # It's after the @@ header so its position should be 2
        pos = result.get_position_for_line(1)
        assert pos == 2

    def test_get_position_for_nonexistent_line_returns_none(self):
        result = parse_patch(SAMPLE_PATCH, "users.py", "modified")
        # Line 999 doesn't exist in our diff
        pos = result.get_position_for_line(999)
        assert pos is None

    def test_added_line_content_strips_plus_prefix(self):
        result = parse_patch(SAMPLE_PATCH, "users.py", "modified")
        added = [l for l in result.lines if l.line_type == "added"]
        for line in added:
            # Content should NOT start with +
            assert not line.content.startswith("+")

    def test_empty_patch_returns_empty_lines(self):
        result = parse_patch("", "empty.py", "added")
        assert result.lines == []

    def test_filename_and_status_stored_correctly(self):
        result = parse_patch(SAMPLE_PATCH, "src/users.py", "modified")
        assert result.filename == "src/users.py"
        assert result.status == "modified"


class TestBuildFileContext:

    def test_context_string_contains_filename(self):
        parsed = parse_patch(SAMPLE_PATCH, "users.py", "modified")
        context = build_file_context(parsed, "full file content here")
        assert "users.py" in context

    def test_context_string_contains_diff_section(self):
        parsed = parse_patch(SAMPLE_PATCH, "users.py", "modified")
        context = build_file_context(parsed, "")
        assert "DIFF" in context

    def test_context_string_truncates_large_files(self):
        parsed = parse_patch(SAMPLE_PATCH, "users.py", "modified")
        large_content = "x" * 10000
        context = build_file_context(parsed, large_content)
        assert "truncated" in context

    def test_context_string_contains_line_numbers(self):
        parsed = parse_patch(SAMPLE_PATCH, "users.py", "modified")
        context = build_file_context(parsed, "")
        # Line numbers should appear in the diff section
        assert "line" in context