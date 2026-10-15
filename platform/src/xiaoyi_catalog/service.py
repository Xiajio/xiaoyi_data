"""目录服务（M3）：策展 UI 的 REST 数据源。

- 只读：目录浏览、证据、整理候选队列、配对与快照状态、运行与审计；
- 写入：整理候选核验、配对核验、冻结/审批——全部经服务层留 review + audit；
- 配对/快照的业务规则复用 pairing.py（提案只能 pending、冻结需已确认、导出需已批准）。

运行：XIAOYI_CATALOG_DB=./data/catalog.db uvicorn xiaoyi_catalog.service:create_app --factory --port 5280
CORS：默认放行本机 vite 开发端口；其他来源用 XIAOYI_CATALOG_CORS（逗号分隔精确 origin）。
"""

from __future__ import annotations

import os
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, or_, select

from .db import create_engine_for, init_db, session_scope
from .embeddings import SentenceTransformerEmbedder, TextEmbedder
from .match import match_datasets, requirement_to_filter
from .models import Audit, Dataset, DatasetVersion, Enrichment, Evidence, Idea, Pairing, Review, Run, Snapshot, utcnow
from .pairing import (PairingError, approve_snapshot, export_snapshot, freeze_snapshot,
                      propose_pairing, review_pairing, snapshot_status)
from .search import catalog_stats, dataset_to_dict

DEFAULT_CORS = "http://localhost:5173,http://127.0.0.1:5173"


class ReviewIn(BaseModel):
    decision: Literal["confirmed", "rejected"]
    note: str = ""
    role: str = "reviewer"


class FreezeIn(BaseModel):
    idea_external_id: str
    pairing_ids: list[int] | None = None
    note: str = ""
    role: str = "manager"


class ApproveIn(BaseModel):
    role: str = "lead"


class MatchIn(BaseModel):
    """智能体检索请求：requirements 为思路卡需求契约，query 为自由文本（有嵌入索引时参与语义排序）。"""

    requirements: dict = Field(default_factory=dict)
    query: str = ""


class ProposeIn(BaseModel):
    """智能体配对提案：idea 为思路卡（external_id/title/requirements 必填），提案一律落 pending。"""

    idea: dict
    dataset_version_ids: list[int]
    rationale: str


def _404(thing: str, ident: object) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{thing} {ident} 不存在")


