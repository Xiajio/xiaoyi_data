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
import re
from dataclasses import asdict
from itertools import chain
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .adapters.base import CollectScope, DatasetRef, RawDataset, SourceAdapter
from .db import session_scope
from .models import Dataset, DatasetVersion, Evidence, Run, Source, utcnow
from .normalize import classify_license, derive_modalities

CARD_EVIDENCE_EXCERPT = 200_000


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
    if status in (401, 403) or isinstance(exc, PermissionError):
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
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
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


def captured_metadata(raw: RawDataset, dataset_id: int) -> dict:
    modalities, modality_source = derive_modalities(raw.tags, raw.task_categories)
    return {"id": dataset_id, "source_key": raw.ref.source_key, "name": raw.name, "author": raw.author,
            "url": raw.url, "license": raw.license, "license_class": classify_license(raw.license),
            "modalities": modalities, "modality_source": modality_source,
            "task_categories": list(raw.task_categories), "languages": list(raw.languages),
            "size_rows": raw.size_rows, "size_bytes": raw.size_bytes, "downloads": raw.downloads,
            "likes": raw.likes, "card_text": raw.card_text,
            "source_modified_at": raw.ref.last_modified.isoformat() if raw.ref.last_modified else None}


def _append_version_and_evidence(session, dataset: Dataset, raw: RawDataset) -> DatasetVersion:
    from .search import dataset_to_dict
    version = session.scalar(select(DatasetVersion).where(
        DatasetVersion.dataset_id == dataset.id, DatasetVersion.revision == raw.ref.revision))
    if version is not None:
        return version
    metadata = captured_metadata(raw, dataset.id)
    version = DatasetVersion(dataset_id=dataset.id, revision=raw.ref.revision,
                             metadata_json=metadata,
                             provenance_status="verified" if raw.ref.revision else "legacy_unverified")
    session.add(version)
    session.flush()
    if raw.card_text.strip():
        session.add(Evidence(dataset_id=dataset.id, dataset_version_id=version.id, kind="card",
                             excerpt=raw.card_text[:CARD_EVIDENCE_EXCERPT], location=f"{raw.ref.revision}/README.md"))
    session.add(Evidence(dataset_id=dataset.id, dataset_version_id=version.id, kind="metadata",
                         excerpt=json.dumps({k: v for k, v in metadata.items() if k != "card_text"}, ensure_ascii=False),
                         location=f"{raw.url}@{raw.ref.revision}:metadata"))
    return version


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class CollectionCancelled(Exception):
    """Cancellation is control flow, never a source-item failure."""


def safe_collection_error(exc: BaseException) -> str:
    messages = {"rate_limit": "来源请求频率受限，请稍后重试", "permission": "来源访问被拒绝，请检查服务器凭据",
                "timeout": "来源请求超时，可手动重试", "network": "来源网络连接失败，可手动重试",
                "other": "来源采集失败，请检查配置或稍后重试"}
    return messages[classify_error(exc)]


def safe_error_fields(exc: BaseException) -> dict:
    """Stable diagnostic fields only: never include exception text, URLs, headers or tokens."""
    name = type(exc).__name__
    error_type = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,95}", name) else "Exception"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return {"error_type": error_type, "http_status": status if type(status) is int and 100 <= status <= 599 else None}


