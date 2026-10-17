"""Real PG16 backup/restore drill in two uniquely named disposable databases."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import uuid
from sqlalchemy import inspect, select, text, func
from sqlalchemy.engine import make_url
from .db import create_engine_for, init_db, session_scope
from .models import Source, Dataset, DatasetVersion, utcnow
from .models_assets import DownloadRequest, Asset
from .models_runtime import AgentRun
from .operations_backup import create_backup, restore_backup, _pg_create_fresh, _pg_environment, _path
from .operations import assert_schema_current


def run_drill(admin_url: str, destination: str | Path) -> dict:
    import psycopg
    from psycopg import sql
    from .agent_runtime import checkpoint_store, create_run
    from langgraph.graph import StateGraph, START, END
    base = make_url(admin_url)
    if base.get_backend_name() != "postgresql":
        raise ValueError("Restore drill requires PostgreSQL")
    root = _path(destination)
    if root.exists():
        raise ValueError("Drill directory must be new")
    token = uuid.uuid4().hex
    names = ("xiaoyi_drill_source_" + token, "xiaoyi_drill_restore_" + token)
    urls = [base.set(database=name) for name in names]
    engines = []
    root.mkdir(parents=True, exist_ok=False)
    try:
        _pg_create_fresh(urls[0], _pg_environment(urls[0]))
        source = create_engine_for(urls[0].render_as_string(hide_password=False))
        engines.append(source)
        init_db(source)
        assets = root / "source-assets"
        data = b"disposable backup drill asset\n"
        digest = hashlib.sha256(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest}"
        file = assets / key
        file.parent.mkdir(parents=True)
        file.write_bytes(data)
        with session_scope(source) as session:
            origin = Source(kind="manual", endpoint="https://invalid.example/drill")
            session.add(origin)
            session.flush()
            dataset = Dataset(source_id=origin.id, source_key="drill/fixture", fingerprint=digest)
            session.add(dataset)
            session.flush()
            version = DatasetVersion(dataset_id=dataset.id, revision="drill", metadata_json={"fixture": True})
            session.add(version)
            session.flush()
            request = DownloadRequest(dataset_version_id=version.id, requested_by="drill", status="completed",
                manifest={}, manifest_hash=digest, approved_hash=digest, max_bytes=len(data), actual_bytes=len(data))
            session.add(request)
            session.flush()
            session.add(Asset(dataset_version_id=version.id, download_request_id=request.id, path="fixture.txt",
                              sha256=digest, size_bytes=len(data), storage_key=key))
            run = create_run(session, {"goal": "Disposable database restore verification", "sources": ["manual"]}, "drill")
            run.status = "completed"
            run.finished_at = utcnow()
            run_id = run.id
        def graph(saver, pause=False):
            builder = StateGraph(dict)
            builder.add_node("first", lambda state: {"value": state["value"] + 1})
            builder.add_node("last", lambda state: {"value": state["value"] * 10})
            builder.add_edge(START, "first")
            builder.add_edge("first", "last")
            builder.add_edge("last", END)
            return builder.compile(checkpointer=saver, interrupt_after=["first"] if pause else [])
        config = {"configurable": {"thread_id": f"research-run-{run_id}"}}
        with checkpoint_store(source) as saver:
            graph(saver, pause=True).invoke({"value": 2}, config)
        backup = root / "backup"
        create_backup(urls[0].render_as_string(hide_password=False), assets, backup, quiesced=True)
        recovered_assets = root / "restored-assets"
        result = restore_backup(backup, urls[1].render_as_string(hide_password=False), recovered_assets, quiesced=True)
        restored = create_engine_for(urls[1].render_as_string(hide_password=False))
        engines.append(restored)
        assert_schema_current(restored)
        counts = []
        for engine in engines:
            with engine.connect() as connection:
                counts.append({name: connection.exec_driver_sql(
                    f"SELECT count(*) FROM {connection.dialect.identifier_preparer.quote(name)}").scalar()
                    for name in inspect(engine).get_table_names()})
        if counts[0] != counts[1] or (recovered_assets / key).read_bytes() != data:
            raise RuntimeError("Restored rows or asset bytes differ")
        with checkpoint_store(restored) as saver:
            if graph(saver).get_state(config).next != ("last",):
                raise RuntimeError("Checkpoint position was not restored")
            if graph(saver).invoke(None, config) != {"value": 30}:
                raise RuntimeError("Restored graph did not resume")
        with checkpoint_store(source) as saver:
            if graph(saver).get_state(config).next != ("last",):
                raise RuntimeError("Restore modified the original checkpoint")
        result.update(status="passed", table_counts=counts[0], checkpoint_resumed=True, asset_sha256=digest)
        (root / "drill-report.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        return result
    finally:
        for engine in engines:
            engine.dispose()
        # Names are generated above; never drop the administrator's configured database.
        with psycopg.connect(base.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False),
                             autocommit=True) as connection:
            for name in names:
                if name == base.database or not name.endswith(token):
                    raise RuntimeError("Unsafe drill cleanup target")
                connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create, restore and remove two disposable PostgreSQL test databases")
    parser.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    try:
        result = run_drill(os.environ["XIAOYI_CATALOG_DB"], args.destination)
        print(json.dumps(result))
        return 0
    except Exception:
        print("Restore drill failed. Original configured database was not modified; retain drill files for inspection.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
