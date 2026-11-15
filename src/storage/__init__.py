"""
CorpusAI SQLite Storage Package.

Provides SQLite connection management, schema migrations, transactional outbox primitives,
and transactional context managers for CorpusAI operational state.
"""

from src.storage.sqlite_backend import (
    CorpusAIStateDatabase,
    DatabaseError,
    MigrationError,
    DatabaseLockedError,
    OutboxEventRecord,
    OutboxStatus,
)

__all__ = [
    "CorpusAIStateDatabase",
    "DatabaseError",
    "MigrationError",
    "DatabaseLockedError",
    "OutboxEventRecord",
    "OutboxStatus",
]
