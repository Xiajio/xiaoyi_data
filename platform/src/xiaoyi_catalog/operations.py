"""Explicit schema migration, runtime readiness and independent worker heartbeats."""
from __future__ import annotations
import argparse
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text, update
from .db import create_engine_for, init_db, session_scope
from .models import utcnow
from .models_operations import WorkerHeartbeatRecord

QUEUES = ("interactive", "io", "cpu")
log = logging.getLogger(__name__)


class OperationsError(RuntimeError):
    pass


def expected_schema_revision() -> str:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def assert_schema_current(engine) -> None:
    expected = expected_schema_revision()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            revisions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    except Exception:
        raise OperationsError("Database unavailable or uninitialized; run the explicit migration command") from None
    if revisions != [expected]:
        raise OperationsError(f"Database schema does not match this release; expected {expected}")


def readiness(engine, storage: Path | str | None = None) -> dict:
    assert_schema_current(engine)
    root = Path(storage or os.getenv("XIAOYI_STORAGE_ROOT", "./data/assets"))
    try:
        # A real exclusive temporary file checks access for the service UID, not merely mode bits.
        with tempfile.TemporaryFile(prefix=".readiness-", dir=root) as stream:
            stream.write(b"ready")
            stream.flush()
    except OSError:
        raise OperationsError("Asset storage is missing or not writable") from None
    return {"status": "ready", "schema": expected_schema_revision(), "database": "ok", "storage": "ok"}


def record_worker_heartbeat(engine, queue: str, owner: str) -> None:
    if queue not in QUEUES:
        raise OperationsError("Unknown worker queue")
    with session_scope(engine) as session:
        row = session.get(WorkerHeartbeatRecord, owner)
        if row is None:
            session.add(WorkerHeartbeatRecord(owner=owner, queue=queue))
        else:
            row.queue, row.seen_at, row.stopped_at = queue, utcnow(), None


class WorkerHeartbeat:
    """Process heartbeat remains live during idle polling and long external calls."""
    def __init__(self, engine, queue: str, owner: str, interval: float = 10):
        self.engine, self.queue, self.owner = engine, queue, owner
        self.interval = max(0.05, interval)
        self.stop = threading.Event()
        self.failed = threading.Event()
        self.thread = threading.Thread(target=self._loop, name=f"heartbeat-{queue}", daemon=True)

    def _loop(self):
        while not self.stop.wait(self.interval):
            try:
                record_worker_heartbeat(self.engine, self.queue, self.owner)
                self.failed.clear()
            except Exception:
                self.failed.set()
                log.warning("Worker heartbeat write failed; check database availability")

    def __enter__(self):
        record_worker_heartbeat(self.engine, self.queue, self.owner)
        self.thread.start()
        return self

    def check(self):
        if self.failed.is_set():
            raise OperationsError("Worker heartbeat cannot reach the database")

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=2)
        try:
            with session_scope(self.engine) as session:
                session.execute(update(WorkerHeartbeatRecord).where(
                    WorkerHeartbeatRecord.owner == self.owner).values(stopped_at=utcnow()))
        except Exception:
            log.warning("Worker stop heartbeat could not be recorded")


def check_worker_health(engine, queue: str, max_age_seconds: int = 60) -> dict:
    if queue not in QUEUES:
        raise OperationsError("Unknown worker queue")
    assert_schema_current(engine)
    with session_scope(engine) as session:
        active = session.scalars(select(WorkerHeartbeatRecord.owner).where(
            WorkerHeartbeatRecord.queue == queue, WorkerHeartbeatRecord.stopped_at.is_(None),
            WorkerHeartbeatRecord.seen_at >= utcnow() - timedelta(seconds=max_age_seconds))).all()
    if not active:
        raise OperationsError(f"No recent worker heartbeat for queue {queue}")
    return {"status": "ok", "queue": queue, "workers": len(active)}


def queue_status(session, *, now=None) -> dict:
    """Queue backlog and process health; human approval waits are not worker jobs."""
    from datetime import timezone
    from sqlalchemy import func
    from .models_jobs import Job
    observed = now or utcnow()
    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value
    counts = {(queue, status): count for queue, status, count in session.execute(
        select(Job.queue, Job.status, func.count()).group_by(Job.queue, Job.status))}
    result = {}
    for queue in QUEUES:
        waiting = session.scalar(select(func.count()).select_from(Job).where(
            Job.queue == queue, Job.status == "queued", Job.available_at <= observed)) or 0
        oldest = session.scalar(select(func.min(Job.available_at)).where(
            Job.queue == queue, Job.status == "queued", Job.available_at <= observed))
        expired = session.scalar(select(func.count()).select_from(Job).where(
            Job.queue == queue, Job.status == "running", Job.lease_until < observed)) or 0
        heartbeats = session.scalars(select(WorkerHeartbeatRecord).where(
            WorkerHeartbeatRecord.queue == queue)).all()
        active = [row for row in heartbeats if row.stopped_at is None and
                  aware(row.seen_at) >= observed - timedelta(seconds=60)]
        last_seen = max((aware(row.seen_at) for row in heartbeats), default=None)
        queued = counts.get((queue, "queued"), 0)
        result[queue] = {
            "queued": queued, "waiting": waiting, "delayed": queued - waiting,
            "running": counts.get((queue, "running"), 0),
            "failed": counts.get((queue, "failed"), 0),
            "succeeded": counts.get((queue, "succeeded"), 0),
            "cancelled": counts.get((queue, "cancelled"), 0),
            "expired_running": expired,
            "oldest_wait_seconds": max(0, (observed - aware(oldest)).total_seconds()) if oldest else None,
            "heartbeat": {"healthy": bool(active), "active_workers": len(active),
                          "last_seen_at": last_seen.isoformat() if last_seen else None},
        }
    return result

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Explicit deployment operations; never migrate from a health probe")
    parser.add_argument("command", choices=("migrate", "check", "worker-health"))
    parser.add_argument("--db", default=os.getenv("XIAOYI_CATALOG_DB", "./data/catalog.db"))
    parser.add_argument("--queue", choices=QUEUES)
    parser.add_argument("--storage", default=os.getenv("XIAOYI_STORAGE_ROOT", "./data/assets"))
    args = parser.parse_args(argv)
    engine = create_engine_for(args.db)
    try:
        if args.command == "migrate":
            init_db(engine)
            result = {"schema": expected_schema_revision(), "status": "migrated"}
        elif args.command == "worker-health":
            if not args.queue:
                raise OperationsError("--queue is required")
            result = check_worker_health(engine, args.queue)
        else:
            result = readiness(engine, args.storage)
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(str(exc) if isinstance(exc, OperationsError) else "Operation failed; check database availability and release configuration")
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
