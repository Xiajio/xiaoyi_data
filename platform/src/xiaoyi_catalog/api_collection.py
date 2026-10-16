"""Authenticated, model-independent HF collection and persisted schedule endpoints."""
from __future__ import annotations

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from .auth import principal, require
from .db import session_scope
from .models import DatasetVersion, Evidence


class CollectionScopeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search: str = Field(default="", max_length=200)
    author: str = Field(default="", max_length=128)
    languages: list[str] = Field(default_factory=list, max_length=20)
    task_categories: list[str] = Field(default_factory=list, max_length=20)
    limit: int = Field(default=50, ge=1, le=300)

    @field_validator("languages", "task_categories")
    @classmethod
    def bounded_labels(cls, values):
        if any(not value.strip() or len(value) > 100 for value in values):
            raise ValueError("筛选项不能为空或超过 100 字符")
        return list(dict.fromkeys(value.strip() for value in values))


class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    scope: CollectionScopeIn = Field(default_factory=CollectionScopeIn)
    interval_minutes: int = Field(default=1440, ge=15, le=10080)
    enabled: bool = False


class ScheduleUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=160)
    scope: CollectionScopeIn | None = None
    interval_minutes: int | None = Field(default=None, ge=15, le=10080)
    enabled: bool | None = None

    @field_validator("name", "scope", "interval_minutes", "enabled", mode="before")
    @classmethod
    def explicit_null_is_invalid(cls, value):
        if value is None:
            raise ValueError("更新字段不能为 null")
        return value


def register_collection(app, engine):
    from .models_collection import CollectionJob, CollectionSchedule
    from .collection_runtime import (
        create_collection_job, job_to_dict, list_job_events,
        cancel_collection_job, retry_collection_job,
        create_collection_schedule, update_collection_schedule, schedule_to_dict,
    )

    def get_job(session, ident):
        job = session.get(CollectionJob, ident)
        if job is None:
            raise HTTPException(404, "采集任务不存在")
        return job

    @app.get("/api/collection-jobs")
    def jobs(request: Request, limit: int = Query(default=100, ge=1, le=300)):
        principal(request)
        with session_scope(engine) as session:
            rows = session.scalars(select(CollectionJob).order_by(CollectionJob.id.desc()).limit(limit))
            return {"items": [job_to_dict(row) for row in rows]}

    @app.post("/api/collection-jobs", status_code=201)
    def create_job(body: CollectionScopeIn, request: Request):
        actor = require(request, {"researcher", "reviewer", "admin"})
        try:
            with session_scope(engine) as session:
                return job_to_dict(create_collection_job(session, body.model_dump(), actor.username))
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/collection-jobs/{job_id}")
    def job(job_id: int, request: Request):
        principal(request)
        with session_scope(engine) as session:
            return job_to_dict(get_job(session, job_id))

    @app.get("/api/collection-jobs/{job_id}/events")
    def events(job_id: int, request: Request, after_id: int = Query(default=0, ge=0)):
        principal(request)
        with session_scope(engine) as session:
            get_job(session, job_id)
            items = list_job_events(session, job_id, after_id=after_id)
            return items if isinstance(items, dict) else {"items": items}

    @app.post("/api/collection-jobs/{job_id}/cancel")
    def cancel(job_id: int, request: Request):
        actor = require(request, {"researcher", "reviewer", "admin"})
        try:
            with session_scope(engine) as session:
                get_job(session, job_id)
                return job_to_dict(cancel_collection_job(session, job_id, actor))
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/collection-jobs/{job_id}/retry", status_code=201)
    def retry(job_id: int, request: Request):
        actor = require(request, {"researcher", "reviewer", "admin"})
        try:
            with session_scope(engine) as session:
                get_job(session, job_id)
                return job_to_dict(retry_collection_job(session, job_id, actor))
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/collection-schedules")
    def schedules(request: Request):
        principal(request)
        with session_scope(engine) as session:
            return {"items": [schedule_to_dict(row) for row in session.scalars(
                select(CollectionSchedule).order_by(CollectionSchedule.id.desc()))]}

    @app.post("/api/collection-schedules", status_code=201)
    def create_schedule(body: ScheduleIn, request: Request):
        actor = require(request, {"reviewer", "admin"}, human=True)
        try:
            with session_scope(engine) as session:
                return schedule_to_dict(create_collection_schedule(session, body.model_dump(), actor))
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/collection-schedules/{schedule_id}")
    def update_schedule(schedule_id: int, body: ScheduleUpdateIn, request: Request):
        actor = require(request, {"reviewer", "admin"}, human=True)
        try:
            with session_scope(engine) as session:
                if session.get(CollectionSchedule, schedule_id) is None:
                    raise HTTPException(404, "同步计划不存在")
                return schedule_to_dict(update_collection_schedule(
                    session, schedule_id, body.model_dump(exclude_unset=True), actor))
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/dataset-versions/{version_id}/evidence")
    def version_evidence(version_id: int, request: Request):
        principal(request)
        with session_scope(engine) as session:
            version = session.get(DatasetVersion, version_id)
            if version is None:
                raise HTTPException(404, "数据集版本不存在")
            items = session.scalars(select(Evidence).where(
                Evidence.dataset_version_id == version_id).order_by(Evidence.id))
            return {"dataset_id": version.dataset_id, "dataset_version_id": version.id,
                    "revision": version.revision, "provenance_status": version.provenance_status,
                    "items": [{"id": item.id, "dataset_version_id": item.dataset_version_id,
                               "kind": item.kind, "location": item.location, "excerpt": item.excerpt}
                              for item in items]}
