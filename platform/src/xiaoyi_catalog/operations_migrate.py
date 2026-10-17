"""Offline, all-record SQLite (0006) -> fresh PostgreSQL migration.

Only verified quiesced backups are accepted. The SQLite graph checkpoint is
archived, never translated or silently replayed. No source database is opened RW.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
import uuid
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, inspect, select, text, update, func
from .db import create_engine_for, init_db, register_models
from .models import Base, utcnow
from .models_operations import MigratedRun, MigrationRecord
from .operations_backup import (
    BackupError, _copy_exclusive, _database, _hash, _path, _pg_create_fresh,
    _pg_environment, _quiesced, _relative, verify_backup,
)

SOURCE_REVISION = "0006_catalog_qa"


class MigrationError(ValueError):
    pass


def is_archived_run(session, run_id: int) -> bool:
    return session.get(MigratedRun, run_id) is not None


def _baseline_metadata() -> MetaData:
    """Derive the exact supported input schema from frozen Alembic revisions."""
    engine = create_engine("sqlite:///:memory:")
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, SOURCE_REVISION)
        metadata = MetaData()
        metadata.reflect(engine)
        metadata.remove(metadata.tables["alembic_version"])
        return metadata
    finally:
        engine.dispose()


def _read_only_engine(path: Path):
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(
        path.as_uri() + "?mode=ro", uri=True))


def _canonical(value):
    if isinstance(value, datetime):
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return value.isoformat(timespec="microseconds")
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if hasattr(value, "tolist"):
        return _canonical(value.tolist())
    if isinstance(value, float) and not math.isfinite(value):
        raise MigrationError("Non-finite numeric values are not portable")
    return value


def _row_digest(row) -> bytes:
    values = dict(row)
    if "vector" in values and values["vector"] is not None:
        # pgvector uses float32. Preserve original float64 JSON in source.sqlite;
        # compare the documented float32 representation on both sides.
        values["vector"] = [struct.unpack("!f", struct.pack("!f", float(v)))[0] for v in values["vector"]]
    return (json.dumps(_canonical(values), sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _table_summary(connection, table, columns=None):
    names = list(columns or table.c.keys())
    count, digest = 0, hashlib.sha256()
    statement = select(*(table.c[name] for name in names)).order_by(*table.primary_key.columns)
    for row in connection.execution_options(stream_results=True).execute(statement).mappings():
        digest.update(_row_digest(row))
        count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def inspect_source(engine) -> tuple[MetaData, dict]:
    baseline = _baseline_metadata()
    found = set(inspect(engine).get_table_names())
    expected = set(baseline.tables) | {"alembic_version"}
    if found != expected:
        raise MigrationError(f"Source tables differ from the supported baseline: unknown={sorted(found-expected)}, missing={sorted(expected-found)}")
    metadata = MetaData()
    metadata.reflect(engine)
    for name, table in baseline.tables.items():
        if set(metadata.tables[name].c.keys()) != set(table.c.keys()):
            raise MigrationError(f"Unexpected source columns in {name}")
    with engine.connect() as connection:
        revisions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        if revisions != [SOURCE_REVISION]:
            raise MigrationError(f"Only a backup at {SOURCE_REVISION} is supported")
        if connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
            raise MigrationError("Source SQLite integrity check failed")
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
            raise MigrationError("Source contains broken foreign keys")
        active_states = {
            "agent_run": ("queued", "running"),
            "qa_turn": ("queued", "running"),
            "collection_job": ("queued", "running"),
            "run": ("running",),
            "enrichment": ("generating",),
            "download_request": ("approved", "running"),
        }
        for name, states in active_states.items():
            table = metadata.tables[name]
            if connection.execute(select(table.c.id).where(table.c.status.in_(states)).limit(1)).first():
                raise MigrationError(f"Drain active work before migration: {name}")
        schedule = metadata.tables["collection_schedule"]
        if connection.execute(select(schedule.c.id).where(schedule.c.enabled.is_(True)).limit(1)).first():
            raise MigrationError("Pause collection schedules before migration")
        _check_json_references(connection, metadata)
        summaries = {name: _table_summary(connection, metadata.tables[name]) for name in sorted(baseline.tables)}
    metadata.remove(metadata.tables["alembic_version"])
    return metadata, summaries


def _check_json_references(connection, metadata):
    checks = (
        ("enrichment", "evidence_ids", "evidence"),
        ("pairing", "evidence_ids", "evidence"),
        ("snapshot", "pairing_ids", "pairing"),
        ("asset", "parent_asset_ids", "asset"),
        ("collection_job", "dataset_version_ids", "dataset_version"),
        ("collection_job", "enriched_version_ids", "dataset_version"),
    )
    # Source legacy records are catalog records, not the new million-row sample index.
    ids = {}
    def exists(target, ident):
        if target not in ids:
            ids[target] = set(connection.execute(select(metadata.tables[target].c.id)).scalars())
        return isinstance(ident, int) and not isinstance(ident, bool) and ident in ids[target]
    for name, column, target in checks:
        for row in connection.execute(select(metadata.tables[name].c[column])):
            if not isinstance(row[0], list) or any(not exists(target, ident) for ident in row[0]):
                raise MigrationError(f"Broken JSON references in {name}.{column}")
    for row in connection.execute(select(metadata.tables["review"])).mappings():
        if row["target_kind"] in {"enrichment", "pairing"} and not exists(row["target_kind"], row["target_id"]):
            raise MigrationError("Review references a missing historical object")
    for result in connection.execute(select(metadata.tables["agent_run"].c.result)).scalars():
        if not isinstance(result, dict):
            raise MigrationError("Invalid agent result")
        if any(not exists("pairing", ident) for ident in result.get("proposal_ids", [])):
            raise MigrationError("Agent result references a missing proposal")
        if result.get("idea_id") is not None and not exists("idea", result["idea_id"]):
            raise MigrationError("Agent result references a missing idea")
    from .downloads import digest_json
    for row in connection.execute(select(metadata.tables["download_request"])).mappings():
        if row["manifest_hash"] != digest_json(row["manifest"]):
            raise MigrationError("Acquisition manifest hash mismatch")
        if row["approved_hash"] and row["approved_hash"] != row["manifest_hash"]:
            raise MigrationError("Approved acquisition scope differs from its manifest")
        if row["manifest"].get("dataset_version_id") != row["dataset_version_id"]:
            raise MigrationError("Acquisition manifest references a different version")


def _verify_registered_assets(source, metadata, backup_root):
    with source.connect() as connection:
        for name in ("asset", "research_bundle"):
            for row in connection.execute(select(metadata.tables[name])).mappings():
                key = _relative(row["storage_key"])
                namespace = "sha256/" if name == "asset" else "bundles/"
                if not key.startswith(namespace):
                    raise MigrationError("Registered asset is outside its storage namespace")
                file = backup_root / "assets" / key
                if not file.is_file() or _hash(file) != row["sha256"]:
                    raise MigrationError("Registered asset is absent or its checksum differs")
                if name == "asset" and file.stat().st_size != row["size_bytes"]:
                    raise MigrationError("Registered asset size differs")


def copy_catalog(source, target, metadata: MetaData, summaries: dict, *, migration_id: str,
                 source_sha256: str, checkpoint_archive: str) -> dict:
    """Transactional typed row transfer; SQLite targets are supported for unit tests only."""
    register_models()
    # Target schema was created by Alembic, never metadata.create_all.
    # Reflect server defaults: new trust fields must remain legacy_unverified,
    # and self-reference repair must not trigger ORM updated_at onupdate hooks.
    target_metadata = MetaData()
    target_metadata.reflect(target)
    tables = {name: target_metadata.tables[name] for name in metadata.tables}
    actual = inspect(target)
    for name, source_table in metadata.tables.items():
        if not set(source_table.c.keys()).issubset(set(tables[name].c.keys())):
            raise MigrationError(f"Target cannot represent all source columns: {name}")
        if not set(source_table.c.keys()).issubset({c["name"] for c in actual.get_columns(name)}):
            raise MigrationError(f"Target schema is incomplete: {name}")
    with source.connect() as src, target.begin() as dst:
        # A fresh DB is mandatory. Also reject records in new subsystem tables.
        for name in inspect(dst).get_table_names():
            if name == "alembic_version":
                continue
            quoted = dst.dialect.identifier_preparer.quote(name)
            if dst.exec_driver_sql(f"SELECT 1 FROM {quoted} LIMIT 1").first():
                raise MigrationError("Target database must contain no business or runtime records")
        for table in metadata.sorted_tables:
            target_table = tables[table.name]
            self_references = [fk.parent.name for fk in table.foreign_keys if fk.column.table.name == table.name]
            for field in self_references:
                if not target_table.c[field].nullable:
                    raise MigrationError("Cannot defer a non-nullable self-reference")
            statement = select(table).order_by(*table.primary_key.columns)
            result = src.execution_options(stream_results=True).execute(statement).mappings()
            while rows := result.fetchmany(500):
                records = [dict(row) for row in rows]
                for record in records:
                    for field in self_references:
                        record[field] = None
                dst.execute(target_table.insert(), records)
            for field in self_references:
                for row in src.execute(select(*table.primary_key.columns, table.c[field]).where(table.c[field].is_not(None))).mappings():
                    condition = [target_table.c[col.name] == row[col.name] for col in table.primary_key.columns]
                    dst.execute(update(target_table).where(*condition).values({field: row[field]}))
        verified = {name: _table_summary(dst, tables[name], metadata.tables[name].c.keys())
                    for name in sorted(metadata.tables)}
        if verified != summaries:
            raise MigrationError("Target table checksums differ; row transfer rolled back")
        # The target reached 0010 before legacy rows were imported. Apply the
        # same additive quality_report backfill here; source rows remain intact.
        profiles = target_metadata.tables["profile_run"]
        old_reports = tables["quality_report"]
        result = dst.execute(select(old_reports).order_by(old_reports.c.id)).mappings()
        profile_count = 0
        while batch := result.fetchmany(500):
            records = [dict(build_id=None, legacy_report_id=row["id"], producer=row["producer"],
                status="completed", manifest={"legacy_asset_id": row["asset_id"], "migration": "0010_samples"},
                summary=row["value"], created_at=row["created_at"], updated_at=row["updated_at"])
                for row in batch]
            dst.execute(profiles.insert(), records)
            profile_count += len(records)
        if profile_count != summaries["quality_report"]["rows"]:
            raise MigrationError("Historical profile backfill is incomplete; row transfer rolled back")
        # Expire operational ownership only after exact historical equality is proven.
        lease = tables["worker_lease"]
        dst.execute(update(lease).values(expires_at=datetime(1970, 1, 1, tzinfo=timezone.utc)))
        if target.dialect.name == "postgresql":
            for table in tables.values():
                for col in table.primary_key.columns:
                    if col.name == "id" and getattr(col.type, "python_type", None) is int:
                        sequence = dst.execute(text("SELECT pg_get_serial_sequence(:table, :column)"),
                                               {"table": table.name, "column": col.name}).scalar()
                        if sequence:
                            maximum = dst.execute(select(func.max(col))).scalar()
                            dst.execute(text("SELECT setval(CAST(:sequence AS regclass), :value, :called)"),
                                        {"sequence": sequence, "value": max(1, maximum or 0), "called": maximum is not None})
        report = {"tables": verified, "source_revision": SOURCE_REVISION,
                  "checkpoint_policy": "archived; old research tasks cannot retry in place",
                  "backfills": {"profile_run_from_quality_report": profile_count},
                  "adjustments": ["worker_lease.expires_at reset after exact row verification"]}
        dst.execute(MigrationRecord.__table__.insert().values(id=migration_id, source_revision=SOURCE_REVISION,
            source_sha256=source_sha256, checkpoint_archive=checkpoint_archive, report=report, created_at=utcnow()))
        run_ids = src.execute(select(metadata.tables["agent_run"].c.id)).scalars()
        batch = []
        for ident in run_ids:
            batch.append({"run_id": ident, "migration_id": migration_id, "checkpoint_policy": "archived"})
            if len(batch) == 500:
                dst.execute(MigratedRun.__table__.insert(), batch)
                batch = []
        if batch:
            dst.execute(MigratedRun.__table__.insert(), batch)
    return report


def migrate_sqlite_backup(backup: str | Path, target_db: str, storage: str | Path,
                          archive: str | Path, *, quiesced: bool = False) -> dict:
    _quiesced(quiesced)
    manifest = verify_backup(backup)
    if manifest["database"]["kind"] != "sqlite":
        raise MigrationError("This command only migrates SQLite; use same-backend restore for PostgreSQL")
    kind, url = _database(target_db)
    if kind != "postgresql":
        raise MigrationError("The deployment migration target must be PostgreSQL")
    root, assets, archived = _path(backup), _path(storage), _path(archive)
    for left, right in ((root, assets), (root, archived), (assets, archived)):
        if left == right or left.is_relative_to(right) or right.is_relative_to(left):
            raise MigrationError("Backup, asset and archive directories must be separate")
    if assets.exists() or archived.exists():
        raise MigrationError("Target asset and archive paths must not exist")
    source_file = root / "database.sqlite"
    source = _read_only_engine(source_file)
    target = None
    try:
        metadata, summaries = inspect_source(source)
        _verify_registered_assets(source, metadata, root)
        source_sha = _hash(source_file)
        _pg_create_fresh(url, _pg_environment(url))
        target = create_engine_for(target_db)
        init_db(target)
        assets.mkdir(parents=True, exist_ok=False)
        archived.mkdir(parents=True, exist_ok=False)
        for relative in ("database.sqlite", manifest["database"].get("checkpoint")):
            if relative:
                destination = archived / relative
                _copy_exclusive(root / relative, destination)
                if _hash(destination) != _hash(root / relative):
                    raise MigrationError("Archive checksum mismatch")
                destination.chmod(0o444)
        for item in manifest["files"]:
            if item["path"].startswith("assets/"):
                destination = assets / item["path"][len("assets/"):]
                _copy_exclusive(root / item["path"], destination)
                if _hash(destination) != item["sha256"]:
                    raise MigrationError("Copied asset checksum mismatch")
        if _hash(source_file) != source_sha:
            raise MigrationError("Source backup changed during migration")
        ident = uuid.uuid4().hex
        checkpoint = str(archived / "checkpoints.sqlite") if manifest["database"].get("checkpoint") else ""
        report = copy_catalog(source, target, metadata, summaries, migration_id=ident,
                              source_sha256=source_sha, checkpoint_archive=checkpoint)
        report.update(migration_id=ident, source_sha256=source_sha,
                      archive_files={p.name: _hash(p) for p in archived.iterdir() if p.is_file()},
                      asset_files=sum(item["path"].startswith("assets/") for item in manifest["files"]))
        report_file = archived / "migration-report.json"
        with report_file.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
        report_file.chmod(0o444)
        return report
    finally:
        source.dispose()
        if target is not None:
            target.dispose()


def inspect_backup(backup: str | Path) -> dict:
    manifest = verify_backup(backup)
    if manifest["database"]["kind"] != "sqlite":
        raise MigrationError("Preflight requires a SQLite backup")
    root = _path(backup)
    source = _read_only_engine(root / "database.sqlite")
    try:
        metadata, summaries = inspect_source(source)
        _verify_registered_assets(source, metadata, root)
        return {"source_revision": SOURCE_REVISION, "tables": summaries,
                "source_sha256": _hash(root / "database.sqlite"),
                "checkpoint_archived": bool(manifest["database"].get("checkpoint"))}
    finally:
        source.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Migrate a verified SQLite 0006 backup to a new PostgreSQL database")
    parser.add_argument("--backup", required=True)
    parser.add_argument("--storage")
    parser.add_argument("--archive")
    parser.add_argument("--check-only", action="store_true", help="Verify all source records and files without writing anything")
    parser.add_argument("--quiesced", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.check_only:
            print(json.dumps(inspect_backup(args.backup), ensure_ascii=False))
            return 0
        if not args.storage or not args.archive:
            raise MigrationError("--storage and --archive are required for migration")
        target = os.environ.get("XIAOYI_MIGRATION_TARGET_DB")
        if not target:
            raise MigrationError("Set XIAOYI_MIGRATION_TARGET_DB; never pass credentials on the command line")
        result = migrate_sqlite_backup(args.backup, target, args.storage, args.archive, quiesced=args.quiesced)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (MigrationError, BackupError, OSError) as exc:
        print(str(exc))
        return 2
    except Exception:
        print("Migration failed; retain the original backup and partial target for inspection. Use a new target to retry.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
