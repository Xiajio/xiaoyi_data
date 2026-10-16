"""Explicit registration of official dataset metadata; no open-web crawling or downloads."""
from __future__ import annotations
from dataclasses import replace
from urllib.parse import urlparse
from .base import CollectScope, DatasetRef, RawDataset


class ManualAdapter:
    kind = "manual"
    endpoint = "manual://registry"

    def __init__(self, records: list[RawDataset]):
        self.records = {record.ref.source_key: record for record in records}
        for record in records:
            if not record.ref.revision.strip():
                raise ValueError("人工登记必须提供来源版本")
            if urlparse(record.url).scheme not in {"https", "http"}:
                raise ValueError("人工登记必须提供官方页面 URL")

    def list(self, scope: CollectScope):
        records = [r for r in self.records.values()
                   if (not scope.search or scope.search.lower() in (r.name + r.ref.source_key + r.card_text).lower())
                   and (not scope.author or scope.author == r.author)
                   and (not scope.task_categories or set(scope.task_categories).intersection(r.task_categories))
                   and (not scope.languages or set(scope.languages).intersection(r.languages))]
        yield from (r.ref for r in (records[:scope.limit] if scope.limit > 0 else records))

    def fetch_detail(self, ref: DatasetRef) -> RawDataset:
        record = self.records[ref.source_key]
        if ref.revision != record.ref.revision:
            raise ValueError("登记版本已变化")
        return record


def register_manual(engine, payload: dict):
    from ..collect import collect
    from ..normalize import derive_modalities
    required = ("source_key", "revision", "url", "access_conditions")
    missing = [key for key in required if not str(payload.get(key, "")).strip()]
    if missing:
        raise ValueError("人工登记缺少字段：" + ", ".join(missing))
    card = str(payload.get("card_text", "")) + "\n\nAccess conditions: " + str(payload["access_conditions"])
    tags = tuple(payload.get("tags") or ()) + tuple(f"modality:{value}" for value in payload.get("modalities", []))
    raw = RawDataset(ref=DatasetRef(str(payload["source_key"]), str(payload["revision"])),
                     name=str(payload.get("name") or payload["source_key"]), url=str(payload["url"]),
                     author=str(payload.get("author", "")), license=str(payload.get("license", "")),
                     tags=tags, task_categories=tuple(payload.get("task_categories") or ()),
                     languages=tuple(payload.get("languages") or ()), size_rows=payload.get("size_rows"),
                     size_bytes=payload.get("size_bytes"), card_text=card)
    return collect(engine, ManualAdapter([raw]), CollectScope(limit=2))
