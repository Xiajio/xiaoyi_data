"""Legacy CLI approval commands authenticate human accounts before mutation."""
import json
import pytest
from sqlalchemy import select
from xiaoyi_catalog.auth import create_user, mint_session
from xiaoyi_catalog.cli import main
from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.models import Audit, DatasetVersion, Pairing, Review, Snapshot
from xiaoyi_catalog.pairing import propose_pairing
from conftest import FakeAdapter, make_raw


@pytest.fixture
def approval_cli(engine, monkeypatch):
    monkeypatch.delenv("XIAOYI_CLI_TOKEN", raising=False)
    collect(engine, FakeAdapter([make_raw("a/cli",license="mit",tasks=("text-classification",),card="evidence")]),CollectScope())
    with session_scope(engine) as session:
        version = session.scalar(select(DatasetVersion.id))
        pid = propose_pairing(session,{"external_id":"cli-idea","title":"CLI study","requirements":{"modalities":["text"]}},[version],"test")["created"][0]["id"]
    return engine, str(engine.url.database), pid


def commands(pid):
    return [["review","--pairing",str(pid),"--decision","confirmed"],
            ["freeze","--idea","cli-idea"], ["approve","--snapshot","999"]]


def test_approval_commands_reject_missing_identity(approval_cli,capsys):
    engine, db, pid = approval_cli
    for command in commands(pid):
        assert main(["--db",db,*command]) == 1
        assert "reviewer/admin" in capsys.readouterr().err
    with session_scope(engine) as session:
        assert session.get(Pairing,pid).status == "pending"
        assert session.scalar(select(Snapshot)) is None


@pytest.mark.parametrize("role,kind",[("admin","agent"),("reviewer","agent"),("reader","human"),("researcher","human")])
def test_approval_commands_reject_machine_or_unprivileged_identity(approval_cli,monkeypatch,role,kind):
    engine, db, pid = approval_cli
    with session_scope(engine) as session:
        user = create_user(session,"limited","long-password-123",role,kind)
        token = mint_session(session,user)
    monkeypatch.setenv("XIAOYI_CLI_TOKEN",token)
    for command in commands(pid):
        assert main(["--db",db,*command]) == 1
    with session_scope(engine) as session:
        assert session.get(Pairing,pid).status == "pending"
        assert session.scalar(select(Snapshot)) is None


def test_human_reviewer_can_review_freeze_approve_and_is_audited(approval_cli,monkeypatch,capsys):
    engine, db, pid = approval_cli
    with session_scope(engine) as session:
        user = create_user(session,"named-reviewer","long-password-123","reviewer")
        token = mint_session(session,user)
    monkeypatch.setenv("XIAOYI_CLI_TOKEN",token)
    assert main(["--db",db,"review","--pairing",str(pid),"--decision","confirmed","--note","checked","--json"]) == 0
    capsys.readouterr()
    assert main(["--db",db,"freeze","--idea","cli-idea","--json"]) == 0
    sid = json.loads(capsys.readouterr().out)["snapshot_id"]
    assert main(["--db",db,"approve","--snapshot",str(sid),"--json"]) == 0
    with session_scope(engine) as session:
        assert session.get(Snapshot,sid).status == "approved"
        audits = session.scalars(select(Audit).where(Audit.action.like("%_cli"))).all()
        assert len(audits) == 3 and all(a.actor == "named-reviewer" for a in audits)
        assert session.scalar(select(Review)).note == "checked"


def test_username_authentication_uses_password_and_server_role(approval_cli,monkeypatch):
    engine, db, pid = approval_cli
    with session_scope(engine) as session:
        create_user(session,"human-admin","long-password-123","admin")
    monkeypatch.setattr("getpass.getpass",lambda prompt:"wrong")
    assert main(["--db",db,"review","--pairing",str(pid),"--decision","confirmed","--username","human-admin"]) == 1
    monkeypatch.setattr("getpass.getpass",lambda prompt:"long-password-123")
    assert main(["--db",db,"review","--pairing",str(pid),"--decision","confirmed","--username","human-admin"]) == 0
