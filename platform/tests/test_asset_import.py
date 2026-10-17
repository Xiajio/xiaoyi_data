"""Controlled local acquisition, derived lineage and snapshot file boundaries."""
import hashlib
import pytest
from sqlalchemy import select
from xiaoyi_catalog.adapters.manual import register_manual
from xiaoyi_catalog.asset_import import request_local_import, process_local_import, derive_asset
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.downloads import decide_download, DownloadError, sha256_file
from xiaoyi_catalog.models import DatasetVersion, Dataset
from xiaoyi_catalog.models_assets import Asset, DownloadRequest
from xiaoyi_catalog.search import latest_version
from xiaoyi_catalog.pairing import propose_pairing, review_pairing, freeze_snapshot, approve_snapshot, snapshot_status, PairingError

IDEA = {"external_id":"local-study", "title":"local text study", "requirements":{"modalities":["text"],"license_class":["permissive"]}}


@pytest.fixture
def local_data(engine, tmp_path, monkeypatch):
    inbox, storage = tmp_path/"inbox", tmp_path/"assets"
    inbox.mkdir()
    monkeypatch.setenv("XIAOYI_IMPORT_ROOT", str(inbox))
    monkeypatch.setenv("XIAOYI_STORAGE_ROOT", str(storage))
    register_manual(engine, {"source_key":"official/table", "revision":"2026", "url":"https://example.org/data",
        "access_conditions":"public archive", "modalities":["text"], "license":"mit", "size_rows":3})
    with session_scope(engine) as session:
        version_id = session.scalar(select(DatasetVersion.id))
    path = inbox/"train.csv"
    path.write_bytes(b"text\nx\nx\ny\n")
    return engine, version_id, inbox, storage, path


def request_for(engine, version_id, path):
    with session_scope(engine) as session:
        request = request_local_import(session, {"dataset_version_id":version_id,
            "files":[{"path":path.name,"size":path.stat().st_size,"sha256":sha256_file(path)}]}, "researcher")
        return request.id


def approve(engine, ident):
    with session_scope(engine) as session:
        decide_download(session, ident, "approved", "reviewer")


def import_ready(local_data):
    engine, version, inbox, storage, path = local_data
    ident = request_for(engine, version, path)
    approve(engine, ident)
    process_local_import(engine, ident, {path.name:path})
    with session_scope(engine) as session:
        asset_id = session.scalar(select(Asset.id).where(Asset.dataset_version_id == version))
    return ident, asset_id


def test_local_import_needs_approval_exact_content_and_inbox(local_data, tmp_path):
    engine, version, inbox, storage, path = local_data
    ident = request_for(engine, version, path)
    with pytest.raises(DownloadError, match="人工批准"):
        process_local_import(engine, ident, {path.name:path})
    approve(engine, ident)
    outside = tmp_path/"outside.csv"
    outside.write_bytes(path.read_bytes())
    with pytest.raises(DownloadError, match="导入目录"):
        process_local_import(engine, ident, {path.name:outside})
    original = path.read_bytes()
    path.write_bytes(b"changed")
    with pytest.raises(DownloadError, match="SHA-256"):
        process_local_import(engine, ident, {path.name:path})
    path.write_bytes(original)
    assert process_local_import(engine, ident, {path.name:path}) == ident
    assert process_local_import(engine, ident, {path.name:path}) == ident
    with session_scope(engine) as session:
        assert session.get(DownloadRequest, ident).status == "completed"
        assert len(session.scalars(select(Asset)).all()) == 1


def test_derived_asset_preserves_original_and_source_latest(local_data):
    engine, version, inbox, storage, path = local_data
    request_id, parent_id = import_ready(local_data)
    original_hash = sha256_file(path)
    with session_scope(engine) as session:
        derived = derive_asset(session, parent_id, actor="reviewer")
        version_row = session.get(DatasetVersion, derived.dataset_version_id)
        assert version_row.kind == "derived" and version_row.parent_version_id == version
        assert version_row.metadata_json["size_rows"] == 2
        assert derived.parent_asset_ids == [parent_id]
        assert (storage/derived.storage_key).read_bytes() == b"text\nx\ny\n"
        assert latest_version(session, version_row.dataset_id).id == version
        assert session.get(Dataset, version_row.dataset_id).size_rows == 3
        assert derive_asset(session, parent_id).id == derived.id
        parent = session.get(Asset, parent_id)
        assert parent.sha256 == original_hash == sha256_file(storage/parent.storage_key)


