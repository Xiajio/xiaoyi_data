"""思路 × 数据集配对与快照服务（M2）。

职责分离（对应设计文档第 6/7 节）：
- 智能体只能 upsert 思路卡、创建 pending 配对提案——不能自我确认；
- review_pairing / freeze_snapshot / approve_snapshot 是人工操作（M3 接入策展 UI，M2 经 CLI 行使）；
- 冻结时深拷贝全部引用内容；配对被驳回会使包含它的待审批快照失效。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .match import missing_for_requirement
from .models import Dataset, DatasetVersion, Evidence, Idea, Pairing, Snapshot, utcnow
from .search import dataset_to_dict

IDEA_REQUIRED = ("external_id", "title", "requirements")


class PairingError(ValueError):
    pass


def upsert_idea(session: Session, payload: dict) -> Idea:
    """按 external_id 幂等导入思路卡；重复导入更新陈述与需求，保留导入时间。"""
    missing = [key for key in IDEA_REQUIRED if not payload.get(key)]
    if missing:
        raise PairingError(f"思路卡缺少必填字段：{', '.join(missing)}")
    if not isinstance(payload["requirements"], dict):
        raise PairingError("requirements 必须是对象")
    idea = session.scalar(select(Idea).where(Idea.external_id == payload["external_id"]))
    if idea is None:
        idea = Idea(external_id=payload["external_id"], title=str(payload["title"]),
                    statement=str(payload.get("statement", "")), requirements=payload["requirements"],
                    source_refs=list(payload.get("source_refs") or []))
        session.add(idea)
        session.flush()
    else:
        idea.title = str(payload["title"])
        idea.statement = str(payload.get("statement", ""))
        idea.requirements = payload["requirements"]
        idea.source_refs = list(payload.get("source_refs") or [])
    return idea


def _latest_card_evidence_id(session: Session, dataset_id: int) -> list[int]:
    row = session.scalar(
        select(Evidence.id).where(Evidence.dataset_id == dataset_id, Evidence.kind == "card").order_by(Evidence.id.desc())
    )
    return [row] if row is not None else []


def propose_pairing(session: Session, idea_payload: dict, dataset_version_ids: list[int], rationale: str,
                    *, proposed_by: str = "agent") -> dict:
    """创建 pending 配对提案；重复提案幂等返回已有记录。缺口提示写入 warnings。"""
    if not rationale.strip():
        raise PairingError("提案必须附带理由（rationale）")
    idea = upsert_idea(session, idea_payload)
    if not dataset_version_ids:
        raise PairingError("至少选择一个数据集版本")
    created: list[dict] = []
    existing: list[dict] = []
    errors: list[str] = []
    for version_id in dict.fromkeys(dataset_version_ids):
        version = session.get(DatasetVersion, version_id)
        if version is None:
            errors.append(f"数据集版本 {version_id} 不存在")
            continue
        dataset = session.get(Dataset, version.dataset_id)
        prior = session.scalar(
            select(Pairing).where(Pairing.idea_id == idea.id, Pairing.dataset_version_id == version_id)
        )
        if prior is not None:
            existing.append({"id": prior.id, "dataset": dataset.source_key, "status": prior.status})
            continue
        warnings = missing_for_requirement(dataset_to_dict(dataset), idea.requirements)
        pairing = Pairing(idea_id=idea.id, dataset_version_id=version_id, rationale=rationale,
                          proposed_by=proposed_by, warnings=warnings,
                          evidence_ids=_latest_card_evidence_id(session, dataset.id))
        session.add(pairing)
        session.flush()
        created.append({"id": pairing.id, "dataset": dataset.source_key, "status": "pending", "warnings": warnings})
    if errors and not created and not existing:
        raise PairingError("；".join(errors))
    return {"idea_id": idea.id, "external_id": idea.external_id, "created": created, "existing": existing,
            "errors": errors, "note": "提案为候选状态，需人工核验确认"}


def review_pairing(session: Session, pairing_id: int, decision: str, note: str = "") -> Pairing:
    """人工核验配对：confirmed / rejected。驳回会使包含该配对的待审批快照失效。"""
    if decision not in ("confirmed", "rejected"):
        raise PairingError("配对核验只支持 confirmed / rejected")
    pairing = session.get(Pairing, pairing_id)
    if pairing is None:
        raise PairingError(f"配对 {pairing_id} 不存在")
    if pairing.status == decision:
        raise PairingError(f"配对 {pairing_id} 已是 {decision} 状态")
    pairing.status = decision
    if decision == "rejected":
        for snapshot in session.scalars(select(Snapshot).where(Snapshot.idea_id == pairing.idea_id, Snapshot.status == "pending")):
            if pairing.id in snapshot.pairing_ids:
                snapshot.status = "invalidated"
                snapshot.note = (snapshot.note + "；" if snapshot.note else "") + f"配对 {pairing.id} 被驳回"
    return pairing


def _frozen_content(session: Session, idea: Idea, pairings: list[Pairing]) -> dict:
    entries = []
    for pairing in pairings:
        version = session.get(DatasetVersion, pairing.dataset_version_id)
        dataset = session.get(Dataset, version.dataset_id)
        entries.append({
            "pairing_id": pairing.id,
            "dataset": dataset_to_dict(dataset),
            "revision": version.revision,
            "rationale": pairing.rationale,
            "warnings": pairing.warnings,
            "evidence_ids": pairing.evidence_ids,
        })
    return {"idea": {"external_id": idea.external_id, "title": idea.title, "statement": idea.statement,
                     "requirements": idea.requirements, "source_refs": idea.source_refs},
            "datasets": entries}


def freeze_snapshot(session: Session, idea_external_id: str, pairing_ids: list[int] | None = None, *, note: str = "") -> Snapshot:
    """冻结指定思路下已确认配对为快照；默认纳入全部已确认配对。"""
    idea = session.scalar(select(Idea).where(Idea.external_id == idea_external_id))
    if idea is None:
        raise PairingError(f"思路 {idea_external_id} 不存在")
    pairings = session.scalars(select(Pairing).where(Pairing.idea_id == idea.id)).all()
    chosen = [p for p in pairings if p.status == "confirmed"] if pairing_ids is None else [
        session.get(Pairing, pid) for pid in pairing_ids
    ]
    chosen = [p for p in chosen if p is not None]
    if not chosen:
        raise PairingError("没有已确认的配对可冻结")
    unconfirmed = [p.id for p in chosen if p.status != "confirmed"]
    if unconfirmed:
        raise PairingError(f"配对 {unconfirmed} 未确认，不能冻结")
    if any(p.idea_id != idea.id for p in chosen):
        raise PairingError("配对不属于该思路")
    pending = session.scalar(select(Snapshot).where(Snapshot.idea_id == idea.id, Snapshot.status == "pending"))
    if pending is not None:
        raise PairingError(f"已有待审批快照 V{pending.version}，请先审批或使其失效")
    version = (session.scalar(select(func.max(Snapshot.version)).where(Snapshot.idea_id == idea.id)) or 0) + 1
    snapshot = Snapshot(idea_id=idea.id, version=version, pairing_ids=[p.id for p in chosen],
                        content=_frozen_content(session, idea, chosen), note=note)
    session.add(snapshot)
    session.flush()
    return snapshot


def approve_snapshot(session: Session, snapshot_id: int) -> Snapshot:
    snapshot = session.get(Snapshot, snapshot_id)
    if snapshot is None:
        raise PairingError(f"快照 {snapshot_id} 不存在")
    if snapshot.status != "pending":
        raise PairingError(f"快照当前状态为 {snapshot.status}，不能审批")
    pairings = [session.get(Pairing, pid) for pid in snapshot.pairing_ids]
    if any(p is None or p.status != "confirmed" for p in pairings):
        snapshot.status = "invalidated"
        raise PairingError("快照内配对已不再全部确认，快照已失效")
    snapshot.status = "approved"
    snapshot.approved_at = utcnow()
    return snapshot


def snapshot_status(session: Session, idea_external_id: str) -> dict:
    idea = session.scalar(select(Idea).where(Idea.external_id == idea_external_id))
    if idea is None:
        return {"idea": None, "pairings": [], "snapshots": []}
    pairings = session.scalars(select(Pairing).where(Pairing.idea_id == idea.id)).all()
    snapshots = session.scalars(select(Snapshot).where(Snapshot.idea_id == idea.id).order_by(Snapshot.version)).all()
    return {
        "idea": {"external_id": idea.external_id, "title": idea.title, "imported_at": idea.imported_at.isoformat()},
        "pairings": [{"id": p.id, "dataset_version_id": p.dataset_version_id, "status": p.status,
                      "warnings": p.warnings, "proposed_by": p.proposed_by} for p in pairings],
        "snapshots": [{"id": s.id, "version": s.version, "status": s.status,
                       "pairing_ids": s.pairing_ids,
                       "approved_at": s.approved_at.isoformat() if s.approved_at else None} for s in snapshots],
    }


def export_snapshot(session: Session, snapshot_id: int) -> dict:
    """导出已批准快照的冻结内容，附许可清单与合成/来源声明。"""
    snapshot = session.get(Snapshot, snapshot_id)
    if snapshot is None:
        raise PairingError(f"快照 {snapshot_id} 不存在")
    if snapshot.status != "approved":
        raise PairingError("只能导出已批准的快照")
    licenses = sorted({entry["dataset"]["license"] or "unknown" for entry in snapshot.content.get("datasets", [])})
    return {
        "type": "idea_dataset_snapshot",
        "snapshot_id": snapshot.id,
        "version": snapshot.version,
        "approved_at": snapshot.approved_at.isoformat() if snapshot.approved_at else None,
        "licenses": licenses,
        "disclaimer": "内容为冻结的思路 × 数据集组合；数据集权利以各自许可为准，使用前请核对 license 清单。",
        **snapshot.content,
    }
