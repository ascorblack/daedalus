"""Durable memory and workspace stores.

The core ships full behavioural implementations of ``IMemory`` and ``IWorkspace``
that live in process memory. For a single-user bot that behaviour is exactly
right; what is missing is durability. These subclasses load the records from
SQLite at start and write every mutation back, so the ranking, dedup and quota
semantics stay the core's own.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from protocore.contracts.memory import MemoryRecord, MemoryScope, MemoryWriteResult
from protocore.contracts.workspace import WorkspaceUnit, WorkspaceWriteOutcome
from protocore.tests_support.adapters import InMemoryMemory, InMemoryWorkspace

from daedalus.stores.database import Database


class PersistentMemory(InMemoryMemory):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db

    async def load(self) -> None:
        rows = await self._db.fetchall("SELECT tenant_id, id, record FROM memory_records")
        for row in rows:
            record = MemoryRecord.model_validate(json.loads(row["record"]))
            self._store[(row["tenant_id"], row["id"])] = record
        self._counter = len(rows)

    async def _persist(self, record: MemoryRecord) -> None:
        await self._db.execute(
            "INSERT INTO memory_records(tenant_id, id, record) VALUES (?, ?, ?)"
            " ON CONFLICT(tenant_id, id) DO UPDATE SET record = excluded.record",
            (record.tenant_id, record.id, record.model_dump_json()),
        )

    async def write(self, tenant_id: str, scope: MemoryScope, scope_key: str, text: str, **kwargs: Any) -> MemoryWriteResult:  # type: ignore[override]
        result = await super().write(tenant_id, scope, scope_key, text, **kwargs)
        await self._persist(result.record)
        return result

    async def delete(self, tenant_id: str, memory_id: str, **kwargs: Any) -> bool:  # type: ignore[override]
        deleted = await super().delete(tenant_id, memory_id, **kwargs)
        if deleted:
            await self._db.execute(
                "DELETE FROM memory_records WHERE tenant_id = ? AND id = ?", (tenant_id, memory_id)
            )
        return deleted

    async def recall(self, tenant_id: str, query: str, **kwargs: Any):  # type: ignore[override]
        hits = await super().recall(tenant_id, query, **kwargs)
        for hit in hits:
            await self._persist(hit.record)
        return hits


class PersistentWorkspace(InMemoryWorkspace):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db

    @staticmethod
    def _row_key(key: tuple[str, str, str, str]) -> str:
        return json.dumps(list(key))

    async def load(self) -> None:
        rows = await self._db.fetchall("SELECT key, unit FROM workspace_units")
        for row in rows:
            key = tuple(json.loads(row["key"]))
            self._store[key] = WorkspaceUnit.model_validate(json.loads(row["unit"]))  # type: ignore[index]
        self._counter = len(rows)

    async def _sync_all(self) -> None:
        """Rewrite the table from the in-memory state (units are few and small)."""
        await self._db.execute("DELETE FROM workspace_units")
        await self._db.executemany(
            "INSERT INTO workspace_units(key, unit) VALUES (?, ?)",
            [(self._row_key(k), v.model_dump_json()) for k, v in self._store.items()],
        )

    async def write(self, *args: Any, **kwargs: Any) -> WorkspaceWriteOutcome:  # type: ignore[override]
        outcome = await super().write(*args, **kwargs)
        await self._sync_all()
        return outcome

    async def delete(self, *args: Any, **kwargs: Any) -> bool:  # type: ignore[override]
        deleted = await super().delete(*args, **kwargs)
        if deleted:
            await self._sync_all()
        return deleted

    async def clear_scope(self, *args: Any, **kwargs: Any) -> int:  # type: ignore[override]
        removed = await super().clear_scope(*args, **kwargs)
        await self._sync_all()
        return removed


__all__ = ["PersistentMemory", "PersistentWorkspace"]


def _unused(_: Sequence[Any]) -> None:  # keeps Sequence imported for type readers
    return None
