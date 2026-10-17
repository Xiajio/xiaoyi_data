from __future__ import annotations
import hashlib
import json
import runpy
from pathlib import Path
import pytest
from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select,func
from xiaoyi_catalog.auth import Principal
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.models import Source,Dataset,DatasetVersion
from xiaoyi_catalog.downloads import digest_json
from xiaoyi_catalog.models_assets import Asset,DownloadRequest,QualityReport
from xiaoyi_catalog.models_samples import DatasetBuild,Sample,BuildSample,ProfileRun,Experiment,SampleRelation
from xiaoyi_catalog.sample_engine import (create_build,execute_build,iter_build_rows,export_build,publish_build,
                                         store_file,execute_profile)
from xiaoyi_catalog.mining import execute_mining
from xiaoyi_catalog.experiments import create_experiment,execute_experiment_import,compare_experiments

@pytest.fixture()
def source(engine,tmp_path,monkeypatch):
    monkeypatch.setenv("XIAOYI_STORAGE_ROOT",str(tmp_path/"assets"))
    with session_scope(engine) as session:
        source=Source(kind="manual",endpoint="fixture")
        session.add(source); session.flush()
        dataset=Dataset(source_id=source.id,source_key="test",fingerprint="test",license="CC0",license_class="permissive")
        session.add(dataset); session.flush()
        version=DatasetVersion(dataset_id=dataset.id,revision="fixed-fixture",provenance_status="verified")
        session.add(version); session.flush()
        request=DownloadRequest(dataset_version_id=version.id,requested_by="fixture",status="completed",manifest={},manifest_hash="f"*64,max_bytes=10000000,decided_by="fixture")
        session.add(request); session.flush()
        version_id,request_id=version.id,request.id
    def add(name,content):
        path=tmp_path/name
        path.parent.mkdir(parents=True,exist_ok=True)
        if isinstance(content,bytes): path.write_bytes(content)
        else: path.write_text(content,encoding="utf-8")
        artifact=store_file(path)
        with session_scope(engine) as session:
            manifest={"files":[{"path":name,"size":artifact["size_bytes"],"sha256":artifact["sha256"]}]}
            fingerprint=digest_json(manifest)
            approval=DownloadRequest(dataset_version_id=version_id,requested_by="fixture",status="completed",manifest=manifest,manifest_hash=fingerprint,approved_hash=fingerprint,max_bytes=10000000,decided_by="fixture")
            session.add(approval); session.flush()
            asset=Asset(dataset_version_id=version_id,download_request_id=approval.id,path=name,**artifact)
            session.add(asset); session.flush()
            return asset.id
    return add

def build(engine,recipe):
    with session_scope(engine) as session:
        row=create_build(session,recipe,"fixture")
        ident=row.id
    execute_build(engine,ident)
    with session_scope(engine) as session:
        row=session.get(DatasetBuild,ident)
        return ident,row.manifest,row.manifest_hash

def test_build_reproducibility_groups_errors_and_portable_export(engine,source,tmp_path):
    aid=source("text.jsonl",'\n'.join(json.dumps(row) for row in [
        {"text":"alpha beta","label":"a","patient":"p1"},
        {"text":"alpha beta","label":"a","patient":"p1"},
        {"text":"gamma delta","label":"b","patient":"p1"},
        {"text":"other","label":"a","patient":"p2"},
        {"text":"","label":"a","patient":"p3"}])+"\nnot-json\n")
    recipe={"asset_ids":[aid],"modality":"text","text_column":"text","label_column":"label","group_column":"patient","shard_rows":2}
    first,manifest,digest=build(engine,recipe)
    second,again,repeated_digest=build(engine,recipe)
    assert digest==repeated_digest
    assert manifest==again
    assert manifest["counts"]["input_rows"]==6
    assert manifest["counts"]["exact_duplicates"]==1
    assert manifest["counts"]["invalid_rows"]==2
    rows=list(iter_build_rows(manifest))
    assert len(rows)==3
    groups={}
    for row in rows:
        groups.setdefault(row["group_key"],set()).add(row["split"])
    assert all(len(splits)==1 for splits in groups.values())
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(Sample))==3
        with pytest.raises(ValueError,match="human"):
            publish_build(session,first,Principal(1,"bot","admin","agent"),digest)
        publish_build(session,first,Principal(2,"reviewer","reviewer"),digest)
        assert session.get(DatasetBuild,second).approved_hash==""
    target=export_build(manifest,tmp_path/"portable")
    loader=runpy.run_path(str(target/"load.py"))
    assert len(list(loader["load"]()))==3
    (target/manifest["shards"][0]["storage_key"]).write_bytes(b"tampered")
    with pytest.raises(ValueError,match="hash"):
        list(loader["load"]())

