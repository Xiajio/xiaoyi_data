"""构建策展 UI 端到端测试用的目录库（确定性、无网络）。

用法：python make_e2e_db.py <目标 db 路径>
内容：3 个数据集（text/image/3d 各一，许可分级各异）+ 卡片证据 + 整理候选
+ 1 张思路卡 + 2 条配对（一条待核验、一条已确认）+ 1 条采集记录。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from sqlalchemy import select

from xiaoyi_catalog.adapters.base import CollectScope, DatasetRef, RawDataset
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.db import create_engine_for, init_db, session_scope
from xiaoyi_catalog.enrich import enrich_all
from xiaoyi_catalog.models import Dataset, DatasetVersion
from xiaoyi_catalog.pairing import propose_pairing, review_pairing


class FixtureAdapter:
    kind = "fixture"
    endpoint = "https://fixture.local"

    def __init__(self, raws):
        self._raws = raws

    def list(self, scope):
        yield from (raw.ref for raw in self._raws)

    def fetch_detail(self, ref):
        return next(raw for raw in self._raws if raw.ref.source_key == ref.source_key)


def make_raw(key, *, license, tasks=(), tags=(), languages=(), card="", size_rows=None, downloads=0):
    return RawDataset(
        ref=DatasetRef(source_key=key, revision="fixture-rev-1"),
        name=key.split("/")[-1], author=key.split("/")[0], url=f"https://example.local/{key}",
        license=license, tags=tags, task_categories=tasks, languages=languages,
        size_rows=size_rows, downloads=downloads, card_text=card,
    )


def main(target: str) -> None:
    path = Path(target)
    if path.exists():
        path.unlink()
    engine = create_engine_for(path)
    init_db(engine)
    collect(engine, FixtureAdapter([
        make_raw("acme/zh-news", license="mit", tasks=("text-classification",), languages=("zh",),
                 card="中文新闻分类数据集，含训练/测试切分。", size_rows=5000, downloads=100),
        make_raw("acme/photo-cls", license="gpl-3.0", tasks=("image-classification",), languages=("en",),
                 card="Photo classification images.", size_rows=800, downloads=50),
        make_raw("acme/mesh-3d", license="cc-by-nc-4.0", tags=("modality:3d",), card="三维模型合集。", downloads=5),
    ]), CollectScope())
    enrich_all(engine)
    with session_scope(engine) as session:
        version_ids = dict(
            (key, value) for key, value in session.execute(
                select(Dataset.source_key, DatasetVersion.id).join(Dataset, Dataset.id == DatasetVersion.dataset_id)
            ).all()
        )
        idea = {"external_id": "e2e-idea-1", "title": "中文新闻分类基准",
                "statement": "端到端验证用的思路卡", "requirements": {"modalities": ["text"], "license_class": ["permissive"]},
                "source_refs": ["10.0000/e2e"]}
        pending = propose_pairing(session, idea, [version_ids["acme/zh-news"]], "许可宽松且为中文文本")["created"][0]["id"]
        confirmed = propose_pairing(session, {**idea, "title": "中文新闻分类基准"},
                                    [version_ids["acme/photo-cls"]], "对照用图像基准")["created"][0]["id"]
        review_pairing(session, confirmed, "confirmed", "预置已确认配对")
        assert pending != confirmed
    engine.dispose()
    print(f"E2E_DB_OK {path}")


if __name__ == "__main__":
    main(sys.argv[1])
