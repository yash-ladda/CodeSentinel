"""
Tests for database storage operations.
Run with: pytest tests/test_storage.py -v
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.models import Base
import app.db.storage as storage


@pytest.fixture(autouse=True)
def use_test_database(monkeypatch):
    """
    Before each test: create a fresh in-memory SQLite database.
    After each test: discard it completely.
    
    monkeypatch replaces the real engine with a test engine
    so tests never touch your development database.
    """
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=test_engine)
    TestSession = sessionmaker(bind=test_engine, autocommit=False, autoflush=False)

    # Replace the engine and session factory in the storage module
    monkeypatch.setattr(storage, "engine", test_engine)
    monkeypatch.setattr(storage, "SessionLocal", TestSession)

    yield  # test runs here

    # Teardown: drop all tables
    Base.metadata.drop_all(bind=test_engine)


class TestIdempotencyCheck:

    def test_new_commit_is_not_already_reviewed(self):
        result = storage.is_already_reviewed("abc123", "owner/repo")
        assert result is False

    def test_completed_review_is_detected(self):
        review_id = storage.create_review(
            repo="owner/repo",
            pr_number=1,
            commit_sha="abc123",
            pr_title="Test PR",
            author="dev",
        )
        storage.update_review_status(review_id, "completed")
        result = storage.is_already_reviewed("abc123", "owner/repo")
        assert result is True

    def test_failed_review_is_not_blocked(self):
        # A failed review should NOT block a retry
        review_id = storage.create_review(
            repo="owner/repo",
            pr_number=1,
            commit_sha="abc123",
            pr_title="Test PR",
            author="dev",
        )
        storage.update_review_status(review_id, "failed")
        result = storage.is_already_reviewed("abc123", "owner/repo")
        assert result is False

    def test_different_repo_same_sha_is_not_blocked(self):
        review_id = storage.create_review(
            repo="owner/repo-a",
            pr_number=1,
            commit_sha="abc123",
            pr_title="Test PR",
            author="dev",
        )
        storage.update_review_status(review_id, "completed")
        # Same SHA but different repo — should NOT be blocked
        result = storage.is_already_reviewed("abc123", "owner/repo-b")
        assert result is False


class TestReviewLifecycle:

    def test_create_review_returns_integer_id(self):
        review_id = storage.create_review("owner/repo", 1, "sha1", "Title", "author")
        assert isinstance(review_id, int)
        assert review_id > 0

    def test_review_starts_in_queued_status(self):
        review_id = storage.create_review("owner/repo", 1, "sha1", "Title", "author")
        summary = storage.get_review_summary(review_id)
        assert summary["status"] == "queued"

    def test_status_transitions_correctly(self):
        review_id = storage.create_review("owner/repo", 1, "sha1", "Title", "author")
        storage.update_review_status(review_id, "processing")
        storage.update_review_status(review_id, "completed")
        summary = storage.get_review_summary(review_id)
        assert summary["status"] == "completed"

    def test_nonexistent_review_returns_empty_dict(self):
        summary = storage.get_review_summary(99999)
        assert summary == {}


class TestCommentStorage:

    def test_save_comments_bulk_returns_correct_count(self):
        review_id = storage.create_review("owner/repo", 1, "sha1", "Title", "author")
        comments = [
            {
                "file_path": "auth.py",
                "line_number": 10,
                "issue_type": "security",
                "severity": "critical",
                "comment_body": "Hardcoded secret detected",
            },
            {
                "file_path": "utils.py",
                "line_number": 25,
                "issue_type": "logic",
                "severity": "major",
                "comment_body": "Missing null check",
            },
        ]
        count = storage.save_comments_bulk(review_id, comments)
        assert count == 2

    def test_review_summary_counts_by_severity(self):
        review_id = storage.create_review("owner/repo", 1, "sha1", "Title", "author")
        storage.save_comments_bulk(review_id, [
            {"file_path": "a.py", "line_number": 1, "issue_type": "security",
             "severity": "critical", "comment_body": "issue 1"},
            {"file_path": "b.py", "line_number": 2, "issue_type": "logic",
             "severity": "major", "comment_body": "issue 2"},
            {"file_path": "c.py", "line_number": 3, "issue_type": "quality",
             "severity": "minor", "comment_body": "issue 3"},
        ])
        summary = storage.get_review_summary(review_id)
        assert summary["by_severity"]["critical"] == 1
        assert summary["by_severity"]["major"] == 1
        assert summary["by_severity"]["minor"] == 1
        assert summary["total_comments"] == 3

    def test_empty_comments_saves_zero(self):
        review_id = storage.create_review("owner/repo", 1, "sha1", "Title", "author")
        count = storage.save_comments_bulk(review_id, [])
        assert count == 0

    def test_file_level_comment_allows_null_line_number(self):
        review_id = storage.create_review("owner/repo", 1, "sha1", "Title", "author")
        count = storage.save_comments_bulk(review_id, [
            {
                "file_path": "auth.py",
                "line_number": None,  # file-level comment
                "issue_type": "security",
                "severity": "major",
                "comment_body": "General security concern",
            }
        ])
        assert count == 1