@pytest.mark.parametrize("suffix,data",[("csv","x,label\n1,a\n2,b\n"),("tsv","x\tlabel\n1\ta\n2\tb\n"),("txt","one\ntwo\n")])
def test_text_table_formats(engine,source,suffix,data):
    aid=source("rows."+suffix,data)
    recipe={"asset_ids":[aid],"modality":"text" if suffix=="txt" else "table"}
    if suffix!="txt": recipe["label_column"]="label"
    _,manifest,_=build(engine,recipe)
    assert len(list(iter_build_rows(manifest)))==2

def test_parquet_and_image_association(engine,source,tmp_path):
    file=tmp_path/"rows-input.parquet"
    pq.write_table(pa.Table.from_pylist([{"x":1,"label":"a"},{"x":2,"label":"b"}]),file)
    aid=source("rows.parquet",file.read_bytes())
    _,manifest,_=build(engine,{"asset_ids":[aid],"label_column":"label"})
    assert len(list(iter_build_rows(manifest)))==2
    image=tmp_path/"image.png"
    Image.new("RGB",(12,9),"red").save(image)
    image_id=source("images/a.png",image.read_bytes())
    broken=source("images/b.png",b"not an image")
    mapping=source("labels.csv","path,label\nimages/a.png,red\nimages/b.png,bad\nimages/missing.png,missing\n")
    _,manifest,_=build(engine,{"asset_ids":[image_id,broken,mapping],"modality":"image","image_path_column":"path","label_column":"label"})
    assert manifest["counts"]["output_rows"]==1
    assert manifest["counts"]["invalid_rows"]==2
    row=next(iter_build_rows(manifest))
    assert json.loads(row["content"])["width"]==12


def test_budget_label_revisions_and_mining(engine,source):
    aid=source("labels.jsonl",'\n'.join(json.dumps(x) for x in [
        {"text":"same sentence","label":"yes"},{"text":"same sentence","label":"no"},
        {"text":"different","label":"yes"},{"text":"another","label":"no"}])+"\n")
    recipe={"asset_ids":[aid],"modality":"text","label_column":"label","splits":{"test":1.0}}
    ident,manifest,_=build(engine,recipe)
    rows=list(iter_build_rows(manifest))
    changed,changed_manifest,_=build(engine,{**recipe,"labels":{rows[0]["sample_id"]:"revised"}})
    assert {r["sample_id"] for r in rows}=={r["sample_id"] for r in iter_build_rows(changed_manifest)}
    _,selected,_=build(engine,{**recipe,"sample_budget":2})
    assert selected["counts"]["output_rows"]==2
    result=execute_mining(engine,{"build_id":ident,"benchmark_build_id":changed})
    with session_scope(engine) as session:
        kinds=set(session.scalars(select(SampleRelation.kind).where(SampleRelation.profile_run_id==result["profile_run_id"])))
        assert "label_conflict" in kinds
        assert "benchmark_contamination_candidate" in kinds
        assert "text_near_duplicate" in kinds


