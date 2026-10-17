"""PostgreSQL/SQLite persistent queues. Only registered operations can run.

The queue is at-least-once; attempts are fenced and output publication is idempotent.
External network requests can still be charged more than once after a crash.
"""
from __future__ import annotations
from datetime import timedelta, timezone
import hashlib
import json
import re
import threading
import uuid
from contextlib import contextmanager
from sqlalchemy import select, update, or_, func
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from .db import session_scope, execution_guard
from .models import utcnow
from .models_jobs import Job, Attempt, Artifact, RunEvent
from .agent_runtime import LeaseLost

LEASE_SECONDS = 120
KINDS = {
    "qa.run": "interactive", "summary.run": "interactive", "research.run": "interactive",
    "collection.run": "io", "download.run": "io", "asset.import": "io", "bundle.build": "io",
    "build.export": "io", "asset.derive": "cpu", "profile.run": "cpu", "build.run": "cpu", "mining.run": "cpu", "experiment.import": "cpu",
}
PUBLIC_KINDS = {"asset.derive", "profile.run", "bundle.build", "build.export"}
TERMINAL = {"succeeded", "failed", "cancelled"}

class JobError(ValueError):
    pass

class JobCancelled(LeaseLost):
    pass


class JobPending(JobError):
    """The bounded business attempt needs another attempt; no artifact is complete."""

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()

def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

def event(session, job, kind, detail=None):
    session.add(RunEvent(job_id=job.id, kind=kind, detail=detail or {}))

def enqueue(session, kind, payload, actor, idempotency_key=None):
    if kind not in KINDS:
        raise JobError("不支持的后台操作")
    if not isinstance(payload, dict) or len(json.dumps(payload, ensure_ascii=False)) > 65536:
        raise JobError("任务只接受有界参数和清单引用，不接受批量样本")
    name = actor.username if hasattr(actor, "username") else str(actor)
    key = idempotency_key or uuid.uuid4().hex
    if not name or len(name) > 128 or not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise JobError("无效任务身份或幂等键")
    fingerprint = digest({"kind": kind, "payload": payload})
    prior = session.scalar(select(Job).where(Job.actor == name, Job.request_key == key))
    if prior:
        if prior.payload_hash != fingerprint:
            raise JobError("同一幂等键不能用于不同输入")
        return prior
    try:
        with session.begin_nested():
            job = Job(kind=kind, queue=KINDS[kind], actor=name, request_key=key, payload=payload, payload_hash=fingerprint)
            session.add(job)
            session.flush()
    except IntegrityError:
        prior = session.scalar(select(Job).where(Job.actor == name, Job.request_key == key))
        if prior is None or prior.payload_hash != fingerprint:
            raise JobError("并发幂等请求与已有任务冲突")
        return prior
    event(session, job, "queued")
    return job

def to_dict(job):
    return {"id":job.id,"job_id":job.id,"kind":job.kind,"queue":job.queue,"status":job.status,
            "attempts":job.attempts,"max_attempts":job.max_attempts,"progress":job.progress,
            "result":job.result,"error":job.error,"cancel_requested":job.cancel_requested,
            "created_at":job.created_at.isoformat(),"updated_at":job.updated_at.isoformat()}

def visible(job, actor):
    return job.actor == actor.username or (actor.role == "admin" and job.kind != "qa.run")

def managed(session, ident, actor):
    job = session.get(Job, ident)
    if job is None or not visible(job, actor):
        raise JobError("任务不存在或无权访问")
    return job

def cancel(session, ident, actor):
    job = managed(session, ident, actor)
    if job.status in TERMINAL:
        return job
    job.cancel_requested = True
    if job.status == "queued":
        job.status, job.finished_at = "cancelled", utcnow()
        _domain_cancel(session, job)
    event(session, job, "cancel_requested")
    return job

def retry(session, ident, actor):
    job = managed(session, ident, actor)
    if job.kind not in PUBLIC_KINDS | {"build.run", "mining.run", "experiment.import", "asset.import"}:
        raise JobError("此任务须通过原业务入口重试，以保留原执行预算")
    if job.status not in {"failed", "cancelled"}:
        raise JobError("只有失败或取消的任务可以重试")
    job.status, job.cancel_requested, job.error, job.finished_at = "queued", False, "", None
    job.max_attempts = job.attempts + 3
    job.available_at = utcnow()
    event(session, job, "retry_requested")
    return job

