"""Authenticated platform endpoints, kept separate from the established catalog routes."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path

from fastapi import HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from . import auth
from .auth import principal, require, COOKIE, check_password, mint_session
from .db import session_scope
from .models import Audit, Enrichment, utcnow
from .models_assets import AuthUser, AuthSession, DownloadRequest, Asset
from .downloads import (DownloadError, request_download, decide_download, request_to_dict,
                        file_inventory, assets_for_version, storage_root)
from .bundles import build_bundle

class LoginIn(BaseModel):
    username: str = Field(min_length=1,max_length=128)
    password: str = Field(min_length=1,max_length=1024)

class DownloadIn(BaseModel):
    dataset_version_id: int
    files: list[str] = Field(min_length=1,max_length=10000)
    max_bytes: int = Field(gt=0)

class DecisionIn(BaseModel):
    decision: str
    note: str = Field(default="",max_length=4000)

def register_platform(app, engine):
    from .api_collection import register_collection
    register_collection(app, engine)

    @app.get("/api/health")
    def health():
        return {"status":"ok","version":"0.5.0"}

    @app.get("/api/auth/me")
    def me(request: Request):
        return principal(request).public()

    @app.post("/api/auth/login")
    def login(body: LoginIn, request: Request, response: Response):
        if app.state.dev_auth:
            return principal(request).public()
        with session_scope(engine) as session:
            user=session.scalar(select(AuthUser).where(AuthUser.username==body.username))
            valid=check_password(body.password,user.password_hash if user else "scrypt:00000000000000000000000000000000:"+"0"*128)
            if not user or not user.enabled or user.kind!="human" or not valid:
                raise HTTPException(401,"账号或密码不正确")
            token=mint_session(session,user)
            response.set_cookie(COOKIE,token,httponly=True,samesite="lax",max_age=43200,
                secure=os.environ.get("XIAOYI_SECURE_COOKIE","0")=="1",path="/")
            session.add(Audit(actor=user.username,action="login",detail="authenticated"))
            return {"id":user.id,"username":user.username,"role":user.role,"kind":user.kind}

    @app.post("/api/auth/logout")
    def logout(request: Request,response: Response):
        token=auth.request_token(request)
        with session_scope(engine) as session:
            row=session.scalar(select(AuthSession).where(AuthSession.token_hash==hashlib.sha256(token.encode()).hexdigest()))
            if row: session.delete(row)
        response.delete_cookie(COOKIE,path="/")
        return {"ok":True}

    @app.get("/api/agent-runs")
    def runs(request: Request):
        principal(request)
        from .models_runtime import AgentRun
        from .agent_runtime import run_to_dict
        with session_scope(engine) as session:
            return {"items":[run_to_dict(r) for r in session.scalars(select(AgentRun).order_by(AgentRun.created_at.desc()).limit(100))]}

    @app.post("/api/agent-runs",status_code=201)
    def create_run(payload: dict,request: Request):
        actor=require(request,{"researcher","reviewer","admin"})
        from .agent_runtime import create_run as create, run_to_dict
        try:
            with session_scope(engine) as session:
                return run_to_dict(create(session,payload,actor.username))
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/api/agent-runs/{run_id}")
    def run(run_id: int,request: Request):
        principal(request)
        from .models_runtime import AgentRun
        from .agent_runtime import run_to_dict
        with session_scope(engine) as session:
            item=session.get(AgentRun,run_id)
            if not item: raise HTTPException(404,"任务不存在")
            return run_to_dict(item)

    @app.get("/api/agent-runs/{run_id}/events")
    def events(run_id: int,request: Request,after_id:int=0):
        principal(request)
        from .agent_runtime import list_events
        with session_scope(engine) as session:
            values=list_events(session,run_id,after_id=after_id)
            return values if isinstance(values,dict) else {"items":values}

    @app.post("/api/agent-runs/{run_id}/cancel")
    def cancel(run_id:int,request:Request):
        actor=require(request,{"researcher","reviewer","admin"})
        from .agent_runtime import cancel_run, run_to_dict
        try:
            with session_scope(engine) as session:
                return run_to_dict(cancel_run(session,run_id,actor))
        except ValueError as exc: raise HTTPException(400,str(exc)) from exc

    @app.post("/api/agent-runs/{run_id}/retry")
    def retry(run_id:int,request:Request):
        actor=require(request,{"researcher","reviewer","admin"})
        from .agent_runtime import retry_run, run_to_dict
        try:
            with session_scope(engine) as session:
                return run_to_dict(retry_run(session,run_id,actor))
        except ValueError as exc: raise HTTPException(400,str(exc)) from exc

    @app.get("/api/dataset-versions/{version_id}/files")
    def files(version_id:int,request:Request):
        principal(request)
        try:
            with session_scope(engine) as session:
                return file_inventory(session,version_id)
        except DownloadError as exc: raise HTTPException(400,str(exc)) from exc

    @app.get("/api/dataset-versions/{version_id}/assets")
    def assets(version_id:int,request:Request):
        principal(request)
        with session_scope(engine) as session:
            return assets_for_version(session,version_id)

    @app.get("/api/download-requests")
    def downloads(request:Request):
        principal(request)
        with session_scope(engine) as session:
            return {"items":[request_to_dict(r) for r in session.scalars(select(DownloadRequest).order_by(DownloadRequest.id.desc()).limit(100))]}

    @app.post("/api/download-requests",status_code=201)
    def download(body:DownloadIn,request:Request):
        actor=require(request,{"researcher","reviewer","admin"})
        try:
            with session_scope(engine) as session:
                return request_to_dict(request_download(session,body.model_dump(),actor.username))
        except DownloadError as exc: raise HTTPException(400,str(exc)) from exc

    @app.post("/api/download-requests/{request_id}/decision")
    def decide(request_id:int,body:DecisionIn,request:Request):
        actor=require(request,{"reviewer","admin"},human=True)
        try:
            with session_scope(engine) as session:
                return request_to_dict(decide_download(session,request_id,body.decision,actor.username,body.note))
        except DownloadError as exc: raise HTTPException(400,str(exc)) from exc

    @app.get("/api/snapshots/{snapshot_id}/bundle")
    def bundle(snapshot_id:int,request:Request):
        principal(request)
        from .models_assets import ResearchBundle
        from .pairing import export_snapshot
        try:
            with session_scope(engine) as session:
                export_snapshot(session,snapshot_id)
                value=session.scalar(select(ResearchBundle).where(ResearchBundle.snapshot_id==snapshot_id))
                if value is None:
                    raise HTTPException(409,{"code":"bundle_not_ready","message":"请先提交研究包构建任务"})
                root=storage_root().resolve()
                path=(root/value.storage_key).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise HTTPException(409,"研究包文件缺失，需要从备份恢复")
                digest=value.sha256
            return FileResponse(path,media_type="application/zip",filename=f"research-{snapshot_id}.zip",headers={"ETag":digest})
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/api/datasets/register",status_code=201)
    def manual(payload:dict,request:Request):
        actor=require(request,{"researcher","reviewer","admin"})
        from .adapters.manual import register_manual
        try:
            run=register_manual(engine,payload)
            with session_scope(engine) as session:
                session.add(Audit(actor=actor.username,action="manual_registration",detail=str(payload.get("source_key",""))))
            return {"id":run.id,"status":run.status,"stats":run.stats}
        except ValueError as exc: raise HTTPException(400,str(exc)) from exc

    @app.get("/api/operations")
    def operations(request:Request):
        require(request,{"reviewer","admin"})
        from .models_runtime import AgentRun
        from .operations import queue_status
        import shutil
        with session_scope(engine) as session:
            return {"tasks":dict(session.execute(select(AgentRun.status,func.count()).group_by(AgentRun.status)).all()),
                    "downloads":dict(session.execute(select(DownloadRequest.status,func.count()).group_by(DownloadRequest.status)).all()),
                    "review_backlog":session.scalar(select(func.count()).select_from(Enrichment).where(Enrichment.status.in_(["candidate","needs-info"]))),
                    "asset_bytes":session.scalar(select(func.coalesce(func.sum(Asset.size_bytes),0))),
                    "disk_free_bytes":shutil.disk_usage(storage_root()).free,
                    "as_of":utcnow().isoformat(), "queues":queue_status(session),
                    "approval_waiting": {
                        "research_runs":session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.status=="waiting_approval")),
                        "download_requests":session.scalar(select(func.count()).select_from(DownloadRequest).where(DownloadRequest.status=="pending"))}}


    @app.get("/api/assets/{asset_id}/preview")
    def preview(asset_id:int,request:Request):
        principal(request)
        from .preview import preview_asset
        try:
            with session_scope(engine) as session:
                return preview_asset(session,asset_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(400,str(exc)) from exc


    @app.post("/api/import-requests",status_code=201)
    def import_request(payload:dict,request:Request):
        actor=require(request,{"researcher","reviewer","admin"})
        from .asset_import import request_local_import
        try:
            with session_scope(engine) as session:
                return request_to_dict(request_local_import(session,payload,actor.username))
        except (KeyError, ValueError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/api/assets/{asset_id}/derive",status_code=202)
    def derive(asset_id:int,payload:dict,request:Request):
        actor=require(request,{"reviewer","admin"},human=True)
        from .api_jobs import submit_public_job
        from .jobs import to_dict
        try:
            if set(payload)-{"operation"}:
                raise ValueError("派生参数包含不支持字段")
            with session_scope(engine) as session:
                return to_dict(submit_public_job(session,"asset.derive",{"asset_id":asset_id,"operation":payload.get("operation","deduplicate")},actor))
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc
