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

    def dataset_info(self, key, revision=None):
        self.detail_revision = revision
        return self._infos[key]

    def hf_hub_download(self, repo_id, repo_type, filename, revision=None):
        self.card_revision = revision
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
        adapter, client = make_adapter([info], cards={"org/ds": "# 卡片标题\n正文"}, tmp=tmp_path)
        ref = next(iter(adapter.list(CollectScope())))
        raw = adapter.fetch_detail(ref)
        assert raw.ref.revision == "sha-9"
        assert client.detail_revision == client.card_revision == "sha-9"
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
        raw = adapter.fetch_detail(next(iter(adapter.list(CollectScope()))))
        assert raw.size_rows is None
        assert raw.size_bytes == 12345

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

def test_explicit_recent_update_order_for_scheduled_collection(tmp_path):
    client = FakeHfClient([FakeInfo("org/recent")], tmp=tmp_path)
    adapter = HuggingFaceAdapter(client=client, sort="last_modified", detail_delay=0)
    list(adapter.list(CollectScope(limit=50)))
    assert client.list_kwargs["sort"] == "last_modified"
    assert "direction" not in client.list_kwargs
    assert client.list_kwargs["limit"] == 50


def test_sorted_collection_matches_installed_hub_signature():
    from unittest.mock import create_autospec
    from huggingface_hub import HfApi
    from inspect import signature
    client = create_autospec(HfApi, instance=True)
    client.list_datasets.return_value = [FakeInfo("org/recent")]
    adapter = HuggingFaceAdapter(client=client, sort="last_modified", detail_delay=0)
    assert len(list(adapter.list(CollectScope(limit=50)))) == 1
    args = client.list_datasets.call_args.kwargs
    assert args["sort"] == "last_modified"
    assert ("direction" in args) == ("direction" in signature(HfApi.list_datasets).parameters)


def test_sorted_collection_keeps_descending_order_for_legacy_hub():
    class LegacyClient:
        def list_datasets(self, *, sort, direction, limit, full):
            assert sort == "last_modified" and direction == -1
            assert limit == 50 and full is True
            return [FakeInfo("org/legacy")]
    adapter = HuggingFaceAdapter(client=LegacyClient(), sort="last_modified", detail_delay=0)
    assert len(list(adapter.list(CollectScope(limit=50)))) == 1


def test_installed_sdk_detail_timeout_and_minimal_expand(tmp_path):
    from unittest.mock import create_autospec
    from huggingface_hub import HfApi
    from inspect import signature
    from xiaoyi_catalog.adapters.base import DatasetRef
    from xiaoyi_catalog.adapters.huggingface import DETAIL_FIELDS
    client = create_autospec(HfApi, instance=True)
    client.dataset_info.return_value = FakeInfo("org/pinned", sha="a" * 40)
    card = tmp_path / "README.md"
    card.write_text("card", encoding="utf-8")
    client.hf_hub_download.return_value = str(card)
    adapter = HuggingFaceAdapter(client=client, detail_delay=0)
    adapter.fetch_detail(DatasetRef("org/pinned", "a" * 40))
    kwargs = client.dataset_info.call_args.kwargs
    assert kwargs["timeout"] == 30
    if "expand" in signature(HfApi.dataset_info).parameters:
        assert kwargs["expand"] == DETAIL_FIELDS and "siblings" not in kwargs["expand"]


def test_real_sdk_http_requests_override_explicit_none_and_keep_hooks(monkeypatch):
    from huggingface_hub import HfApi
    from huggingface_hub.utils import httpx
    from huggingface_hub.utils import _http
    import huggingface_hub.hf_api as api_module
    from xiaoyi_catalog.adapters.base import DatasetRef
    from xiaoyi_catalog.adapters import huggingface as adapter_module
    seen, original_hooks = [], []
    def transport(request):
        seen.append(request)
        data = {"id": "org/pinned", "sha": "a" * 40, "lastModified": "2026-10-16T00:00:00Z"}
        return httpx.Response(200, json=[data] if request.url.path == "/api/datasets" else data)
    monkeypatch.setattr(_http, "default_client_factory", lambda: httpx.Client(
        transport=httpx.MockTransport(transport), follow_redirects=True, timeout=None,
        event_hooks={"request": [lambda request: original_hooks.append(request)]}))
    client = adapter_module._bounded_http_client()
    monkeypatch.setattr(_http, "get_session", lambda: client)
    monkeypatch.setattr(api_module, "get_session", lambda: client)
    adapter = HuggingFaceAdapter(client=HfApi(endpoint="https://test.invalid"), detail_delay=0)
    monkeypatch.setattr(adapter, "_load_card", lambda *args: "card")
    try:
        refs = list(adapter.list(CollectScope(limit=1)))
        adapter.fetch_detail(refs[0])
        # Pagination explicitly sends timeout=None; a client default alone fails this.
        assert len(seen) == len(original_hooks) == 2
        for request in seen:
            assert all(value is not None and 0 < value <= 30 for value in request.extensions["timeout"].values())
            assert "siblings" not in request.url.params.get_list("expand")
        assert seen[0].url.params.get_list("expand") == ["sha", "lastModified"]
        assert "full" not in seen[0].url.params
        assert "/revision/" + "a" * 40 in seen[1].url.path
    finally:
        client.close()


def test_request_budget_stops_sdk_retry_after_timeout(monkeypatch):
    from huggingface_hub import HfApi
    from huggingface_hub.utils import httpx
    from huggingface_hub.utils import _http
    from xiaoyi_catalog.adapters import huggingface as adapter_module
    elapsed = [0.0]
    requests = []
    def transport(request):
        requests.append(request)
        elapsed[0] = 31.0
        raise httpx.ReadTimeout("must not leak token", request=request)
    monkeypatch.setattr(_http, "default_client_factory", lambda: httpx.Client(transport=httpx.MockTransport(transport)))
    client = adapter_module._bounded_http_client()
    monkeypatch.setattr(_http, "get_session", lambda: client)
    monkeypatch.setattr(adapter_module.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(_http.time, "sleep", lambda seconds: None)
    adapter = HuggingFaceAdapter(client=HfApi(endpoint="https://test.invalid"), detail_delay=0)
    try:
        with pytest.raises(TimeoutError):
            list(adapter.list(CollectScope(limit=1)))
        assert len(requests) == 1
    finally:
        client.close()


def test_shorter_timeouts_are_preserved_and_expired_budget_is_not_retried():
    from huggingface_hub.utils import httpx
    from xiaoyi_catalog.adapters import huggingface as module
    request = httpx.Request("GET", "https://test.invalid", extensions={"timeout": {"connect": 2, "read": None, "write": 10, "pool": 60}})
    module._bound_request_timeout(request)
    assert request.extensions["timeout"] == {"connect": 2, "read": 30, "write": 10, "pool": 30}
    token = module._CATALOG_DEADLINE.set(0)
    try:
        with pytest.raises(TimeoutError):
            module._bound_request_timeout(request)
    finally:
        module._CATALOG_DEADLINE.reset(token)
