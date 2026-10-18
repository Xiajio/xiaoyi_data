"""思路 × 数据集配对与快照服务（M2）。

职责分离（对应设计文档第 6/7 节）：
- 智能体只能 upsert 思路卡、创建 pending 配对提案——不能自我确认；
- review_pairing / freeze_snapshot / approve_snapshot 是人工操作（M3 接入策展 UI，M2 经 CLI 行使）；
- 冻结时深拷贝全部引用内容；配对被驳回会使包含它的待审批快照失效。
"""

from __future__ import annotations

from copy import deepcopy

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .match import missing_for_requirement
from .task_spec import validate_task_spec, evaluate_task_spec
from .trust import content_hash, review_content, enrichment_identity
from .models import Dataset, DatasetVersion, Evidence, Enrichment, Review, Idea, Pairing, Snapshot, utcnow
from .search import version_to_dict

IDEA_REQUIRED = ("external_id", "title", "requirements")


class PairingError(ValueError):
    pass


def upsert_idea(session: Session, payload: dict) -> Idea:
    """按 external_id 幂等导入思路卡；重复导入更新陈述与需求，保留导入时间。"""
    missing = [key for key in IDEA_REQUIRED if key not in payload or (key != "requirements" and not payload.get(key))]
    if missing:
        raise PairingError(f"思路卡缺少必填字段：{', '.join(missing)}")
    if not isinstance(payload["requirements"], dict):
        raise PairingError("requirements 必须是对象")
    try:
        validate_task_spec(payload["requirements"])
    except ValueError as exc:
        raise PairingError(str(exc)) from exc
    idea = session.scalar(select(Idea).where(Idea.external_id == payload["external_id"]))
    if idea is None:
        idea = Idea(external_id=payload["external_id"], title=str(payload["title"]),
                    statement=str(payload.get("statement", "")), requirements=payload["requirements"],
                    source_refs=list(payload.get("source_refs") or []))
        session.add(idea)
        session.flush()
    else:
        if idea.requirements != payload["requirements"]:
            for pending in session.scalars(select(Snapshot).where(Snapshot.idea_id == idea.id, Snapshot.status == "pending")):
                pending.status = "invalidated"
                pending.note = (pending.note + "；" if pending.note else "") + "科研硬约束已变更"
        idea.title = str(payload["title"])
        idea.statement = str(payload.get("statement", ""))
        idea.requirements = payload["requirements"]
        idea.source_refs = list(payload.get("source_refs") or [])
    return idea


def _latest_card_evidence_id(session: Session, dataset_version_id: int) -> list[int]:
    return list(session.scalars(select(Evidence.id).where(Evidence.dataset_version_id == dataset_version_id)
                               .order_by(Evidence.id)))


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
        warnings = missing_for_requirement(version_to_dict(session, version), idea.requirements)
        pairing = Pairing(idea_id=idea.id, dataset_version_id=version_id, rationale=rationale,
                          proposed_by=proposed_by, warnings=warnings,
                          evidence_ids=_latest_card_evidence_id(session, version.id))
        session.add(pairing)
        session.flush()
        created.append({"id": pairing.id, "dataset": dataset.source_key, "status": "pending", "warnings": warnings})
    if errors and not created and not existing:
        raise PairingError("；".join(errors))
    return {"idea_id": idea.id, "external_id": idea.external_id, "created": created, "existing": existing,
            "errors": errors, "note": "提案为候选状态，需人工核验确认"}


def review_pairing(session: Session, pairing_id: int, decision: str, note: str = "", *, actor: str = "reviewer", role: str = "reviewer") -> Pairing:
    """人工核验配对：confirmed / rejected。驳回会使包含该配对的待审批快照失效。"""
    if decision not in ("confirmed", "rejected", "needs-info"):
        raise PairingError("配对核验只支持 confirmed / rejected / needs-info")
    pairing = session.get(Pairing, pairing_id)
    if pairing is None:
        raise PairingError(f"配对 {pairing_id} 不存在")
    if pairing.status == decision and (decision != "confirmed" or _current_pairing_review(session, pairing)):
        raise PairingError(f"配对 {pairing_id} 已是 {decision} 状态")
    if decision == "confirmed":
        version = session.get(DatasetVersion, pairing.dataset_version_id)
        if version and version.provenance_status == "verified":
            # The explicit human confirmation may replace unbound legacy pointers with newly recovered evidence.
            existing = [session.get(Evidence, eid) for eid in pairing.evidence_ids]
            if any(e is None or e.dataset_version_id != version.id for e in existing):
                pairing.evidence_ids = _latest_card_evidence_id(session, version.id)
        _validate_pairing(session, pairing)
    pairing.status = decision
    from .models import Audit
    session.add(Review(target_kind="pairing", target_id=pairing.id, decision=decision, note=note, role=role))
    session.add(Audit(actor=actor, action="pairing_review", detail=f"pairing #{pairing.id} → {decision}；{note}"))
    session.flush()
    if decision != "confirmed":
        for snapshot in session.scalars(select(Snapshot).where(Snapshot.idea_id == pairing.idea_id, Snapshot.status == "pending")):
            if pairing.id in snapshot.pairing_ids:
                snapshot.status = "invalidated"
                snapshot.note = (snapshot.note + "；" if snapshot.note else "") + f"配对 {pairing.id} 改为 {decision}"
    return pairing