def test_snapshot_does_not_include_assets_added_after_freeze(local_data):
    engine, version, inbox, storage, path = local_data
    request_id, asset_id = import_ready(local_data)
    with session_scope(engine) as session:
        pid = propose_pairing(session, IDEA, [version], "chosen input")["created"][0]["id"]
        review_pairing(session, pid, "confirmed")
        snap = freeze_snapshot(session, IDEA["external_id"])
        snapshot_id = snap.id
        assert [a["id"] for a in snap.content["assets"]] == [asset_id]
    later = inbox/"test.csv"
    later.write_bytes(b"text\nz\n")
    later_id = request_for(engine, version, later)
    approve(engine, later_id)
    process_local_import(engine, later_id, {later.name:later})
    with session_scope(engine) as session:
        snap = approve_snapshot(session, snapshot_id)
        assert [a["id"] for a in snap.content["assets"]] == [asset_id]
        assert snapshot_status(session, IDEA["external_id"])["snapshots"][0]["bundle_available"]
        from xiaoyi_catalog.bundles import build_bundle
        import zipfile, json
        bundle = build_bundle(session, snapshot_id)
        with zipfile.ZipFile(bundle) as z:
            manifest = json.loads(z.read("manifest.json"))
            assert [a["id"] for a in manifest["assets"]] == [asset_id]


def test_incomplete_acquisition_is_not_frozen_and_changes_block_approval(local_data):
    engine, version, inbox, storage, path = local_data
    request_id, asset_id = import_ready(local_data)
    with session_scope(engine) as session:
        pid = propose_pairing(session, IDEA, [version], "chosen input")["created"][0]["id"]
        review_pairing(session, pid, "confirmed")
        request = session.get(DownloadRequest, request_id)
        request.status = "failed"
        snap = freeze_snapshot(session, IDEA["external_id"])
        assert snap.content["assets"] == []
        snap.status = "invalidated"
        request.status = "completed"
        snap = freeze_snapshot(session, IDEA["external_id"])
        request.status = "failed"
        with pytest.raises(PairingError, match="完整批准"):
            approve_snapshot(session, snap.id)


def test_import_cli_requires_human_identity_and_runs_real_commands(local_data, monkeypatch, capsys):
    import json
    from xiaoyi_catalog.auth import create_user, mint_session
    from xiaoyi_catalog.asset_import import main
    engine, version, inbox, storage, path = local_data
    db = str(engine.url.database)
    manifest = inbox/"request.json"
    manifest.write_text(json.dumps({"dataset_version_id":version,
        "files":[{"path":path.name,"size":path.stat().st_size,"sha256":sha256_file(path)}]}), encoding="utf-8")
    assert main(["--db",db,"request","--manifest",str(manifest)]) == 1
    with session_scope(engine) as session:
        machine = create_user(session,"machine", "", "researcher", "agent")
        machine_token = mint_session(session,machine)
        human = create_user(session,"curator", "test-password-123", "reviewer")
        human_token = mint_session(session,human)
    monkeypatch.setenv("XIAOYI_CLI_TOKEN",machine_token)
    assert main(["--db",db,"request","--manifest",str(manifest)]) == 1
    monkeypatch.setenv("XIAOYI_CLI_TOKEN",human_token)
    capsys.readouterr()
    assert main(["--db",db,"request","--manifest",str(manifest)]) == 0
    ident = json.loads(capsys.readouterr().out)["request_id"]
    mapping = inbox/"files.json"
    mapping.write_text(json.dumps({path.name:str(path)}),encoding="utf-8")
    assert main(["--db",db,"import","--request",str(ident),"--files",str(mapping)]) == 1
    approve(engine,ident)
    capsys.readouterr()
    assert main(["--db",db,"import","--request",str(ident),"--files",str(mapping)]) == 0
    import_job=json.loads(capsys.readouterr().out)
    from xiaoyi_catalog.jobs import run_queue_once
    from xiaoyi_catalog.models_jobs import Job
    assert run_queue_once(engine,"io")
    with session_scope(engine) as session:
        assert session.get(Job,import_job["job_id"]).status=="succeeded"
    with session_scope(engine) as session:
        asset_id = session.scalar(select(Asset.id))
    capsys.readouterr()
    assert main(["--db",db,"derive","--asset",str(asset_id)]) == 0
    derive_job = json.loads(capsys.readouterr().out)
    assert run_queue_once(engine,"cpu")
    with session_scope(engine) as session:
        job=session.get(Job,derive_job["job_id"])
        assert job.status=="succeeded",job.error
        result=job.result
    assert result["origin"] == "derived" and result["dataset_version_id"] != version


