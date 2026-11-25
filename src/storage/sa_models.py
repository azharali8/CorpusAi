"""
SQLAlchemy 2.0 Declarative ORM Mappings for CorpusAI Deposit Storage.

Maps exactly to existing V1 SQLite tables:
- deposit_records
- deposit_artifacts

Guarantees 100% schema parity without creating separate tables or migrations.
"""

from typing import List, Optional
from sqlalchemy import (
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    Text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    """Base class for SQLAlchemy 2.0 declarative models."""
    pass


class DepositRecordORM(Base):
    """
    SQLAlchemy mapping for `deposit_records` table.

    Matches schema in src/storage/schema.py exactly.
    """
    __tablename__ = "deposit_records"
    __table_args__ = (
        PrimaryKeyConstraint("portal_id", "deposit_id"),
    )

    portal_id: Mapped[str] = mapped_column(Text, nullable=False)
    deposit_id: Mapped[str] = mapped_column(Text, nullable=False)
    package_id: Mapped[str] = mapped_column(Text, nullable=False)
    target_repository: Mapped[str] = mapped_column(Text, nullable=False)
    deposition_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    doi: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    concept_doi: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    badge_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[str] = mapped_column(Text, nullable=False)
    completed_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # One-to-many relationship with cascade delete matching SQLite FK
    artifacts: Mapped[List["DepositArtifactORM"]] = relationship(
        "DepositArtifactORM",
        back_populates="deposit_record",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class DepositArtifactORM(Base):
    """
    SQLAlchemy mapping for `deposit_artifacts` table.

    Matches schema in src/storage/schema.py exactly.
    """
    __tablename__ = "deposit_artifacts"
    __table_args__ = (
        PrimaryKeyConstraint("portal_id", "deposit_id", "artifact_filename"),
        ForeignKeyConstraint(
            ["portal_id", "deposit_id"],
            ["deposit_records.portal_id", "deposit_records.deposit_id"],
            ondelete="CASCADE",
        ),
    )

    portal_id: Mapped[str] = mapped_column(Text, nullable=False)
    deposit_id: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_filename: Mapped[str] = mapped_column(Text, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256_checksum: Mapped[str] = mapped_column(Text, nullable=False)
    remote_file_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    upload_status: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    deposit_record: Mapped["DepositRecordORM"] = relationship(
        "DepositRecordORM",
        back_populates="artifacts",
    )
