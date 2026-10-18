"""Task eligibility and immutable review regressions use isolated fixtures only."""
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select, text
from pydantic import ValidationError

from conftest import FakeAdapter, make_raw
from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.db import create_engine_for, init_db, session_scope
from xiaoyi_catalog.enrich import enrich_all
from xiaoyi_catalog.llm import EvidenceClaim
from xiaoyi_catalog.match import match_datasets, requirement_to_filter
from xiaoyi_catalog.models import DatasetVersion, Enrichment, Idea, Pairing, Review
from xiaoyi_catalog.pairing import (PairingError, approve_snapshot, freeze_snapshot, propose_pairing,
                                  review_pairing, upsert_idea)
from xiaoyi_catalog.search import search_datasets
from xiaoyi_catalog.summary_runtime import submit_summary
from xiaoyi_catalog.task_spec import evaluate_task_spec, validate_task_spec
from xiaoyi_catalog.trust import content_hash, enrichment_content, review_enrichment


def seed(engine):
    collect(engine, FakeAdapter([make_raw("lab/text", tasks=("text-classification",),
            languages=("zh",), license="mit", card="fixed evidence", size_rows=100)]), CollectScope())
    enrich_all(engine)
    with session_scope(engine) as session:
        return session.scalar(select(DatasetVersion.id)), session.scalar(select(Enrichment.id))


@pytest.mark.parametrize("item,spec,status", [
    ({"modalities": ["text"]}, {"modalities": ["text"]}, "pass"),
    ({"modalities": ["image"]}, {"modalities": ["text"]}, "fail"),
    ({}, {"modalities": ["text"]}, "unknown"),
    ({}, {"bounding_box_quality": 0.95}, "unsupported"),
    ({"modalities": ["text", "image"]}, {"all_of": [{"modalities": ["text"]}, {"modalities": ["image"]}]}, "pass"),
    ({"modalities": ["text"]}, {"all_of": [{"modalities": ["text"]}, {"modalities": ["image"]}]}, "fail"),
    ({"modalities": ["text"]}, {"any_of": [{"modalities": ["text"]}, {"future": True}]}, "pass"),
    ({}, {"any_of": [{"modalities": ["text"]}, {"future": True}]}, "unsupported"),
    ({}, {"optional": [{"future": True}]}, "pass"),
])
def test_task_spec_states(item, spec, status):
    assert evaluate_task_spec(item, spec)["status"] == status


@pytest.mark.parametrize("spec", [{"any_of": []}, {"min_rows": -1}, {"schema_version": 2},
                                    {"all_of": ["text"]}, {"min_rows": 5, "max_rows": 2}])
def test_invalid_task_shapes_rejected(spec):
    with pytest.raises(ValueError):
        validate_task_spec(spec)


@pytest.mark.parametrize("quote", ["", " ", "\n\t"])
def test_shared_claim_rejects_empty_quote(quote):
    with pytest.raises(ValidationError):
        EvidenceClaim(text="assertion", evidence_ids=[1], quotes=[quote])


def test_matching_and_direct_search_cannot_ignore_unknown(engine):
    seed(engine)
    with session_scope(engine) as session:
        filt = requirement_to_filter({"future_required_label": True})
        assert search_datasets(session, filt) == []
        result = match_datasets(session, filt)
        assert result["results"] == [] and result["constraint_summary"]["unsupported"] == 1


def test_correction_creates_revision_and_binds_exact_original(engine):
    _, ident = seed(engine)
    with session_scope(engine) as session:
        old = session.get(Enrichment, ident)
        original = deepcopy(old.value)
        original_hash = old.content_hash
        corrected = review_enrichment(session, ident, "corrected", actor="alice", role="reviewer",
                                      note="checked source", value={"corrected": True}, expected_hash=original_hash)
        session.flush()
        assert corrected.id != ident and corrected.parent_enrichment_id == ident
        assert old.value == original and old.status == "superseded"
        review = session.scalar(select(Review).where(Review.target_id == ident))
        assert review.target_snapshot["value"] == original
        assert review.target_content_hash == original_hash
        assert review.resulting_enrichment_id == corrected.id


