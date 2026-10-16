"""结构化检索测试：字段内 OR、字段间 AND、missing 清单、目录统计。"""

from __future__ import annotations

import pytest

from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.search import SearchFilter, catalog_stats, search_datasets

from conftest import FakeAdapter, make_raw


@pytest.fixture()
def seeded(engine):
    raws = [
        make_raw("alice/zh-sentiment", tasks=("text-classification",), languages=("zh",), license="mit",
                 card="中文情感分类数据集", size_rows=100, downloads=10),
        make_raw("bob/imgs", tasks=("image-classification",), languages=("en",), license="gpl-3.0",
                 card="image classification set", size_rows=200, downloads=20),
        make_raw("carl/mesh", tags=("modality:3d",), license="cc-by-nc-4.0", downloads=5),
        make_raw("dave/qa", tasks=("question-answering",), languages=("zh", "en"), card="qa dataset"),
    ]
    collect(engine, FakeAdapter(raws), CollectScope())
    return engine


def _keys(results):
    return [item["source_key"] for item in results]


class TestSearch:
    def test_modality_or_within_field(self, seeded):
        with session_scope(seeded) as session:
            assert _keys(search_datasets(session, SearchFilter(modalities=("text",)))) == ["alice/zh-sentiment", "dave/qa"]
            assert set(_keys(search_datasets(session, SearchFilter(modalities=("text", "image"))))) == {"alice/zh-sentiment", "bob/imgs", "dave/qa"}

    def test_and_across_fields(self, seeded):
        with session_scope(seeded) as session:
            assert _keys(search_datasets(session, SearchFilter(modalities=("image",), languages=("zh",)))) == []
            assert _keys(search_datasets(session, SearchFilter(modalities=("text",), languages=("zh",)))) == ["alice/zh-sentiment", "dave/qa"]

    def test_license_class_filter(self, seeded):
        with session_scope(seeded) as session:
            assert _keys(search_datasets(session, SearchFilter(license_classes=("permissive",)))) == ["alice/zh-sentiment"]
            assert _keys(search_datasets(session, SearchFilter(license_classes=("research-only",)))) == ["carl/mesh"]
            assert _keys(search_datasets(session, SearchFilter(license_classes=("unknown",)))) == ["dave/qa"]

    def test_query_and_min_rows(self, seeded):
        with session_scope(seeded) as session:
            assert _keys(search_datasets(session, SearchFilter(query="情感"))) == ["alice/zh-sentiment"]
            assert _keys(search_datasets(session, SearchFilter(min_rows=150))) == ["bob/imgs"]

    def test_missing_list_flags_unknown_without_hiding(self, seeded):
        with session_scope(seeded) as session:
            results = {item["source_key"]: item for item in search_datasets(session, SearchFilter())}
            assert results["carl/mesh"]["missing"] == ["license", "card"] or results["carl/mesh"]["missing"] == ["card"]
            assert results["carl/mesh"]["license_class"] == "research-only"
            assert results["carl/mesh"]["missing"] == ["card"]
            assert results["dave/qa"]["missing"] == ["license"]
            assert results["alice/zh-sentiment"]["missing"] == []

    def test_results_sorted_by_downloads_then_id(self, seeded):
        with session_scope(seeded) as session:
            assert _keys(search_datasets(session, SearchFilter()))[0] == "bob/imgs"


class TestStats:
    def test_catalog_stats(self, seeded):
        with session_scope(seeded) as session:
            stats = catalog_stats(session)
            assert stats["datasets"] == 4 and stats["runs"] == 1 and stats["evidence"] == 7
            assert stats["by_license_class"] == {"permissive": 1, "copyleft": 1, "research-only": 1, "unknown": 1}
            assert stats["by_modality"] == {"3d": 1, "image": 1, "text": 2}
