"""HuggingFace 数据集适配器。

- 端点可用 endpoint 参数或 HF_ENDPOINT 环境变量切换（内网默认走镜像站）。
- list() 使用服务端 search/author/filter 过滤；filter 组合 task_categories 与 language。
- fetch_detail() = dataset_info（权威字段）+ README 原文（卡片证据），两者失败分类由 collect 负责。
- url 一律记录 huggingface.co 规范地址；镜像端点只影响抓取，不进入数据身份。
"""

from __future__ import annotations

import time
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from inspect import signature
from datetime import datetime
from typing import Iterator

from huggingface_hub import HfApi

from .base import CollectScope, DatasetRef, RawDataset, SourceAdapter

DEFAULT_ENDPOINT = "https://huggingface.co"
CARD_FILENAME = "README.md"
CARD_MAX_CHARS = 200_000
CATALOG_REQUEST_TIMEOUT = 30.0
DETAIL_FIELDS = ["author", "cardData", "downloads", "lastModified", "likes", "sha", "tags"]
_HTTP_CONFIG_LOCK = threading.Lock()
_HTTP_CONFIGURED = False
_CATALOG_DEADLINE = ContextVar("hf_catalog_deadline", default=None)


@contextmanager
def _catalog_request():
    token = _CATALOG_DEADLINE.set(time.monotonic() + CATALOG_REQUEST_TIMEOUT)
    try:
        yield
    finally:
        _CATALOG_DEADLINE.reset(token)


def _remaining_timeout():
    deadline = _CATALOG_DEADLINE.get()
    remaining = CATALOG_REQUEST_TIMEOUT if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        # A builtin TimeoutError avoids the SDK's automatic HTTP transport retries
        # restarting a timed-out catalog operation with a fresh unbounded budget.
        raise TimeoutError("Hugging Face catalog request exceeded its time budget")
    return min(CATALOG_REQUEST_TIMEOUT, remaining)


def _bound_request_timeout(request):
    cap = _remaining_timeout()
    current = request.extensions.get("timeout") or {}
    request.extensions["timeout"] = {name: min(float(current[name]), cap) if current.get(name) is not None else cap
        for name in ("connect", "read", "write", "pool")}


def _bounded_http_client():
    # Keep the SDK's own HTTP implementation (httpx in v1, httpx2 in v2),
    # environment proxy/certificate settings, request IDs and offline hook.
    from huggingface_hub.utils._http import default_client_factory
    client = default_client_factory()
    client.timeout = type(client.timeout)(CATALOG_REQUEST_TIMEOUT)
    client.event_hooks["request"] = [*client.event_hooks.get("request", []), _bound_request_timeout]
    return client


def _bounded_legacy_session():
    from huggingface_hub.utils._http import _default_backend_factory
    session = _default_backend_factory()
    original = session.request
    def request(method, url, **kwargs):
        cap = _remaining_timeout()
        requested = kwargs.get("timeout")
        if isinstance(requested, tuple):
            kwargs["timeout"] = tuple(min(float(value), cap) if value is not None else cap for value in requested)
        else:
            kwargs["timeout"] = min(float(requested), cap) if requested is not None else cap
        return original(method, url, **kwargs)
    session.request = request
    return session


def _configure_bounded_http():
    global _HTTP_CONFIGURED
    if _HTTP_CONFIGURED:
        return
    with _HTTP_CONFIG_LOCK:
        if _HTTP_CONFIGURED:
            return
        try:
            from huggingface_hub import set_client_factory
        except ImportError:  # Supported Hub 0.x uses requests and its official factory API.
            from huggingface_hub import configure_http_backend
            configure_http_backend(backend_factory=_bounded_legacy_session)
        else:
            set_client_factory(_bounded_http_client)
        _HTTP_CONFIGURED = True


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

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, *, client: HfApi | None = None, detail_delay: float = 0.1, sort: str | None = None):
        self.endpoint = endpoint.rstrip("/")
        self.sort = sort
        if client is None:
            _configure_bounded_http()
        self._api = client if client is not None else HfApi(endpoint=self.endpoint)
        # fetch_detail 之间的礼貌间隔（秒），避免对端点造成突发压力；测试注入 client 时可置 0。
        self.detail_delay = detail_delay

    def list(self, scope: CollectScope) -> Iterator[DatasetRef]:
        filters = [f"task_categories:{task}" for task in scope.task_categories]
        filters += [f"language:{language}" for language in scope.languages]
        kwargs: dict = {"limit": scope.limit}
        if "expand" in signature(self._api.list_datasets).parameters:
            kwargs["expand"] = ["sha", "lastModified"]
        else:
            kwargs["full"] = True
        if self.sort:
            kwargs["sort"] = self.sort
            # Hub 2.x uses descending sort by default and removed direction.
            # Older supported clients need it explicitly to list latest first.
            if "direction" in signature(self._api.list_datasets).parameters:
                kwargs["direction"] = -1
        if scope.search:
            kwargs["search"] = scope.search
        if scope.author:
            kwargs["author"] = scope.author
        if filters:
            kwargs["filter"] = filters
        iterator = iter(self._api.list_datasets(**kwargs))
        while True:
            # A deadline covers one network page fetch, not the caller's per-item
            # processing time between generator yields.
            with _catalog_request():
                try:
                    info = next(iterator)
                except StopIteration:
                    return
            yield DatasetRef(
                source_key=str(info.id),
                revision=str(_getattr_ci(info, "sha", "revision") or ""),
                last_modified=_getattr_ci(info, "last_modified", "lastModified"),
            )

    def fetch_detail(self, ref: DatasetRef) -> RawDataset:
        if self.detail_delay > 0:
            time.sleep(self.detail_delay)
        kwargs = {"revision": ref.revision or None}
        parameters = signature(self._api.dataset_info).parameters
        if "timeout" in parameters:
            kwargs["timeout"] = CATALOG_REQUEST_TIMEOUT
        if "expand" in parameters:
            kwargs["expand"] = list(DETAIL_FIELDS)
        with _catalog_request():
            info = self._api.dataset_info(ref.source_key, **kwargs)
        revision = str(_getattr_ci(info, "sha", "revision") or ref.revision)
        if not revision:
            raise ValueError("source did not supply a pinned revision")
        card_text = self._load_card(ref.source_key, revision)
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
            size_bytes=self._size_bytes(card_data),
            size_rows=size_rows,
            downloads=int(_getattr_ci(info, "downloads") or 0),
            likes=int(_getattr_ci(info, "likes") or 0),
            card_text=card_text,
        )

    def _load_card(self, source_key: str, revision: str) -> str:
        try:
            with _catalog_request():
                path = self._api.hf_hub_download(repo_id=source_key, repo_type="dataset", filename=CARD_FILENAME, revision=revision)
        except Exception as exc:
            # Missing README is valid; permission, timeout and network failures must be retried.
            if isinstance(exc, FileNotFoundError) or type(exc).__name__ in {"EntryNotFoundError", "RemoteEntryNotFoundError"}:
                return ""
            raise
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
        splits = dataset_info.get("splits")
        if isinstance(splits, list):
            if splits and all(isinstance(item, dict) and isinstance(item.get("num_examples"), int)
                              and not isinstance(item.get("num_examples"), bool) and item["num_examples"] >= 0 for item in splits):
                return sum(item["num_examples"] for item in splits)
        return None

    @staticmethod
    def _size_bytes(card_data: dict) -> int | None:
        info = card_data.get("dataset_info")
        size = info.get("dataset_size") if isinstance(info, dict) else None
        return int(size) if isinstance(size, (int, float)) and not isinstance(size, bool) and size >= 0 else None
