"""采集源适配器协议与传输对象。

适配器只负责"从来源拿到原始元数据"，模态推导、许可分级等归一化在 normalize.py，
去重与落库在 collect.py——三方都可独立测试。新增数据源（如三维登记适配器）
实现 SourceAdapter 即可进入同一目录与配对流程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Protocol


@dataclass(frozen=True)
class CollectScope:
    """采集范围：结构化过滤条件 + 数量上限。各字段语义由适配器映射到来源 API。"""

    search: str = ""
    task_categories: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    author: str = ""
    limit: int = 100


@dataclass(frozen=True)
class DatasetRef:
    """list() 阶段的轻量引用；revision/last_modified 用于增量与指纹。"""

    source_key: str
    revision: str = ""
    last_modified: datetime | None = None


@dataclass(frozen=True)
class RawDataset:
    """fetch_detail() 的原始返回；tags 保留来源原样标签，归一化在下游做。"""

    ref: DatasetRef
    name: str = ""
    author: str = ""
    url: str = ""
    license: str = ""
    tags: tuple[str, ...] = ()
    task_categories: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    size_bytes: int | None = None
    size_rows: int | None = None
    downloads: int = 0
    likes: int = 0
    card_text: str = ""


class SourceAdapter(Protocol):
    kind: str
    endpoint: str

    def list(self, scope: CollectScope) -> Iterator[DatasetRef]: ...

    def fetch_detail(self, ref: DatasetRef) -> RawDataset: ...


@dataclass(frozen=True)
class CollectStats:
    found: int = 0
    new: int = 0
    updated: int = 0
    duplicates: int = 0
    failed: int = 0
    failures: tuple[dict, ...] = field(default_factory=tuple)