def claim(engine, queue, owner):
    if queue not in set(KINDS.values()):
        raise JobError("未知资源队列")
    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            if engine.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            now = utcnow()
            stmt = select(Job).where(Job.queue == queue, Job.available_at <= now,
                or_(Job.status == "queued", (Job.status == "running") & (Job.lease_until < now)))
            job = session.scalar(stmt.order_by(Job.available_at, Job.id).with_for_update(skip_locked=True).limit(1))
            if job is None:
                return None
            if job.status == "running":
                previous = session.scalar(select(Attempt).where(Attempt.job_id==job.id, Attempt.number==job.attempts))
                if previous:
                    previous.status, previous.finished_at = "expired", now
                event(session, job, "lease_expired", {"attempt":job.attempts})
            receipt = session.scalar(select(Artifact).where(Artifact.job_id == job.id))
            if receipt is not None:
                # Output and its receipt were committed before the previous worker stopped.
                valid = receipt.content_hash == digest(receipt.manifest)
                job.status = "succeeded" if valid else "failed"
                job.result, job.error = (receipt.manifest, "") if valid else ({}, "任务产物回执校验失败")
                job.finished_at, job.lease_until = now, None
                previous = session.scalar(select(Attempt).where(Attempt.job_id == job.id, Attempt.number == job.attempts))
                if previous:
                    previous.status, previous.finished_at, previous.error = job.status, now, job.error
                event(session, job, "completion_recovered" if valid else "failed")
                return None
            if job.cancel_requested or job.attempts >= job.max_attempts:
                job.status = "cancelled" if job.cancel_requested else "failed"
                job.error = "已取消" if job.cancel_requested else "后台恢复次数已达上限"
                job.finished_at = now
                _domain_cancel(session, job)
                event(session, job, job.status)
                return None
            job.attempts += 1
            job.owner, job.fence, job.status = owner, uuid.uuid4().hex, "running"
            job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            job.progress = {"stage": "starting", "processed": 0, "total": None, "counts": {}}
            session.add(Attempt(job_id=job.id, number=job.attempts, owner=owner, fence=job.fence))
            event(session, job, "started", {"attempt":job.attempts})
        return job

