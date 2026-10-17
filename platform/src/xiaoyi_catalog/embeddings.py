"""嵌入索引（M2）：TextEmbedder 协议 + 存储 + 余弦检索。

- HashEmbedder：确定性词袋哈希向量，无模型依赖，供单元测试与离线冒烟。
- SentenceTransformerEmbedder：内网可部署的开源模型（默认 bge-small-zh-v1.5），
  懒加载；模型名与版本写入 enrichment.producer，换模型即重建索引。
- Enrichment 保留版本证据；PostgreSQL 使用 pgvector 余弦查询，SQLite 使用 numpy。
"""

from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .db import session_scope
from .enrich import embedding_text, embedding_text_hash
from .models import Dataset, DatasetVersion, Enrichment, VectorEmbedding
from .search import latest_version, version_to_dict

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
    from types import SimpleNamespace
    from .enrich import _card_evidence_id
    stats = {"embedded": 0, "skipped": 0}
    pending = []
    for dataset in datasets:
        version = latest_version(session, dataset.id)
        if version is None or version.provenance_status != "verified":
            stats["skipped"] += 1
            continue
        captured = SimpleNamespace(**version_to_dict(session, version))
        text, text_hash = embedding_text(captured), embedding_text_hash(captured)
        existing = session.scalar(select(Enrichment).where(
            Enrichment.dataset_version_id == version.id, Enrichment.status != "superseded",
            Enrichment.kind == EMBEDDING_KIND, Enrichment.producer == embedder.model_name))
        if existing is not None and existing.value.get("text_hash") == text_hash:
            stats["skipped"] += 1
            continue
        if existing is not None:
            existing.status = "superseded"
        pending.append((dataset, version, text, text_hash))
    if pending:
        vectors = embedder.embed([text for _, _, text, _ in pending])
        if len(vectors) != len(pending):
            raise ValueError("Embedding response count does not match input count")
        for (dataset, version, text, text_hash), vector in zip(pending, vectors):
            if len(vector) != embedder.dim or not np.isfinite(vector).all():
                raise ValueError("Invalid embedding vector")
            entry = Enrichment(dataset_id=dataset.id, dataset_version_id=version.id,
                kind=EMBEDDING_KIND, producer=embedder.model_name,
                value={"dim": embedder.dim, "vector": vector, "text_hash": text_hash}, confidence="",
                evidence_ids=_card_evidence_id(session, dataset.id, version.id))
            session.add(entry)
            session.flush()
            session.add(VectorEmbedding(enrichment_id=entry.id, dataset_id=dataset.id,
                dataset_version_id=version.id, producer=embedder.model_name, dimension=embedder.dim, vector=vector))
        stats["embedded"] = len(pending)
    return stats


def embed_all(engine: Engine, embedder: TextEmbedder) -> dict:
    with session_scope(engine) as session:
        datasets = session.scalars(select(Dataset).where(Dataset.status == "active")).all()
        return upsert_embeddings(session, embedder, datasets)


def _load_index(session: Session, producer: str | None = None, dataset_ids: list[int] | None = None) -> tuple[list[int], np.ndarray]:
    """读取嵌入索引。指定 producer 时只用该模型的向量（查询与索引必须同模型）；
    缺省取数量最多的一组。"""
    from sqlalchemy import func
    latest_ids = select(func.max(DatasetVersion.id)).where(DatasetVersion.kind == "source").group_by(DatasetVersion.dataset_id)
    stmt = select(Enrichment).where(Enrichment.kind == EMBEDDING_KIND, Enrichment.status != "superseded",
                                    Enrichment.dataset_version_id.in_(latest_ids))
    if producer is not None:
        stmt = stmt.where(Enrichment.producer == producer)
    if dataset_ids is not None:
        stmt = stmt.where(Enrichment.dataset_id.in_(dataset_ids))
    rows = session.scalars(stmt.order_by(Enrichment.id.desc())).all()
    latest = {row.dataset_id: latest_version(session, row.dataset_id) for row in rows}
    rows = [row for row in rows if latest[row.dataset_id] and row.dataset_version_id == latest[row.dataset_id].id]
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


def cosine_top_k(session: Session, query_vector: list[float], k: int, *, producer: str | None = None, dataset_ids: list[int] | None = None) -> list[tuple[int, float]]:
    """返回 (dataset_id, 相似度) 降序前 k 个；空索引或生产者无匹配返回空列表。"""
    if not query_vector or not np.isfinite(query_vector).all() or np.linalg.norm(query_vector) == 0:
        return []
    if session.bind.dialect.name == "postgresql" and producer is not None:
        from sqlalchemy import Float, bindparam, cast, func
        latest_ids = select(func.max(DatasetVersion.id)).where(DatasetVersion.kind == "source").group_by(DatasetVersion.dataset_id)
        distance = VectorEmbedding.vector.op("<=>", return_type=Float)(
            cast(bindparam("query_vector", query_vector, type_=VectorEmbedding.Vector()), VectorEmbedding.Vector()))
        stmt = (select(VectorEmbedding.dataset_id, (1 - distance).label("similarity"))
                .join(Enrichment, Enrichment.id == VectorEmbedding.enrichment_id)
                .where(VectorEmbedding.producer == producer, VectorEmbedding.dimension == len(query_vector),
                       VectorEmbedding.dataset_version_id.in_(latest_ids), Enrichment.status != "superseded"))
        if dataset_ids is not None:
            stmt = stmt.where(VectorEmbedding.dataset_id.in_(dataset_ids))
        return [(row[0], float(row[1])) for row in session.execute(stmt.order_by(distance).limit(k))]
    ids, matrix = _load_index(session, producer, dataset_ids)
    if not ids or matrix.size == 0:
        return []
    query = np.asarray(query_vector, dtype=np.float32)
    if matrix.shape[1] != query.shape[0]:
        return []  # 索引与查询维度不一致（模型不匹配），不报错误导排序
    norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query)
    scores = np.divide(matrix @ query, norms, out=np.zeros(len(ids), dtype=np.float32), where=norms != 0)
    order = np.argsort(-scores)[:k]
    return [(ids[int(index)], float(scores[int(index)])) for index in order]
