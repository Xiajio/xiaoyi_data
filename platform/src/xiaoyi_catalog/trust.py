"""Immutable generated revisions and content-bound human review."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .models import Audit, DatasetVersion, Enrichment, Idea, Pairing, Review


def content_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def enrichment_content(row: Enrichment) -> dict:
    return {"dataset_id": row.dataset_id, "dataset_version_id": row.dataset_version_id,
            "kind": row.kind, "producer": row.producer or "", "value": deepcopy(row.value or {}),
            "confidence": row.confidence or "", "evidence_ids": list(row.evidence_ids or []),
            "parent_enrichment_id": row.parent_enrichment_id}


def seal_enrichment(row: Enrichment) -> None:
    if (row.status or "candidate") != "generating":
        row.content_hash = content_hash(enrichment_content(row))


def enrichment_identity(row: Enrichment) -> dict:
    observed_hash = row.content_hash or (content_hash(enrichment_content(row)) if row.status != "generating" else "")
    return {"content_hash": observed_hash, "parent_enrichment_id": row.parent_enrichment_id,
            "history_status": row.history_status}


def review_content(session, kind: str, target_id: int) -> dict:
    if kind == "enrichment":
        row = session.get(Enrichment, target_id)
        if row is None or row.status == "generating":
            raise ValueError("不能核验不存在或仍在生成的产物")
        return enrichment_content(row)
    if kind == "pairing":
        row = session.get(Pairing, target_id)
        if row is None:
            raise ValueError("配对不存在")
        idea = session.get(Idea, row.idea_id)
        return {"idea_id": row.idea_id, "requirements": deepcopy(idea.requirements),
                "dataset_version_id": row.dataset_version_id, "rationale": row.rationale,
                "warnings": list(row.warnings), "evidence_ids": list(row.evidence_ids)}
    raise ValueError("不支持的审核目标")


def bind_review(session, review: Review) -> None:
    snapshot = review_content(session, review.target_kind, review.target_id)
    digest = content_hash(snapshot)
    if review.target_content_hash and review.target_content_hash != digest:
        raise ValueError("审核内容已变化，请刷新后重新核验")
    review.target_snapshot, review.target_content_hash = snapshot, digest
    review.history_status = "native"


def review_enrichment(session, ident: int, decision: str, *, actor: str, role: str,
                      note: str = "", value: dict | None = None, expected_hash: str | None = None) -> Enrichment:
    row = session.get(Enrichment, ident, with_for_update=True)
    if row is None:
        raise ValueError("整理候选不存在")
    if row.status in {"generating", "failed", "superseded"}:
        raise ValueError("该产物尚未完成或已被取代，不能核验")
    if decision not in {"confirmed", "rejected", "needs-info", "corrected"}:
        raise ValueError("无效的审核决定")
    if row.status == decision:
        raise ValueError(f"该候选已是 {decision} 状态")
    if decision in {"corrected", "rejected", "needs-info"} and not note.strip():
        raise ValueError("更正、待补资料或驳回必须填写说明")
    before = enrichment_content(row)
    digest = content_hash(before)
    if expected_hash is not None and expected_hash != digest:
        raise ValueError("审核内容已变化，请刷新后重新核验")
    result = row
    if decision == "corrected":
        if not isinstance(value, dict):
            raise ValueError("更正需要提供结构化值")
        result = Enrichment(dataset_id=row.dataset_id, dataset_version_id=row.dataset_version_id,
                            kind=row.kind, producer=row.producer, value=deepcopy(value),
                            confidence=row.confidence, evidence_ids=list(row.evidence_ids),
                            status="confirmed", parent_enrichment_id=row.id)
        session.add(result)
        session.flush()
        row.status = "superseded"
    else:
        row.status = decision
    session.add(Review(target_kind="enrichment", target_id=row.id, decision=decision,
                       note=note, role=role, target_content_hash=digest, target_snapshot=before,
                       resulting_enrichment_id=result.id if result is not row else None))
    session.add(Audit(actor=actor, action="enrichment_review",
                      detail=f"enrichment #{row.id} → {decision}；{note}；sha256={digest}"))
    return result
