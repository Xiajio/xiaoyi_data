"""嵌入索引测试：确定性嵌入器、增量跳过、余弦检索。"""

from __future__ import annotations

import numpy as np
from sqlalchemy import select

from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.embeddings import HashEmbedder, cosine_top_k, embed_all
from xiaoyi_catalog.models import Dataset, Enrichment

from conftest import FakeAdapter, make_raw


def _seed(engine):
    raws = [
        make_raw("a/zh-sentiment", card="中文 情感 分类 评论 数据", tasks=("text-classification",)),
        make_raw("a/image-set", card="image photo picture classification", tasks=("image-classification",)),
        make_raw("a/qa", card="question answer reading comprehension", tasks=("question-answering",)),
    ]
    collect(engine, FakeAdapter(raws), CollectScope())


class TestHashEmbedder:
    def test_deterministic_and_normalized(self):
        embedder = HashEmbedder()
        first, = embedder.embed(["中文 情感 分类"])
        again, = embedder.embed(["中文 情感 分类"])
        assert first == again
        assert abs(float(np.linalg.norm(first)) - 1.0) < 1e-5
        other, = embedder.embed(["完全不同的 内容"])
        assert first != other


class TestEmbedAll:
    def test_embeds_all_and_skips_unchanged(self, engine):
        _seed(engine)
        embedder = HashEmbedder()
        stats = embed_all(engine, embedder)
        assert stats == {"embedded": 3, "skipped": 0}
        with session_scope(engine) as session:
            rows = session.scalars(select(Enrichment).where(Enrichment.kind == "embedding")).all()
            assert len(rows) == 3
            assert all(row.producer == embedder.model_name and row.status == "candidate" for row in rows)
            assert all(len(row.value["vector"]) == embedder.dim for row in rows)
        assert embed_all(engine, embedder) == {"embedded": 0, "skipped": 3}

    def test_changed_text_reembeds(self, engine):
        _seed(engine)
        embedder = HashEmbedder()
        embed_all(engine, embedder)
        with session_scope(engine) as session:
            dataset = session.scalar(select(Dataset).where(Dataset.source_key == "a/qa"))
            dataset.card_text = "彻底改写的内容 完全 不同"
        stats = embed_all(engine, embedder)
        assert stats == {"embedded": 1, "skipped": 2}
        with session_scope(engine) as session:
            rows = session.scalars(select(Enrichment).where(Enrichment.kind == "embedding")).all()
            assert len(rows) == 3  # 重新生成是替换，不堆积


class TestCosineTopK:
    def test_empty_index_returns_empty(self, engine):
        _seed(engine)
        with session_scope(engine) as session:
            assert cosine_top_k(session, HashEmbedder().embed(["任意"])[0], 5) == []

    def test_ranking_finds_topically_closest(self, engine):
        _seed(engine)
        embedder = HashEmbedder()
        embed_all(engine, embedder)
        with session_scope(engine) as session:
            hits = cosine_top_k(session, embedder.embed(["情感 分类"])[0], 2)
            assert hits and hits[0][0] == session.scalar(select(Dataset.id).where(Dataset.source_key == "a/zh-sentiment"))
            assert hits[0][1] > 0
            assert hits[0][1] >= hits[-1][1]

    def test_mixed_producers_do_not_cross_query(self, engine):
        _seed(engine)
        small = HashEmbedder(dim=128)
        big = HashEmbedder(dim=256)
        embed_all(engine, small)
        embed_all(engine, big)
        with session_scope(engine) as session:
            hits = cosine_top_k(session, big.embed(["情感"])[0], 3, producer=big.model_name)
            assert hits  # 只用同模型的 256 维索引
            mismatched = cosine_top_k(session, big.embed(["情感"])[0], 3, producer=small.model_name)
            assert mismatched == []  # 维度不一致时不报错、不误导排序
