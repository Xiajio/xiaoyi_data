from __future__ import annotations

from datetime import datetime, timezone

import pytest

from xiaoyi_catalog.adapters.base import DatasetRef, RawDataset
from xiaoyi_catalog.db import create_engine_for, init_db


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine_for(tmp_path / "catalog.db")
    init_db(engine)
    yield engine
    engine.dispose()


def make_raw(
    key: str,
    *,
    revision: str = "rev-1",
    last_modified: datetime | None = None,
    license: str = "",
    tags: tuple[str, ...] = (),
    tasks: tuple[str, ...] = (),
    languages: tuple[str, ...] = (),
    card: str = "",
    downloads: int = 0,
    likes: int = 0,
    size_rows: int | None = None,
) -> RawDataset:
    return RawDataset(
        ref=DatasetRef(source_key=key, revision=revision, last_modified=last_modified),
        name=key.split("/")[-1],
        author=key.split("/")[0] if "/" in key else "",
        url=f"https://example.local/datasets/{key}",
        license=license,
        tags=tags,
        task_categories=tasks,
        languages=languages,
        size_rows=size_rows,
        downloads=downloads,
        likes=likes,
        card_text=card,
    )


class FakeAdapter:
    """内存适配器：failures 中的 source_key 在 fetch_detail 时抛出给定异常。"""

    kind = "fake"
    endpoint = "https://fake.local"

    def __init__(self, raws: list[RawDataset], *, list_error: Exception | None = None, failures: dict[str, Exception] | None = None):
        self._raws = list(raws)
        self._list_error = list_error
        self._failures = dict(failures or {})

    def list(self, scope):
        if self._list_error is not None:
            raise self._list_error
        yield from (raw.ref for raw in self._raws)

    def fetch_detail(self, ref: DatasetRef) -> RawDataset:
        if ref.source_key in self._failures:
            raise self._failures[ref.source_key]
        return next(raw for raw in self._raws if raw.ref.source_key == ref.source_key)


def day(month: int, day_: int) -> datetime:
    return datetime(2026, month, day_, tzinfo=timezone.utc)
