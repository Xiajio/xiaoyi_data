from __future__ import annotations
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import FakeAdapter, make_raw
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.db import session_scope,create_engine_for,init_db
from xiaoyi_catalog.models import DatasetVersion, Audit
from xiaoyi_catalog.models_assets import DownloadRequest, Asset
from xiaoyi_catalog.auth import create_user,mint_session
from xiaoyi_catalog.service import create_app
from xiaoyi_catalog.downloads import request_download,decide_download,process_next_download,DownloadError,sha256_file
from xiaoyi_catalog.bundles import build_bundle
from xiaoyi_catalog.pairing import propose_pairing,review_pairing,freeze_snapshot,approve_snapshot
from xiaoyi_catalog.profiling import profile_file

DATA=b"text,label\ngood,1\nbad,0\ngood,1\n"
SHA="a"*40
class Files:
    def list_repo_tree(self,**kwargs):
        assert kwargs["revision"]==SHA
        return [SimpleNamespace(path="data.csv",size=len(DATA))]
def fetch(manifest,name,staging):
    assert manifest["revision"]==SHA
    p=staging/name
    p.write_bytes(DATA)
    return p

@pytest.fixture
def setup(engine,tmp_path,monkeypatch):
    monkeypatch.setenv("XIAOYI_STORAGE_ROOT",str(tmp_path/"assets"))
    adapter=FakeAdapter([make_raw("test/data",revision=SHA,license="mit",
                      tags=("modality:text",),card="Public text dataset. license: MIT",size_rows=3)])
    adapter.kind="huggingface"
    collect(engine,adapter,CollectScope())
    with session_scope(engine) as session:
        version=session.scalar(select(DatasetVersion))
        return engine,version.id,tmp_path/"assets"

def proposal(session,version_id):
    return request_download(session,{"dataset_version_id":version_id,"files":["data.csv"],
                   "max_bytes":10000},"researcher",api=Files())

def test_no_download_without_approval_then_idempotent(setup):
    engine,version_id,root=setup
    with session_scope(engine) as session:
        r=proposal(session,version_id); ident=r.id
        assert proposal(session,version_id).id==ident
    assert not process_next_download(engine,fetcher=fetch)
    with session_scope(engine) as session:
        decide_download(session,ident,"approved","reviewer")
    assert process_next_download(engine,fetcher=fetch)
    assert not process_next_download(engine,fetcher=fetch)
    with session_scope(engine) as session:
        r=session.get(DownloadRequest,ident)
        assert r.status=="completed" and r.actual_bytes==len(DATA)
        asset=session.scalar(select(Asset))
        assert sha256_file(root/asset.storage_key)==asset.sha256
        assert len(session.scalars(select(Asset)).all())==1

def test_changed_approved_scope_rejected(setup):
    engine,version_id,_=setup
    with session_scope(engine) as session:
        r=proposal(session,version_id)
        decide_download(session,r.id,"approved","reviewer")
        r.manifest={**r.manifest,"files":[{"path":"secret.csv","size":len(DATA)}]}
    assert process_next_download(engine,fetcher=lambda *args:pytest.fail("must not download"))
    with session_scope(engine) as session:
        assert session.scalar(select(DownloadRequest)).status=="failed"

def test_budget_and_path_checks(setup):
    engine,version_id,_=setup
    with session_scope(engine) as session:
        with pytest.raises(DownloadError):
            request_download(session,{"dataset_version_id":version_id,"files":["../bad"],"max_bytes":100},"u",api=Files())
        with pytest.raises(DownloadError):
            request_download(session,{"dataset_version_id":version_id,"files":["data.csv"],"max_bytes":1},"u",api=Files())

def test_research_bundle_reproduces_and_detects_tampering(setup,tmp_path):
    engine,version_id,root=setup
    with session_scope(engine) as session:
        r=proposal(session,version_id)
        decide_download(session,r.id,"approved","reviewer")
    process_next_download(engine,fetcher=fetch)
    with session_scope(engine) as session:
        result=propose_pairing(session,{"external_id":"research","title":"Text research","requirements":{"modalities":["text"],"license_class":["permissive"]}},[version_id],"evidence")
        review_pairing(session,result["created"][0]["id"],"confirmed")
        snapshot=freeze_snapshot(session,"research")
        approve_snapshot(session,snapshot.id)
        ident=snapshot.id
        path=build_bundle(session,ident)
    bundle_hash=sha256_file(path)
    with session_scope(engine) as session:
        assert build_bundle(session,ident)==path
    assert sha256_file(path)==bundle_hash
    clean=tmp_path/"clean"
    with zipfile.ZipFile(path) as z:
        z.extractall(clean)
        croissant=json.loads(z.read("croissant.json"))
        assert croissant["@context"]["conformsTo"]=="dct:conformsTo"
        assert croissant["@context"]["sha256"]=="cr:sha256"
        fields=croissant["recordSet"][0]["field"]
        assert {f["name"] for f in fields}=={"text","label"}
        assert {f["dataType"] for f in fields}=={"sc:Text"}
        assert fields[0]["source"]["fileObject"]["@id"]==croissant["distribution"][0]["@id"]
        assert (clean/croissant["distribution"][0]["contentUrl"]).is_file()
    completed=subprocess.run([sys.executable,str(clean/"load_data.py"),str(clean)],capture_output=True,text=True)
    assert completed.returncode==0,completed.stderr
    manifest=json.loads((clean/"manifest.json").read_text(encoding="utf-8"))
    target=clean/manifest["assets"][0]["bundle_path"]
    target.write_bytes(b"tampered")
    assert subprocess.run([sys.executable,str(clean/"load_data.py"),str(clean)],capture_output=True).returncode!=0

