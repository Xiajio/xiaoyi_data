"""采集编排：适配器结果 → 目录库。

不变量（对应设计文档与演示版语义）：
- fingerprint（kind:source_key:revision）唯一约束保证重复采集零重复记录；
- 同 source_key 不同 revision → 原地更新元数据并新增 dataset_version；
- 单项失败不中断批次，按 error_kind 分类计数；list() 失败则整个 run 记 failed；
- 成功后把最大 last_modified 写回 source.cursor，作为下次增量水位。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .adapters.base import CollectScope, RawDataset, SourceAdapter
from .db import session_scope
from .models import Dataset, DatasetVersion, Evidence, Run, Source, utcnow
from .normalize import classify_license, derive_modalities

CARD_EVIDENCE_EXCERPT = 1500


def fingerprint(kind: str, source_key: str, revision: str) -> str:
    return hashlib.sha256(f"{kind}:{source_key}:{revision}".encode("utf-8")).hexdigest()


def scope_key(scope: CollectScope) -> str:
    """水位按 scope 维度记录：不同过滤条件的结果集互不共享水位，limit 也参与键。"""
    payload = json.dumps({
        "search": scope.search,
        "task_categories": sorted(scope.task_categories),
        "languages": sorted(scope.languages),
        "author": scope.author,
        "limit": scope.limit,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def classify_error(exc: BaseException) -> str:
    """失败分类：rate_limit / permission / timeout / network / other。不静默吞错。"""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429:
        return "rate_limit"
    if status in (401, 403):
        return "permission"
    name = type(exc).__name__.lower()
    if "timeout" in name or "timedout" in name.replace("_", ""):
        return "timeout"
    if any(token in name for token in ("connect", "network", "ssl", "resolve", "unreachable")):
        return "network"
    return "other"


def _parse_watermark(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _upsert_dataset(session, source: Source, raw: RawDataset, fp: str) -> tuple[Dataset, bool]:
    """按指纹幂等落库。返回 (记录, 是否新建)。revision 变化时更新行并追加版本。"""
    existing = session.scalar(select(Dataset).where(Dataset.fingerprint == fp))
    if existing is not None:
        existing.last_seen_at = utcnow()
        return existing, False
    modalities, modality_source = derive_modalities(raw.tags, raw.task_categories)
    values = dict(
        name=raw.name,
        author=raw.author,
        url=raw.url,
        license=raw.license,
        license_class=classify_license(raw.license),
        modalities=modalities,
        modality_source=modality_source,
        task_categories=list(raw.task_categories),
        languages=list(raw.languages),
        size_bytes=raw.size_bytes,
        size_rows=raw.size_rows,
        downloads=raw.downloads,
        likes=raw.likes,
        card_text=raw.card_text,
        source_modified_at=raw.ref.last_modified,
        last_seen_at=utcnow(),
    )
    same_key = session.scalar(select(Dataset).where(Dataset.source_id == source.id, Dataset.source_key == raw.ref.source_key))
    if same_key is not None:
        for field_name, value in values.items():
            setattr(same_key, field_name, value)
        same_key.fingerprint = fp
        return same_key, False
    dataset = Dataset(source_id=source.id, source_key=raw.ref.source_key, fingerprint=fp, **values)
    session.add(dataset)
    session.flush()
    return dataset, True


def _append_version_and_evidence(session, dataset: Dataset, raw: RawDataset) -> None:
    revision_exists = session.scalar(
        select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset.id, DatasetVersion.revision == raw.ref.revision)
    )
    if revision_exists is None:
        session.add(DatasetVersion(dataset_id=dataset.id, revision=raw.ref.revision))
    if raw.card_text.strip():
        session.add(Evidence(dataset_id=dataset.id, kind="card", excerpt=raw.card_text[:CARD_EVIDENCE_EXCERPT], location="README.md"))


def collect(engine: Engine, adapter: SourceAdapter, scope: CollectScope, *, retry_of: int | None = None) -> Run:
    """执行一次采集并返回 run 记录；除键盘中断外不向调用方抛出来源异常。"""
    with session_scope(engine) as session:
        source = session.scalar(select(Source).where(Source.kind == adapter.kind, Source.endpoint == adapter.endpoint))
        if source is None:
            source = Source(kind=adapter.kind, endpoint=adapter.endpoint)
            session.add(source)
            session.flush()
        run = Run(kind="collect", adapter=adapter.kind, status="running", input_scope=asdict(scope), retry_of=retry_of)
        session.add(run)
        session.flush()

        key = scope_key(scope)
        watermark = _parse_watermark((source.cursor or {}).get(key, {}).get("last_modified"))
        stats: dict = {"found": 0, "new": 0, "updated": 0, "duplicates": 0, "skipped_stale": 0, "failed": 0, "failures": []}
        max_seen = watermark
        try:
            for ref in adapter.list(scope):
                if watermark and ref.last_modified and ref.last_modified <= watermark:
                    stats["skipped_stale"] += 1
                    continue
                stats["found"] += 1
                try:
                    raw = adapter.fetch_detail(ref)
                except Exception as exc:  # 单项失败不拖垮批次
                    stats["failed"] += 1
                    stats["failures"].append({"source_key": ref.source_key, "error_kind": classify_error(exc), "message": str(exc)[:200]})
                    continue
                fp = fingerprint(adapter.kind, raw.ref.source_key, raw.ref.revision)
                existed = session.scalar(select(Dataset.id).where(Dataset.fingerprint == fp))
                if existed is not None:
                    dataset, _ = _upsert_dataset(session, source, raw, fp)
                    stats["duplicates"] += 1
                else:
                    dataset, is_new = _upsert_dataset(session, source, raw, fp)
                    _append_version_and_evidence(session, dataset, raw)
                    stats["new" if is_new else "updated"] += 1
                if raw.ref.last_modified and (max_seen is None or raw.ref.last_modified > max_seen):
                    max_seen = raw.ref.last_modified
            # 结果数达到 limit 视为可能截断：不推进水位，下次全量重扫（指纹去重保证正确性）；
            # 确认取到结果集末尾时才把最大 last_modified 写入该 scope 的水位。
            total_refs = stats["found"] + stats["skipped_stale"]
            truncated = scope.limit > 0 and total_refs >= scope.limit
            if not truncated:
                source.cursor = {**(source.cursor or {}), key: {"last_modified": max_seen.isoformat() if max_seen else ""}}
            stats["watermark_advanced"] = not truncated
            run.status = "completed"
        except KeyboardInterrupt:
            run.status = "interrupted"
            run.stats = stats
            run.finished_at = utcnow()
            raise
        except Exception as exc:
            run.status = "failed"
            run.error_kind = classify_error(exc)
            stats["failures"].append({"source_key": "", "error_kind": run.error_kind, "message": str(exc)[:200]})
        run.stats = {**stats, "failures": stats["failures"][:50]}
        run.finished_at = utcnow()
        session.flush()
        session.expunge(run)
        return run