class Guard:
    def __init__(self, engine, job):
        self.engine, self.ident, self.owner, self.fence = engine, job.id, job.owner, job.fence
        self.stop, self.lost, self.cancelled = threading.Event(), threading.Event(), threading.Event()
        self._progress_lock, self._progress = threading.Lock(), None
        self.thread = threading.Thread(target=self._heartbeat, name="job-lease-heartbeat", daemon=True)

    def __call__(self, session=None, *, allow_cancel=False):
        if self.lost.is_set():
            raise LeaseLost("任务执行租约已失效")
        if self.cancelled.is_set() and not allow_cancel:
            raise JobCancelled("任务已请求取消")
        if session is None:
            with Session(self.engine) as check:
                return self._check(check, lock=False, allow_cancel=allow_cancel)
        return self._check(session, lock=True, allow_cancel=allow_cancel)

    def _check(self, session, *, lock, allow_cancel):
        if lock and session.get_bind().dialect.name == "sqlite":
            # Lock the queue row in the SAME transaction as the result publication.
            session.execute(update(Job).where(Job.id==self.ident).values(id=self.ident))
        stmt = select(Job).where(Job.id==self.ident).execution_options(populate_existing=True)
        if lock:
            stmt = stmt.with_for_update()
        job = session.scalar(stmt)
        if (job is None or job.status != "running" or job.owner != self.owner or job.fence != self.fence
                or _utc(job.lease_until) <= utcnow()):
            raise LeaseLost("任务已由其他执行进程接管")
        if job.cancel_requested and not allow_cancel:
            raise JobCancelled("任务已请求取消")
        return job

    def report_progress(self, stage, processed, total=None, **smallcounts):
        """Buffer <=2 KiB of numeric progress; never opens a SQL transaction.

        Stage is 1-64 ASCII identifier characters. Counts are non-negative signed
        64-bit integers (at most 16 named counts); totals are omitted when unknown.
        The normal heartbeat persists this snapshot using the owner's fence.
        """
        if self.lost.is_set():
            raise LeaseLost("任务执行租约已失效")
        if self.cancelled.is_set():
            raise JobCancelled("任务已请求取消")
        if not isinstance(stage, str) or re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", stage) is None:
            raise JobError("进度阶段必须是1至64位ASCII标识")
        def valid_count(value):
            return type(value) is int and 0 <= value <= 2**63 - 1
        if not valid_count(processed) or (total is not None and (not valid_count(total) or total < processed)):
            raise JobError("进度计数必须非负且不能超过已知总数")
        if len(smallcounts) > 16 or any(re.fullmatch(r"[a-z][a-z0-9_]{0,47}", key) is None or not valid_count(value)
                                      for key, value in smallcounts.items()):
            raise JobError("进度最多接受16个有界整数计数")
        value = {"stage": stage, "processed": processed, "total": total, "counts": dict(smallcounts)}
        if len(json.dumps(value, separators=(",", ":")).encode()) > 2048:
            raise JobError("进度内容超过2 KiB")
        with self._progress_lock:
            self._progress = value

    def progress_snapshot(self):
        with self._progress_lock:
            return {**self._progress, "counts": dict(self._progress["counts"])} if self._progress else None

    def _renew_lease(self):
        if self.lost.is_set() or self.cancelled.is_set():
            return False
        values = {"lease_until": utcnow() + timedelta(seconds=LEASE_SECONDS)}
        progress = self.progress_snapshot()
        if progress is not None:
            values["progress"] = progress
        with Session(self.engine) as session, session.begin():
            result = session.execute(update(Job).where(Job.id == self.ident, Job.status == "running",
                Job.owner == self.owner, Job.fence == self.fence, Job.lease_until > utcnow(),
                Job.cancel_requested.is_(False)).values(**values))
            if result.rowcount:
                return True
            owner = session.execute(select(Job.owner, Job.fence, Job.cancel_requested).where(Job.id == self.ident)).first()
            if owner and owner.owner == self.owner and owner.fence == self.fence and owner.cancel_requested:
                self.cancelled.set()
            else:
                self.lost.set()
            return False

    def _heartbeat(self):
        while not self.stop.wait(15):
            try:
                if not self._renew_lease():
                    return
            except Exception:
                self.lost.set()
                return

    def __enter__(self):
        self.thread.start()
        self.token = execution_guard.set(self)
        return self

    def __exit__(self, *args):
        execution_guard.reset(self.token)
        self.stop.set()
        self.thread.join(timeout=2)

def _domain_cancel(session, job):
    ident = job.payload.get("id")
    if job.kind == "qa.run":
        from .models_qa import QaTurn
        item = session.get(QaTurn, ident)
    elif job.kind == "research.run":
        from .models_runtime import AgentRun
        item = session.get(AgentRun, ident)
    elif job.kind == "collection.run":
        from .models_collection import CollectionJob
        item = session.get(CollectionJob, ident)
    elif job.kind == "summary.run":
        from .models import Enrichment
        item = session.get(Enrichment, ident)
    elif job.kind in {"download.run", "asset.import"}:
        from .models_assets import DownloadRequest
        item = session.get(DownloadRequest, ident if job.kind == "download.run" else job.payload.get("request_id"))
    elif job.kind == "build.run":
        from .models_samples import DatasetBuild
        item = session.get(DatasetBuild, job.payload.get("build_id"))
    elif job.kind == "experiment.import":
        from .models_samples import Experiment
        item = session.get(Experiment, job.payload.get("experiment_id"))
    else:
        item = None
    if item is not None and item.status in {"queued", "running", "generating", "approved"}:
        item.status = "cancelled" if job.status == "cancelled" else "failed"
        if hasattr(item, "cancel_requested"):
            item.cancel_requested = True
        if hasattr(item, "finished_at"):
            item.finished_at = utcnow()
        if hasattr(item, "lease_until"):
            item.lease_until = None
        if hasattr(item, "error"):
            item.error = job.error or ("任务已取消" if item.status == "cancelled" else "后台执行失败")
        if hasattr(item, "stage"):
            item.stage = item.status
        if job.kind == "collection.run":
            from .models import Run
            from .models_collection import CollectionEvent
            item.active_key, item.current_source_key, item.stage = None, "", "finished"
            if item.run_id:
                collection_run = session.get(Run, item.run_id)
                if collection_run and collection_run.status in {"running", "interrupted"}:
                    collection_run.status, collection_run.finished_at = item.status, utcnow()
            session.add(CollectionEvent(job_id=item.id, kind=item.status, message=item.error,
                                        detail={"queue_job_id": job.id}))
        if job.kind == "summary.run":
            # Preserve attempts and failures, but never leave a cancelled generation eligible for bridging.
            item.value = {key: value for key, value in (item.value or {}).items() if key != "execution"}
            item.value = {**item.value, "error": job.error or "后台任务已终止"}


