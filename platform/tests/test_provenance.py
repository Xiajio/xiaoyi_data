"""Legacy provenance can only be repaired by a human's explicit pinned acquisition."""
from copy import deepcopy
import pytest
from sqlalchemy import select
from xiaoyi_catalog.auth import Principal
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.models import Source, Dataset, DatasetVersion, Evidence, Audit, Idea, Pairing, Snapshot
from xiaoyi_catalog.pairing import review_pairing, freeze_snapshot
from xiaoyi_catalog.provenance import recover_version
from conftest import FakeAdapter, make_raw

SHA = "a"*40
ACTOR = Principal(1, "human-reviewer", "reviewer", "human")


@pytest.fixture
def legacy(engine):
    with session_scope(engine) as session:
        source = Source(kind="huggingface", endpoint="https://huggingface.co")
        session.add(source); session.flush()
        dataset = Dataset(source_id=source.id, source_key="org/legacy", fingerprint="old-fingerprint", license="gpl-3.0")
        session.add(dataset); session.flush()
        version = DatasetVersion(dataset_id=dataset.id, revision=SHA)
        session.add(version); session.flush()
        evidence = Evidence(dataset_id=dataset.id, kind="card", excerpt="unbound legacy card")
        session.add(evidence); session.flush()
        idea = Idea(external_id="legacy-idea", title="legacy", requirements={"modalities":["text"],"license_class":["permissive"]})
        session.add(idea); session.flush()
        pairing = Pairing(idea_id=idea.id,dataset_version_id=version.id,rationale="legacy",evidence_ids=[evidence.id])
        session.add(pairing); session.flush()
        snapshot = Snapshot(idea_id=idea.id,version=1,pairing_ids=[pairing.id],status="approved",
                            content={"datasets":[{"revision":SHA,"dataset":{"license":"old-unverified"}}]})
        session.add(snapshot); session.flush()
        return version.id, pairing.id, snapshot.id, deepcopy(snapshot.content)


def adapter(revision=SHA):
    return FakeAdapter([make_raw("org/legacy",revision=revision,license="mit",tasks=("text-classification",),card="Pinned official evidence")])


def test_recovery_requires_human_and_exact_revision(engine, legacy):
    vid, _, _, _ = legacy
    for actor in (Principal(2,"machine","admin","agent"), Principal(3,"reader","reader","human")):
        with pytest.raises(ValueError, match="人类"):
            recover_version(engine,vid,actor,adapter=adapter())
    with pytest.raises(ValueError, match="commit 不一致"):
        recover_version(engine,vid,ACTOR,adapter=adapter("b"*40))
    with session_scope(engine) as session:
        assert session.get(DatasetVersion,vid).provenance_status == "legacy_unverified"


def test_explicit_recovery_preserves_history_and_requires_new_review(engine, legacy):
    vid, pid, sid, content = legacy
    result = recover_version(engine,vid,ACTOR,adapter=adapter())
    assert result["snapshots_changed"] is False and result["provenance_status"] == "verified"
    with session_scope(engine) as session:
        version = session.get(DatasetVersion,vid)
        assert version.metadata_json["license"] == "mit"
        assert session.get(Snapshot,sid).content == content
        assert session.scalar(select(Evidence).where(Evidence.dataset_version_id.is_(None))) is not None
        assert session.get(Pairing,pid).status == "pending"
        assert session.scalar(select(Audit).where(Audit.action == "legacy_provenance_recovered")).actor == ACTOR.username
        review_pairing(session,pid,"confirmed")
        fresh = freeze_snapshot(session,"legacy-idea")
        assert fresh.version == 2 and fresh.content["datasets"][0]["dataset"]["license"] == "mit"
        assert all(session.get(Evidence,eid).dataset_version_id == vid for eid in session.get(Pairing,pid).evidence_ids)
    with pytest.raises(ValueError, match="不可重写"):
        recover_version(engine,vid,ACTOR,adapter=adapter())


def test_ordinary_collection_does_not_promote_legacy_version(engine, legacy):
    vid, _, _, _ = legacy
    source_adapter = adapter()
    source_adapter.kind, source_adapter.endpoint = "huggingface", "https://huggingface.co"
    result = collect(engine,source_adapter,CollectScope())
    assert result.stats["duplicates"] == 1
    with session_scope(engine) as session:
        version = session.get(DatasetVersion,vid)
        assert version.provenance_status == "legacy_unverified" and version.metadata_json == {}
