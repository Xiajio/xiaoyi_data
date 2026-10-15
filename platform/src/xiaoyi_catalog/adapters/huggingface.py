"""HuggingFace 数据集适配器。

- 端点可用 endpoint 参数或 HF_ENDPOINT 环境变量切换（内网默认走镜像站）。
- list() 使用服务端 search/author/filter 过滤；filter 组合 task_categories 与 language。
- fetch_detail() = dataset_info（权威字段）+ README 原文（卡片证据），两者失败分类由 collect 负责。
- url 一律记录 huggingface.co 规范地址；镜像端点只影响抓取，不进入数据身份。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Iterator

from huggingface_hub import HfApi

from .base import CollectScope, DatasetRef, RawDataset, SourceAdapter

DEFAULT_ENDPOINT = "https://huggingface.co"
CARD_FILENAME = "README.md"
CARD_MAX_CHARS = 200_000


def _getattr_ci(obj: object, *names: str) -> object:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def tag_values(tags: tuple[str, ...] | list[str], prefix: str) -> tuple[str, ...]:
    """提取 HF 标签中指定前缀的值，如 tags 里的 'language:zh' → 'zh'。"""
    marker = prefix + ":"
    return tuple(sorted({tag[len(marker):].strip().lower() for tag in tags if tag.startswith(marker) and tag[len(marker):].strip()}))


def _as_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


class HuggingFaceAdapter(SourceAdapter):
    kind = "huggingface"

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, *, client: HfApi | None = None, detail_delay: float = 0.1):
        self.endpoint = endpoint.rstrip("/")
        self._api = client if client is not None else HfApi(endpoint=self.endpoint)
        # fetch_detail 之间的礼貌间隔（秒），避免对端点造成突发压力；测试注入 client 时可置 0。
        self.detail_delay = detail_delay

    def list(self, scope: CollectScope) -> Iterator[DatasetRef]:
        filters = [f"task_categories:{task}" for task in scope.task_categories]
        filters += [f"language:{language}" for language in scope.languages]
        kwargs: dict = {"limit": scope.limit, "full": True}
        if scope.search:
            kwargs["search"] = scope.search
        if scope.author:
            kwargs["author"] = scope.author
        if filters:
            kwargs["filter"] = filters
        for info in self._api.list_datasets(**kwargs):
            yield DatasetRef(
                source_key=str(info.id),
                revision=str(_getattr_ci(info, "sha", "revision") or ""),
                last_modified=_getattr_ci(info, "last_modified", "lastModified"),
            )

    def fetch_detail(self, ref: DatasetRef) -> RawDataset:
        if self.detail_delay > 0:
            time.sleep(self.detail_delay)
        info = self._api.dataset_info(ref.source_key)
        card_text = self._load_card(ref.source_key)
        card_raw = _getattr_ci(info, "card_data", "cardData") or {}
        # huggingface_hub 的 DatasetCardData 是对象而非字典，需经 to_dict() 展开。
        card_data = card_raw.to_dict() if hasattr(card_raw, "to_dict") else dict(card_raw)
        tags = tuple(_getattr_ci(info, "tags") or ())
        task_categories = tuple(sorted(set(_as_tuple(card_data.get("task_categories")) + tag_values(tags, "task_categories"))))
        languages = tuple(sorted(set(_as_tuple(card_data.get("language")) + tag_values(tags, "language"))))
        license_ids = _as_tuple(card_data.get("license")) or tag_values(tags, "license")
        size_rows = self._size_rows(card_data)
        return RawDataset(
            ref=DatasetRef(
                source_key=ref.source_key,
                revision=str(_getattr_ci(info, "sha", "revision") or ref.revision),
                last_modified=_getattr_ci(info, "last_modified", "lastModified") or ref.last_modified,
            ),
            name=ref.source_key.split("/")[-1],
            author=str(_getattr_ci(info, "author") or ""),
            url=f"https://huggingface.co/datasets/{ref.source_key}",
            license=(license_ids[0] if license_ids else ""),
            tags=tags,
            task_categories=task_categories,
            languages=languages,
            size_bytes=None,
            size_rows=size_rows,
            downloads=int(_getattr_ci(info, "downloads") or 0),
            likes=int(_getattr_ci(info, "likes") or 0),
            card_text=card_text,
        )

    def _load_card(self, source_key: str) -> str:
        try:
            path = self._api.hf_hub_download(repo_id=source_key, repo_type="dataset", filename=CARD_FILENAME)
        except Exception:
            return ""  # 无卡片是常见情况，按缺失处理（missing_fields 会标出）
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read(CARD_MAX_CHARS)
        except OSError:
            return ""

    @staticmethod
    def _size_rows(card_data: dict) -> int | None:
        dataset_info = card_data.get("dataset_info")
        if not isinstance(dataset_info, dict):
            return None
        size = dataset_info.get("dataset_size")
        if isinstance(size, (int, float)) and size >= 0:
            return int(size)
        splits = dataset_info.get("splits")
        if isinstance(splits, list):
            total = sum(int(item.get("num_examples", 0)) for item in splits if isinstance(item, dict))
            return total or None
        return None
