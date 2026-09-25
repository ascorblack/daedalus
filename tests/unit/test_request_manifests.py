from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from protocore.conformance.request_manifest import RequestManifestSinkConformance
from protocore.contracts.llm import LLMObservabilityContext, LLMRequest
from protocore.contracts.observability import build_request_manifest
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolDefinition, ToolParameterSchema

from daedalus.host.request_manifests import RequestManifestStore
from daedalus.stores.blobs import FileBlobStore
from daedalus.stores.database import Database


async def test_request_manifest_is_durable_and_large_values_are_by_reference(tmp_path) -> None:
    db = Database(tmp_path / "state.db")
    await db.open()
    try:
        await db.execute(
            "INSERT INTO projects(id, name, created_at, settings, system) VALUES ('project', 'p', 'now', '{}', '')"
        )
        await db.execute(
            "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES ('session', 'tenant', '', 'now', 'now', '{}', 'project')"
        )
        blobs = FileBlobStore(tmp_path / "blobs")
        store = RequestManifestStore(db, blobs, tenant_id="tenant")
        request = LLMRequest(
            model="model",
            messages=[Message(role=MessageRole.system, content_blocks=[TextBlock(text="rules")]), Message(role=MessageRole.user, content_blocks=[TextBlock(text="x" * 500)])],
            tools=[ToolDefinition(name="Read", description="read", parameters=ToolParameterSchema())],
            observability=LLMObservabilityContext(run_id="run", session_id="session", call_category="stream", call_purpose="test"),
        )
        manifest, bodies = build_request_manifest(
            request=request,
            attempt_scope="run/1",
            constants_sha256="a" * 64,
            inline_value_max_bytes=64,
        )

        await store.record_request_manifest(manifest=manifest, manifest_id=manifest.manifest_id, bodies=bodies)
        await store.record_request_manifest(manifest=manifest, manifest_id=manifest.manifest_id, bodies=bodies)

        rows = await db.fetchall("SELECT manifest FROM request_manifests")
        assert len(rows) == 1
        saved = json.loads(rows[0]["manifest"])
        assert saved["messages"]["inline"] is None
        assert await blobs.exists("tenant", saved["messages"]["blob_ref"])
        assert (await store.latest_for_session("session"))["request_sha256"] == manifest.request_sha256
    finally:
        await db.close()


async def test_request_manifest_cascades_with_session(tmp_path) -> None:
    db = Database(tmp_path / "state.db")
    await db.open()
    try:
        await db.execute(
            "INSERT INTO projects(id, name, created_at, settings, system) VALUES ('p', 'p', 'now', '{}', '')"
        )
        await db.execute(
            "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES ('s', 't', '', 'now', 'now', '{}', 'p')"
        )
        await db.execute(
            "INSERT INTO request_manifests(manifest_id, session_id, attempt_id, request_sha256, model, manifest, created_at) VALUES ('m', 's', 'a', 'd', 'model', '{}', 'now')"
        )
        await db.execute("DELETE FROM sessions WHERE id = 's'")
        assert await db.fetchone("SELECT 1 FROM request_manifests") is None
        plan = await db.fetchone("EXPLAIN QUERY PLAN SELECT * FROM request_manifests WHERE session_id = 's' ORDER BY seq DESC LIMIT 1")
        assert plan is not None and "request_manifests_by_session" in str(plan["detail"])
    finally:
        await db.close()


class TestTheStoreKeepsTheCoresSinkContract(RequestManifestSinkConformance):
    """The core's own conformance suite, run against this store.

    The store once took the manifest and bodies positionally while the core passed them by keyword
    together with `manifest_id`, so every call raised inside the core and no manifest was kept.
    """

    @pytest.fixture
    async def subject(self, tmp_path) -> AsyncIterator[RequestManifestStore]:
        db = Database(tmp_path / "state.db")
        await db.open()
        try:
            yield RequestManifestStore(db, FileBlobStore(tmp_path / "blobs"), tenant_id="tenant")
        finally:
            await db.close()
