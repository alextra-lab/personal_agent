"""Repository for the settings override store and its audit record (FRE-1722)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.service.models import SettingsEditAuditModel, SettingsOverrideModel


async def record_settings_edit(
    db: AsyncSession,
    *,
    parameter: str,
    action: Literal["set", "clear"],
    changed_by: UUID,
    old_value: JsonValue,
    new_value: JsonValue,
) -> None:
    """Add one audit row to the caller's transaction. The caller commits.

    Args:
        db: The session that holds the edit, so the edit and its record commit together.
        parameter: The settings menu row name.
        action: ``"set"`` for an edit, ``"clear"`` for a Clear.
        changed_by: The user who made the change.
        old_value: The value before the change, or ``None`` when no value applied.
        new_value: The value after the change, or ``None`` when no value applies.
    """
    db.add(
        SettingsEditAuditModel(
            changed_at=datetime.now(UTC),
            changed_by=changed_by,
            parameter=parameter,
            action=action,
            old_value=old_value,
            new_value=new_value,
        )
    )
    await db.flush()


class SettingsOverridesRepository:
    """CRUD for ``settings_overrides``. Each write commits with one audit row."""

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the repository with a database session.

        Args:
            db: Async SQLAlchemy session.
        """
        self.db = db

    async def list_all(self) -> dict[str, JsonValue]:
        """Return every stored override, keyed by parameter name."""
        result = await self.db.execute(
            select(SettingsOverrideModel.parameter, SettingsOverrideModel.value)
        )
        return {str(parameter): value for parameter, value in result.all()}

    async def get(self, parameter: str) -> JsonValue:
        """Return the stored value of one parameter, or ``None`` when no row exists.

        Args:
            parameter: The settings menu row name.
        """
        result = await self.db.execute(
            select(SettingsOverrideModel.value).where(SettingsOverrideModel.parameter == parameter)
        )
        value: JsonValue = result.scalar_one_or_none()
        return value

    async def set(
        self,
        *,
        parameter: str,
        value: JsonValue,
        changed_by: UUID,
        old_value: JsonValue,
    ) -> None:
        """Store an override and its audit row in one transaction.

        Args:
            parameter: The settings menu row name.
            value: The validated new value.
            changed_by: The user who made the edit.
            old_value: The value that applied before this edit.
        """
        now = datetime.now(UTC)
        await self.db.execute(
            pg_insert(SettingsOverrideModel)
            .values(parameter=parameter, value=value, updated_by=changed_by, updated_at=now)
            .on_conflict_do_update(
                index_elements=["parameter"],
                set_={"value": value, "updated_by": changed_by, "updated_at": now},
            )
        )
        await record_settings_edit(
            self.db,
            parameter=parameter,
            action="set",
            changed_by=changed_by,
            old_value=old_value,
            new_value=value,
        )
        await self.db.commit()

    async def clear(
        self,
        *,
        parameter: str,
        changed_by: UUID,
        new_value: JsonValue,
    ) -> bool:
        """Remove an override and write its audit row in one transaction.

        Args:
            parameter: The settings menu row name.
            changed_by: The user who pressed Clear.
            new_value: The value that applies after the row is gone.

        Returns:
            ``True`` when a row existed. With no row, nothing is written.
        """
        result = await self.db.execute(
            sa_delete(SettingsOverrideModel)
            .where(SettingsOverrideModel.parameter == parameter)
            .returning(SettingsOverrideModel.value)
        )
        row = result.first()
        if row is None:
            await self.db.rollback()
            return False
        await record_settings_edit(
            self.db,
            parameter=parameter,
            action="clear",
            changed_by=changed_by,
            old_value=row[0],
            new_value=new_value,
        )
        await self.db.commit()
        return True