def test_real_accounts_ignore_body_role_and_agent_cannot_approve(tmp_path):
    db=tmp_path/"auth.db"
    app=create_app(str(db),dev_auth=False)
    engine=app.state.engine
    with session_scope(engine) as session:
        create_user(session,"viewer","correct-password-123","reader")
        create_user(session,"reviewer","correct-password-123","reviewer")
        user=create_user(session,"machine","","researcher","agent")
        token=mint_session(session,user)
    with TestClient(app) as client:
        assert client.get("/api/overview").status_code==401
        assert client.post("/api/snapshots/1/approve",json={"role":"admin"}).status_code==401
        assert client.post("/api/auth/login",json={"username":"viewer","password":"wrong-password"}).status_code==401
        assert client.post("/api/auth/login",json={"username":"viewer","password":"correct-password-123"}).status_code==200
        assert client.get("/api/auth/me").json()["role"]=="reader"
        assert client.post("/api/snapshots/1/approve",json={"role":"admin"}).status_code==403
        assert client.post("/api/snapshots/1/approve",headers={"Authorization":f"Bearer {token}"},json={"role":"admin"}).status_code==403
        client.post("/api/auth/logout")
        assert client.get("/api/overview").status_code==401

def test_agent_routes_create_events_owner_and_no_secret(tmp_path):
    app=create_app(str(tmp_path/"agent.db"),dev_auth=True)
    with TestClient(app) as client:
        r=client.post("/api/agent-runs",json={"goal":"寻找公开文本数据","hard_constraints":{"modalities":["text"]}})
        assert r.status_code==201,r.text
        ident=r.json()["id"]
        assert client.get(f"/api/agent-runs/{ident}").json()["status"]=="queued"
        assert client.get(f"/api/agent-runs/{ident}/events").json()["items"]
        assert client.post(f"/api/agent-runs/{ident}/cancel").json()["status"]=="cancelled"
        assert client.post(f"/api/agent-runs/{ident}/retry").json()["status"]=="queued"

def test_profiles_are_measurements_with_scope(tmp_path):
    csv=tmp_path/"test.csv";csv.write_bytes(DATA)
    report=profile_file(csv,"test.csv")
    assert report["rows_scanned"]==3 and report["duplicate_rows"]==1
    from PIL import Image
    image=tmp_path/"image.png"; Image.new("RGB",(8,6)).save(image)
    assert profile_file(image,"image.png")["width"]==8
    mesh=tmp_path/"m.obj";mesh.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    assert profile_file(mesh,"m.obj")["faces"]==1

