"""HuggingFace 适配器测试：客户端全部 fake，不访问网络。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.adapters.huggingface import HuggingFaceAdapter, tag_values


class FakeInfo:
    def __init__(self, id, *, author="", sha="sha-1", last_modified=None, tags=(), downloads=0, likes=0, card_data=None):
        self.id = id
        self.author = author
        self.sha = sha
        self.last_modified = last_modified
        self.tags = list(tags)
        self.downloads = downloads
        self.likes = likes
        self.card_data = card_data or {}


class FakeHfClient:
    def __init__(self, infos, *, cards=None, card_errors=None, tmp=None):
        self._infos = {info.id: info for info in infos}
        self._cards = dict(cards or {})
        self._card_errors = dict(card_errors or {})
        self._tmp = tmp
        self.list_kwargs = None

    def list_datasets(self, **kwargs):
        self.list_kwargs = kwargs
        return iter(self._infos.values())

    def dataset_info(self, key):
        return self._infos[key]

    def hf_hub_download(self, repo_id, repo_type, filename):
        assert repo_type == "dataset" and filename == "README.md"
        if repo_id in self._card_errors:
            raise self._card_errors[repo_id]
        path = self._tmp / (repo_id.replace("/", "_") + ".md")
        path.write_text(self._cards[repo_id], encoding="utf-8")
        return str(path)


def make_adapter(infos, **kwargs):
    client = FakeHfClient(infos, tmp=kwargs.pop("tmp"), **kwargs)
    return HuggingFaceAdapter("https://hf-mirror.com", client=client, detail_delay=0), client


class TestTagValues:
    def test_extracts_prefix_values_sorted_and_lowered(self):
        assert tag_values(["language:zh", "language:en", "task_categories:text-classification", "other"], "language") == ("en", "zh")

    def test_ignores_blank_values(self):
        assert tag_values(["license:", "license:mit"], "license") == ("mit",)


class TestList:
    def test_combines_filters_and_maps_refs(self, tmp_path):
        when = datetime(2026, 9, 1, tzinfo=timezone.utc)
        adapter, client = make_adapter([FakeInfo("a/ds", sha="abc", last_modified=when)], tmp=tmp_path)
        refs = list(adapter.list(CollectScope(search="情感", author="a", task_categories=("text-classification",), languages=("zh",), limit=30)))
        assert client.list_kwargs == {"limit": 30, "full": True, "search": "情感", "author": "a",
                                      "filter": ["task_categories:text-classification", "language:zh"]}
        assert [(ref.source_key, ref.revision, ref.last_modified) for ref in refs] == [("a/ds", "abc", when)]

    def test_omits_empty_filters(self, tmp_path):
        adapter, client = make_adapter([FakeInfo("a/ds")], tmp=tmp_path)
        list(adapter.list(CollectScope(limit=5)))
        assert client.list_kwargs == {"limit": 5, "full": True}


class TestFetchDetail:
    def test_card_data_wins_and_splits_sum_to_rows(self, tmp_path):
        info = FakeInfo("org/ds", author="org", sha="sha-9", tags=["license:apache-2.0", "modality:text"],
                        downloads=42, likes=7,
                        card_data={"license": ["mit"], "task_categories": ["question-answering"], "language": ["zh"],
                                   "dataset_info": {"splits": [{"num_examples": 10}, {"num_examples": 5}]}})
        adapter, _ = make_adapter([info], cards={"org/ds": "# 卡片标题\n正文"}, tmp=tmp_path)
        ref = next(iter(adapter.list(CollectScope())))
        raw = adapter.fetch_detail(ref)
        assert raw.ref.revision == "sha-9"
        assert raw.license == "mit"
        assert raw.task_categories == ("question-answering",)
        assert raw.languages == ("zh",)
        assert raw.size_rows == 15
        assert raw.card_text.startswith("# 卡片标题")
        assert raw.url == "https://huggingface.co/datasets/org/ds"  # 规范地址，不随镜像端点变化

    def test_tags_fallback_when_card_data_empty(self, tmp_path):
        info = FakeInfo("org/plain", card_data={}, tags=["task_categories:text-classification", "language:en", "license:gpl-3.0"])
        adapter, _ = make_adapter([info], cards={"org/plain": "readme"}, tmp=tmp_path)
        raw = adapter.fetch_detail(next(iter(adapter.list(CollectScope()))))
        assert raw.task_categories == ("text-classification",)
        assert raw.languages == ("en",)
        assert raw.license == "gpl-3.0"

    def test_missing_or_failed_card_means_empty_text_not_error(self, tmp_path):
        adapter, _ = make_adapter([FakeInfo("org/no-card")], card_errors={"org/no-card": FileNotFoundError("404")}, tmp=tmp_path)
        raw = adapter.fetch_detail(next(iter(adapter.list(CollectScope()))))
        assert raw.card_text == ""

    def test_dataset_size_field_used_when_present(self, tmp_path):
        info = FakeInfo("org/sized", card_data={"dataset_info": {"dataset_size": 12345}})
        adapter, _ = make_adapter([info], card_errors={"org/sized": FileNotFoundError()}, tmp=tmp_path)
        assert adapter.fetch_detail(next(iter(adapter.list(CollectScope())))).size_rows == 12345

    def test_card_data_object_form_is_unpacked_via_to_dict(self, tmp_path):
        class CardDataObject:  # 仿 huggingface_hub.DatasetCardData：对象是映射但 dict() 会炸
            def __getitem__(self, key):
                raise KeyError(key)

            def to_dict(self):
                return {"license": "mit", "task_categories": ["text-classification"]}

        info = FakeInfo("org/obj-card", card_data=CardDataObject())
        adapter, _ = make_adapter([info], cards={"org/obj-card": "readme"}, tmp=tmp_path)
        raw = adapter.fetch_detail(next(iter(adapter.list(CollectScope()))))
        assert raw.license == "mit" and raw.task_categories == ("text-classification",)