def test_experiment_import_comparison_and_unknown_rejection(engine,source):
    aid=source("classification.jsonl",'{"text":"red","label":"r"}\n{"text":"blue","label":"b"}\n')
    ident,manifest,_=build(engine,{"asset_ids":[aid],"modality":"text","label_column":"label","splits":{"test":1}})
    rows=list(iter_build_rows(manifest))
    prediction=source("predictions.jsonl",'\n'.join(json.dumps({"sample_id":r["sample_id"],"build_id":ident,"prediction":json.loads(r["label"])}) for r in rows)+"\n")
    spec={"build_id":ident,"prediction_asset_id":prediction,"model":{"name":"fixed","version":"1","training_budget":"one-pass"},"protocol":{"evaluation_build_id":ident,"split":"test"}}
    ids=[]
    for _ in range(2):
        with session_scope(engine) as session:
            experiment=create_experiment(session,spec,"fixture"); ids.append(experiment.id)
        result=execute_experiment_import(engine,{"experiment_id":ids[-1]})
        assert result["metrics"]["accuracy"]==1
    with session_scope(engine) as session:
        assert compare_experiments(session,ids)["items"][1]["accuracy_delta"]==0
    bad=source("bad-predictions.jsonl",json.dumps({"sample_id":"unknown","build_id":ident,"prediction":"r"})+"\n")
    with session_scope(engine) as session:
        experiment=create_experiment(session,{**spec,"prediction_asset_id":bad},"fixture"); bad_id=experiment.id
    with pytest.raises(ValueError,match="not in"):
        execute_experiment_import(engine,{"experiment_id":bad_id})
    with session_scope(engine) as session:
        assert session.get(Experiment,bad_id).status=="failed"


def test_hash_failure_and_late_guard_cannot_publish(engine,source):
    aid=source("guard.txt","one\ntwo\n")
    with session_scope(engine) as session:
        row=create_build(session,{"asset_ids":[aid],"modality":"text"},"fixture"); ident=row.id
    calls=0
    def guard(session=None):
        nonlocal calls
        calls+=1
        if calls>=4: raise RuntimeError("lease lost")
    with pytest.raises(RuntimeError,match="lease"):
        execute_build(engine,ident,guard=guard)
    with session_scope(engine) as session:
        row=session.get(DatasetBuild,ident)
        assert row.status!="ready"
        assert not row.manifest_hash
    execute_build(engine,ident)
    with session_scope(engine) as session:
        asset=session.get(Asset,aid)
        from xiaoyi_catalog.sample_engine import asset_path
        asset_path(asset.storage_key).write_bytes(b"tampered")
        row=create_build(session,{"asset_ids":[aid],"modality":"text"},"fixture"); other=row.id
    with pytest.raises(ValueError,match="hash"):
        execute_build(engine,other)


def test_profile_appends_without_overwriting(engine,source):
    aid=source("profile.csv","x\n1\n2\n")
    with session_scope(engine) as session:
        asset=session.get(Asset,aid)
        old=QualityReport(asset_id=aid,dataset_version_id=asset.dataset_version_id,value={"untouched":True})
        session.add(old)
    execute_profile(engine,aid)
    execute_profile(engine,aid)
    with session_scope(engine) as session:
        assert session.scalar(select(QualityReport)).value=={"untouched":True}
        assert session.scalar(select(func.count()).select_from(ProfileRun))==2


def test_unapproved_assets_cannot_build(engine,source):
    aid=source("unapproved.txt","test\n")
    with session_scope(engine) as session:
        asset=session.get(Asset,aid)
        request=session.get(DownloadRequest,asset.download_request_id)
        request.approved_hash=None
    with session_scope(engine) as session:
        with pytest.raises(ValueError,match="approval"):
            create_build(session,{"asset_ids":[aid],"modality":"text"},"fixture")


