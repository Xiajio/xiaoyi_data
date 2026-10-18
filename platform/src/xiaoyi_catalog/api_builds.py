"""Authenticated asynchronous sample construction and experiment APIs."""
from __future__ import annotations
import json
from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
import pyarrow.parquet as pq
from .auth import principal, require
from .db import session_scope
from .experiments import ExperimentSpec, create_experiment, compare_experiments
from .mining import MiningSpec
from .models_samples import DatasetBuild, BuildSample, Experiment, ProfileRun, SampleRelation
from .recipes import Recipe
from .downloads import digest_json
from .sample_engine import asset_path, create_build, ready_build, publish_build

class BuildIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe: Recipe

class ApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

class ComparisonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment_ids: list[int] = Field(min_length=2,max_length=20)

def public_build(build):
    return {"id":build.id,"status":build.status,"requested_by":build.requested_by,"recipe":build.recipe,
            "recipe_hash":build.recipe_hash,"manifest_hash":build.manifest_hash,"sample_count":build.sample_count,
            "approved_by":build.approved_by,"error":build.error}

def public_experiment(item):
    return {"id":item.id,"build_id":item.build_id,"status":item.status,"model":item.model,
            "protocol":item.protocol,"metrics":item.metrics,"manifest":item.manifest,"error":item.error}

def submit_build_job(session,recipe,actor,key=None):
    """Shared HTTP/CLI/MCP submission contract; caller supplies authenticated principal."""
    from .jobs import enqueue
    from .models_jobs import Job
    recipe=Recipe.model_validate(recipe).model_dump()
    name=actor.username if hasattr(actor,"username") else str(actor)
    key=key or "build:"+digest_json(recipe)
    previous=session.scalar(select(Job).where(Job.actor==name,Job.request_key==key))
    if previous:
        old=session.get(DatasetBuild,previous.payload.get("build_id")) if previous.kind=="build.run" else None
        if not old or old.recipe!=recipe:
            raise ValueError("idempotency key was already used for different inputs")
        return {"build_id":old.id,"job_id":previous.id,"status":previous.status}
    build=create_build(session,recipe,name)
    job=enqueue(session,"build.run",{"build_id":build.id},actor,idempotency_key=key)
    return {"build_id":build.id,"job_id":job.id,"status":"queued"}

def submit_experiment_job(session,spec,actor,key=None):
    """Idempotent experiment submission with immutable prediction/model/protocol inputs."""
    from .jobs import enqueue
    from .models_jobs import Job
    spec=ExperimentSpec.model_validate(spec).model_dump()
    name=actor.username if hasattr(actor,"username") else str(actor)
    key=key or "experiment:"+digest_json(spec)
    previous=session.scalar(select(Job).where(Job.actor==name,Job.request_key==key))
    if previous:
        old=session.get(Experiment,previous.payload.get("experiment_id")) if previous.kind=="experiment.import" else None
        old_spec={"build_id":old.build_id,"prediction_asset_id":old.prediction_asset_id,"model":old.model,"protocol":old.protocol} if old else None
        if old_spec!=spec:
            raise ValueError("idempotency key was already used for different inputs")
        return {"experiment_id":old.id,"job_id":previous.id,"status":previous.status}
    experiment=create_experiment(session,spec,name)
    job=enqueue(session,"experiment.import",{"experiment_id":experiment.id},actor,idempotency_key=key)
    return {"experiment_id":experiment.id,"job_id":job.id,"status":"queued"}

