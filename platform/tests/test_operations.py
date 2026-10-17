from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import uuid
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, select, text
from xiaoyi_catalog.db import create_engine_for, init_db, session_scope
from xiaoyi_catalog.models_operations import MigratedRun, WorkerHeartbeatRecord
from xiaoyi_catalog.operations import (OperationsError, WorkerHeartbeat, assert_schema_current,
    check_worker_health, readiness, record_worker_heartbeat)
from xiaoyi_catalog.operations_migrate import (MigrationError, SOURCE_REVISION, copy_catalog,
    inspect_source, is_archived_run, migrate_sqlite_backup)
from xiaoyi_catalog.operations_backup import create_backup

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


@pytest.fixture
def legacy_catalog(tmp_path):
    from xiaoyi_catalog import operations_migrate
    engine = create_engine_for(tmp_path / "source.sqlite")
    config = Config()
    config.set_main_option("script_location", str(Path(operations_migrate.__file__).parent / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, SOURCE_REVISION)
    metadata = MetaData()
    metadata.reflect(engine)
    metadata.remove(metadata.tables["alembic_version"])
    content, bundle = b"historical asset\n", b"historical immutable research bundle\n"
    asset_sha, bundle_sha = hashlib.sha256(content).hexdigest(), hashlib.sha256(bundle).hexdigest()
    storage = tmp_path / "source-assets"
    asset_key, bundle_key = f"sha256/{asset_sha[:2]}/{asset_sha}", f"bundles/{bundle_sha}.zip"
    for key, value in ((asset_key, content), (bundle_key, bundle)):
        path = storage / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    from xiaoyi_catalog.downloads import digest_json
    version_metadata = {"license": "mit", "rows": 2, "nested": {"original": "中文证据"}}
    manifest = {"dataset_version_id": 1, "revision": "a"*40, "metadata_hash": digest_json(version_metadata),
                "files": [{"path": "a.txt", "size": len(content)}]}
    overrides = {
        "source": {"kind": "manual", "endpoint": "https://example.test"},
        "dataset": {"source_key": "fixture/data", "modalities": ["text"], "task_categories": ["text-classification"],
                    "languages": ["zh"], "status": "active", "fingerprint": "f"*64},
        "dataset_version": {"revision": "a"*40, "metadata_json": version_metadata, "provenance_status": "verified", "kind": "source"},
        "run": {"status": "completed", "stats": {"rows": 2}},
        "enrichment": {"evidence_ids": [1], "value": {"historical": True}, "status": "confirmed"},
        "idea": {"source_refs": []},
        "pairing": {"evidence_ids": [1], "warnings": [], "status": "pending"},
        "snapshot": {"pairing_ids": [1], "content": {"original": "不可重写"}, "status": "approved"},
        "review": {"target_kind": "enrichment", "target_id": 1, "decision": "confirmed"},
        "agent_run": {"status": "waiting_approval", "stage": "human_review",
                      "payload": {"goal": "Historical research goal", "sources": ["manual"], "max_tool_calls": 20}, "result": {"proposal_ids": [1], "idea_id": 1},
                      "attempts": 1, "cancel_requested": False, "usage": {"total_tokens": 42}},
        "agent_tool_call": {"status": "completed", "call_key": "parse", "result": {"versions": [1]}},
        "download_request": {"status": "completed", "manifest": manifest,
            "manifest_hash": digest_json(manifest), "approved_hash": digest_json(manifest), "actual_bytes": len(content)},
        "asset": {"path": "a.txt", "sha256": asset_sha, "storage_key": asset_key, "size_bytes": len(content),
                  "parent_asset_ids": [], "recipe": {}},
        "research_bundle": {"storage_key": bundle_key, "sha256": bundle_sha, "manifest": {"original": True}},
        "vector_embedding": {"vector": [0.1, 0.2], "dimension": 2},
        "collection_schedule": {"enabled": False, "scope": {}, "next_run_at": NOW},
        "collection_job": {"status": "completed", "dataset_version_ids": [1], "enriched_version_ids": [1], "cancel_requested": False},
        "qa_turn": {"status": "completed", "observations": [{"historical": True}], "state": {"phase": "done"},
                    "answer": {"text": "历史回答"}, "cancel_requested": False},
        "worker_lease": {"name": "research_worker", "owner": "old-worker", "expires_at": NOW - timedelta(days=1)},
    }
    with engine.begin() as connection:
        for table in metadata.sorted_tables:
            row = {}
            for column in table.c:
                if column.primary_key:
                    row[column.name] = 1 if isinstance(column.type, sa.Integer) else "fixture"
                elif column.nullable:
                    row[column.name] = None
                elif isinstance(column.type, sa.JSON):
                    row[column.name] = {}
                elif isinstance(column.type, sa.Boolean):
                    row[column.name] = False
                elif isinstance(column.type, sa.DateTime):
                    row[column.name] = NOW
                elif isinstance(column.type, (sa.Integer, sa.Float)):
                    row[column.name] = 1
                else:
                    row[column.name] = "fixture"
                if column.foreign_keys and not column.nullable:
                    row[column.name] = 1
            row.update(overrides.get(table.name, {}))
            connection.execute(table.insert().values(**row))
    yield engine, storage
    engine.dispose()


def test_readiness_does_not_initialize_missing_schema(tmp_path):
    empty = create_engine_for(tmp_path / "empty.db")
    with pytest.raises(OperationsError):
        assert_schema_current(empty)
    assert sa.inspect(empty).get_table_names() == []
    empty.dispose()


def test_readiness_and_independent_idle_heartbeat(engine, tmp_path):
    assert readiness(engine, tmp_path)["status"] == "ready"
    with pytest.raises(OperationsError):
        readiness(engine, tmp_path / "missing")
    with pytest.raises(OperationsError):
        check_worker_health(engine, "cpu")
    with WorkerHeartbeat(engine, "cpu", "fixture-worker", interval=0.05):
        assert check_worker_health(engine, "cpu")["workers"] == 1
    with pytest.raises(OperationsError):
        check_worker_health(engine, "cpu")
    record_worker_heartbeat(engine, "io", "expired")
    with session_scope(engine) as session:
        session.get(WorkerHeartbeatRecord, "expired").seen_at = NOW - timedelta(days=100)
    with pytest.raises(OperationsError):
        check_worker_health(engine, "io")


def test_complete_legacy_history_copy_preserves_ids_json_and_approvals(legacy_catalog, engine):
    source, _ = legacy_catalog
    metadata, summaries = inspect_source(source)
    report = copy_catalog(source, engine, metadata, summaries, migration_id="test-migration",
                          source_sha256="a"*64, checkpoint_archive="/archive/checkpoints.sqlite")
    assert report["tables"] == summaries
    assert len(report["tables"]) == len(metadata.tables)
    assert report["backfills"] == {"profile_run_from_quality_report": 1}
    assert all(value["rows"] == 1 for value in summaries.values())
    with engine.connect() as connection:
        assert connection.execute(text("SELECT status FROM agent_run WHERE id=1")).scalar() == "waiting_approval"
        assert connection.execute(text("SELECT status FROM snapshot WHERE id=1")).scalar() == "approved"
        assert connection.execute(text("SELECT history_status FROM enrichment WHERE id=1")).scalar() == "legacy_unverified"
        assert connection.execute(text("SELECT history_status FROM review WHERE id=1")).scalar() == "legacy_unverified"
        assert json.loads(connection.execute(text("SELECT usage FROM agent_run WHERE id=1")).scalar())["total_tokens"] == 42
    with session_scope(engine) as session:
        from xiaoyi_catalog.models_assets import QualityReport
        from xiaoyi_catalog.models_samples import ProfileRun
        old_report = session.get(QualityReport, 1)
        profile = session.scalar(select(ProfileRun).where(ProfileRun.legacy_report_id == 1))
        assert profile is not None and profile.build_id is None
        assert profile.summary == old_report.value and profile.producer == old_report.producer
        assert profile.created_at == old_report.created_at and profile.updated_at == old_report.updated_at
        assert profile.manifest == {"legacy_asset_id": old_report.asset_id, "migration": "0010_samples"}
        assert is_archived_run(session, 1)
        assert session.get(MigratedRun, 1).migration_id == "test-migration"
    with pytest.raises(MigrationError, match="no business"):
        copy_catalog(source, engine, metadata, summaries, migration_id="again",
                     source_sha256="a"*64, checkpoint_archive="")


@pytest.mark.parametrize("mutation", [
    "CREATE TABLE unknown_source (id INTEGER)",
    "ALTER TABLE dataset ADD COLUMN unknown_column TEXT",
    "UPDATE agent_run SET status='running'",
    "UPDATE collection_schedule SET enabled=1",
    "UPDATE enrichment SET evidence_ids='[987654]'",
    "UPDATE download_request SET approved_hash='tampered'",
])
def test_migration_preflight_rejects_unsafe_sources(legacy_catalog, mutation):
    source, _ = legacy_catalog
    with source.begin() as connection:
        connection.exec_driver_sql(mutation)
    with pytest.raises(MigrationError):
        inspect_source(source)


def test_target_transfer_failure_rolls_back_all_rows(legacy_catalog, engine):
    source, _ = legacy_catalog
    metadata, summaries = inspect_source(source)
    summaries["dataset"]["sha256"] = "wrong"
    with pytest.raises(MigrationError, match="checksums"):
        copy_catalog(source, engine, metadata, summaries, migration_id="bad",
                     source_sha256="a"*64, checkpoint_archive="")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM source")).scalar() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM migration_record")).scalar() == 0