def _current_pairing_review(session: Session, pairing: Pairing) -> bool:
    latest = session.scalar(select(Review).where(Review.target_kind == "pairing",
        Review.target_id == pairing.id).order_by(Review.id.desc()).limit(1))
    return bool(latest and latest.decision == "confirmed" and latest.history_status == "native"
                and latest.target_content_hash == content_hash(review_content(session, "pairing", pairing.id)))


def _validate_pairing(session: Session, pairing: Pairing, *, require_review: bool = False) -> None:
    version = session.get(DatasetVersion, pairing.dataset_version_id)
    idea = session.get(Idea, pairing.idea_id)
    if version is None or version.provenance_status != "verified" or not version.metadata_json:
        from .provenance import RECOVERY_HELP
        raise PairingError(RECOVERY_HELP)
    gaps = missing_for_requirement(version_to_dict(session, version), idea.requirements)
    if gaps:
        raise PairingError("科研硬约束不满足：" + "；".join(gaps))
    for evidence_id in pairing.evidence_ids:
        evidence = session.get(Evidence, evidence_id)
        if evidence is None or evidence.dataset_version_id != version.id:
            raise PairingError("证据未绑定所选数据集版本")
    if require_review and not _current_pairing_review(session, pairing):
        raise PairingError("配对审核未绑定当前内容或为历史未验证记录，请重新人工核验")


def _approved_asset(session, asset, _visited=None) -> bool:
    from .downloads import digest_json
    from .models_assets import Asset, DownloadRequest
    visited = set(_visited or ())
    if asset.id in visited:
        return False
    visited.add(asset.id)
    request = session.get(DownloadRequest, asset.download_request_id)
    if request is None or request.status != "completed" or not request.decided_by:
        return False
    if request.approved_hash != request.manifest_hash or request.manifest_hash != digest_json(request.manifest):
        return False
    if asset.origin == "derived":
        inputs = asset.recipe.get("inputs", [])
        version = session.get(DatasetVersion, asset.dataset_version_id)
        if not inputs or sorted(i.get("asset_id") for i in inputs) != sorted(asset.parent_asset_ids):
            return False
        if version is None or version.kind != "derived" or version.parent_version_id != inputs[0].get("dataset_version_id"):
            return False
        for item in inputs:
            parent = session.get(Asset, item.get("asset_id"))
            if parent is None or parent.sha256 != item.get("sha256") or parent.dataset_version_id != item.get("dataset_version_id"):
                return False
            if not _approved_asset(session, parent, visited):
                return False
        return True
    if request.dataset_version_id != asset.dataset_version_id:
        return False
    return any(item.get("path") == asset.path and item.get("size") == asset.size_bytes
               and (not item.get("sha256") or item["sha256"] == asset.sha256)
               for item in request.manifest.get("files", []))


def _frozen_content(session: Session, idea: Idea, pairings: list[Pairing]) -> dict:
    from .downloads import asset_to_dict
    from .models_assets import Asset, QualityReport
    from .profiling import public_report
    entries = []
    version_ids = [p.dataset_version_id for p in pairings]
    for pairing in pairings:
        version = session.get(DatasetVersion, pairing.dataset_version_id)
        entries.append({
            "pairing_id": pairing.id, "dataset": version_to_dict(session, version),
            "dataset_version_id": version.id, "provenance_status": version.provenance_status,
            "revision": version.revision, "rationale": pairing.rationale,
            "warnings": list(pairing.warnings), "evidence_ids": list(pairing.evidence_ids),
            "evidence": [{"id": e.id, "kind": e.kind, "excerpt": e.excerpt,
                          "location": e.location, "dataset_version_id": e.dataset_version_id}
                         for e in session.scalars(select(Evidence).where(Evidence.dataset_version_id == version.id))],
        })
    assets = [a for a in session.scalars(select(Asset).where(Asset.dataset_version_id.in_(version_ids)).order_by(Asset.id))
              if _approved_asset(session, a)]
    asset_ids = [a.id for a in assets]
    reports = session.scalars(select(QualityReport).where(QualityReport.asset_id.in_(asset_ids))).all()
    enrichments = session.scalars(select(Enrichment).where(Enrichment.dataset_version_id.in_(version_ids),
                                                        Enrichment.status == "confirmed")).all()
    pairing_ids, enrichment_ids = [p.id for p in pairings], [e.id for e in enrichments]
    reviews = session.scalars(select(Review).where(
        ((Review.target_kind == "pairing") & Review.target_id.in_(pairing_ids)) |
        ((Review.target_kind == "enrichment") & (Review.target_id.in_(enrichment_ids) |
            Review.resulting_enrichment_id.in_(enrichment_ids)))).order_by(Review.id)).all()
    return deepcopy({"idea": {"external_id": idea.external_id, "title": idea.title, "statement": idea.statement,
                     "requirements": idea.requirements, "source_refs": idea.source_refs},
            "datasets": entries,
            "assets": [{**asset_to_dict(a), "download_request_id": a.download_request_id} for a in assets],
            "reports": [{"asset_id": r.asset_id, "producer": r.producer, "value": public_report(r.value)} for r in reports],
            "enrichments": [{"id": e.id, "dataset_version_id": e.dataset_version_id, "kind": e.kind,
                             "value": e.value, "producer": e.producer, "evidence_ids": e.evidence_ids, **enrichment_identity(e)} for e in enrichments],
            "reviews": [{"id": r.id, "target_kind": r.target_kind, "target_id": r.target_id,
                         "decision": r.decision, "note": r.note, "role": r.role, "target_content_hash": r.target_content_hash,
                         "target_snapshot": r.target_snapshot, "history_status": r.history_status,
                         "resulting_enrichment_id": r.resulting_enrichment_id} for r in reviews]})


