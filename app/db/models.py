from datetime import datetime
from sqlalchemy import (
    create_engine,
    Column,
    String,
    Integer,
    DateTime,
    Text,
    ForeignKey,
)
from sqlalchemy.orm import declarative_base, relationship
import os
from dotenv import load_dotenv

load_dotenv()

# DATABASE_URL controls which database we connect to.
# SQLite locally (a file), Postgres in production.
# Default to SQLite so the app works with zero setup.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./pr_reviews.db")

# The engine is the connection to the database.
# connect_args is SQLite-specific — it allows the same connection
# to be used across threads (FastAPI uses multiple threads).
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(DATABASE_URL)

# Base class that all models inherit from.
# SQLAlchemy uses it to track your table definitions.
Base = declarative_base()


class Review(Base):
    """
    One row per PR review job.
    
    Tracks the full lifecycle: queued → processing → completed/failed.
    The commit_sha field is the key for idempotency checking.
    """
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo = Column(String, nullable=False)          # "owner/repo"
    pr_number = Column(Integer, nullable=False)
    commit_sha = Column(String, nullable=False)    # head SHA — used for idempotency
    pr_title = Column(String, nullable=True)
    author = Column(String, nullable=True)
    status = Column(String, nullable=False, default="queued")
    # status values: "queued" | "processing" | "completed" | "failed"
    error_message = Column(Text, nullable=True)    # populated if status = "failed"
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # Relationship: one review has many comments
    comments = relationship("ReviewComment", back_populates="review")

    def __repr__(self):
        return f"<Review repo={self.repo} pr={self.pr_number} status={self.status}>"


class ReviewComment(Base):
    """
    One row per comment the agent posted on GitHub.
    
    Storing these lets you query "what did the agent find most often?"
    and gives you real metrics for your README and interviews.
    """
    __tablename__ = "review_comments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_id = Column(Integer, ForeignKey("reviews.id"), nullable=False)
    file_path = Column(String, nullable=False)
    line_number = Column(Integer, nullable=True)   # None = file-level comment
    issue_type = Column(String, nullable=False)    # security | logic | quality | test_gap
    severity = Column(String, nullable=False)      # critical | major | minor
    comment_body = Column(Text, nullable=False)
    github_comment_id = Column(Integer, nullable=True)  # ID returned by GitHub after posting
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationship back to the parent review
    review = relationship("Review", back_populates="comments")

    def __repr__(self):
        return f"<ReviewComment file={self.file_path} line={self.line_number} severity={self.severity}>"


def create_tables():
    """Create all tables if they don't exist. Safe to call on every startup."""
    Base.metadata.create_all(bind=engine)