"""Real PG16 backup/restore drill in two uniquely named disposable databases."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import json
import os
import subprocess
import sys
from types import SimpleNamespace
import zipfile
from pathlib import Path
import uuid
from sqlalchemy import inspect, select, func
from sqlalchemy.engine import make_url
from .db import create_engine_for, init_db, session_scope
from .models import Audit, DatasetVersion, utcnow
from .models_assets import Asset, ResearchBundle
from .operations_backup import create_backup, restore_backup, _pg_create_fresh, _pg_environment, _path
from .operations import assert_schema_current


@contextmanager
def _fixture_storage(root: Path):
    """Scope all fixture writes to the new drill directory, restoring caller settings."""
    values = {"XIAOYI_STORAGE_ROOT": str(root / "source-assets"),
              "XIAOYI_IMPORT_ROOT": str(root / "source-import")}
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _fixture_actor(engine, token: str, roles):
    # Exercise the same persisted-session and human-role check as the API. These
    # disposable fixture identities never grant authority in the configured DB.
    from starlette.requests import Request
    from .auth import require
    app = SimpleNamespace(state=SimpleNamespace(engine=engine, dev_auth=False))
    request = Request({"type": "http", "app": app,
                       "headers": [(b"authorization", ("Bearer " + token).encode())]})
    return require(request, roles, human=True)


def _create_bundle_fixture(engine, root: Path) -> dict:
    """Create real approved inputs and a completed bundle, without external I/O."""
    import secrets
    from .adapters.manual import register_manual
    from .asset_import import request_local_import, process_local_import
    from .auth import create_user, mint_session
    from .bundles import build_bundle_job
    from .downloads import decide_download, sha256_file
    from .pairing import propose_pairing, review_pairing, freeze_snapshot, approve_snapshot
    with _fixture_storage(root):
        inbox = root / "source-import"
        inbox.mkdir()
        file = inbox / "fixture.csv"
        file.write_bytes(b"text,label\nrestore alpha,positive\nrestore beta,negative\n")
        expected_rows = [{"text": "restore alpha", "label": "positive"},
                         {"text": "restore beta", "label": "negative"}]
        register_manual(engine, {"source_key": "drill/fixture", "revision": "drill-v1",
            "url": "https://invalid.example/drill", "access_conditions": "synthetic isolated restore fixture",
            "modalities": ["text"], "license": "mit", "size_rows": len(expected_rows),
            "size_bytes": file.stat().st_size, "card_text": "Synthetic data created solely for a disposable recovery drill."})
        with session_scope(engine) as session:
            researcher = create_user(session, "drill-researcher", secrets.token_urlsafe(32), "researcher")
            reviewer = create_user(session, "drill-reviewer", secrets.token_urlsafe(32), "reviewer")
            researcher_token, reviewer_token = mint_session(session, researcher), mint_session(session, reviewer)
            version_id = session.scalar(select(DatasetVersion.id))
        actor = _fixture_actor(engine, researcher_token, {"researcher", "admin"})
        with session_scope(engine) as session:
            request = request_local_import(session, {"dataset_version_id": version_id,
                "files": [{"path": file.name, "size": file.stat().st_size, "sha256": sha256_file(file)}]}, actor.username)
            request_id = request.id
        actor = _fixture_actor(engine, reviewer_token, {"reviewer", "admin"})
        with session_scope(engine) as session:
            decide_download(session, request_id, "approved", actor.username, "Disposable synthetic recovery fixture")
        process_local_import(engine, request_id, {file.name: file})
        idea = {"external_id": "drill-recovery", "title": "Isolated recovery verification",
                "requirements": {"modalities": ["text"], "license_class": ["permissive"]}}
        actor = _fixture_actor(engine, researcher_token, {"researcher", "admin"})
        with session_scope(engine) as session:
            proposal = propose_pairing(session, idea, [version_id], "Verify exact restored synthetic inputs", proposed_by=actor.username)
            pairing_id = proposal["created"][0]["id"]
        actor = _fixture_actor(engine, reviewer_token, {"reviewer", "admin"})
        with session_scope(engine) as session:
            review_pairing(session, pairing_id, "confirmed", "Disposable synthetic fixture", actor=actor.username, role=actor.role)
            snapshot = freeze_snapshot(session, idea["external_id"], note="Isolated backup and restore drill")
            snapshot_id = snapshot.id
            session.add(Audit(actor=actor.username, action="snapshot_freeze", detail=f"drill snapshot={snapshot_id}"))
        actor = _fixture_actor(engine, reviewer_token, {"reviewer", "admin"})
        with session_scope(engine) as session:
            approve_snapshot(session, snapshot_id)
            session.add(Audit(actor=actor.username, action="snapshot_approve", detail=f"drill snapshot={snapshot_id}"))
        result = build_bundle_job(engine, snapshot_id)
        with session_scope(engine) as session:
            asset = session.scalar(select(Asset).where(Asset.dataset_version_id == version_id))
            bundle = session.scalar(select(ResearchBundle).where(ResearchBundle.snapshot_id == snapshot_id))
            return {**result, "bundle_id": bundle.id, "asset_id": asset.id,
                    "asset_storage_key": asset.storage_key, "asset_sha256": asset.sha256,
                    "asset_bytes": file.read_bytes(), "expected_rows": expected_rows}


def _verify_restored_bundle(engine, assets: Path, destination: Path, fixture: dict) -> dict:
    from .downloads import sha256_file
    with session_scope(engine) as session:
        bundle_count = session.scalar(select(func.count()).select_from(ResearchBundle))
        bundle = session.get(ResearchBundle, fixture["bundle_id"])
        if not bundle_count or bundle is None or bundle.snapshot_id != fixture["snapshot_id"]:
            raise RuntimeError("Completed research bundle was not restored")
        if bundle.sha256 != fixture["sha256"] or bundle.storage_key != fixture["storage_key"]:
            raise RuntimeError("Restored research bundle metadata differs")
        bundle_id, digest, key = bundle.id, bundle.sha256, bundle.storage_key
    path = (assets / key).resolve()
    if not path.is_relative_to(assets.resolve()) or not path.is_file() or sha256_file(path) != digest:
        raise RuntimeError("Restored research bundle SHA-256 differs")
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            if not (destination / entry.filename).resolve().is_relative_to(destination.resolve()):
                raise RuntimeError("Restored research bundle contains an unsafe path")
        archive.extractall(destination)
    # -I ignores PYTHONPATH, the repository and user packages. The embedded loader
    # must verify and load from this independent directory using only the ZIP.
    script = """import hashlib, json, runpy, sys