def test_derived_input_can_be_reviewed_and_exported_with_lineage(local_data):
    from xiaoyi_catalog.bundles import build_bundle
    import zipfile, json
    engine, version, inbox, storage, path = local_data
    _, parent_id = import_ready(local_data)
    with session_scope(engine) as session:
        derived = derive_asset(session, parent_id)
        idea = {**IDEA, "external_id":"derived-study"}
        pid = propose_pairing(session, idea, [derived.dataset_version_id], "use deduplicated data")["created"][0]["id"]
        review_pairing(session, pid, "confirmed")
        snap = freeze_snapshot(session, idea["external_id"])
        approve_snapshot(session, snap.id)
        path = build_bundle(session, snap.id)
        with zipfile.ZipFile(path) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            assert manifest["assets"][0]["origin"] == "derived"
            assert manifest["assets"][0]["recipe"]["inputs"][0]["asset_id"] == parent_id


def test_async_derivation_computes_outside_db_transaction_and_reuses_receipt(local_data,monkeypatch):
    from xiaoyi_catalog.asset_import import execute_derive
    from xiaoyi_catalog import derivation
    from xiaoyi_catalog.jobs import enqueue,claim,Guard
    from xiaoyi_catalog.models_jobs import Artifact
    from xiaoyi_catalog.models_samples import ProfileRun
    from sqlalchemy import func
    engine,version,inbox,storage,path=local_data
    _,parent_id=import_ready(local_data)
    with session_scope(engine) as session:
        job=enqueue(session,"asset.derive",{"asset_id":parent_id,"operation":"deduplicate"},"reviewer");jid=job.id
    job=claim(engine,"cpu","derive-fixture");guard=Guard(engine,job)
    original=derivation._compute
    calls=[]
    def compute(state,guard=None):
        assert engine.pool.checkedout()==0
        calls.append(True)
        return original(state,guard)
    monkeypatch.setattr(derivation,"_compute",compute)
    first=execute_derive(engine,parent_id,guard=guard,actor="reviewer")
    second=execute_derive(engine,parent_id,guard=guard,actor="reviewer")
    assert first==second and len(calls)==1
    assert (storage/("sha256/"+first["sha256"][:2]+"/"+first["sha256"])).read_bytes()==b"text\nx\ny\n"
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(Asset))==2
        assert session.scalar(select(func.count()).select_from(ProfileRun))==1
        assert session.scalar(select(Artifact).where(Artifact.job_id==jid)).manifest==first


def test_async_derivation_lost_lease_cannot_publish(local_data,monkeypatch):
    from xiaoyi_catalog.asset_import import execute_derive
    from xiaoyi_catalog import derivation
    from xiaoyi_catalog.agent_runtime import LeaseLost
    from sqlalchemy import func
    engine,_,_,_,_=local_data
    _,parent_id=import_ready(local_data)
    lost=False
    def guard(session=None):
        if lost:raise LeaseLost("fixture lease lost")
    original=derivation._compute
    def compute(state,guard=None):
        nonlocal lost
        result=original(state,guard)
        lost=True
        return result
    monkeypatch.setattr(derivation,"_compute",compute)
    with pytest.raises(LeaseLost):execute_derive(engine,parent_id,guard=guard)
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(Asset))==1
        assert session.scalar(select(func.count()).select_from(DatasetVersion).where(DatasetVersion.kind=="derived"))==0


def test_async_derivation_disk_failure_and_corrupt_input_publish_nothing(local_data,monkeypatch):
    import errno
    from sqlalchemy import func
    from xiaoyi_catalog.asset_import import execute_derive
    from xiaoyi_catalog import derivation
    engine,_,_,storage,_=local_data
    _,parent_id=import_ready(local_data)
    original=derivation.store_file
    def full(*args,**kwargs):raise OSError(errno.ENOSPC,"fixture disk full")
    monkeypatch.setattr(derivation,"store_file",full)
    with pytest.raises(OSError,match="disk full"):execute_derive(engine,parent_id)
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(Asset))==1
        parent=session.get(Asset,parent_id)
        source=storage/parent.storage_key
    monkeypatch.setattr(derivation,"store_file",original)
    source.write_bytes(b"corrupt")
    with pytest.raises(DownloadError,match="校验"):execute_derive(engine,parent_id)
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(DatasetVersion).where(DatasetVersion.kind=="derived"))==0