def collect(engine: Engine, adapter: SourceAdapter, scope: CollectScope, *, retry_of: int | None = None,
            run_id: int | None = None, progress=None, guard=None) -> Run:
    """Resume a durable run; commit each item's data and checkpoint together.

    ``progress`` receives persisted counts and a precise version id (including stale/duplicate
    items). ``guard`` exceptions stop work and propagate without becoming source failures.
    Metadata and cards only: this function never downloads dataset payload files.
    """
    key = scope_key(scope)
    with session_scope(engine) as session:
        source = session.scalar(select(Source).where(Source.kind == adapter.kind, Source.endpoint == adapter.endpoint))
        if source is None:
            source = Source(kind=adapter.kind, endpoint=adapter.endpoint)
            session.add(source)
            session.flush()
        source_id = source.id
        watermark = _parse_watermark((source.cursor or {}).get(key, {}).get("last_modified"))
        run = session.get(Run, run_id) if run_id is not None else None
        if run_id is not None and run is None:
            raise ValueError("采集运行记录不存在")
        if run is None:
            run = Run(kind="collect", adapter=adapter.kind, status="running", input_scope=asdict(scope), retry_of=retry_of)
            session.add(run)
            session.flush()
        elif run.adapter != adapter.kind or json.dumps(run.input_scope, sort_keys=True) != json.dumps(asdict(scope), sort_keys=True):
            raise ValueError("恢复的采集来源或范围与运行记录不一致")
        run_id = run.id
        items = dict((run.stats or {}).get("items", {}))
        saved_max = _parse_watermark((run.stats or {}).get("max_seen"))
        max_seen = max((value for value in (watermark, saved_max) if value is not None), default=None)
        run.status, run.error_kind, run.finished_at = "running", "", None
    status, error_kind = "completed", ""
    interrupted: BaseException | None = None
    control_error: BaseException | None = None
    list_failure = None
    stats = {}

    def counts():
        values = list(items.values())
        result = {"found": sum(v["outcome"] != "skipped_stale" for v in values),
                  "processed": sum(v["outcome"] != "processing" for v in values),
                  **{name: sum(v["outcome"] == name for v in values)
                     for name in ("new", "updated", "duplicates", "skipped_stale", "failed")}}
        failures = [{k:v for k,v in item.items() if k not in {"outcome", "dataset_version_id"}}
                    for item in values if item["outcome"] == "failed"]
        if list_failure:
            failures.append(list_failure)
        result.update(items=items, failures=failures[:50], watermark_advanced=False,
                      max_seen=max_seen.isoformat() if max_seen else "",
                      dataset_version_ids=list(dict.fromkeys(v["dataset_version_id"] for v in values if v.get("dataset_version_id"))))
        return result

    def check():
        nonlocal control_error
        if guard:
            try:
                guard()
            except BaseException as exc:
                control_error = exc
                raise

    def notify(item=None, **extra):
        if progress:
            progress({"run_id": run_id, "stats": counts(), "source_key": (item or {}).get("source_key", ""),
                      "dataset_version_id": (item or {}).get("dataset_version_id"),
                      "outcome": (item or {}).get("outcome", "started"), **extra})

    def persist():
        with session_scope(engine) as session:
            session.get(Run, run_id).stats = counts()

    try:
        check()
        persist()
        notify()
        pending = [DatasetRef(item["source_key"], item["revision"], _parse_watermark(item.get("last_modified")))
                   for item in items.values() if item["outcome"] == "processing"]
        def remaining_refs():
            # A retry/resume already has a bounded original scope. Do not make a
            # new list request when all slots are occupied by retained entries.
            if scope.limit <= 0 or len(items) < scope.limit:
                yield from adapter.list(scope)
        for ref in chain(pending, remaining_refs()):
            check()
            item_key = fingerprint(adapter.kind, ref.source_key, ref.revision)
            previous = items.get(item_key)
            if previous and previous["outcome"] != "processing":
                notify(previous, resumed=True)
                continue
            if previous is None and scope.limit > 0 and len(items) >= scope.limit:
                break
            modified = _aware(ref.last_modified)
            with session_scope(engine) as session:
                seen = session.scalar(select(DatasetVersion.id).join(Dataset).where(
                    Dataset.source_id == source_id, Dataset.source_key == ref.source_key,
                    DatasetVersion.revision == ref.revision)) if ref.revision else None
            if watermark and modified and modified <= watermark and seen is not None:
                items[item_key] = {"source_key": ref.source_key, "revision": ref.revision,
                                   "dataset_version_id": seen, "outcome": "skipped_stale"}
                persist()
                notify(items[item_key])
                continue
            items[item_key] = {"source_key": ref.source_key, "revision": ref.revision, "outcome": "processing",
                               "last_modified": modified.isoformat() if modified else ""}
            persist()
            notify(items[item_key])
            check()
            try:
                raw = adapter.fetch_detail(ref)
            except Exception as exc:
                # Guards supplied through adapters must also remain control flow.
                if isinstance(exc, CollectionCancelled) or type(exc).__name__ in {"RunCancelled", "LeaseLost"}:
                    control_error = exc
                    raise
                items[item_key] = {"source_key": ref.source_key, "revision": ref.revision, "outcome": "failed",
                                   "error_kind": classify_error(exc), "message": safe_collection_error(exc), **safe_error_fields(exc),
                                   "last_modified": _aware(ref.last_modified).isoformat() if ref.last_modified else ""}
                persist()
                notify(items[item_key])
                continue
            check()
            try:
                fp = fingerprint(adapter.kind, raw.ref.source_key, raw.ref.revision)
                with session_scope(engine) as session:
                    source = session.get(Source, source_id)
                    prior = session.scalar(select(DatasetVersion).join(Dataset).where(
                        Dataset.source_id == source_id, Dataset.source_key == raw.ref.source_key,
                        DatasetVersion.revision == raw.ref.revision))
                    if prior is not None:
                        session.get(Dataset, prior.dataset_id).last_seen_at = utcnow()
                        version_id, outcome = prior.id, "duplicates"
                    else:
                        dataset, is_new = _upsert_dataset(session, source, raw, fp)
                        version = _append_version_and_evidence(session, dataset, raw)
                        version_id, outcome = version.id, "new" if is_new else "updated"
                    modified = _aware(raw.ref.last_modified)
                    if modified and (max_seen is None or modified > max_seen):
                        max_seen = modified
                    items[item_key] = {"source_key": raw.ref.source_key, "revision": raw.ref.revision,
                                       "dataset_version_id": version_id, "outcome": outcome}
                    session.get(Run, run_id).stats = counts()
            except Exception as exc:
                items[item_key] = {"source_key": ref.source_key, "revision": ref.revision, "outcome": "failed",
                                   "error_kind": classify_error(exc), "message": safe_collection_error(exc), **safe_error_fields(exc),
                                   "last_modified": _aware(ref.last_modified).isoformat() if ref.last_modified else ""}
                persist()
            notify(items[item_key])
    except (KeyboardInterrupt, SystemExit) as exc:
        status, interrupted = "interrupted", exc
    except Exception as exc:
        if exc is control_error or isinstance(exc, CollectionCancelled) or type(exc).__name__ in {"LeaseLost", "RunCancelled"}:
            status, interrupted = ("cancelled" if isinstance(exc, CollectionCancelled) else "interrupted"), exc
        else:
            status, error_kind = "failed", classify_error(exc)
            list_failure = {"source_key": "", "error_kind": error_kind, "message": safe_collection_error(exc), **safe_error_fields(exc)}
    finally:
        # An expired worker must never overwrite progress committed by its successor.
        # Per-item checkpoints already make the last valid state recoverable.
        if type(interrupted).__name__ != "LeaseLost":
            with session_scope(engine) as session:
                stats = counts()
                truncated = scope.limit > 0 and len(items) >= scope.limit
                if status == "completed" and not truncated and not stats["failed"]:
                    source = session.get(Source, source_id)
                    source.cursor = {**(source.cursor or {}), key: {"last_modified": max_seen.isoformat() if max_seen else ""}}
                    stats["watermark_advanced"] = True
                run = session.get(Run, run_id)
                run.status, run.error_kind, run.stats, run.finished_at = status, error_kind, stats, utcnow()
                session.flush()
                session.expunge(run)
    if interrupted is not None:
        raise interrupted
    return run
