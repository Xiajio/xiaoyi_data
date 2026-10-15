"""混合检索测试：结构化硬过滤 + 向量重排 + 缺口提示。"""

from __future__ import annotations

from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.embeddings import HashEmbedder, embed_all
from xiaoyi_catalog.match import match_datasets, missing_for_requirement, requirement_to_filter
from xiaoyi_catalog.search import SearchFilter

from conftest import FakeAdapter, make_raw


def _seed(engine, *, embed=False):
    raws = [
        make_raw("a/zh-sentiment", tasks=("text-classification",), languages=("zh",), license="mit",
                 card="中文 情感 分类 评论", size_rows=100, downloads=10),
        make_raw("a/image-set", tasks=("image-classification",), languages=("en",), license="gpl-3.0",
                 card="image photo classification", size_rows=200, downloads=20),
        make_raw("a/unknown-license", tasks=("text-classification",), languages=("zh",), card="文本 分类 语料"),
    ]
    collect(engine, FakeAdapter(raws), CollectScope())
    if embed:
        embed_all(engine, HashEmbedder())


class TestMatchDatasets:
    def test_structured_only_ranks_by_downloads(self, engine):
        _seed(engine)
        with session_scope(engine) as session:
            result = match_datasets(session, SearchFilter())
            assert result["ranking"] == "downloads"
            assert result["results"][0]["source_key"] == "a/image-set"

    def test_semantic_rerank_within_structured_filter(self, engine):
        _seed(engine, embed=True)
        with session_scope(engine) as session:
            result = match_datasets(session, SearchFilter(modalities=("text",)), query_text="情感 分类", embedder=HashEmbedder())
            assert result["ranking"] == "structured+cosine"
            assert [item["source_key"] for item in result["results"]] == ["a/zh-sentiment", "a/unknown-license"]
            assert result["results"][0]["similarity"] > result["results"][1]["similarity"]
            assert all(item["modalities"] == ["text"] for item in result["results"])

    def test_query_without_embedder_falls_back(self, engine):
        _seed(engine, embed=True)
        with session_scope(engine) as session:
            result = match_datasets(session, SearchFilter(), query_text="情感", embedder=None)
            assert result["ranking"] == "downloads"

    def test_empty_index_falls_back_with_note(self, engine):
        _seed(engine)
        with session_scope(engine) as session:
            result = match_datasets(session, SearchFilter(), query_text="情感", embedder=HashEmbedder())
            assert result["ranking"] == "downloads" and "嵌入索引为空" in result["note"]


class TestRequirementMapping:
    def test_maps_contract_keys_and_ignores_unknown(self):
        filt = requirement_to_filter({"modalities": ["text"], "license_class": ["permissive"], "min_rows": 50,
                                      "limit": 5, "future_key": "ignored"})
        assert filt.modalities == ("text",) and filt.license_classes == ("permissive",)
        assert filt.min_rows == 50 and filt.limit == 5

    def test_defaults(self):
        filt = requirement_to_filter({})
        assert filt.modalities == () and filt.min_rows is None and filt.limit == 10


class TestMissingForRequirement:
    def test_flags_modality_license_and_size_gaps(self):
        item = {"modalities": ["image"], "license_class": "unknown", "size_rows": 10}
        gaps = missing_for_requirement(item, {"modalities": ["text"], "license_class": ["permissive"], "min_rows": 100})
        assert any("模态不匹配" in gap for gap in gaps)
        assert any("许可分级" in gap for gap in gaps)
        assert any("低于需求" in gap for gap in gaps)

    def test_unknown_size_is_explicit_gap(self):
        gaps = missing_for_requirement({"modalities": ["text"], "license_class": "permissive", "size_rows": None},
                                       {"min_rows": 100})
        assert gaps == ["规模未知，无法确认满足最小行数"]

    def test_satisfied_requirement_has_no_gaps(self):
        item = {"modalities": ["text"], "license_class": "permissive", "size_rows": 500}
        assert missing_for_requirement(item, {"modalities": ["text"], "license_class": ["permissive"], "min_rows": 100}) == []
