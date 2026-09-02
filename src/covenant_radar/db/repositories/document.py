"""Scoped repository operations for uploaded document metadata."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.orm import Session

from covenant_radar.db.models.document import Document
from covenant_radar.db.repositories.base import RepositoryAuditWriter, RepositoryBase
from covenant_radar.db.scoping import Scope, ownership_path_for


class DocumentRepository(RepositoryBase[Document]):
    """Repository whose every document read is constrained to a portfolio."""

    def __init__(self, session: Session, *, audit: RepositoryAuditWriter | None = None) -> None:
        super().__init__(session, Document, ownership=ownership_path_for(Document), audit=audit)

    def by_content_hash(
        self,
        borrower_id: UUID,
        content_hash: str,
        *,
        scope: Scope,
    ) -> Document | None:
        """Return an in-scope document for one borrower and exact hash."""
        return self.find(
            scope=scope,
            borrower_id=borrower_id,
            content_hash=content_hash,
        )

    def by_storage_key(self, storage_key: str, *, scope: Scope) -> Document | None:
        """Return the in-scope metadata row for one storage key."""
        return self.find(scope=scope, storage_key=storage_key)

    def for_borrower(
        self,
        borrower_id: UUID,
        *,
        scope: Scope,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[Document]:
        """List one borrower's documents in stable upload order."""
        if not isinstance(borrower_id, UUID):
            raise TypeError("Document borrower_id must be a UUID.")
        if offset < 0:
            raise ValueError("Document list offset cannot be negative.")
        if limit is not None and not 1 <= limit <= 200:
            raise ValueError("Document list limit must be between 1 and 200.")
        statement: Select[tuple[Document]] = cast(
            Select[tuple[Document]], self._scoped_select(scope)
        )
        statement = statement.where(Document.borrower_id == borrower_id)
        statement = statement.order_by(Document.created_at, Document.id).offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        return tuple(self.session.execute(statement).scalars().all())


__all__ = ["DocumentRepository"]