def _write_review(session, target_kind: str, target_id: int, body: ReviewIn) -> Review:
    review = Review(target_kind=target_kind, target_id=target_id, decision=body.decision,
                    note=body.note, role=body.role)
    session.add(review)
    session.add(Audit(actor=body.role, action=f"{target_kind}_review",
                      detail=f"{target_kind} #{target_id} → {body.decision}{('；' + body.note) if body.note else ''}"))
    return review


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("XIAOYI_CATALOG_DB", "./data/catalog.db")
    engine = create_engine_for(path)
    init_db(engine)
    app = FastAPI(title="xiaoyi-catalog", version="0.2.0")
    app.state.db_path = path
    # 语义排序可选：XIAOYI_CATALOG_EMBED=1 时懒加载嵌入模型（服务机默认关闭，避免重依赖）
    app.state.embedder: TextEmbedder | None = None
    app.state.embedder_disabled = os.environ.get("XIAOYI_CATALOG_EMBED", "0") != "1"

    def service_embedder() -> TextEmbedder | None:
        if app.state.embedder_disabled:
            return None
        if app.state.embedder is None:
            try:
                embedder = SentenceTransformerEmbedder(os.environ.get("XIAOYI_CATALOG_EMBED_MODEL", "BAAI/bge-small-zh-v1.5"))
                embedder.embed(["预热"])
                app.state.embedder = embedder
            except Exception:
                app.state.embedder_disabled = True
                return None
        return app.state.embedder
    origins = [origin.strip() for origin in os.environ.get("XIAOYI_CATALOG_CORS", DEFAULT_CORS).split(",") if origin.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["*"])

    @app.on_event("shutdown")
    def _shutdown() -> None:
        engine.dispose()

    @app.get("/api/overview")
    def overview() -> dict:
        with session_scope(engine) as session:
            stats = catalog_stats(session)
            stats["enrichment_candidates"] = session.scalar(
                select(func.count(Enrichment.id)).where(Enrichment.status == "candidate")) or 0
            stats["pairings_pending"] = session.scalar(
                select(func.count(Pairing.id)).where(Pairing.status == "pending")) or 0
            stats["snapshots"] = dict(session.execute(
                select(Snapshot.status, func.count()).group_by(Snapshot.status)).all())
            stats["ideas"] = session.scalar(select(func.count(Idea.id))) or 0
            return stats

    @app.get("/api/datasets")
    def datasets(modality: str = "", license_class: str = "", q: str = "",
                 page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100)) -> dict:
        with session_scope(engine) as session:
            stmt = select(Dataset).where(Dataset.status == "active")
            if modality:
                stmt = stmt.where(cast(Dataset.modalities, String).like(f'%"{modality}"%'))
            if license_class:
                stmt = stmt.where(Dataset.license_class == license_class)
            if q:
                pattern = f"%{q}%"
                stmt = stmt.where(or_(Dataset.name.like(pattern), Dataset.source_key.like(pattern),
                                      Dataset.card_text.like(pattern)))
            total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            rows = session.scalars(stmt.order_by(Dataset.downloads.desc(), Dataset.id)
                                   .limit(size).offset((page - 1) * size)).all()
            return {"total": total, "page": page, "size": size,
                    "items": [dataset_to_dict(row) for row in rows]}

    @app.get("/api/datasets/{dataset_id}")
    def dataset_detail(dataset_id: int) -> dict:
        with session_scope(engine) as session:
            dataset = session.get(Dataset, dataset_id)
            if dataset is None:
                raise _404("数据集", dataset_id)
            evidence = session.scalars(select(Evidence).where(Evidence.dataset_id == dataset_id)).all()
            enrichments = session.scalars(select(Enrichment).where(Enrichment.dataset_id == dataset_id)).all()
            versions = session.scalars(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id)).all()
            return {"dataset": dataset_to_dict(dataset),
                    "evidence": [{"id": e.id, "kind": e.kind, "location": e.location, "excerpt": e.excerpt} for e in evidence],
                    "enrichments": [{"id": en.id, "kind": en.kind, "producer": en.producer, "status": en.status,
                                     "value": en.value, "evidence_ids": en.evidence_ids} for en in enrichments],
                    "versions": [{"id": v.id, "revision": v.revision} for v in versions]}

    @app.get("/api/review/enrichments")
    def enrichment_queue(status: str = "candidate", kind: str = "") -> dict:
        with session_scope(engine) as session:
            stmt = select(Enrichment, Dataset.source_key).join(Dataset, Dataset.id == Enrichment.dataset_id)
            if status:
                stmt = stmt.where(Enrichment.status == status)
            if kind:
                stmt = stmt.where(Enrichment.kind == kind)
            rows = session.execute(stmt.order_by(Enrichment.id.desc()).limit(200)).all()
            return {"items": [{"id": en.id, "dataset": key, "dataset_id": en.dataset_id, "kind": en.kind,
                               "producer": en.producer, "status": en.status, "value": en.value,
                               "evidence_ids": en.evidence_ids} for en, key in rows]}

    @app.post("/api/review/enrichments/{enrichment_id}")
    def review_enrichment(enrichment_id: int, body: ReviewIn) -> dict:
        with session_scope(engine) as session:
            enrichment = session.get(Enrichment, enrichment_id)
            if enrichment is None:
                raise _404("整理候选", enrichment_id)
            if enrichment.status == body.decision:
                raise HTTPException(status_code=400, detail=f"该候选已是 {body.decision} 状态")
            enrichment.status = body.decision
            _write_review(session, "enrichment", enrichment_id, body)
            return {"id": enrichment.id, "status": enrichment.status}

    @app.get("/api/pairings")
    def pairings(status: str = "") -> dict:
        with session_scope(engine) as session:
            stmt = (select(Pairing, Dataset.source_key, Idea.external_id)
                    .join(DatasetVersion, DatasetVersion.id == Pairing.dataset_version_id)
                    .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
                    .join(Idea, Idea.id == Pairing.idea_id))
            if status:
                stmt = stmt.where(Pairing.status == status)
            rows = session.execute(stmt.order_by(Pairing.id.desc()).limit(200)).all()
            return {"items": [{"id": p.id, "idea": ext, "dataset": key, "dataset_version_id": p.dataset_version_id,
                               "status": p.status, "rationale": p.rationale, "warnings": p.warnings,
                               "proposed_by": p.proposed_by, "evidence_ids": p.evidence_ids} for p, key, ext in rows]}

    @app.post("/api/pairings/{pairing_id}/review")
    def review_pairing_endpoint(pairing_id: int, body: ReviewIn) -> dict:
        with session_scope(engine) as session:
            try:
                pairing = review_pairing(session, pairing_id, body.decision, body.note)
            except PairingError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            _write_review(session, "pairing", pairing_id, body)
            return {"id": pairing.id, "status": pairing.status}

    @app.get("/api/ideas")
    def ideas() -> dict:
        with session_scope(engine) as session:
            items = session.scalars(select(Idea).order_by(Idea.id.desc())).all()
            return {"items": [{"id": idea.id, "external_id": idea.external_id, "title": idea.title,
                               "statement": idea.statement, "requirements": idea.requirements,
                               "source_refs": idea.source_refs,
                               "status": snapshot_status(session, idea.external_id)} for idea in items]}

    @app.get("/api/ideas/{external_id}/status")
    def idea_status(external_id: str) -> dict:
        """智能体轮询入口：思路卡下的配对与快照状态（含驳回原因位置）。"""
        with session_scope(engine) as session:
            return snapshot_status(session, external_id)

    @app.post("/api/match")
    def match(body: MatchIn) -> dict:
        """智能体检索：requirements 结构化硬过滤 + query 语义排序（嵌入模型未启用时按下载量）。"""
        with session_scope(engine) as session:
            return match_datasets(session, requirement_to_filter(body.requirements),
                                  query_text=body.query, embedder=service_embedder())

    @app.post("/api/pairings", status_code=201)
    def propose(body: ProposeIn) -> dict:
        """智能体提交配对提案：只落 pending，人工核验在策展 UI 完成。"""
        with session_scope(engine) as session:
            try:
                result = propose_pairing(session, body.idea, body.dataset_version_ids, body.rationale,
                                         proposed_by="agent")
            except PairingError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            rejected = f"，{len(result['errors'])} 条版本引用被拒" if result["errors"] else ""
            session.add(Audit(actor="agent", action="pairing_propose",
                              detail=f"思路 {result['external_id']}：新增 {len(result['created'])} 条待核验配对{rejected}"))
            return result

    @app.post("/api/snapshots/freeze")
    def freeze(body: FreezeIn) -> dict:
        with session_scope(engine) as session:
            try:
                snapshot = freeze_snapshot(session, body.idea_external_id, body.pairing_ids, note=body.note)
            except PairingError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            session.add(Audit(actor=body.role, action="snapshot_freeze",
                              detail=f"快照 #{snapshot.id} V{snapshot.version} / 思路 {body.idea_external_id}"))
            return {"id": snapshot.id, "version": snapshot.version, "status": snapshot.status}

    @app.post("/api/snapshots/{snapshot_id}/approve")
    def approve(snapshot_id: int, body: ApproveIn | None = None) -> dict:
        with session_scope(engine) as session:
            try:
                snapshot = approve_snapshot(session, snapshot_id)
            except PairingError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            session.add(Audit(actor=(body.role if body else "lead"), action="snapshot_approve",
                              detail=f"快照 #{snapshot.id} V{snapshot.version} 批准"))
            return {"id": snapshot.id, "status": snapshot.status, "approved_at": snapshot.approved_at.isoformat()}

    @app.get("/api/snapshots/{snapshot_id}/export")
    def export(snapshot_id: int) -> dict:
        with session_scope(engine) as session:
            try:
                return export_snapshot(session, snapshot_id)
            except PairingError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/runs")
    def runs() -> dict:
        with session_scope(engine) as session:
            items = session.scalars(select(Run).order_by(Run.id.desc()).limit(100)).all()
            return {"items": [{"id": r.id, "kind": r.kind, "adapter": r.adapter, "status": r.status,
                               "stats": r.stats, "error_kind": r.error_kind,
                               "started_at": r.started_at.isoformat() if r.started_at else None,
                               "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in items]}

    @app.get("/api/audit")
    def audit() -> dict:
        with session_scope(engine) as session:
            items = session.scalars(select(Audit).order_by(Audit.id.desc()).limit(200)).all()
            return {"items": [{"id": a.id, "actor": a.actor, "action": a.action, "detail": a.detail,
                               "created_at": a.created_at.isoformat() if a.created_at else None} for a in items]}

    return app