from pathlib import Path
root = Path(sys.argv[1])
loader = runpy.run_path(str(root / 'load_data.py'))
manifest = loader['verify'](root)
rows = loader['load'](root, int(sys.argv[2]))
if rows != json.loads(sys.argv[3]):
    raise RuntimeError('Restored research bundle rows differ')
payload = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
print(json.dumps({'verified': True, 'independent_process': True, 'asset_count': len(manifest['assets']),
                  'row_count': len(rows), 'rows_sha256': hashlib.sha256(payload).hexdigest()}))
"""
    process = subprocess.run([sys.executable, "-I", "-c", script, str(destination.resolve()),
        str(fixture["asset_id"]), json.dumps(fixture["expected_rows"])], cwd=destination,
        capture_output=True, text=True, check=True, timeout=30)
    loaded = json.loads(process.stdout)
    if not loaded.get("verified") or not loaded.get("asset_count") or not loaded.get("row_count"):
        raise RuntimeError("Restored research bundle independent load was empty")
    return {"research_bundle_count": bundle_count, "research_bundles": [{"id": bundle_id,
            "snapshot_id": fixture["snapshot_id"], "sha256": digest, "storage_key": key,
            "independent_load": loaded}]}


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
        fixture = _create_bundle_fixture(source, root)
        digest, key, data = fixture["asset_sha256"], fixture["asset_storage_key"], fixture["asset_bytes"]
        with session_scope(source) as session:
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
        bundle_report = _verify_restored_bundle(restored, recovered_assets, root / "independent-load", fixture)
        result.update(status="passed", table_counts=counts[0], checkpoint_resumed=True,
                      asset_sha256=digest, **bundle_report)
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