def test_build_api_enqueues_idempotently_and_requires_publication(engine,source):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from xiaoyi_catalog.api_builds import register_builds
    aid=source("api.txt","first\nsecond\n")
    app=FastAPI();app.state.dev_auth=True;app.state.engine=engine
    register_builds(app,engine)
    client=TestClient(app)
    payload={"recipe":{"asset_ids":[aid],"modality":"text"}}
    first=client.post("/api/builds",json=payload)
    assert first.status_code==202,first.text
    again=client.post("/api/builds",json=payload)
    assert first.json()["job_id"]==again.json()["job_id"]
    ident=first.json()["build_id"]
    assert client.get(f"/api/builds/{ident}/manifest").status_code==409
    execute_build(engine,ident)
    body=client.get(f"/api/builds/{ident}/manifest").json()
    digest=body["manifest"]["shards"][0]["sha256"]
    assert client.get(f"/api/builds/{ident}/artifacts/{digest}").status_code==409
    assert client.get(f"/api/builds/{ident}/samples?limit=101").status_code==422
    published=client.post(f"/api/builds/{ident}/publish",json={"manifest_hash":body["sha256"]})
    assert published.status_code==200,published.text
    assert client.get(f"/api/builds/{ident}/artifacts/{digest}").status_code==200


def test_prediction_duplicates_and_incomplete_coverage_rejected(engine,source):
    aid=source("eval.jsonl",'{"text":"a","label":"a"}\n{"text":"b","label":"b"}\n')
    ident,manifest,_=build(engine,{"asset_ids":[aid],"modality":"text","label_column":"label","splits":{"test":1}})
    row=next(iter_build_rows(manifest))
    prediction={"sample_id":row["sample_id"],"build_id":ident,"prediction":json.loads(row["label"])}
    for name,records,match in [("duplicate",[prediction,prediction],"duplicate"),("incomplete",[prediction],"cover")]:
        aid=source(name+".jsonl","".join(json.dumps(r)+"\n" for r in records))
        with session_scope(engine) as session:
            experiment=create_experiment(session,{"build_id":ident,"prediction_asset_id":aid,"model":{"name":"fixed","version":"1","training_budget":"1"},"protocol":{"evaluation_build_id":ident}},"fixture");eid=experiment.id
        with pytest.raises(ValueError,match=match):
            execute_experiment_import(engine,{"experiment_id":eid})


def test_image_near_duplicate_candidates(engine,source,tmp_path):
    file=tmp_path/"picture.png"
    Image.new("RGB",(8,8),"red").save(file)
    a=source("a.png",file.read_bytes())
    Image.new("RGB",(8,8),"blue").save(file)
    b=source("b.png",file.read_bytes())
    ident,_,_=build(engine,{"asset_ids":[a,b],"modality":"image"})
    result=execute_mining(engine,{"build_id":ident})
    with session_scope(engine) as session:
        assert session.scalar(select(SampleRelation.kind).where(SampleRelation.profile_run_id==result["profile_run_id"]))=="image_near_duplicate"


def test_portable_zip_export_requires_approval_and_is_deterministic(engine,source,tmp_path):
    import zipfile
    from xiaoyi_catalog.sample_engine import execute_export,asset_path
    aid=source("export.txt","hello\nworld\n")
    ident,manifest,digest=build(engine,{"asset_ids":[aid],"modality":"text"})
    with pytest.raises(ValueError,match="approval"):
        execute_export(engine,ident)
    with session_scope(engine) as session:
        publish_build(session,ident,Principal(1,"reviewer","reviewer"),digest)
    result=execute_export(engine,ident)
    assert execute_export(engine,ident)["sha256"]==result["sha256"]
    with zipfile.ZipFile(asset_path(result["storage_key"])) as archive:
        assert "load.py" in archive.namelist()
        archive.extractall(tmp_path/"extracted")
    loader=runpy.run_path(str(tmp_path/"extracted"/"load.py"))
    assert len(list(loader["load"]()))==2


def test_profile_receipt_prevents_replay_after_publish_before_job_finish(engine,source):
    from xiaoyi_catalog.jobs import enqueue,claim,Guard
    from xiaoyi_catalog.models_jobs import Artifact
    aid=source("replay.csv","x\n1\n")
    with session_scope(engine) as session:
        job=enqueue(session,"profile.run",{"asset_id":aid},"fixture");jid=job.id
    job=claim(engine,"cpu","fixture-worker")
    guard=Guard(engine,job)
    first=execute_profile(engine,aid,guard)
    second=execute_profile(engine,aid,guard)
    assert first==second
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(ProfileRun))==1
        assert session.scalar(select(Artifact).where(Artifact.job_id==jid)).manifest==first


