"""嵌入索引（M2）：TextEmbedder 协议 + 存储 + 余弦检索。

- HashEmbedder：确定性词袋哈希向量，无模型依赖，供单元测试与离线冒烟。
- SentenceTransformerEmbedder：内网可部署的开源模型（默认 bge-small-zh-v1.5），
  懒加载；模型名与版本写入 enrichment.producer，换模型即重建索引。
- 存储用 enrichment 表（kind='embedding'），查询时读全量向量做 numpy 余弦。
  千级规模下足够快；上量后按设计文档迁 pgvector/sqlite-vec，接口不变。
"""

from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .db import session_scope
from .enrich import embedding_text, embedding_text_hash
from .models import Dataset, Enrichment

EMBEDDING_KIND = "embedding"


class TextEmbedder(Protocol):
    model_name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """确定性词袋哈希嵌入器：同一文本永远得到同一向量，无外部依赖。"""

    def __init__(self, dim: int = 128):
        self.model_name = f"hash-embedder/0.1(dim={dim})"
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = np.zeros(self.dim, dtype=np.float32)
            for token in text.lower().split():
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                vector[int.from_bytes(digest[:4], "big") % self.dim] += 1.0
            norm = float(np.linalg.norm(vector))
            if norm > 0:
                vector /= norm
            vectors.append(vector.tolist())
        return vectors


class SentenceTransformerEmbedder:
    """真实嵌入模型，首次 embed 时才下载/加载权重。"""

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5"):
        self.model_name = model_name
        self._model = None
        self._dim = 0

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
            dimension = getattr(self._model, "get_embedding_dimension", None) or getattr(self._model, "get_sentence_embedding_dimension")
            self._dim = int(dimension())
        return self._model

    @property
    def dim(self) -> int:
        if not self._dim:
            self._load()
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        return model.encode(texts, normalize_embeddings=True).tolist()


def upsert_embeddings(session: Session, embedder: TextEmbedder, datasets: list[Dataset]) -> dict:
    """为给定数据集生成/更新嵌入候选；文本未变化（哈希一致）的跳过。返回统计。"""
    stats = {"embedded": 0, "skipped": 0}
    pending: list[tuple[Dataset, str]] = []
    for dataset in datasets:
        text = embedding_text(dataset)
        text_hash = embedding_text_hash(dataset)
        existing = session.scalar(
            select(Enrichment).where(
                Enrichment.dataset_id == dataset.id,
                Enrichment.kind == EMBEDDING_KIND,
                Enrichment.producer == embedder.model_name,
            )
        )
        if existing is not None and existing.value.get("text_hash") == text_hash:
            stats["skipped"] += 1
            continue
        if existing is not None:
            session.delete(existing)
        pending.append((dataset, text))
    if pending:
        vectors = embedder.embed([text for _, text in pending])
        for (dataset, text), vector in zip(pending, vectors):
            session.add(Enrichment(
                dataset_id=dataset.id,
                kind=EMBEDDING_KIND,
                producer=embedder.model_name,
                value={"dim": embedder.dim, "vector": vector, "text_hash": embedding_text_hash(dataset)},
                confidence="",
                evidence_ids=[],
            ))
        stats["embedded"] = len(pending)
    return stats


def embed_all(engine: Engine, embedder: TextEmbedder) -> dict:
    with session_scope(engine) as session:
        datasets = session.scalars(select(Dataset).where(Dataset.status == "active")).all()
        return upsert_embeddings(session, embedder, datasets)


def _load_index(session: Session, producer: str | None = None) -> tuple[list[int], np.ndarray]:
    """读取嵌入索引。指定 producer 时只用该模型的向量（查询与索引必须同模型）；
    缺省取数量最多的一组。"""
    rows = session.scalars(select(Enrichment).where(Enrichment.kind == EMBEDDING_KIND)).all()
    if producer is not None:
        rows = [row for row in rows if row.producer == producer]
    elif rows:
        producers: dict[str, int] = {}
        for row in rows:
            producers[row.producer] = producers.get(row.producer, 0) + 1
        main_producer = max(producers, key=producers.get)
        rows = [row for row in rows if row.producer == main_producer]
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    ids = [row.dataset_id for row in rows]
    vectors = [row.value["vector"] for row in rows]
    return ids, np.asarray(vectors, dtype=np.float32)


def cosine_top_k(session: Session, query_vector: list[float], k: int, *, producer: str | None = None) -> list[tuple[int, float]]:
    """返回 (dataset_id, 相似度) 降序前 k 个；空索引或生产者无匹配返回空列表。"""
    ids, matrix = _load_index(session, producer)
    if not ids or matrix.size == 0:
        return []
    query = np.asarray(query_vector, dtype=np.float32)
    if matrix.shape[1] != query.shape[0]:
        return []  # 索引与查询维度不一致（模型不匹配），不报错误导排序
    scores = matrix @ query
    order = np.argsort(-scores)[:k]
    return [(ids[int(index)], float(scores[int(index)])) for index in order]