def finish(engine, guard, status, result=None, error=""):
    with Session(engine) as session, session.begin():
        job = guard(session, allow_cancel=True)
        prior = session.scalar(select(Artifact).where(Artifact.job_id == job.id))
        # An artifact committed with its domain output is the durable completion boundary.
        # Cancellation arriving afterwards cannot turn that completed result into a retry.
        if prior is not None:
            if prior.content_hash == digest(prior.manifest):
                status, result, error = "succeeded", prior.manifest, ""
            else:
                status, result, error = "failed", {}, "任务产物回执校验失败"
        elif job.cancel_requested:
            status, error = "cancelled", "任务已取消"
        progress = guard.progress_snapshot()
        if progress is not None and not job.cancel_requested and status != "cancelled":
            job.progress = progress
        now = utcnow()
        job.status, job.result, job.error = status, result or {}, error[:2000]
        job.finished_at = None if status == "queued" else now
        job.lease_until = None
        if status == "queued":
            job.available_at = now
        attempt = session.scalar(select(Attempt).where(Attempt.job_id == job.id, Attempt.number == job.attempts))
        attempt.status, attempt.finished_at, attempt.error = "retryable" if status == "queued" else status, now, job.error
        if status == "succeeded" and prior is None:
            session.add(Artifact(job_id=job.id, content_hash=digest(job.result), manifest=job.result))
        elif status in {"failed", "cancelled"}:
            _domain_cancel(session, job)
        event(session, job, "retry_scheduled" if status == "queued" else status,
              {"error": job.error} if error else {})


def bridge_legacy(engine, queue):
    """Idempotently map existing business IDs without changing approval or ownership."""
    from .models_qa import QaTurn, QaConversation
    from .models_runtime import AgentRun
    from .models_collection import CollectionJob
    from .models import Enrichment
    from .models_assets import DownloadRequest
    with session_scope(engine) as session:
        entries = []
        if queue == "interactive":
            entries.extend(("qa.run",r.id,c.actor,r.status) for r,c in session.execute(select(QaTurn,QaConversation).join(QaConversation).where(QaTurn.status.in_(["queued","running"])).limit(100)))
            entries.extend(("research.run",r.id,r.actor,r.status) for r in session.scalars(select(AgentRun).where(AgentRun.status.in_(["queued","running"])).limit(100)))
            entries.extend(("summary.run",r.id,"summary-service","queued") for r in session.scalars(select(Enrichment).where(Enrichment.kind=="llm_summary",Enrichment.producer.like("agent-summary/%"),Enrichment.status=="generating").limit(100)))
        elif queue == "io":
            entries.extend(("collection.run",r.id,r.actor,r.status) for r in session.scalars(select(CollectionJob).where(CollectionJob.status.in_(["queued","running"])).limit(100)))
            entries.extend(("download.run",r.id,r.requested_by,r.status) for r in session.scalars(select(DownloadRequest).where(DownloadRequest.status.in_(["approved","running"]),func.coalesce(DownloadRequest.manifest["acquisition"].as_string(),"huggingface")!="local").limit(100)))
        for kind,ident,actor,state in entries:
            key = f"legacy:{kind}:{ident}"
            job = session.scalar(select(Job).where(Job.actor==actor,Job.request_key==key))
            if job is None:
                enqueue(session,kind,{"id":ident},actor,key)
            elif job.status in {"failed", "cancelled"} and state in {"queued", "approved"}:
                job.status, job.error, job.finished_at, job.cancel_requested = "queued", "", None, False
                job.max_attempts, job.available_at = job.attempts+3, utcnow()
                event(session,job,"business_retry")

