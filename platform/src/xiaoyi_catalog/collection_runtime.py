"""Persistent metadata collection and rule enrichment; no model API or file download."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .adapters.base import CollectScope
from .collect import CollectionCancelled, classify_error, collect, safe_collection_error, safe_error_fields
from .db import session_scope
from .enrich import PRODUCER, enrich_version
from .summary_runtime import submit_summary
from .models import Audit, DatasetVersion, Enrichment, Run, utcnow
from .models_collection import CollectionEvent, CollectionJob, CollectionSchedule

MAX_ATTEMPTS = 3
STAT_KEYS = ("found", "new", "updated", "duplicates", "skipped_stale", "failed", "processed")
ACTIVE_STATUSES = ("queued", "running")


class CollectionScopeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    search: str = Field(default="", max_length=256)
    author: str = Field(default="", max_length=160)
    languages: list[str] = Field(default_factory=list, max_length=20)
    task_categories: list[str] = Field(default_factory=list, max_length=20)
    limit: int = Field(default=50, ge=1, le=300)

    @field_validator("search", "author")
    @classmethod
    def clean_text(cls, value):
        return value.strip()

    @field_validator("languages", "task_categories")
    @classmethod
    def clean_filters(cls, values):
        if any(not value.strip() or len(value) > 100 for value in values):
            raise ValueError("语言和任务标签必须是非空、最多 100 字符的字符串")
        return sorted(set(value.strip().lower() for value in values))


def normalize_scope(scope: dict) -> dict:
    return CollectionScopeInput.model_validate(scope).model_dump()


def _collect_scope(scope):
    return CollectScope(search=scope["search"], author=scope["author"],
        languages=tuple(scope["languages"]), task_categories=tuple(scope["task_categories"]), limit=scope["limit"])


def _key(scope):
    return hashlib.sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _iso(value):
    if value is None:
        return None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()


def _event(session, job, kind, message, detail=None):
    session.add(CollectionEvent(job_id=job.id, kind=kind, message=message, detail=detail or {}))


def _public_stats(stats):
    return {**{key: (stats or {}).get(key, 0) for key in STAT_KEYS},
            "failures": list((stats or {}).get("failures", []))[:50]}


def job_to_dict(job):
    failures = (job.stats or {}).get("failures", [])
    failure = next((item for item in failures if not item.get("source_key")), failures[0] if failures else {})
    return {"id": job.id, "actor": job.actor, "requested_by": job.actor, "status": job.status,
        "stage": job.stage, "scope": job.scope, "trigger": job.trigger, "schedule_id": job.schedule_id,
        "retry_of": job.retry_of, "run_id": job.run_id,
        "stats": _public_stats(job.stats),
        "current_source_key": job.current_source_key, "error": job.error, "error_kind": job.error_kind,
        "error_type": failure.get("error_type", ""), "http_status": failure.get("http_status"),
        "analysis_status": job.analysis_status, "dataset_version_ids": job.dataset_version_ids,
        "attempts": job.attempts, "cancel_requested": job.cancel_requested,
        "created_at": _iso(job.created_at), "started_at": _iso(job.started_at), "finished_at": _iso(job.finished_at)}


def list_job_events(session, job_id: int, after_id: int = 0):
    if not session.get(CollectionJob, job_id):
        raise ValueError("采集任务不存在")
    return [{"id": event.id, "job_id": event.job_id, "kind": event.kind, "message": event.message,
             "detail": event.detail, "timestamp": _iso(event.created_at), "created_at": _iso(event.created_at)}
            for event in session.scalars(select(CollectionEvent).where(CollectionEvent.job_id == job_id,
                CollectionEvent.id > max(0, after_id)).order_by(CollectionEvent.id).limit(1000))]


def create_collection_job(session, scope: dict, actor, trigger="manual", schedule_id=None, retry_of=None):
    scope = normalize_scope(scope)
    username = actor if isinstance(actor, str) else getattr(actor, "username", "")
    if not username or len(username) > 128:
        raise ValueError("采集任务必须绑定已认证账号")
    if trigger not in {"manual", "schedule", "retry"}:
        raise ValueError("未知的采集触发方式")
    key = _key(scope)
    existing = session.scalar(select(CollectionJob).where(CollectionJob.active_key == key))
    if existing:
        return existing
    job = CollectionJob(actor=username, scope=scope, active_key=key, trigger=trigger, schedule_id=schedule_id,
        retry_of=retry_of, stats={key: 0 for key in STAT_KEYS}, dataset_version_ids=[], enriched_version_ids=[])
    # Python sqlite's legacy transaction mode does not BEGIN for SELECT or SAVEPOINT.
    # Force an outer transaction so releasing the uniqueness savepoint cannot commit
    # a job independently of the schedule advancement / caller's rollback.
    connection = session.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(CollectionJob).where(CollectionJob.active_key == key))
        if existing:
            return existing
        raise
    _event(session, job, "queued", "采集任务已排队", {"trigger": trigger, "scope": scope})
    session.add(Audit(actor=username, action="collection.queue", detail=json.dumps({"job_id": job.id})))
    return job


def _can_manage(job, actor):
    return bool(getattr(actor, "username", None) and getattr(actor, "role", None) in {"researcher", "reviewer", "admin"}
        and (actor.username == job.actor or (getattr(actor, "kind", None) == "human" and actor.role in {"reviewer", "admin"})))


def cancel_collection_job(session, job_id, actor):
    job = session.get(CollectionJob, job_id)
    if not job:
        raise ValueError("采集任务不存在")
    if not _can_manage(job, actor):
        raise PermissionError("仅任务发起者或人工审核员可取消采集")
    if job.status not in ACTIVE_STATUSES:
        return job
    job.cancel_requested = True
    if job.status == "queued":
        job.status, job.stage, job.finished_at, job.active_key = "cancelled", "finished", utcnow(), None
    _event(session, job, "cancel_requested", "已请求取消；正在执行的采集会在安全边界停止", {"actor": actor.username})
    session.add(Audit(actor=actor.username, action="collection.cancel", detail=json.dumps({"job_id": job.id})))
    session.flush()
    return job


def retry_collection_job(session, job_id, actor):
    job = session.get(CollectionJob, job_id)
    if not job:
        raise ValueError("采集任务不存在")
    if not _can_manage(job, actor):
        raise PermissionError("仅任务发起者或人工审核员可重试采集")
    if job.status not in {"failed", "partial", "cancelled"}:
        raise ValueError("只有失败、部分完成或取消的任务可以重试")
    return create_collection_job(session, job.scope, actor, trigger="retry", schedule_id=job.schedule_id, retry_of=job.id)


def _schedule_actor(actor):
    if getattr(actor, "kind", None) != "human" or getattr(actor, "role", None) not in {"reviewer", "admin"}:
        raise PermissionError("仅人工审核员或管理员可维护采集计划")


def schedule_to_dict(schedule):
    return {"id": schedule.id, "name": schedule.name, "scope": schedule.scope,
        "interval_minutes": schedule.interval_minutes, "enabled": schedule.enabled,
        "next_run_at": _iso(schedule.next_run_at) if schedule.enabled else None, "last_run_at": _iso(schedule.last_run_at),
        "last_job_id": schedule.last_job_id, "created_by": schedule.created_by,
        "created_at": _iso(schedule.created_at), "updated_at": _iso(schedule.updated_at)}


def _schedule_values(payload, previous=None):
    if not isinstance(payload, dict) or set(payload) - {"name", "scope", "interval_minutes", "enabled"}:
        raise ValueError("采集计划包含不支持的字段")
    value = {"name": "Hugging Face 定期采集", "scope": {}, "interval_minutes": 1440, "enabled": True}
    if previous:
        value.update({key: getattr(previous, key) for key in value})
    value.update(payload)
    name = value["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise ValueError("计划名称需要 1–160 个字符")
    if type(value["interval_minutes"]) is not int or not 15 <= value["interval_minutes"] <= 10080:
        raise ValueError("采集间隔需要 15–10080 分钟")
    if type(value["enabled"]) is not bool:
        raise ValueError("enabled 必须是布尔值")
    value["name"] = name.strip()
    value["scope"] = normalize_scope(value["scope"])
    return value


def create_collection_schedule(session, payload, actor):
    _schedule_actor(actor)
    value = _schedule_values(payload)
    schedule = CollectionSchedule(**value, created_by=actor.username,
        next_run_at=utcnow() + timedelta(minutes=value["interval_minutes"]))
    session.add(schedule)
    session.flush()
    session.add(Audit(actor=actor.username, action="collection.schedule.create", detail=json.dumps({"schedule_id": schedule.id})))
    return schedule


def update_collection_schedule(session, schedule_id, payload, actor):
    _schedule_actor(actor)
    schedule = session.get(CollectionSchedule, schedule_id)
    if not schedule:
        raise ValueError("采集计划不存在")
    value = _schedule_values(payload, schedule)
    reset = value["scope"] != schedule.scope or value["interval_minutes"] != schedule.interval_minutes or (value["enabled"] and not schedule.enabled)
    for key, item in value.items():
        setattr(schedule, key, item)
    if reset:
        schedule.next_run_at = utcnow() + timedelta(minutes=schedule.interval_minutes)
    session.add(Audit(actor=actor.username, action="collection.schedule.update", detail=json.dumps({"schedule_id": schedule.id, "enabled": schedule.enabled})))
    session.flush()
    return schedule


def tick_collection_schedules(engine, *, now=None):
    """Called under the shared worker lease. Coalesce all missed periods into one dispatch."""
    now = now or utcnow()
    queued = []
    with session_scope(engine) as session:
        schedules = session.scalars(select(CollectionSchedule).where(CollectionSchedule.enabled.is_(True),
            CollectionSchedule.next_run_at <= now).order_by(CollectionSchedule.id).with_for_update()).all()
        for schedule in schedules:
            active = session.scalar(select(CollectionJob).where(CollectionJob.active_key == _key(schedule.scope)))
            job = active or create_collection_job(session, schedule.scope, schedule.created_by,
                trigger="schedule", schedule_id=schedule.id)
            schedule.last_job_id, schedule.last_run_at = job.id, now
            schedule.next_run_at = now + timedelta(minutes=schedule.interval_minutes)
            if active:
                _event(session, job, "schedule_coalesced", "已有相同范围的任务，合并本次周期", {"schedule_id": schedule.id})
            else:
                queued.append(job.id)
    return queued


def _default_adapter():
    from .adapters.huggingface import DEFAULT_ENDPOINT, HuggingFaceAdapter
    return HuggingFaceAdapter(endpoint=os.getenv("HF_ENDPOINT", DEFAULT_ENDPOINT), sort="last_modified")


def _seed_retry_run(session, job):
    original = session.get(CollectionJob, job.retry_of) if job.retry_of else None
    prior = session.get(Run, original.run_id) if original and original.run_id else None
    if not prior:
        return None
    items = {}
    for key, value in (prior.stats or {}).get("items", {}).items():
        # Reuse completed pinned versions while retrying failures at their original
        # revision before reading a changing latest-N source listing.
        item = {name: value[name] for name in ("source_key", "revision", "last_modified", "dataset_version_id") if name in value}
        item["outcome"] = "processing" if value["outcome"] in {"failed", "processing"} else "duplicates"
        items[key] = item
    stats = {**{key: 0 for key in STAT_KEYS}, "items": items,
        "max_seen": (prior.stats or {}).get("max_seen", ""), "failures": [],
        "dataset_version_ids": list(original.dataset_version_ids)}
    stats["duplicates"] = sum(item["outcome"] == "duplicates" for item in items.values())
    stats["processed"], stats["found"] = stats["duplicates"], len(items)
    only_enrichment = prior.status == "completed" and not (prior.stats or {}).get("failed") and original.analysis_status == "pending"
    run = Run(kind="collect", adapter=prior.adapter, input_scope=prior.input_scope,
        status="completed" if only_enrichment else "running", stats=stats, retry_of=prior.id,
        finished_at=utcnow() if only_enrichment else None)
    session.add(run)
    session.flush()
    job.run_id = run.id
    job.dataset_version_ids = list(original.dataset_version_ids)
    _event(session, job, "retry_scope", "重试保留原版本范围，优先处理原失败条目", {"previous_job_id": original.id,
        "original_refs": len(items), "enrichment_only": only_enrichment})
    return run.id


def _sync_run(job, run):
    job.stats = _public_stats(run.stats)
    job.dataset_version_ids = list(dict.fromkeys((run.stats or {}).get("dataset_version_ids", [])))


def _finish(session, job, status, error="", error_kind=""):
    job.status, job.stage, job.error, job.error_kind = status, "finished", error, error_kind
    job.finished_at, job.active_key = utcnow(), None
    job.current_source_key = ""
    _event(session, job, status, {"completed": "采集与规则整理完成，等待人工核验", "partial": "采集部分完成，请检查失败记录",
        "failed": "采集失败，可查看原因后重试", "cancelled": "采集已取消"}[status], {"error": error, "error_kind": error_kind})


def process_next_collection(engine, *, adapter_factory=None, guard=None):
    """Execute one persistent job under the worker lease; each side effect commits with its progress."""
    if guard:
        guard()
    with session_scope(engine) as session:
        job = session.scalar(select(CollectionJob).where(CollectionJob.status.in_(ACTIVE_STATUSES))
            .order_by((CollectionJob.status == "running").desc(), CollectionJob.id).limit(1))
        if not job:
            return False
        job_id = job.id
        if job.cancel_requested:
            if job.run_id:
                previous = session.get(Run, job.run_id)
                if previous:
                    _sync_run(job, previous)
                    if previous.status in {"running", "interrupted"}:
                        previous.status, previous.finished_at = "cancelled", utcnow()
            _finish(session, job, "cancelled")
            return True
        if job.attempts >= MAX_ATTEMPTS:
            _finish(session, job, "failed", "后台中断恢复次数已达上限，请人工重试", "retry_limit")
            if job.run_id:
                run = session.get(Run, job.run_id)
                if run and run.status in {"running", "interrupted"}:
                    run.status, run.error_kind, run.finished_at = "failed", "retry_limit", utcnow()
            return True
        job.attempts += 1
        job.status = "running"
        job.started_at = job.started_at or utcnow()
        if job.stage == "queued":
            job.stage = "collecting"
        scope, run_id = job.scope, job.run_id
        _event(session, job, "started", "开始采集" if job.attempts == 1 else "恢复采集任务", {"attempt": job.attempts})

    def check():
        if guard:
            guard()
        with session_scope(engine) as session:
            state = session.get(CollectionJob, job_id)
            if state.cancel_requested or state.status == "cancelled":
                raise CollectionCancelled("采集已由用户取消")

    def progress(value):
        check()
        with session_scope(engine) as session:
            state = session.get(CollectionJob, job_id)
            state.run_id = value["run_id"]
            state.stats = _public_stats(value["stats"])
            state.dataset_version_ids = list(value["stats"].get("dataset_version_ids", []))
            state.current_source_key = value.get("source_key", "")
            outcome = value.get("outcome", "started")
            detail = {"source_key": state.current_source_key, "dataset_version_id": value.get("dataset_version_id"), "outcome": outcome}
            if outcome == "failed":
                detail["failure"] = next((failure for failure in value["stats"].get("failures", [])
                    if failure.get("source_key") == state.current_source_key), {})
            if not value.get("resumed"):
                _event(session, state, "item" if state.current_source_key else "collecting", "正在采集来源元数据" if outcome == "processing" else "来源条目处理结果", detail)

    try:
        check()
        with session_scope(engine) as session:
            state = session.get(CollectionJob, job_id)
            if not run_id and state.retry_of:
                run_id = _seed_retry_run(session, state)
            previous = session.get(Run, run_id) if run_id else None
            collected = bool(previous and previous.status in {"completed", "failed"})
        if not collected:
            adapter = (adapter_factory or _default_adapter)()
            if not run_id:
                with session_scope(engine) as session:
                    run = Run(kind="collect", adapter=adapter.kind, status="running", input_scope=asdict(_collect_scope(scope)))
                    session.add(run)
                    session.flush()
                    run_id = run.id
                    session.get(CollectionJob, job_id).run_id = run_id
            collect(engine, adapter, _collect_scope(scope), run_id=run_id, progress=progress, guard=check)
        check()
        with session_scope(engine) as session:
            state = session.get(CollectionJob, job_id)
            run = session.get(Run, run_id)
            _sync_run(state, run)
            state.stage = "enriching"
            version_ids = list(state.dataset_version_ids)
            _event(session, state, "enriching", "正在从已保存证据生成规则整理候选", {"versions": len(version_ids)})
        enrichment_failed = False
        auto_summary = os.environ.get("XIAOYI_CATALOG_AUTO_SUMMARY", "1") != "0"
        if auto_summary:
            try:
                from .llm import LLMClient
                LLMClient.from_env()
            except Exception:
                auto_summary = False
        for version_id in version_ids:
            check()
            try:
                with session_scope(engine) as session:
                    state = session.get(CollectionJob, job_id)
                    if version_id in state.enriched_version_ids:
                        continue
                    version = session.get(DatasetVersion, version_id)
                    if not version or version.provenance_status != "verified":
                        raise ValueError("历史版本证据尚未验证")
                    # Existing reviewed outputs are never superseded or regenerated.
                    present = set(session.scalars(select(Enrichment.kind).where(Enrichment.dataset_version_id == version_id,
                        Enrichment.kind.in_(["schema", "quality"]), Enrichment.status != "superseded")))
                    if present != {"schema", "quality"}:
                        previous_entries = list(session.scalars(select(Enrichment).where(Enrichment.dataset_version_id == version_id)))
                        previous_statuses = {entry.id: entry.status for entry in previous_entries}
                        generated = enrich_version(session, version)
                        for entry in previous_entries:
                            entry.status = previous_statuses[entry.id]
                        for entry in generated:
                            if entry.kind in present:
                                session.expunge(entry)
                    state.enriched_version_ids = [*state.enriched_version_ids, version_id]
                    _event(session, state, "enriched", "规则整理候选已保存，待人工核验", {"dataset_version_id": version_id})
                    if auto_summary:
                        try:
                            if submit_summary(session, version.dataset_id, version_id, "agent")["status"] == "generating":
                                _event(session, state, "agent_summary_queued", "智能体总结已排队，后台生成后进入核验队列", {"dataset_version_id": version_id})
                        except Exception:
                            pass  # 智能体总结是增强项，绝不影响规则整理与采集结果
            except Exception as exc:
                if type(exc).__name__ in {"LeaseLost", "RunCancelled"} or isinstance(exc, CollectionCancelled):
                    raise
                enrichment_failed = True
                with session_scope(engine) as session:
                    _event(session, session.get(CollectionJob, job_id), "enrichment_failed", "规则整理未完成，版本需补充或恢复证据",
                        {"dataset_version_id": version_id, "error_kind": classify_error(exc), **safe_error_fields(exc)})
        check()
        with session_scope(engine) as session:
            state, run = session.get(CollectionJob, job_id), session.get(Run, run_id)
            _sync_run(state, run)
            failures = state.stats["failed"] or run.status == "failed" or enrichment_failed
            state.analysis_status = "pending" if enrichment_failed or (failures and not state.dataset_version_ids) else "rule_based"
            status = ("partial" if state.dataset_version_ids else "failed") if failures else "completed"
            error_kind = run.error_kind or ("item_failures" if state.stats["failed"] else "enrichment" if enrichment_failed else "")
            error = "部分来源未能处理，请查看逐项事件并重试" if failures else ""
            if status == "failed" and state.stats.get("failures"):
                error = next((entry["message"] for entry in state.stats["failures"] if not entry.get("source_key")), state.stats["failures"][0]["message"])
            _finish(session, state, status, error, error_kind)
    except CollectionCancelled:
        with session_scope(engine) as session:
            state = session.get(CollectionJob, job_id)
            if state.run_id:
                run = session.get(Run, state.run_id)
                if run:
                    _sync_run(state, run)
                    if run.status in {"running", "interrupted"}:
                        run.status, run.finished_at = "cancelled", utcnow()
            _finish(session, state, "cancelled")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        if type(exc).__name__ in {"LeaseLost", "RunCancelled"}:
            raise
        with session_scope(engine) as session:
            state = session.get(CollectionJob, job_id)
            if state.run_id:
                run = session.get(Run, state.run_id)
                if run:
                    _sync_run(state, run)
                    if run.status == "running":
                        run.status, run.error_kind, run.finished_at = "failed", classify_error(exc), utcnow()
            state.stats = {**_public_stats(state.stats), "failures": [*(state.stats or {}).get("failures", [])[:49],
                {"source_key": "", "error_kind": classify_error(exc), "message": safe_collection_error(exc), **safe_error_fields(exc)}]}
            if not state.dataset_version_ids:
                state.analysis_status = "pending"
            _finish(session, state, "failed", safe_collection_error(exc), classify_error(exc))
    return True