def test_immutable_content_and_reviews_reject_overwrite(engine):
    _, ident = seed(engine)
    with session_scope(engine) as session:
        review_enrichment(session, ident, "confirmed", actor="alice", role="reviewer")
    with pytest.raises(ValueError, match="immutable"):
        with session_scope(engine) as session:
            session.get(Enrichment, ident).value = {"rewritten": True}
    with pytest.raises(ValueError, match="immutable"):
        with session_scope(engine) as session:
            session.scalar(select(Review)).note = "rewritten"


def test_stale_hash_and_unfinished_review_fail(engine):
    version, ident = seed(engine)
    with session_scope(engine) as session:
        with pytest.raises(ValueError, match="变化"):
            review_enrichment(session, ident, "confirmed", actor="alice", role="reviewer", expected_hash="0" * 64)
        dataset_id = session.get(DatasetVersion, version).dataset_id
        pending = submit_summary(session, dataset_id, version, "alice")
        with pytest.raises(ValueError, match="尚未完成"):
            review_enrichment(session, pending["id"], "confirmed", actor="alice", role="reviewer")


def test_rejected_summary_retry_preserves_review_and_original(engine):
    version, ident = seed(engine)
    with session_scope(engine) as session:
        base = session.get(Enrichment, ident)
        summary = Enrichment(dataset_id=base.dataset_id, dataset_version_id=version, kind="llm_summary",
                             producer="agent-summary/1", value={"claims": [], "uncertainty": ["unknown"]},
                             evidence_ids=[], status="candidate")
        session.add(summary)
        session.flush()
        review_enrichment(session, summary.id, "rejected", actor="alice", role="reviewer", note="missing facts")
        retry = submit_summary(session, base.dataset_id, version, "alice")
        assert retry["id"] != summary.id and retry["parent_enrichment_id"] == summary.id
        assert summary.value["uncertainty"] == ["unknown"] and summary.status == "rejected"
        session.flush()
        review = session.scalar(select(Review).where(Review.target_id == summary.id))
        assert review.target_content_hash == summary.content_hash


def test_changed_task_requires_new_confirmation_even_if_new_constraint_passes(engine):
    version, _ = seed(engine)
    idea = {"external_id": "bound", "title": "bound task", "requirements": {"modalities": ["text"]}}
    with session_scope(engine) as session:
        ident = propose_pairing(session, idea, [version], "fit")["created"][0]["id"]
        review_pairing(session, ident, "confirmed")
        upsert_idea(session, {**idea, "requirements": {"modalities": ["text"], "min_rows": 50}})
        with pytest.raises(PairingError, match="重新人工核验"):
            freeze_snapshot(session, "bound")
        review_pairing(session, ident, "confirmed")
        snapshot = freeze_snapshot(session, "bound")
        approve_snapshot(session, snapshot.id)
        assert len(snapshot.content["reviews"]) == 2
        assert all(row["target_content_hash"] for row in snapshot.content["reviews"])


def test_unsupported_task_cannot_be_confirmed(engine):
    version, _ = seed(engine)
    with session_scope(engine) as session:
        ident = propose_pairing(session, {"external_id": "unsupported", "title": "future",
            "requirements": {"all_of": [{"modalities": ["text"]}, {"segmentation_masks": True}]}},
            [version], "candidate")["created"][0]["id"]
        with pytest.raises(PairingError, match="暂不支持"):
            review_pairing(session, ident, "confirmed")


def test_migration_preserves_legacy_review_without_invented_hash(tmp_path):
    from alembic import command
    from alembic.config import Config
    import xiaoyi_catalog
    engine = create_engine_for(tmp_path / "historical.db")
    config = Config()
    config.set_main_option("script_location", str(Path(xiaoyi_catalog.__file__).parent / "migrations"))
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0007_operations")
        conn.execute(text("INSERT INTO review (id,target_kind,target_id,decision,note,role,created_at) "
                          "VALUES (1,'enrichment',99,'rejected','original rejection','reviewer',CURRENT_TIMESTAMP)"))
    init_db(engine)
    init_db(engine)
    with session_scope(engine) as session:
        review = session.get(Review, 1)
        assert review.target_id == 99 and review.note == "original rejection"
        assert review.target_content_hash == "" and review.target_snapshot == {}
        assert review.history_status == "legacy_unverified"
    engine.dispose()
