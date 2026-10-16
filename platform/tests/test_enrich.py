"""整理流水线测试：frontmatter 解析、schema 摘要、质量信号、幂等重算。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.enrich import enrich_all, enrich_dataset, parse_card_frontmatter, quality_signals, schema_summary
from xiaoyi_catalog.models import Dataset, Enrichment

from conftest import FakeAdapter, make_raw

CARD = """---
license: mit
dataset_info:
  features:
    - name: text
      dtype: string
    - name: label
      dtype: int32
  splits:
    - name: train
      num_examples: 1000
    - name: test
      num_examples: 200
---
# 正文
"""


class TestFrontmatter:
    def test_parses_valid_frontmatter(self):
        assert parse_card_frontmatter(CARD)["license"] == "mit"

    def test_no_frontmatter_or_broken_yaml(self):
        assert parse_card_frontmatter("# 只有正文") == {}
        assert parse_card_frontmatter("") == {}
        assert parse_card_frontmatter("---\n[::\n---\n正文") == {}


class TestSchemaSummary:
    def test_extracts_features_splits_and_totals(self):
        summary = schema_summary(CARD)
        assert summary["present"] is True
        assert summary["features"] == [{"name": "text", "dtype": "string"}, {"name": "label", "dtype": "int32"}]
        assert summary["splits"] == [{"name": "train", "num_examples": 1000}, {"name": "test", "num_examples": 200}]
        assert summary["total_examples"] == 1200

    def test_absent_dataset_info_marks_not_present(self):
        assert schema_summary("# 无元数据") == {"present": False}


class TestQualitySignals:
    def _dataset(self, **overrides) -> Dataset:
        values = dict(card_text="x" * 600, license_class="permissive", downloads=5000,
                      source_modified_at=datetime.now(timezone.utc) - timedelta(days=10))
        values.update(overrides)
        return Dataset(source_id=1, source_key="a/b", fingerprint="fp", **values)

    def test_high_quality(self):
        assert quality_signals(self._dataset())["level"] == "high"

    def test_low_quality_with_reasons(self):
        result = quality_signals(self._dataset(card_text="", license_class="unknown", downloads=0,
                                               source_modified_at=None))
        assert result["level"] == "low"
        assert "缺少数据集卡片" in result["reasons"] and "许可未知" in result["reasons"]

    def test_stale_update_is_noted_but_other_signals_dominate(self):
        old = datetime.now(timezone.utc) - timedelta(days=400)
        result = quality_signals(self._dataset(source_modified_at=old))
        assert any("未更新" in reason for reason in result["reasons"])
        # 规则是加法的：卡片充实 + 许可明确 + 高下载仍可得 high，staleness 只少加分不封顶
        assert result["level"] == "high"


class TestEnrichAll:
    def test_enriches_every_dataset_with_evidence_pointer_and_is_idempotent(self, engine):
        collect(engine, FakeAdapter([make_raw("a/ds1", card=CARD), make_raw("a/ds2", card="无元数据正文")]), CollectScope())
        stats = enrich_all(engine)
        assert stats == {"processed": 2, "skipped": 0}
        with session_scope(engine) as session:
            entries = session.scalars(select(Enrichment)).all()
            assert len(entries) == 4  # 每集 schema + quality
            assert all(entry.status == "candidate" and entry.producer == "enrich/0.2" for entry in entries)
            with_card = [entry for entry in entries if entry.evidence_ids]
            assert len(with_card) == 4  # 两个数据集都有卡片证据
            schema_entry = next(entry for entry in entries if entry.kind == "schema" and entry.value.get("present"))
            assert schema_entry.value["total_examples"] == 1200
        again = enrich_all(engine)
        assert again["processed"] == 2
        with session_scope(engine) as session:
            assert session.scalar(select(func.count(Enrichment.id)).where(Enrichment.status == "candidate")) == 4
            assert session.scalar(select(func.count(Enrichment.id)).where(Enrichment.status == "superseded")) == 4  # 历史产物保留
        skipped = enrich_all(engine, only_missing=True)
        assert skipped == {"processed": 0, "skipped": 2}