def test_deployment_migration_refuses_non_postgres_target(legacy_catalog, tmp_path):
    source, storage = legacy_catalog
    backup = create_backup(source.url.database, storage, tmp_path / "backup", quiesced=True).parent
    with pytest.raises(MigrationError, match="must be PostgreSQL"):
        migrate_sqlite_backup(backup, str(tmp_path / "target.db"), tmp_path / "assets",
                              tmp_path / "archive", quiesced=True)


def test_real_postgres_full_migration_and_sequences(legacy_catalog, tmp_path):
    import os
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url
    connection_url = os.getenv("XIAOYI_TEST_POSTGRES")
    if not connection_url:
        pytest.skip("requires disposable PostgreSQL with CREATEDB")
    source, storage = legacy_catalog
    # Include an actual LangGraph SQLite checkpoint in the archive.
    from xiaoyi_catalog.agent_runtime import checkpoint_store
    from langgraph.graph import START, END, StateGraph
    from typing import TypedDict
    class State(TypedDict):
        value: int
    builder = StateGraph(State)
    builder.add_node("once", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "once")
    builder.add_edge("once", END)
    with checkpoint_store(source) as saver:
        builder.compile(checkpointer=saver).invoke({"value": 1}, {"configurable": {"thread_id": "research-run-1"}})
    backup = create_backup(source.url.database, storage, tmp_path / "backup", quiesced=True).parent
    database = "migration_test_" + uuid.uuid4().hex
    url = make_url(connection_url).set(database=database)
    report = None
    try:
        report = migrate_sqlite_backup(backup, url.render_as_string(hide_password=False),
            tmp_path / "pg-assets", tmp_path / "archive", quiesced=True)
        assert report["asset_files"] == 2
        assert set(report["archive_files"]) == {"database.sqlite", "checkpoints.sqlite"}
        target = create_engine_for(url.render_as_string(hide_password=False))
        try:
            with target.begin() as connection:
                assert connection.execute(text("SELECT COUNT(*) FROM review")).scalar() == 1
                assert connection.execute(text("SELECT pg_typeof(vector)::text FROM vector_embedding")).scalar() == "vector"
                new_id = connection.execute(text("INSERT INTO qa_conversation(actor,title,created_at,updated_at) VALUES ('new','new',now(),now()) RETURNING id")).scalar()
                assert new_id == 2
        finally:
            target.dispose()
    finally:
        admin_url = make_url(connection_url).set(drivername="postgresql", database="postgres")
        with psycopg.connect(admin_url.render_as_string(hide_password=False), autocommit=True) as connection:
            # Only the test-generated UUID database is ever removed.
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))

