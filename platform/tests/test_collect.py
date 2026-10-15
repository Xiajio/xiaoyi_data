"""采集编排测试：幂等、增量水位、版本追加、失败分类。"""

from __future__ import annotations

from sqlalchemy import func, select

from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.collect import classify_error, collect, fingerprint
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.models import Dataset, DatasetVersion, Evidence, Source

from conftest import FakeAdapter, day, make_raw


def _counts(engine):
    with session_scope(engine) as session:
        return {
            "datasets": session.scalar(select(func.count(Dataset.id))),
            "versions": session.scalar(select(func.count(DatasetVersion.id))),
            "evidence": session.scalar(select(func.count(Evidence.id))),
        }


class TestCollect:
    def test_fresh_collect_inserts_datasets_versions_evidence_and_run(self, engine):
        raws = [make_raw("a/ds1", license="mit", tasks=("text-classification",), card="# A"),
                make_raw("a/ds2", license="gpl-3.0", tasks=("image-classification",)),
                make_raw("b/ds3", tags=("modality:3d",), license="cc-by-nc-4.0", card="三维")]
        run = collect(engine, FakeAdapter(raws), CollectScope())
        assert run.status == "completed"
        assert {k: run.stats[k] for k in ("found", "new", "updated", "duplicates", "failed")} == {"found": 3, "new": 3, "updated": 0, "duplicates": 0, "failed": 0}
        assert _counts(engine) == {"datasets": 3, "versions": 3, "evidence": 2}
        with session_scope(engine) as session:
            ds1 = session.scalar(select(Dataset).where(Dataset.source_key == "a/ds1"))
            assert ds1.license_class == "permissive" and ds1.modalities == ["text"] and ds1.modality_source == "task-map"
            ds3 = session.scalar(select(Dataset).where(Dataset.source_key == "b/ds3"))
            assert ds3.modalities == ["3d"] and ds3.modality_source == "tag" and ds3.license_class == "research-only"
            source = session.scalar(select(Source))
            assert source.kind == "fake" and source.endpoint == "https://fake.local"

    def test_recollect_is_idempotent_without_watermark(self, engine):
        raws = [make_raw("a/ds1", card="x"), make_raw("a/ds2", card="y")]
        collect(engine, FakeAdapter(raws), CollectScope())
        second = collect(engine, FakeAdapter(raws), CollectScope())
        assert second.stats["duplicates"] == 2 and second.stats["new"] == 0
        assert _counts(engine) == {"datasets": 2, "versions": 2, "evidence": 2}

    def test_watermark_skips_unchanged_items(self, engine):
        raws = [make_raw("a/ds1", last_modified=day(3, 1)), make_raw("a/ds2", last_modified=day(3, 5))]
        collect(engine, FakeAdapter(raws), CollectScope())
        second = collect(engine, FakeAdapter(raws), CollectScope())
        assert second.stats["skipped_stale"] == 2 and second.stats["found"] == 0
        newer = [raws[0], make_raw("a/ds2", revision="rev-2", last_modified=day(4, 1))]
        third = collect(engine, FakeAdapter(newer), CollectScope())
        assert third.stats["skipped_stale"] == 1 and third.stats["updated"] == 1
        assert _counts(engine)["datasets"] == 2

    def test_watermark_is_scoped_and_does_not_starve_other_scopes(self, engine):
        scope_a = CollectScope(task_categories=("text-classification",))
        collect(engine, FakeAdapter([make_raw("a/ds1", last_modified=day(9, 1))]), scope_a)
        scope_b = CollectScope(task_categories=("image-classification",))
        run = collect(engine, FakeAdapter([make_raw("b/ds1", last_modified=day(1, 1))]), scope_b)
        assert run.stats["new"] == 1 and run.stats["skipped_stale"] == 0

    def test_truncated_result_holds_watermark_and_recollect_dedupes(self, engine):
        scope = CollectScope(limit=2)
        raws = [make_raw("a/ds1", last_modified=day(3, 1)), make_raw("a/ds2", last_modified=day(3, 5))]
        first = collect(engine, FakeAdapter(raws), scope)
        assert first.stats["watermark_advanced"] is False
        second = collect(engine, FakeAdapter(raws), scope)
        assert second.stats["skipped_stale"] == 0 and second.stats["duplicates"] == 2
        assert _counts(engine)["datasets"] == 2

    def test_new_revision_updates_in_place_and_appends_version(self, engine):
        collect(engine, FakeAdapter([make_raw("a/ds1", card="v1")]), CollectScope())
        run = collect(engine, FakeAdapter([make_raw("a/ds1", revision="rev-2", card="v2")]), CollectScope())
        assert run.stats["updated"] == 1 and run.stats["new"] == 0
        with session_scope(engine) as session:
            dataset = session.scalar(select(Dataset))
            assert dataset.fingerprint == fingerprint("fake", "a/ds1", "rev-2") and dataset.card_text == "v2"
            versions = session.scalars(select(DatasetVersion)).all()
            assert sorted(v.revision for v in versions) == ["rev-1", "rev-2"]

    def test_item_failure_is_classified_and_does_not_abort_batch(self, engine):

        class Response:  # noqa: D401 - 最小伪造
            status_code = 429

        class RateLimited(Exception):
            response = Response()

        raws = [make_raw("a/ok", card="x"), make_raw("a/boom")]
        run = collect(engine, FakeAdapter(raws, failures={"a/boom": RateLimited("slow down")}), CollectScope())
        assert run.status == "completed"
        assert run.stats["new"] == 1 and run.stats["failed"] == 1
        assert run.stats["failures"][0]["error_kind"] == "rate_limit"
        assert _counts(engine)["datasets"] == 1

    def test_list_failure_marks_run_failed(self, engine):
        run = collect(engine, FakeAdapter([], list_error=TimeoutError("timed out")), CollectScope())
        assert run.status == "failed" and run.error_kind == "timeout"
        assert run.finished_at is not None


class TestClassifyError:
    def _http(self, status: int) -> Exception:
        return type("FakeHttpError", (Exception,), {"response": type("R", (), {"status_code": status})()})()

    def test_status_based(self):
        assert classify_error(self._http(429)) == "rate_limit"
        assert classify_error(self._http(403)) == "permission"
        assert classify_error(self._http(401)) == "permission"
        assert classify_error(self._http(500)) == "other"

    def test_name_based(self):
        assert classify_error(TimeoutError()) == "timeout"
        assert classify_error(ConnectionError()) == "network"
        assert classify_error(ValueError()) == "other"
