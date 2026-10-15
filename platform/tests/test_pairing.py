"""配对与快照服务测试：提案幂等、人工核验、冻结/审批/导出、失效联动。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.models import Dataset, DatasetVersion
from xiaoyi_catalog.pairing import (PairingError, approve_snapshot, export_snapshot, freeze_snapshot,
                                    propose_pairing, review_pairing, snapshot_status, upsert_idea)

from conftest import FakeAdapter, make_raw

IDEA = {"external_id": "lingxi-idea-001", "title": "中文情感分类基准",
        "statement": "验证中文评论情感分类的基准可行性",
        "requirements": {"modalities": ["text"], "license_class": ["permissive"], "min_rows": 50},
        "source_refs": ["10.1234/demo"]}


@pytest.fixture()
def seeded(engine):
    collect(engine, FakeAdapter([
        make_raw("a/zh-sentiment", tasks=("text-classification",), languages=("zh",), license="mit",
                 card="中文情感", size_rows=100),
        make_raw("a/no-license", tasks=("text-classification",), card="无许可"),
    ]), CollectScope())
    return engine


def _version_id(session, source_key):
    return session.scalar(
        select(DatasetVersion.id).join(Dataset, Dataset.id == DatasetVersion.dataset_id).where(Dataset.source_key == source_key)
    )


class TestUpsertIdea:
    def test_requires_fields_and_updates_on_reimport(self, engine):
        with session_scope(engine) as session:
            with pytest.raises(PairingError, match="external_id"):
                upsert_idea(session, {"title": "缺字段"})
            idea = upsert_idea(session, IDEA)
            imported_at = idea.imported_at
            again = upsert_idea(session, {**IDEA, "title": "改名"})
            assert again.id == idea.id and again.title == "改名" and again.imported_at == imported_at

    def test_requirements_must_be_dict(self, engine):
        with session_scope(engine) as session:
            with pytest.raises(PairingError, match="requirements"):
                upsert_idea(session, {"external_id": "x", "title": "t", "requirements": ["not", "dict"]})


class TestProposePairing:
    def test_creates_pending_with_warnings_and_evidence(self, seeded):
        with session_scope(seeded) as session:
            result = propose_pairing(session, IDEA, [_version_id(session, "a/zh-sentiment"), _version_id(session, "a/no-license")],
                                     "两者覆盖目标模态")
            assert [item["status"] for item in result["created"]] == ["pending", "pending"]
            assert result["created"][0]["warnings"] == []
            assert any("许可分级" in warning for warning in result["created"][1]["warnings"])
            assert result["created"][0]["id"] != result["created"][1]["id"]
            assert result["note"]

    def test_duplicate_propose_is_idempotent_and_missing_version_reported(self, seeded):
        with session_scope(seeded) as session:
            version_id = _version_id(session, "a/zh-sentiment")
            first = propose_pairing(session, IDEA, [version_id], "理由")
            again = propose_pairing(session, IDEA, [version_id], "理由")
            assert again["created"] == [] and again["existing"][0]["id"] == first["created"][0]["id"]
            with pytest.raises(PairingError, match="999"):
                propose_pairing(session, IDEA, [999], "理由")

    def test_requires_rationale_and_versions(self, seeded):
        with session_scope(seeded) as session:
            with pytest.raises(PairingError, match="rationale"):
                propose_pairing(session, IDEA, [1], "  ")
            with pytest.raises(PairingError, match="至少"):
                propose_pairing(session, IDEA, [], "理由")


class TestReviewFreezeApproveExport:
    def _proposed(self, session):
        return propose_pairing(session, IDEA, [_version_id(session, "a/zh-sentiment")], "理由")["created"][0]["id"]

    def test_full_lifecycle(self, seeded):
        with session_scope(seeded) as session:
            pairing_id = self._proposed(session)
            with pytest.raises(PairingError, match="没有已确认"):
                freeze_snapshot(session, IDEA["external_id"])
            review_pairing(session, pairing_id, "confirmed")
            snapshot = freeze_snapshot(session, IDEA["external_id"], note="首版")
            assert snapshot.version == 1 and snapshot.status == "pending"
            assert snapshot.content["datasets"][0]["dataset"]["source_key"] == "a/zh-sentiment"
            with pytest.raises(PairingError, match="只能导出已批准"):
                export_snapshot(session, snapshot.id)
            approve_snapshot(session, snapshot.id)
            exported = export_snapshot(session, snapshot.id)
            assert exported["type"] == "idea_dataset_snapshot" and exported["licenses"] == ["mit"]
            assert "冻结" in exported["disclaimer"]
            status = snapshot_status(session, IDEA["external_id"])
            assert status["pairings"][0]["status"] == "confirmed"
            assert status["snapshots"][0]["status"] == "approved"

    def test_second_pending_snapshot_blocked(self, seeded):
        with session_scope(seeded) as session:
            pairing_id = self._proposed(session)
            review_pairing(session, pairing_id, "confirmed")
            freeze_snapshot(session, IDEA["external_id"])
            with pytest.raises(PairingError, match="待审批"):
                freeze_snapshot(session, IDEA["external_id"])

    def test_rejecting_pairing_invalidates_pending_snapshot(self, seeded):
        with session_scope(seeded) as session:
            pairing_id = self._proposed(session)
            review_pairing(session, pairing_id, "confirmed")
            snapshot = freeze_snapshot(session, IDEA["external_id"])
            review_pairing(session, pairing_id, "rejected")
            assert session.get(type(snapshot), snapshot.id).status == "invalidated"
            with pytest.raises(PairingError, match="已是"):
                review_pairing(session, pairing_id, "rejected")

    def test_status_for_unknown_idea(self, seeded):
        with session_scope(seeded) as session:
            assert snapshot_status(session, "no-such-idea") == {"idea": None, "pairings": [], "snapshots": []}
