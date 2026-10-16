"""Single leased worker for checkpointed agent tasks and approved downloads.

Run: python -m xiaoyi_catalog.worker --db catalog.db [--once]
The API queues work; no long-running model/collection work runs in request handlers.
"""
from __future__ import annotations
import argparse
import logging
import os
import threading
import time
import uuid
from datetime import timedelta
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from .agent_runtime import LeaseLost, execute_run, reconcile_reviews
from .db import create_engine_for, init_db, session_scope
from .models import utcnow
from .models_runtime import AgentRun, WorkerLease

log = logging.getLogger(__name__)
LEASE_NAME = "research_worker"
LEASE_SECONDS = 120


def acquire_lease(engine, owner: str, *, seconds: int = LEASE_SECONDS) -> bool:
    now = utcnow()
    with session_scope(engine) as session:
        changed = session.execute(update(WorkerLease).where(WorkerLease.name == LEASE_NAME,
            or_(WorkerLease.owner == owner, WorkerLease.expires_at < now)).values(
                owner=owner, expires_at=now + timedelta(seconds=seconds)))
        if changed.rowcount:
            return True
        if session.get(WorkerLease, LEASE_NAME):
            return False
        try:
            with session.begin_nested():
                session.add(WorkerLease(name=LEASE_NAME, owner=owner, expires_at=now + timedelta(seconds=seconds)))
                session.flush()
            return True
        except IntegrityError:
            return False


def renew_lease(engine, owner: str) -> bool:
    now = utcnow()
    with session_scope(engine) as session:
        changed = session.execute(update(WorkerLease).where(WorkerLease.name == LEASE_NAME,
            WorkerLease.owner == owner, WorkerLease.expires_at > now).values(
                expires_at=now + timedelta(seconds=LEASE_SECONDS)))
        return bool(changed.rowcount)


def release_lease(engine, owner: str) -> None:
    with session_scope(engine) as session:
        session.execute(update(WorkerLease).where(WorkerLease.name == LEASE_NAME,
            WorkerLease.owner == owner).values(expires_at=utcnow() - timedelta(seconds=1)))


class LeaseHeartbeat:
    def __init__(self, engine, owner: str):
        self.engine, self.owner = engine, owner
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="catalog-worker-heartbeat")

    def _loop(self):
        while not self.stop.wait(15):
            try:
                if not renew_lease(self.engine, self.owner):
                    self.lost.set()
                    return
            except Exception:
                self.lost.set()
                log.exception("Worker lease renewal failed")
                return

    def check(self):
        if self.lost.is_set():
            raise LeaseLost("后台任务租约已失效；等待下一工作进程恢复")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=2)


def run_once(engine, *, owner: str | None = None, llm=None, adapter_factory=None, collection_adapter_factory=None) -> bool:
    owner = owner or uuid.uuid4().hex
    if not acquire_lease(engine, owner):
        return False
    worked = False
    try:
        with LeaseHeartbeat(engine, owner) as heartbeat:
            from .collection_runtime import tick_collection_schedules, process_next_collection
            tick_collection_schedules(engine)
            worked = bool(process_next_collection(engine, adapter_factory=collection_adapter_factory or adapter_factory, guard=heartbeat.check))
            heartbeat.check()
            with session_scope(engine) as session:
                reconcile_reviews(session)
                # A running job belongs to a worker whose lease expired; resume its graph first.
                run_id = session.scalar(select(AgentRun.id).where(AgentRun.status.in_(["queued", "running"]))
                    .order_by((AgentRun.status == "running").desc(), AgentRun.id).limit(1))
            if run_id:
                execute_run(engine, run_id, llm=llm, guard=heartbeat.check, adapter_factory=adapter_factory)
                worked = True
            heartbeat.check()
            try:
                from .downloads import process_next_download
            except ImportError:
                process_next_download = None
            if process_next_download:
                worked = bool(process_next_download(engine, guard=heartbeat.check)) or worked
    finally:
        release_lease(engine, owner)
    return worked


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="小亿科研数据后台 worker")
    parser.add_argument("--db", default=os.getenv("XIAOYI_CATALOG_DB") or os.getenv("XIAOYI_DATABASE_URL") or os.getenv("DATABASE_URL") or os.getenv("XIAOYI_DB", "data/catalog.db"))
    parser.add_argument("--once", action="store_true", help="处理到期计划、一个采集任务、一个智能体任务和一个已批准下载后退出")
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    engine = create_engine_for(args.db)
    init_db(engine)
    owner = uuid.uuid4().hex
    try:
        while True:
            try:
                worked = run_once(engine, owner=owner)
            except Exception:
                log.exception("Worker iteration failed; durable work will resume on the next iteration")
                worked = False
            if args.once:
                return 0
            if not worked:
                time.sleep(max(0.2, args.poll_seconds))
    except KeyboardInterrupt:
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
