from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from xiaoyi_catalog.auth import Principal
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.models import utcnow
from xiaoyi_catalog.models_jobs import Job, Attempt, Artifact, RunEvent
from xiaoyi_catalog.jobs import enqueue, claim, Guard, finish, cancel, retry, managed, JobError, JobCancelled, run_queue_once
from xiaoyi_catalog.agent_runtime import LeaseLost


def queued(engine, key="one", actor="alice"):
    with session_scope(engine) as session:
        ident=enqueue(session,"build.run",{"build_id":1},actor,key).id
    return ident


def test_idempotent_submission_rejects_changed_input(engine):
    ident=queued(engine)
    with session_scope(engine) as session:
        assert enqueue(session,"build.run",{"build_id":1},"alice","one").id==ident
        with pytest.raises(JobError):
            enqueue(session,"build.run",{"build_id":2},"alice","one")
        with pytest.raises(JobError):
            enqueue(session,"shell.run",{},"alice")


def test_atomic_claim_and_queue_isolation(engine):
    ident=queued(engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=list(pool.map(lambda owner:claim(engine,"cpu",owner),["one","two"]))
    assert [j.id for j in jobs if j] == [ident]
    assert claim(engine,"interactive","three") is None
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(Attempt))==1


def test_expired_worker_cannot_commit_or_finish(engine):
    queued(engine)
    old=claim(engine,"cpu","old")
    guard=Guard(engine,old)
    with Session(engine) as session, session.begin():
        session.get(Job,old.id).lease_until=utcnow()-timedelta(seconds=1)
    new=claim(engine,"cpu","new")
    assert new.id==old.id and new.fence!=old.fence
    with pytest.raises(LeaseLost):
        finish(engine,guard,"succeeded",{"bad":True})
    with guard, pytest.raises(LeaseLost):
        with session_scope(engine) as session:
            session.add(Artifact(job_id=old.id,content_hash="x"*64,manifest={"bad":True}))
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(Artifact))==0
    finish(engine,Guard(engine,new),"succeeded",{"good":True})


def test_cancellation_fences_output(engine):
    ident=queued(engine)
    job=claim(engine,"cpu","worker")
    with session_scope(engine) as session:
        cancel(session,ident,Principal(1,"alice","researcher"))
    guard=Guard(engine,job)
    with pytest.raises(JobCancelled):
        guard()
    finish(engine,guard,"cancelled")
    with session_scope(engine) as session:
        assert session.get(Job,ident).status=="cancelled"
        assert session.scalar(select(func.count()).select_from(Artifact))==0
        assert retry(session,ident,Principal(1,"alice","researcher")).attempts==1
    next_job=claim(engine,"cpu","worker2")
    assert next_job.attempts==2


def test_attempt_budget_stops_crash_loop(engine):
    ident=queued(engine)
    with session_scope(engine) as session:
        session.get(Job,ident).max_attempts=1
    claim(engine,"cpu","worker")
    with session_scope(engine) as session:
        session.get(Job,ident).lease_until=utcnow()-timedelta(seconds=1)
    assert claim(engine,"cpu","replacement") is None
    with session_scope(engine) as session:
        assert session.get(Job,ident).status=="failed"


def test_dispatch_produces_one_artifact(engine,monkeypatch):
    import xiaoyi_catalog.jobs as jobs
    ident=queued(engine)
    monkeypatch.setattr(jobs,"dispatch",lambda engine,job,guard,**kwargs:{"manifest":"verified"})
    assert run_queue_once(engine,"cpu")
    assert not run_queue_once(engine,"cpu")
    with session_scope(engine) as session:
        assert session.get(Job,ident).status=="succeeded"
        assert session.scalar(select(func.count()).select_from(Artifact))==1


def test_private_qa_jobs_are_owner_only(engine):
    with session_scope(engine) as session:
        item=enqueue(session,"qa.run",{"id":1},"alice")
        with pytest.raises(JobError):
            managed(session,item.id,Principal(2,"bob","admin"))
        assert managed(session,item.id,Principal(1,"alice","reader")).id==item.id


def test_job_failure_preserves_attempt(engine,monkeypatch):
    import xiaoyi_catalog.jobs as jobs
    ident=queued(engine)
    def fail(*args,**kwargs):
        raise JobError("invalid input")
    monkeypatch.setattr(jobs,"dispatch",fail)
    assert run_queue_once(engine,"cpu")
    with session_scope(engine) as session:
        assert session.get(Job,ident).status=="failed"
        assert session.get(Job,ident).error=="invalid input"
        assert session.scalar(select(Attempt)).status=="failed"