def _validate_frozen_assets(session, snapshot) -> None:
    from .downloads import storage_root, sha256_file
    from .models_assets import Asset
    root = storage_root()
    for frozen in snapshot.content.get("assets", []):
        asset = session.get(Asset, frozen["id"])
        if asset is None or not _approved_asset(session, asset):
            raise PairingError("冻结文件不再来自完整批准的获取请求")
        if any(getattr(asset, key) != frozen[key] for key in ("dataset_version_id", "path", "sha256", "size_bytes", "download_request_id")):
            raise PairingError("冻结文件清单已变化")
        path = (root / asset.storage_key).resolve()
        if not path.is_relative_to(root) or not path.is_file() or sha256_file(path) != frozen["sha256"]:
            raise PairingError("冻结文件缺失或校验失败")


def freeze_snapshot(session: Session, idea_external_id: str, pairing_ids: list[int] | None = None, *, note: str = "") -> Snapshot:
    """冻结指定思路下已确认配对为快照；默认纳入全部已确认配对。"""
    idea = session.scalar(select(Idea).where(Idea.external_id == idea_external_id))
    if idea is None:
        raise PairingError(f"思路 {idea_external_id} 不存在")
    pairings = session.scalars(select(Pairing).where(Pairing.idea_id == idea.id)).all()
    chosen = [p for p in pairings if p.status == "confirmed"] if pairing_ids is None else [
        session.get(Pairing, pid) for pid in pairing_ids
    ]
    if any(p is None for p in chosen):
        raise PairingError("指定配对不存在")
    chosen = list({p.id: p for p in chosen}.values())
    if not chosen:
        raise PairingError("没有已确认的配对可冻结")
    unconfirmed = [p.id for p in chosen if p.status != "confirmed"]
    if unconfirmed:
        raise PairingError(f"配对 {unconfirmed} 未确认，不能冻结")
    if any(p.idea_id != idea.id for p in chosen):
        raise PairingError("配对不属于该思路")
    for pairing in chosen:
        _validate_pairing(session, pairing, require_review=True)
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
    idea = session.get(Idea, snapshot.idea_id)
    if snapshot.content.get("idea", {}).get("requirements") != idea.requirements:
        raise PairingError("思路硬约束已变更，必须重新冻结快照")
    for pairing in pairings:
        _validate_pairing(session, pairing, require_review=True)
    _validate_frozen_assets(session, snapshot)
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
                       "bundle_available": bool(s.content.get("assets")) and
                           {a.get("dataset_version_id") for a in s.content.get("assets", [])} ==
                           {d.get("dataset_version_id") for d in s.content.get("datasets", [])},
                       "approved_at": s.approved_at.isoformat() if s.approved_at else None} for s in snapshots],
    }


def export_snapshot(session: Session, snapshot_id: int) -> dict:
    """导出已批准快照的冻结内容，附许可清单与合成/来源声明。"""
    snapshot = session.get(Snapshot, snapshot_id)
    if snapshot is None:
        raise PairingError(f"快照 {snapshot_id} 不存在")
    if snapshot.status != "approved":
        raise PairingError("只能导出已批准的快照")
    frozen_requirements = snapshot.content.get("idea", {}).get("requirements", {})
    for entry in snapshot.content.get("datasets", []):
        gaps = missing_for_requirement(entry.get("dataset", {}), frozen_requirements)
        if gaps:
            raise PairingError("冻结任务约束不可验证，不能发布：" + "；".join(gaps))
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