def register_builds(app,engine):
    @app.post("/api/builds",status_code=202)
    def submit_build(body:BuildIn,request:Request):
        actor=require(request,{"researcher","reviewer","admin"})
        try:
            with session_scope(engine) as session:
                return submit_build_job(session,body.recipe,actor,request.headers.get("Idempotency-Key"))
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/api/builds")
    def builds(request:Request,after_id:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),order:Literal["asc","desc"]="asc"):
        principal(request)
        with session_scope(engine) as session:
            query=select(DatasetBuild)
            if order=="asc":
                query=query.where(DatasetBuild.id>after_id).order_by(DatasetBuild.id)
            else:
                query=query.order_by(DatasetBuild.id.desc())
                if after_id:
                    query=query.where(DatasetBuild.id<after_id)
            rows=list(session.scalars(query.limit(limit)))
            return {"items":[public_build(row) for row in rows],"next_after_id":rows[-1].id if rows else after_id}

    @app.get("/api/builds/{build_id}")
    def get_build(build_id:int,request:Request):
        principal(request)
        with session_scope(engine) as session:
            build = session.get(DatasetBuild,build_id)
            if not build:
                raise HTTPException(404,"build not found")
            return public_build(build)

    @app.get("/api/builds/{build_id}/manifest")
    def manifest(build_id:int,request:Request):
        principal(request)
        try:
            with session_scope(engine) as session:
                build = ready_build(session,build_id)
                return {"manifest":build.manifest,"sha256":build.manifest_hash}
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc

    @app.get("/api/builds/{build_id}/samples")
    def samples(build_id:int,request:Request,after_id:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100)):
        principal(request)
        try:
            with session_scope(engine) as session:
                build = ready_build(session,build_id)
                members = list(session.scalars(select(BuildSample).where(BuildSample.build_id==build_id,BuildSample.id>after_id).order_by(BuildSample.id).limit(limit)))
                # Read one bounded Parquet row group per required shard; large values stay out of JSON by default.
                result=[]
                for member in members:
                    result.append({"id":member.id,"sample_id":member.sample_id,"split":member.split,
                                   "group_key":member.group_key,"shard":member.shard,"row_index":member.row_index})
                return {"items":result,"next_after_id":members[-1].id if members else after_id}
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc

    @app.get("/api/builds/{build_id}/artifacts/{sha256}")
    def artifact(build_id:int,sha256:str,request:Request):
        principal(request)
        try:
            with session_scope(engine) as session:
                build = ready_build(session,build_id)
                if build.status != "published" or build.approved_hash != build.manifest_hash:
                    raise ValueError("artifact delivery requires human publication approval")
                objects = build.manifest["shards"]+build.manifest["inputs"]+[build.manifest["exclusions"]]
                entry = next((item for item in objects if item["sha256"]==sha256),None)
                if not entry:
                    raise HTTPException(404,"artifact not in build")
                return FileResponse(asset_path(entry["storage_key"]),filename=sha256)
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc

    @app.post("/api/builds/{build_id}/publish")
    def publish(build_id:int,body:ApprovalIn,request:Request):
        actor=require(request,{"reviewer","admin"},human=True)
        try:
            with session_scope(engine) as session:
                return public_build(publish_build(session,build_id,actor,body.manifest_hash))
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/api/builds/{build_id}/mining",status_code=202)
    def mine(build_id:int,body:MiningSpec,request:Request):
        actor=require(request,{"researcher","reviewer","admin"})
        from .jobs import enqueue
        if body.build_id != build_id:
            raise HTTPException(400,"build_id mismatch")
        try:
            with session_scope(engine) as session:
                ready_build(session,build_id)
                if body.benchmark_build_id:
                    ready_build(session,body.benchmark_build_id)
                job=enqueue(session,"mining.run",body.model_dump(),actor)
                return {"job_id":job.id,"build_id":build_id}
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/api/builds/{build_id}/profiles")
    def profiles(build_id:int,request:Request,after_id:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100)):
        principal(request)
        with session_scope(engine) as session:
            rows=session.scalars(select(ProfileRun).where(ProfileRun.build_id==build_id,ProfileRun.id>after_id).order_by(ProfileRun.id).limit(limit))
            return {"items":[{"id":row.id,"producer":row.producer,"summary":row.summary,"manifest":row.manifest} for row in rows]}

    @app.get("/api/profiles/{profile_id}/findings")
    def findings(profile_id:int,request:Request,after_id:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100)):
        principal(request)
        with session_scope(engine) as session:
            rows=session.scalars(select(SampleRelation).where(SampleRelation.profile_run_id==profile_id,SampleRelation.id>after_id).order_by(SampleRelation.id).limit(limit))
            return {"items":[{"id":r.id,"source_sample_id":r.source_sample_id,"target_sample_id":r.target_sample_id,"kind":r.kind,"score":r.score,"algorithm":r.algorithm} for r in rows]}

    @app.post("/api/experiments/import",status_code=202)
    def import_experiment(body:ExperimentSpec,request:Request):
        actor=require(request,{"researcher","reviewer","admin"})
        try:
            with session_scope(engine) as session:
                return submit_experiment_job(session,body,actor,request.headers.get("Idempotency-Key"))
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/api/experiments")
    def experiments(request:Request,after_id:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),order:Literal["asc","desc"]="asc"):
        principal(request)
        with session_scope(engine) as session:
            query=select(Experiment)
            if order=="asc":
                query=query.where(Experiment.id>after_id).order_by(Experiment.id)
            else:
                query=query.order_by(Experiment.id.desc())
                if after_id:
                    query=query.where(Experiment.id<after_id)
            rows=list(session.scalars(query.limit(limit)))
            return {"items":[public_experiment(row) for row in rows],"next_after_id":rows[-1].id if rows else after_id}

    @app.get("/api/experiments/{experiment_id}")
    def get_experiment(experiment_id:int,request:Request):
        principal(request)
        with session_scope(engine) as session:
            item=session.get(Experiment,experiment_id)
            if not item:
                raise HTTPException(404,"experiment not found")
            return public_experiment(item)

    @app.post("/api/experiments/compare")
    def compare(body:ComparisonIn,request:Request):
        principal(request)
        try:
            with session_scope(engine) as session:
                return compare_experiments(session,body.experiment_ids)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/api/experiments/{experiment_id}/errors")
    def errors(experiment_id:int,request:Request):
        require(request,{"researcher","reviewer","admin"})
        with session_scope(engine) as session:
            item=session.get(Experiment,experiment_id)
            if not item or item.status!="completed":
                raise HTTPException(409,"experiment not completed")
            return FileResponse(asset_path(item.manifest["errors"]["storage_key"]),filename=f"experiment-{experiment_id}-errors.parquet")
