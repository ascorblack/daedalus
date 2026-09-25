"""Durable, content-addressed records of provider requests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from protocore.contracts.observability import RequestManifest

from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.database import Database


class RequestManifestStore:
    """Keep the exact request boundary without copying large prompt bodies into SQLite."""

    def __init__(self, db: Database, blobs: FileBlobStore, *, tenant_id: str) -> None:
        self.db = db
        self.blobs = blobs
        self.tenant_id = tenant_id

    async def record_request_manifest(
        self,
        *,
        manifest: RequestManifest,
        manifest_id: str,
        bodies: Mapping[str, bytes],
    ) -> None:
        # The signature is the core's sink contract, keyword for keyword. A narrower one made every
        # call raise a TypeError the core logs and swallows, so no manifest was recorded at all.
        # `manifest_id` repeats `manifest.manifest_id`; the stored id is the manifest's own.
        refs: dict[str, str] = {}
        for slot, body in bodies.items():
            meta = await self.blobs.put(
                self.tenant_id,
                body,
                content_type="application/json",
                metadata={"kind": "request_manifest", "slot": slot},
            )
            refs[slot] = meta.ref
        stored = manifest.with_blob_refs(refs)
        identity = dict(stored.identity)
        await self.db.execute(
            "INSERT INTO request_manifests(manifest_id, run_id, session_id, attempt_id, request_sha256, model, manifest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(manifest_id) DO NOTHING",
            (
                stored.manifest_id,
                identity.get("run_id"),
                identity.get("session_id"),
                stored.attempt_id,
                stored.request_sha256,
                stored.model,
                stored.model_dump_json(),
                datetime.now(UTC).isoformat(),
            ),
        )

    async def latest_for_session(self, session_id: str) -> dict[str, Any] | None:
        rows = await self.recent_for_session(session_id, limit=1)
        return rows[0] if rows else None

    async def recent_for_session(self, session_id: str, *, limit: int = 2) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT manifest FROM request_manifests WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
            (session_id, max(1, min(limit, 10))),
        )
        return [json.loads(row["manifest"]) for row in rows]


__all__ = ["RequestManifestStore"]
