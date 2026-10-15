"""CLI 测试：init-db / search / stats 走真实命令行入口（collect 需联网，属集成验证）。"""

from __future__ import annotations

import json

from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.cli import main
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.db import create_engine_for, init_db

from conftest import FakeAdapter, make_raw


def _seed(db_path):
    engine = create_engine_for(db_path)
    init_db(engine)
    collect(engine, FakeAdapter([make_raw("alice/zh-sentiment", tasks=("text-classification",), languages=("zh",),
                                         license="mit", card="中文情感分类", downloads=10)]), CollectScope())
    engine.dispose()


class TestCli:
    def test_init_db_creates_file(self, tmp_path):
        db = str(tmp_path / "c.db")
        assert main(["--db", db, "init-db"]) == 0
        assert (tmp_path / "c.db").exists()

    def test_stats_and_search_human_and_json(self, tmp_path, capsys):
        db = str(tmp_path / "c.db")
        _seed(db)
        assert main(["--db", db, "stats"]) == 0
        assert "数据集 1" in capsys.readouterr().out
        assert main(["--db", db, "search", "--modality", "text", "--license-class", "permissive"]) == 0
        assert "alice/zh-sentiment" in capsys.readouterr().out
        assert main(["--db", db, "search", "--modality", "3d", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == []
        assert main(["--db", db, "search", "--query", "情感", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["source_key"] == "alice/zh-sentiment" and payload[0]["missing"] == []

    def test_search_no_match_message(self, tmp_path, capsys):
        db = str(tmp_path / "c.db")
        _seed(db)
        assert main(["--db", db, "search", "--query", "不存在的词"]) == 0
        assert "没有匹配的数据集" in capsys.readouterr().out