def test_bundle_first_export_is_idempotent_across_concurrent_sessions(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from xiaoyi_catalog.models_assets import ResearchBundle
    engine,version_id,_=setup
    with session_scope(engine) as session:
        r=proposal(session,version_id)
        decide_download(session,r.id,"approved","reviewer")
    process_next_download(engine,fetcher=fetch)
    with session_scope(engine) as session:
        result=propose_pairing(session,{"external_id":"parallel","title":"Parallel export",
            "requirements":{"modalities":["text"]}},[version_id],"evidence")
        review_pairing(session,result["created"][0]["id"],"confirmed")
        snap=freeze_snapshot(session,"parallel")
        approve_snapshot(session,snap.id)
        ident=snap.id
    barrier=Barrier(2)
    def export(_):
        barrier.wait(timeout=10)
        with session_scope(engine) as session:
            return build_bundle(session,ident)
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths=list(pool.map(export,range(2)))
    assert paths[0]==paths[1] and paths[0].is_file()
    with session_scope(engine) as session:
        assert len(session.scalars(select(ResearchBundle)).all())==1


def test_preview_jsonl_non_object_rows_are_explicit_values(setup):
    from xiaoyi_catalog.preview import preview_asset
    engine,version_id,root=setup
    with session_scope(engine) as session:
        r=proposal(session,version_id)
        decide_download(session,r.id,"approved","reviewer")
    process_next_download(engine,fetcher=fetch)
    with session_scope(engine) as session:
        asset=session.scalar(select(Asset))
        content=b'null\n{"text":"hello"}\n[1,2]\n'
        (root/asset.storage_key).write_bytes(content)
        asset.sha256=sha256_file(root/asset.storage_key)
        asset.size_bytes=len(content)
        asset.path="sample.jsonl"
        result=preview_asset(session,asset.id)
        assert result["rows"]==[{"value":None},{"text":"hello"},{"value":[1,2]}]


def test_bearer_logout_revokes_the_authenticated_session(tmp_path):
    app=create_app(str(tmp_path/"revoke.db"),dev_auth=False)
    with session_scope(app.state.engine) as session:
        agent=create_user(session,"worker","","researcher","agent")
        token=mint_session(session,agent)
    with TestClient(app) as client:
        headers={"Authorization":f"Bearer {token}"}
        assert client.get("/api/overview",headers=headers).status_code==200
        assert client.post("/api/auth/logout",headers=headers).status_code==200
        assert client.get("/api/overview",headers=headers).status_code==401

def test_failed_download_retry_reuses_approved_files_within_remaining_quota(setup,monkeypatch):
    engine,version_id,_=setup
    class TwoFiles:
        def list_repo_tree(self,**kwargs):
            return [SimpleNamespace(path=name,size=len(DATA)) for name in ("first.csv","second.csv")]
    payload={"dataset_version_id":version_id,"files":["first.csv","second.csv"],"max_bytes":2*len(DATA)}
    with session_scope(engine) as session:
        first=request_download(session,payload,"researcher",api=TwoFiles())
        decide_download(session,first.id,"approved","reviewer")
        first_id=first.id
    def fail_after_first(manifest,name,staging):
        if name=="second.csv":
            raise RuntimeError("source temporarily unavailable")
        return fetch(manifest,name,staging)
    process_next_download(engine,fetcher=fail_after_first)
    with session_scope(engine) as session:
        assert session.get(DownloadRequest,first_id).status=="failed"
        second=request_download(session,payload,"researcher",api=TwoFiles())
        assert second.id!=first_id
        decide_download(session,second.id,"approved","reviewer")
        second_id=second.id
    monkeypatch.setenv("XIAOYI_STORAGE_QUOTA_BYTES",str(2*len(DATA)))
    fetched=[]
    def resume(manifest,name,staging):
        fetched.append(name)
        return fetch(manifest,name,staging)
    assert process_next_download(engine,fetcher=resume)
    assert fetched==["second.csv"]
    with session_scope(engine) as session:
        assert session.get(DownloadRequest,second_id).status=="completed"
        assets=session.scalars(select(Asset)).all()
        assert len(assets)==2 and {a.download_request_id for a in assets}=={second_id}
        result=propose_pairing(session,{"external_id":"retry","title":"Resumed download",
            "requirements":{"modalities":["text"]}},[version_id],"evidence")
        review_pairing(session,result["created"][0]["id"],"confirmed")
        snapshot=freeze_snapshot(session,"retry")
        approve_snapshot(session,snapshot.id)
        assert build_bundle(session,snapshot.id).is_file()


def test_bundle_job_publication_receipt_recovers_before_job_finish(setup):
    from datetime import timedelta
    from xiaoyi_catalog.models import utcnow
    from xiaoyi_catalog.models_jobs import Job, Artifact
    from xiaoyi_catalog.models_assets import ResearchBundle
    from xiaoyi_catalog.jobs import enqueue, claim, Guard
    from xiaoyi_catalog.bundles import build_bundle_job
    engine,version_id,root=setup
    with session_scope(engine) as session:
        request=proposal(session,version_id)
        decide_download(session,request.id,"approved","reviewer")
    process_next_download(engine,fetcher=fetch)
    with session_scope(engine) as session:
        result=propose_pairing(session,{"external_id":"receipt","title":"Receipt test","requirements":{"modalities":["text"],"license_class":["permissive"]}},[version_id],"evidence")
        review_pairing(session,result["created"][0]["id"],"confirmed")
        snapshot=freeze_snapshot(session,"receipt")
        approve_snapshot(session,snapshot.id)
        sid=snapshot.id
        job=enqueue(session,"bundle.build",{"snapshot_id":sid},"reviewer");jid=job.id
    leased=claim(engine,"io","first")
    with Guard(engine,leased) as guard:
        result=build_bundle_job(engine,sid,guard=guard)
    # Crash after publication, before finish. A late cancellation must not undo a committed result.
    with session_scope(engine) as session:
        assert session.scalar(select(Artifact).where(Artifact.job_id==jid)).manifest==result
        item=session.get(Job,jid)
        item.lease_until=utcnow()-timedelta(seconds=1);item.cancel_requested=True
    assert claim(engine,"io","replacement") is None
    with session_scope(engine) as session:
        assert session.get(Job,jid).status=="succeeded"
        assert session.get(Job,jid).result==result
        assert len(session.scalars(select(ResearchBundle).where(ResearchBundle.snapshot_id==sid)).all())==1
