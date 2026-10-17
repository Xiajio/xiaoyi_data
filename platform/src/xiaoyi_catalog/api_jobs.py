"""Authenticated asynchronous operations; provider and filesystem details stay server-side."""
from typing import Literal
from fastapi import HTTPException, Request, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from .auth import principal, require
from .db import session_scope
from .jobs import enqueue, to_dict, managed, cancel, retry, PUBLIC_KINDS, JobError
from .models_jobs import Job, RunEvent

class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    payload: dict = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None,min_length=1,max_length=128)

def submit_public_job(session,kind,payload,actor,key=None):
    from .models_assets import Asset
    from .pairing import _approved_asset, export_snapshot
    if kind not in PUBLIC_KINDS:
        raise JobError("请通过数据构建或原业务入口提交此操作")
    if kind == "asset.derive" and (actor.kind != "human" or actor.role not in {"reviewer","admin"}):
        raise JobError("派生操作需要人类审核员账号")
    allowed = {"build_id"} if kind == "build.export" else {"snapshot_id"} if kind == "bundle.build" else {"asset_id","operation"} if kind == "asset.derive" else {"asset_id"}
    if set(payload)-allowed:
        raise JobError("任务参数含不支持字段")
    field = "build_id" if kind == "build.export" else "snapshot_id" if kind == "bundle.build" else "asset_id"
    ident = payload.get(field)
    if isinstance(ident,bool) or not isinstance(ident,int) or ident <= 0:
        raise JobError("任务需要有效的对象 ID")
    if kind == "build.export":
        from .sample_engine import ready_build
        build = ready_build(session,ident)
        if build.status != "published" or build.approved_hash != build.manifest_hash:
            raise JobError("数据构建必须先经人工批准发布")
    elif kind == "bundle.build":
        snapshot = export_snapshot(session,ident)
        if not snapshot.get("assets"):
            raise JobError("快照没有冻结资产清单，不能生成研究包")
    else:
        asset = session.get(Asset,ident)
        if asset is None or not _approved_asset(session,asset):
            raise JobError("输入资产不存在或尚未完整获批")
        if kind == "asset.derive" and payload.get("operation","deduplicate") != "deduplicate":
            raise JobError("不支持的派生操作")
    key = key or f"{kind}:{ident}:v1"
    return enqueue(session,kind,payload,actor,key)

def register_jobs(app,engine):
    @app.get("/api/ready")
    def ready():
        from .operations import readiness,OperationsError
        try:
            return readiness(engine)
        except OperationsError as exc:
            raise HTTPException(503,str(exc)) from exc

    @app.post("/api/jobs",status_code=202)
    def submit(body:JobIn,request:Request):
        actor=require(request,{"researcher","reviewer","admin"})
        try:
            with session_scope(engine) as session:
                return to_dict(submit_public_job(session,body.kind,body.payload,actor,body.idempotency_key))
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/api/jobs")
    def jobs(request:Request,after_id:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),order:Literal["asc","desc"]="asc"):
        actor=principal(request)
        with session_scope(engine) as session:
            rows=session.scalars(select(Job).where(Job.actor==actor.username,Job.id>after_id).order_by(Job.id.desc() if order=="desc" else Job.id).limit(limit)).all()
            return {"items":[to_dict(row) for row in rows],"next_after_id":rows[-1].id if rows else after_id}

    @app.get("/api/jobs/{job_id}")
    def job(job_id:int,request:Request):
        actor=principal(request)
        try:
            with session_scope(engine) as session:
                return to_dict(managed(session,job_id,actor))
        except JobError as exc:
            raise HTTPException(404,str(exc)) from exc

    @app.get("/api/jobs/{job_id}/events")
    def events(job_id:int,request:Request,after_id:int=Query(0,ge=0)):
        actor=principal(request)
        try:
            with session_scope(engine) as session:
                managed(session,job_id,actor)
                return {"items":[{"id":e.id,"kind":e.kind,"detail":e.detail,"created_at":e.created_at.isoformat()} for e in session.scalars(select(RunEvent).where(RunEvent.job_id==job_id,RunEvent.id>after_id).order_by(RunEvent.id).limit(100))]}
        except JobError as exc:
            raise HTTPException(404,str(exc)) from exc

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id:int,request:Request):
        actor=require(request,{"researcher","reviewer","admin"})
        try:
            with session_scope(engine) as session:
                return to_dict(cancel(session,job_id,actor))
        except JobError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/api/jobs/{job_id}/retry",status_code=202)
    def retry_job(job_id:int,request:Request):
        actor=require(request,{"researcher","reviewer","admin"})
        try:
            with session_scope(engine) as session:
                return to_dict(retry(session,job_id,actor))
        except JobError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/api/jobs/{job_id}/artifact")
    def download_artifact(job_id:int,request:Request):
        from fastapi.responses import FileResponse
        from .sample_engine import asset_path
        actor=principal(request)
        try:
            with session_scope(engine) as session:
                job=managed(session,job_id,actor)
                if job.kind!="build.export" or job.status!="succeeded":
                    raise JobError("导出任务尚未完成")
                path=asset_path(job.result["storage_key"])
                return FileResponse(path,media_type="application/zip",filename=f"dataset-build-{job.payload['build_id']}.zip")
        except (JobError,ValueError,KeyError) as exc:
            raise HTTPException(409,str(exc)) from exc
