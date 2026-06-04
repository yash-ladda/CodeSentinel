from datetime import datetime
from sqlalchemy.orm import Session, sessionmaker
from app.db.models import Review, ReviewComment, engine

# SessionLocal is a factory that creates database sessions.
# Each request/background task should create its own session,
# use it, then close it. Never share sessions between tasks.
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_session() -> Session:
    """Create and return a new database session."""
    return SessionLocal()


# ── Review operations ──────────────────────────────────────────────────────────

def is_already_reviewed(commit_sha: str, repo: str) -> bool:
    """
    Idempotency check: has this exact commit already been reviewed?
    
    We check commit_sha + repo together because the same SHA could
    theoretically appear in different repos (though extremely unlikely,
    it's more correct to check both).
    
    Returns True if a completed review exists → skip this PR.
    Returns False if no completed review exists → proceed.
    """
    session = get_session()
    try:
        existing = session.query(Review).filter(
            Review.commit_sha == commit_sha,
            Review.repo == repo,
            Review.status == "completed"
        ).first()
        return existing is not None
    finally:
        session.close()


def create_review(
    repo: str,
    pr_number: int,
    commit_sha: str,
    pr_title: str,
    author: str,
) -> int:
    """
    Create a new review record in 'queued' status.
    Returns the review ID to use for subsequent updates.
    """
    session = get_session()
    try:
        review = Review(
            repo=repo,
            pr_number=pr_number,
            commit_sha=commit_sha,
            pr_title=pr_title,
            author=author,
            status="queued",
        )
        session.add(review)
        session.commit()
        session.refresh(review)
        review_id = review.id
        print(f"  DB: Created review #{review_id} for PR #{pr_number}")
        return review_id
    finally:
        session.close()


def update_review_status(review_id: int, status: str, error_message: str = None):
    """
    Update a review's status.
    Called at each stage: queued → processing → completed/failed.
    """
    session = get_session()
    try:
        review = session.query(Review).filter(Review.id == review_id).first()
        if not review:
            print(f"  DB WARNING: Review #{review_id} not found")
            return

        review.status = status
        if error_message:
            review.error_message = error_message
        if status in ("completed", "failed"):
            review.completed_at = datetime.utcnow()

        session.commit()
        print(f"  DB: Review #{review_id} status → {status}")
    finally:
        session.close()


# ── Comment operations ─────────────────────────────────────────────────────────

def save_comment(
    review_id: int,
    file_path: str,
    line_number: int | None,
    issue_type: str,
    severity: str,
    comment_body: str,
    github_comment_id: int | None = None,
) -> int:
    """
    Save one review comment to the database.
    Returns the comment ID.
    """
    session = get_session()
    try:
        comment = ReviewComment(
            review_id=review_id,
            file_path=file_path,
            line_number=line_number,
            issue_type=issue_type,
            severity=severity,
            comment_body=comment_body,
            github_comment_id=github_comment_id,
        )
        session.add(comment)
        session.commit()
        session.refresh(comment)
        return comment.id
    finally:
        session.close()


def save_comments_bulk(review_id: int, comments: list[dict]) -> int:
    """
    Save multiple comments in a single transaction.
    More efficient than calling save_comment() in a loop.
    Returns the count of saved comments.
    
    Each dict in comments should have:
    file_path, line_number, issue_type, severity, comment_body
    Optional: github_comment_id
    """
    if not comments:
        return 0

    session = get_session()
    try:
        db_comments = [
            ReviewComment(
                review_id=review_id,
                file_path=c["file_path"],
                line_number=c.get("line_number"),
                issue_type=c["issue_type"],
                severity=c["severity"],
                comment_body=c["comment_body"],
                github_comment_id=c.get("github_comment_id"),
            )
            for c in comments
        ]
        session.add_all(db_comments)
        session.commit()
        print(f"  DB: Saved {len(db_comments)} comment(s) for review #{review_id}")
        return len(db_comments)
    finally:
        session.close()


# ── Query operations (for debugging and future dashboard) ─────────────────────

def get_review_summary(review_id: int) -> dict:
    """
    Return a summary of a completed review.
    Used for the summary comment posted to GitHub.
    """
    session = get_session()
    try:
        review = session.query(Review).filter(Review.id == review_id).first()
        if not review:
            return {}

        comments = session.query(ReviewComment).filter(
            ReviewComment.review_id == review_id
        ).all()

        # Count by severity
        severity_counts = {"critical": 0, "major": 0, "minor": 0}
        type_counts = {"security": 0, "logic": 0, "quality": 0, "test_gap": 0}

        for c in comments:
            if c.severity in severity_counts:
                severity_counts[c.severity] += 1
            if c.issue_type in type_counts:
                type_counts[c.issue_type] += 1

        return {
            "review_id": review_id,
            "repo": review.repo,
            "pr_number": review.pr_number,
            "total_comments": len(comments),
            "by_severity": severity_counts,
            "by_type": type_counts,
            "status": review.status,
        }
    finally:
        session.close()