def test_cpu_comparison_demo_executes_fixed_budget_and_prediction_roundtrip(tmp_path,monkeypatch):
    from xiaoyi_catalog.sample_demo import run_demo
    monkeypatch.setenv("XIAOYI_STORAGE_ROOT",str(tmp_path/"unused"))
    result=run_demo(tmp_path/"fixture-demo")
    assert len(result["runs"])==4
    assert all(row["training_samples"]==80 for row in result["runs"])
    assert all(row["metrics"]["coverage"]==1 and row["metrics"]["expected_samples"]==40 for row in result["runs"])
    assert result["comparison"]["evaluation_build_hash"]==result["evaluation_manifest_hash"]
    assert len(result["comparison"]["items"])==4
    assert "synthetic" in result["evidence"]


def test_experiment_api_idempotency_lists_and_shared_submit(engine,source):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from xiaoyi_catalog.api_builds import register_builds,submit_experiment_job
    aid=source("idempotent-evaluation.jsonl",'{"text":"yes","label":"a"}\n')
    bid,manifest,_=build(engine,{"asset_ids":[aid],"modality":"text","label_column":"label","splits":{"test":1}})
    row=next(iter_build_rows(manifest))
    pred=source("idempotent-prediction.jsonl",json.dumps({"build_id":bid,"sample_id":row["sample_id"],"prediction":"a"})+"\n")
    app=FastAPI();app.state.dev_auth=True;app.state.engine=engine;register_builds(app,engine)
    client=TestClient(app)
    spec={"build_id":bid,"prediction_asset_id":pred,"model":{"name":"fixed","version":"1","training_budget":"1"},"protocol":{"evaluation_build_id":bid}}
    first=client.post("/api/experiments/import",json=spec)
    assert first.status_code==202,first.text
    assert client.post("/api/experiments/import",json=spec).json()==first.json()
    with session_scope(engine) as session:
        assert submit_experiment_job(session,spec,Principal(0,"local-demo","admin"))==first.json()
    explicit=client.post("/api/experiments/import",json=spec,headers={"Idempotency-Key":"repeat"})
    assert explicit.status_code==202
    changed={**spec,"model":{"name":"other","version":"1","training_budget":"1"}}
    assert client.post("/api/experiments/import",json=changed,headers={"Idempotency-Key":"repeat"}).status_code==400
    page=client.get("/api/experiments?order=desc&limit=1").json()
    assert page["items"][0]["id"]==explicit.json()["experiment_id"]
    next_page=client.get(f"/api/experiments?order=desc&after_id={page['next_after_id']}").json()
    assert next_page["items"][0]["id"]==first.json()["experiment_id"]


def test_sample_identity_is_location_based_and_source_content_cannot_be_overwritten(engine,source,tmp_path):
    aid=source("immutable.txt","original\n")
    first,manifest,_=build(engine,{"asset_ids":[aid],"modality":"text"})
    row=next(iter_build_rows(manifest))
    with session_scope(engine) as session:
        asset=session.get(Asset,aid)
        assert row["sample_id"]==digest_json([asset.dataset_version_id,asset.path,"1"])
    replacement=tmp_path/"replacement.txt";replacement.write_text("replacement\n",encoding="utf-8")
    artifact=store_file(replacement)
    with session_scope(engine) as session:
        asset=session.get(Asset,aid)
        for key,value in artifact.items():setattr(asset,key,value)
        approval=session.get(DownloadRequest,asset.download_request_id)
        approval.manifest={"files":[{"path":asset.path,"size":asset.size_bytes,"sha256":asset.sha256}]}
        approval.manifest_hash=approval.approved_hash=digest_json(approval.manifest)
    with session_scope(engine) as session:
        next_build=create_build(session,{"asset_ids":[aid],"modality":"text"},"fixture");bid=next_build.id
    with pytest.raises(ValueError,match="new source version"):
        execute_build(engine,bid)
    with session_scope(engine) as session:
        assert session.get(DatasetBuild,bid).status=="failed"
        assert session.scalar(select(func.count()).select_from(Sample))==1