def test_self_references_preserve_original_timestamps(legacy_catalog, engine):
    source, _ = legacy_catalog
    metadata = MetaData()
    metadata.reflect(source)
    versions = metadata.tables["dataset_version"]
    with source.begin() as connection:
        original = dict(connection.execute(select(versions).where(versions.c.id == 1)).mappings().one())
        second = {**original, "id": 2, "revision": "b" * 40}
        connection.execute(versions.insert(), second)
        # A valid source can reference a later primary key; transfer must not rely on ID ordering.
        connection.execute(versions.update().where(versions.c.id == 1).values(parent_version_id=2))
    metadata, summaries = inspect_source(source)
    copy_catalog(source, engine, metadata, summaries, migration_id="self-reference",
                 source_sha256="a"*64, checkpoint_archive="")
    with engine.connect() as connection:
        row = connection.execute(text("SELECT parent_version_id, updated_at FROM dataset_version WHERE id=1")).one()
        assert row.parent_version_id == 2
        assert str(row.updated_at).startswith("2026-10-18")


def test_readonly_backup_preflight_does_not_change_original(legacy_catalog, tmp_path):
    from xiaoyi_catalog.operations_migrate import inspect_backup, main
    source, storage = legacy_catalog
    backup = create_backup(source.url.database, storage, tmp_path / "backup", quiesced=True).parent
    before = {path.relative_to(backup): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in backup.rglob("*") if path.is_file()}
    report = inspect_backup(backup)
    assert report["tables"]["review"]["rows"] == 1
    assert main(["--backup", str(backup), "--check-only"]) == 0
    after = {path.relative_to(backup): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in backup.rglob("*") if path.is_file()}
    assert before == after


