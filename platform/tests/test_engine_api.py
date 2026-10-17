from fastapi.testclient import TestClient
from sqlalchemy import select
from xiaoyi_catalog.service import create_app
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.jobs import run_queue_once
from xiaoyi_catalog.models_jobs import Job
from xiaoyi_catalog.models_samples import DatasetBuild
from test_sample_engine import source


def test_build_job_publish_mine_export_api(engine, source):
    aid=source("rows.csv","text,label,group\na,a,x\nb,b,y\na,a,x\n")
    with TestClient(create_app(engine.url.database,dev_auth=True)) as client:
        response=client.post("/api/builds",json={"recipe":{"asset_ids":[aid],"label_column":"label","group_column":"group"}})
        assert response.status_code==202,response.text
        data=response.json()
        assert client.get(f"/api/jobs/{data['job_id']}").json()["status"]=="queued"
        assert run_queue_once(engine,"cpu")
        job=client.get(f"/api/jobs/{data['job_id']}").json()
        assert job["status"]=="succeeded",job
        build=client.get(f"/api/builds/{data['build_id']}").json()
        assert build["status"]=="ready",build
        assert build["sample_count"]==2
        assert client.post("/api/jobs",json={"kind":"build.export","payload":{"build_id":build["id"]}}).status_code==400
        assert client.post(f"/api/builds/{build['id']}/publish",json={"manifest_hash":"f"*64}).status_code==400
        assert client.post(f"/api/builds/{build['id']}/publish",json={"manifest_hash":build["manifest_hash"]}).status_code==200
        exported=client.post("/api/jobs",json={"kind":"build.export","payload":{"build_id":build["id"]}})
        assert exported.status_code==202,exported.text
        assert run_queue_once(engine,"io")
        exported_job=client.get(f"/api/jobs/{exported.json()['job_id']}").json()
        assert exported_job["status"]=="succeeded",exported_job
        response=client.get(f"/api/jobs/{exported.json()['job_id']}/artifact")
        assert response.status_code==200 and response.content[:2]==b"PK"
        mined=client.post(f"/api/builds/{build['id']}/mining",json={"build_id":build["id"]})
        assert mined.status_code==202,mined.text
        assert run_queue_once(engine,"cpu")
        assert client.get(f"/api/jobs/{mined.json()['job_id']}").json()["status"]=="succeeded"
        assert client.get(f"/api/builds/{build['id']}/profiles").json()["items"]


def test_readiness_and_reject_executable_job(engine,source):
    source("ready.csv","x\n1\n")
    with TestClient(create_app(engine.url.database,dev_auth=True)) as client:
        assert client.get("/api/health").status_code==200
        assert client.get("/api/ready").status_code==200
        assert client.post("/api/jobs",json={"kind":"shell.run","payload":{"code":"echo test"}}).status_code==400