def dispatch(engine, job, guard, *, llm=None, adapter_factory=None, collection_adapter_factory=None, fetcher=None):
    payload, kind = job.payload, job.kind
    ident = payload.get("id")
    if kind == "qa.run":
        from .qa_runtime import execute_turn
        from .models_qa import QaTurn
        execute_turn(engine, ident, llm=llm, guard=guard)
        model = QaTurn
    elif kind == "summary.run":
        from .summary_runtime import process_next_summary
        from .models import Enrichment
        process_next_summary(engine,llm=llm,guard=guard,owner=job.owner,enrichment_id=ident)
        model = Enrichment
    elif kind == "research.run":
        from .agent_runtime import execute_run
        from .models_runtime import AgentRun
        execute_run(engine,ident,llm=llm,guard=guard,adapter_factory=adapter_factory)
        model = AgentRun
    elif kind == "collection.run":
        from .collection_runtime import process_next_collection
        from .models_collection import CollectionJob
        process_next_collection(engine,adapter_factory=collection_adapter_factory or adapter_factory,guard=guard,job_id=ident)
        model = CollectionJob
    elif kind == "download.run":
        from .downloads import process_next_download
        from .models_assets import DownloadRequest
        process_next_download(engine,fetcher=fetcher,guard=guard,request_id=ident)
        model = DownloadRequest
    elif kind == "asset.import":
        from .asset_import import process_local_import
        return {"request_id":process_local_import(engine,payload["request_id"],payload["files"])}
    elif kind == "asset.derive":
        from .asset_import import execute_derive
        return execute_derive(engine, payload["asset_id"], payload.get("operation", "deduplicate"),
                              guard=guard, actor=job.actor)
    elif kind == "bundle.build":
        from .bundles import build_bundle_job
        return build_bundle_job(engine,payload["snapshot_id"],guard=guard)
    elif kind == "profile.run":
        from .sample_engine import execute_profile
        return execute_profile(engine,payload["asset_id"],guard=guard)
    elif kind == "build.export":
        from .sample_engine import execute_export
        return execute_export(engine,payload["build_id"],guard=guard)
    elif kind == "build.run":
        from .sample_engine import execute_build
        return execute_build(engine,payload["build_id"],guard=guard)
    elif kind == "mining.run":
        from .mining import execute_mining
        return execute_mining(engine,payload,guard=guard)
    elif kind == "experiment.import":
        from .experiments import execute_experiment_import
        return execute_experiment_import(engine,payload,guard=guard)
    else:
        raise JobError("未注册的任务操作")
    with session_scope(engine) as session:
        value = session.get(model,ident)
        if value.status in {"failed", "partial", "cancelled"}:
            raise JobError(getattr(value, "error", "") or f"业务任务状态：{value.status}")
        if value.status in {"queued", "running", "generating", "approved"}:
            raise JobPending(f"业务任务尚未完成：{value.status}")
        return {"id": ident, "status": value.status}

def run_queue_once(engine, queue, *, owner=None, **dependencies):
    owner = owner or uuid.uuid4().hex
    bridge_legacy(engine,queue)
    job = claim(engine,queue,owner)
    if job is None:
        return False
    guard = Guard(engine,job)
    try:
        with guard:
            result = dispatch(engine,job,guard,**dependencies)
        finish(engine,guard,"succeeded",result)
    except JobPending as exc:
        try:
            finish(engine, guard, "queued", error=str(exc))
        except LeaseLost:
            pass
    except JobCancelled:
        try:
            finish(engine,guard,"cancelled")
        except LeaseLost:
            pass
    except LeaseLost:
        # A later owner alone may commit; retain this attempt for expiry recovery.
        return True
    except Exception as exc:
        try:
            # Errors exposed by providers can contain URLs or secrets. Use their existing sanitiser.
            from .collection_runtime import safe_error_fields
            message = safe_error_fields(exc).get("message") or type(exc).__name__
            if isinstance(exc,JobError):
                message = str(exc)
            finish(engine,guard,"failed",error=message)
        except LeaseLost:
            pass
    return True