def test_backup_verify_cli_is_readonly(legacy_catalog, tmp_path, capsys):
    from xiaoyi_catalog.operations_backup import main
    source, storage = legacy_catalog
    backup = create_backup(source.url.database, storage, tmp_path / "backup", quiesced=True).parent
    assert main(["verify", "--backup", str(backup)]) == 0
    assert json.loads(capsys.readouterr().out)["files_verified"] == 3

def test_archived_failed_run_restart_keeps_old_record_and_approval_scope(legacy_catalog, engine):
    from xiaoyi_catalog.agent_runtime import retry_run
    from xiaoyi_catalog.models_runtime import AgentRun, AgentEvent, ToolCall
    from xiaoyi_catalog.models_assets import DownloadRequest
    source, _ = legacy_catalog
    with source.begin() as connection:
        connection.execute(text("UPDATE agent_run SET status='failed', attempts=3, tool_calls=20 WHERE id=1"))
    metadata, summaries = inspect_source(source)
    copy_catalog(source, engine, metadata, summaries, migration_id="restart",
                 source_sha256="a"*64, checkpoint_archive="/archive/checkpoints.sqlite")
    with session_scope(engine) as session:
        old = session.get(AgentRun, 1)
        old_payload, old_result = dict(old.payload), dict(old.result)
        approval = session.get(DownloadRequest, 1).approved_hash
        replacement = retry_run(session, 1, actor=old.actor)
        session.flush()
        assert replacement.id != 1 and replacement.status == "queued"
        assert replacement.attempts == 0 and replacement.tool_calls == 0 and replacement.result == {}
        assert old.status == "failed" and old.attempts == 3 and old.tool_calls == 20
        assert old.payload == old_payload and old.result == old_result
        assert session.get(DownloadRequest, 1).approved_hash == approval
        assert session.scalar(select(sa.func.count()).select_from(DownloadRequest)) == 1
        assert not session.scalars(select(ToolCall).where(ToolCall.run_id == replacement.id)).all()
        events = session.scalars(select(AgentEvent).where(AgentEvent.kind.in_(["migration_restart", "replacement_created"]))).all()
        assert {event.kind for event in events} == {"migration_restart", "replacement_created"}


def test_queue_metrics_distinguish_ready_delayed_expired_and_stopped(engine):
    from xiaoyi_catalog.models_jobs import Job
    from xiaoyi_catalog.operations import queue_status
    with session_scope(engine) as session:
        for index, (queue, status, available, lease) in enumerate([
            ("interactive", "queued", NOW - timedelta(seconds=45), None),
            ("interactive", "queued", NOW + timedelta(seconds=90), None),
            ("io", "running", NOW, NOW - timedelta(seconds=1)),
            ("cpu", "failed", NOW, None),
        ]):
            session.add(Job(kind="fixture", queue=queue, actor="fixture", request_key=str(index),
                payload={}, payload_hash="a"*64, status=status, available_at=available, lease_until=lease))
        session.add(WorkerHeartbeatRecord(owner="live", queue="interactive", started_at=NOW, seen_at=NOW))
        session.add(WorkerHeartbeatRecord(owner="old", queue="io", started_at=NOW, seen_at=NOW - timedelta(seconds=70)))
        session.add(WorkerHeartbeatRecord(owner="stopped", queue="cpu", started_at=NOW, seen_at=NOW, stopped_at=NOW))
    with session_scope(engine) as session:
        result = queue_status(session, now=NOW)
    assert result["interactive"]["queued"] == 2
    assert result["interactive"]["waiting"] == result["interactive"]["delayed"] == 1
    assert result["interactive"]["oldest_wait_seconds"] == 45
    assert result["interactive"]["heartbeat"]["healthy"]
    assert result["io"]["expired_running"] == 1
    assert not result["io"]["heartbeat"]["healthy"]
    assert result["cpu"]["failed"] == 1
    assert result["cpu"]["oldest_wait_seconds"] is None
    assert not result["cpu"]["heartbeat"]["healthy"]


def test_operations_api_keeps_legacy_fields_and_separates_approval(engine, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from xiaoyi_catalog.service import create_app
    from xiaoyi_catalog.agent_runtime import create_run
    monkeypatch.setenv("XIAOYI_STORAGE_ROOT", str(tmp_path))
    with session_scope(engine) as session:
        run = create_run(session, {"goal": "Waiting for a human decision"}, "fixture")
        run.status = "waiting_approval"
    with TestClient(create_app(engine.url.database, dev_auth=True)) as client:
        response = client.get("/api/operations")
        assert response.status_code == 200
        data = response.json()
        assert {"tasks", "downloads", "review_backlog", "asset_bytes", "disk_free_bytes"} <= data.keys()
        assert data["approval_waiting"]["research_runs"] == 1
        assert data["queues"]["interactive"]["queued"] == 0
        assert set(data["queues"]) == {"interactive", "io", "cpu"}
