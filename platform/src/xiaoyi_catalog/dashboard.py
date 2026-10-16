"""Read-only homepage aggregates. Counts retain their distinct business units."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta, timezone

from fastapi import HTTPException, Query
from sqlalchemy import ARRAY, JSON, String, Text, case, cast, exists, func, literal, or_, select

from .db import session_scope
from .models import Dataset, DatasetVersion, Enrichment, Evidence, Pairing, Snapshot, utcnow
from .models_assets import Asset, DownloadRequest, ResearchBundle
from .models_collection import CollectionJob, CollectionSchedule
from .collection_runtime import job_to_dict

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
MODALITY_ORDER = ("text", "table", "image", "audio", "video", "document", "3d", "unknown")
LICENSE_ORDER = ("permissive", "copyleft", "research-only", "unknown")
REVIEW_ORDER = ("candidate", "needs-info", "confirmed", "corrected", "rejected")
JOB_STATUSES = ("queued", "running", "completed", "partial", "failed", "cancelled")


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _iso(value):
    return _aware(value).astimezone(timezone.utc).isoformat() if value else None


def normalized_modalities(values):
    if not isinstance(values, list):
        return {"unknown"}
    return {value.strip().lower() for value in values if isinstance(value, str) and value.strip()} or {"unknown"}


def modality_filter(column, key, dialect_name):
    """JSON-array membership, including empty/missing labels as unknown, on both databases."""
    key = key.strip().lower()
    if dialect_name == "postgresql":
        safe_array = case((func.json_typeof(column) == "array", column), else_=cast(literal("[]"), JSON))
        entries = func.json_array_elements(safe_array).table_valued("value").render_derived(name="modality_values")
        text_value = func.lower(func.trim(entries.c.value.op("#>>")(cast(literal("{}"), ARRAY(Text)))))
        valid = (func.json_typeof(entries.c.value) == "string") & (text_value != "")
    else:
        safe_array = case((func.json_type(column) == "array", column), else_=literal("[]"))
        entries = func.json_each(safe_array).table_valued("value", "type").alias("modality_values")
        text_value = func.lower(func.trim(cast(entries.c.value, String)))
        valid = (entries.c.type == "text") & (text_value != "")
    match = exists(select(literal(1)).select_from(entries).where(valid, text_value == key))
    if key == "unknown":
        return or_(match, ~exists(select(literal(1)).select_from(entries).where(valid)))
    return match


def license_filter(column, key):
    normalized = func.coalesce(func.nullif(func.trim(column), ""), "unknown")
    return normalized == (key.strip() or "unknown")


def _ordered(counter, preferred, name="key", *, include_zero=True):
    keys = list(preferred) + sorted(set(counter) - set(preferred))
    return [{name: key, "count": int(counter.get(key, 0))} for key in keys if include_zero or counter.get(key)]


def dashboard_stats(session, *, days=14, now=None):
    if days not in {14, 30}:
        raise ValueError("趋势仅支持 14 或 30 天")
    now = _aware(now or utcnow())
    today = now.astimezone(SHANGHAI).date()
    start_date = today - timedelta(days=days - 1)
    start_utc = datetime.combine(start_date, time.min, SHANGHAI).astimezone(timezone.utc)
    active_ids = select(Dataset.id).where(Dataset.status == "active")
    active_versions = select(DatasetVersion.id).where(DatasetVersion.dataset_id.in_(active_ids))
    def count(model, *conditions):
        return int(session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0)

    rows = session.execute(select(Dataset.modalities, Dataset.license_class).where(Dataset.status == "active")).all()
    modalities, licenses = Counter(), Counter()
    for labels, license_class in rows:
        modalities.update(normalized_modalities(labels))
        licenses[(license_class or "unknown").strip() or "unknown"] += 1
    review = dict(session.execute(select(Enrichment.status, func.count()).where(
        Enrichment.dataset_id.in_(active_ids), Enrichment.status != "superseded").group_by(Enrichment.status)).all())
    totals = {
        "datasets": len(rows),
        "versions": count(DatasetVersion, DatasetVersion.dataset_id.in_(active_ids)),
        "evidence": count(Evidence, Evidence.dataset_id.in_(active_ids)),
        "enrichment_pending": sum(review.get(status, 0) for status in ("candidate", "needs-info")),
        "pairings_pending": count(Pairing, Pairing.dataset_version_id.in_(active_versions), Pairing.status.in_(["pending", "needs-info"])),
        "downloads_pending": count(DownloadRequest, DownloadRequest.dataset_version_id.in_(active_versions), DownloadRequest.status == "pending"),
        "assets": count(Asset, Asset.dataset_version_id.in_(active_versions)),
        "asset_bytes": int(session.scalar(select(func.coalesce(func.sum(Asset.size_bytes), 0)).where(Asset.dataset_version_id.in_(active_versions))) or 0),
        "snapshots_approved": count(Snapshot, Snapshot.status == "approved"),
        "bundles": count(ResearchBundle),
    }
    points = {start_date + timedelta(days=offset): {"date": (start_date + timedelta(days=offset)).isoformat(), "datasets": 0, "versions": 0}
        for offset in range(days)}
    first_seen = func.coalesce(Dataset.first_seen_at, Dataset.created_at)
    dataset_dates = session.scalars(select(first_seen).where(Dataset.status == "active", first_seen >= start_utc, first_seen <= now))
    version_dates = session.scalars(select(DatasetVersion.created_at).where(
        DatasetVersion.dataset_id.in_(active_ids), DatasetVersion.created_at >= start_utc, DatasetVersion.created_at <= now))
    for kind, values in (("datasets", dataset_dates), ("versions", version_dates)):
        for value in values:
            local_day = _aware(value).astimezone(SHANGHAI).date()
            if local_day in points:
                points[local_day][kind] += 1
    job_counts = dict(session.execute(select(CollectionJob.status, func.count()).group_by(CollectionJob.status)).all())
    job = session.scalar(select(CollectionJob).order_by(CollectionJob.created_at.desc(), CollectionJob.id.desc()).limit(1))
    latest = None
    if job:
        serialized = job_to_dict(job)
        latest = {key: serialized[key] for key in ("id", "status", "stage", "trigger", "created_at", "started_at", "finished_at", "stats", "current_source_key", "error")}
    schedule = session.scalar(select(CollectionSchedule).where(CollectionSchedule.enabled.is_(True))
        .order_by(CollectionSchedule.next_run_at, CollectionSchedule.id).limit(1))
    return {
        "generated_at": _iso(now), "timezone": "Asia/Shanghai", "totals": totals,
        "modalities": _ordered(modalities, MODALITY_ORDER), "licenses": _ordered(licenses, LICENSE_ORDER),
        "review": _ordered(review, REVIEW_ORDER, "status"),
        "trend": {"days": days, "start_date": start_date.isoformat(), "end_date": today.isoformat(), "points": list(points.values())},
        "collection": {"jobs_by_status": {key: int(job_counts.get(key, 0)) for key in JOB_STATUSES},
            "latest_job": latest, "enabled_schedules": count(CollectionSchedule, CollectionSchedule.enabled.is_(True)),
            "next_run_at": _iso(schedule.next_run_at) if schedule else None,
            "next_schedule_name": schedule.name if schedule else None},
        "definitions": {
            "catalog_scope": "目录、版本、证据、整理、配对、下载申请及资产均关联当前 active 数据集。",
            "versions": "版本数含历史来源版本和派生版本；证据与整理也含历史版本关联记录。",
            "modalities": "按数据集对模态标签去重；多模态分别计入，各项合计可能大于数据集数；空标签记 unknown。",
            "review": "整理结果的当前状态计数，排除 superseded；待核验包括 candidate 和 needs-info。",
            "pending": "待处理配对包括 pending 和 needs-info；待审批下载仅 pending。",
            "trend": "上海自然日，含尚未结束的今天；datasets 按 first_seen_at 首次入库，versions 按 created_at 版本入库，均为期间新增而非累计。",
            "asset_bytes": "按版本登记文件的逻辑字节数，可能包含复用和部分获取的文件，不代表物理磁盘占用。",
            "delivery": "approved 快照与按需生成研究包为团队历史交付计数，不因目录软删除而消失。",
            "collection": "全团队持久采集任务和计划，不与旧 Run 执行记录相加；重试是独立任务。",
        },
    }


def register_dashboard(app, engine):
    @app.get("/api/overview/dashboard")
    def dashboard(days: int = Query(default=14)):
        if days not in {14, 30}:
            raise HTTPException(422, "趋势仅支持 14 或 30 天")
        with session_scope(engine) as session:
            return dashboard_stats(session, days=days)
