from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from xiaoyi_catalog.dashboard import dashboard_stats, modality_filter
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.models import Audit, Dataset, DatasetVersion, Enrichment, Evidence, Idea, Pairing, Snapshot, Source
from xiaoyi_catalog.models_assets import Asset, DownloadRequest, ResearchBundle
from xiaoyi_catalog.models_collection import CollectionJob, CollectionSchedule
from xiaoyi_catalog.service import create_app

NOW = datetime(2026, 10, 16, 4, tzinfo=timezone.utc)
BEFORE_MIDNIGHT = datetime(2026, 10, 15, 15, 59, 59, tzinfo=timezone.utc)
MIDNIGHT = datetime(2026, 10, 15, 16, tzinfo=timezone.utc)


def add_dataset(session, source, key, *, modalities=None, status="active", first_seen=NOW, license_class="unknown"):
    row = Dataset(source_id=source.id, source_key=key, fingerprint=key, modalities=modalities or [],
        status=status, license_class=license_class, first_seen_at=first_seen, created_at=NOW-timedelta(days=5))
    session.add(row)
    session.flush()
    return row


def add_version(session, dataset, revision, *, kind="source", created=NOW, parent=None):
    row = DatasetVersion(dataset_id=dataset.id, revision=revision, kind=kind, parent_version_id=parent,
        created_at=created, metadata_json={}, provenance_status="legacy_unverified")
    session.add(row)
    session.flush()
    return row


def test_dashboard_counts_active_rows_without_join_multiplication(engine):
    with session_scope(engine) as session:
        source = Source(kind="manual")
        session.add(source)
        session.flush()
        active = add_dataset(session, source, "active", modalities=["text", "text", "image", "custom-sensor"], license_class="permissive")
        hidden = add_dataset(session, source, "archived", modalities=["audio"], status="archived")
        versions = [add_version(session, active, "v1"), add_version(session, active, "v2")]
        versions.append(add_version(session, active, "derived", kind="derived", parent=versions[1].id))
        hidden_version = add_version(session, hidden, "hidden")
        for version in versions + [hidden_version]:
            session.add_all([Evidence(dataset_id=version.dataset_id, dataset_version_id=version.id, kind="metadata"),
                Evidence(dataset_id=version.dataset_id, dataset_version_id=version.id, kind="card")])
            session.add_all([Enrichment(dataset_id=version.dataset_id, dataset_version_id=version.id, kind="schema", status="candidate"),
                Enrichment(dataset_id=version.dataset_id, dataset_version_id=version.id, kind="quality", status="confirmed"),
                Enrichment(dataset_id=version.dataset_id, dataset_version_id=version.id, kind="schema", status="superseded")])
        session.add(Evidence(dataset_id=active.id, kind="metadata"))  # historical evidence without version binding
        session.add(Enrichment(dataset_id=active.id, kind="schema", status="needs-info"))
        idea = Idea(external_id="dashboard-test")
        session.add(idea)
        session.flush()
        session.add_all([Pairing(idea_id=idea.id, dataset_version_id=versions[0].id, status="pending"),
            Pairing(idea_id=idea.id, dataset_version_id=versions[1].id, status="needs-info"),
            Pairing(idea_id=idea.id, dataset_version_id=hidden_version.id, status="pending")])
        for index, version in enumerate([versions[0], hidden_version]):
            request = DownloadRequest(dataset_version_id=version.id, requested_by="test", manifest_hash=str(index), max_bytes=1000, status="pending")
            session.add(request)
            session.flush()
            session.add(Asset(dataset_version_id=version.id, download_request_id=request.id,
                path="part.csv", sha256="a"*64, size_bytes=100, storage_key="shared-storage"))
        snapshot = Snapshot(idea_id=idea.id, version=1, status="approved")
        session.add(snapshot)
        session.flush()
        session.add(ResearchBundle(snapshot_id=snapshot.id, manifest={}, sha256="b"*64, storage_key="bundle"))
    with session_scope(engine) as session:
        result = dashboard_stats(session, now=NOW)
        assert result["totals"] == {"datasets": 1, "versions": 3, "evidence": 7, "enrichment_pending": 4,
            "pairings_pending": 2, "downloads_pending": 1, "assets": 1, "asset_bytes": 100, "snapshots_approved": 1, "bundles": 1}
        modalities = {row["key"]: row["count"] for row in result["modalities"]}
        assert modalities["text"] == 1 and modalities["image"] == 1 and modalities["custom-sensor"] == 1 and modalities["audio"] == 0
        review = {row["status"]: row["count"] for row in result["review"]}
        assert review["candidate"] == 3 and review["confirmed"] == 3 and review["needs-info"] == 1
        assert "pending" not in review and "superseded" not in review
        assert session.scalar(select(func.count(Audit.id))) == 0


def test_trend_uses_first_seen_and_shanghai_dates_and_excludes_future(engine):
    with session_scope(engine) as session:
        source = Source(kind="manual")
        session.add(source)
        session.flush()
        first = add_dataset(session, source, "before-midnight", first_seen=BEFORE_MIDNIGHT)
        second = add_dataset(session, source, "at-midnight", first_seen=MIDNIGHT)
        future = add_dataset(session, source, "future", first_seen=NOW + timedelta(hours=1))
        hidden = add_dataset(session, source, "archived", status="archived", first_seen=MIDNIGHT)
        add_version(session, first, "v1", created=BEFORE_MIDNIGHT)
        add_version(session, first, "v2", created=MIDNIGHT)
        add_version(session, second, "v1", created=MIDNIGHT)
        add_version(session, future, "future", created=NOW + timedelta(hours=1))
        add_version(session, hidden, "hidden", created=MIDNIGHT)
    with session_scope(engine) as session:
        result = dashboard_stats(session, now=NOW)
        assert result["generated_at"] == NOW.isoformat() and result["timezone"] == "Asia/Shanghai"
        points = result["trend"]["points"]
        assert len(points) == 14 and result["trend"]["start_date"] == "2026-10-03" and result["trend"]["end_date"] == "2026-10-16"
        assert points[-2] == {"date": "2026-10-15", "datasets": 1, "versions": 1}
        assert points[-1] == {"date": "2026-10-16", "datasets": 1, "versions": 2}
        assert sum(point["datasets"] for point in points) == 2
        assert all(point["datasets"] == point["versions"] == 0 for point in points[:-2])


def test_empty_catalog_and_schedule_summary_are_read_only(engine):
    with session_scope(engine) as session:
        session.add_all([CollectionJob(actor="a", scope={}, status="completed", stage="finished", created_at=NOW-timedelta(days=1)),
            CollectionJob(actor="a", scope={}, status="failed", stage="finished", error="safe failure", created_at=NOW)])
        session.add_all([CollectionSchedule(name="Paused", scope={}, enabled=False, interval_minutes=15, next_run_at=NOW-timedelta(days=1), created_by="a"),
            CollectionSchedule(name="Daily", scope={}, enabled=True, interval_minutes=1440, next_run_at=NOW+timedelta(days=1), created_by="a"),
            CollectionSchedule(name="Sooner", scope={}, enabled=True, interval_minutes=60, next_run_at=NOW+timedelta(hours=1), created_by="a")])
    with session_scope(engine) as session:
        result = dashboard_stats(session, days=30, now=NOW)
        assert set(result["totals"].values()) == {0}
        assert len(result["trend"]["points"]) == 30
        assert all(point["datasets"] == point["versions"] == 0 for point in result["trend"]["points"])
        collection = result["collection"]
        assert collection["jobs_by_status"]["failed"] == collection["jobs_by_status"]["completed"] == 1
        assert collection["latest_job"]["error"] == "safe failure"
        assert collection["enabled_schedules"] == 2 and collection["next_schedule_name"] == "Sooner"
        assert collection["next_run_at"] == (NOW+timedelta(hours=1)).isoformat()
        assert session.scalar(select(func.count(CollectionJob.id))) == 2  # no scheduler tick on GET


def test_http_dashboard_validation_and_unknown_modality_drilldown(tmp_path):
    app = create_app(str(tmp_path / "dashboard-api.db"), dev_auth=True)
    with session_scope(app.state.engine) as session:
        source = Source(kind="manual")
        session.add(source)
        session.flush()
        for key, values in [("empty", []), ("blank", ["", "  "]), ("explicit", ["unknown"]),
                            ("known", ["Text", "text", "custom-sensor"]), ("hidden", [])]:
            add_dataset(session, source, key, modalities=values, status="archived" if key == "hidden" else "active")
    with TestClient(app) as client:
        assert client.get("/api/overview/dashboard?days=7").status_code == 422
        assert client.get("/api/overview/dashboard?days=30").json()["trend"]["days"] == 30
        result = client.get("/api/overview/dashboard").json()
        modalities = {row["key"]: row["count"] for row in result["modalities"]}
        assert modalities["unknown"] == 3 and modalities["text"] == 1
        assert client.get("/api/datasets?modality=unknown").json()["total"] == 3
        assert client.get("/api/datasets?modality=text").json()["total"] == 1
        assert client.get("/api/datasets?modality=custom-sensor").json()["total"] == 1
        assert client.get("/api/overview").status_code == 200
        assert client.get("/api/overview").json()["datasets"] == 4


def test_postgres_unknown_modality_uses_json_array_expansion():
    from sqlalchemy.dialects import postgresql
    statement = select(Dataset.id).where(modality_filter(Dataset.modalities, "unknown", "postgresql"))
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "json_array_elements" in compiled and "json_typeof" in compiled and "#>>" in compiled
    assert "LIKE" not in compiled


def test_drilldown_matches_invalid_labels_and_blank_licenses(tmp_path):
    app = create_app(str(tmp_path / "invalid-labels.db"), dev_auth=True)
    with session_scope(app.state.engine) as session:
        source = Source(kind="manual")
        session.add(source)
        session.flush()
        for key, labels in [("number", [123]), ("object", {"key": "text"}),
                            ("blank", ["", " "]), ("boolean", [True]), ("mixed", ["image", 123])]:
            add_dataset(session, source, key, modalities=labels, license_class="   ")
        add_dataset(session, source, "trimmed", modalities=["text"], license_class=" permissive ")
    with TestClient(app) as client:
        dashboard = client.get("/api/overview/dashboard").json()
        modalities = {row["key"]: row["count"] for row in dashboard["modalities"]}
        assert modalities["unknown"] == 4 and modalities["text"] == 1 and modalities["image"] == 1
        assert client.get("/api/datasets?modality=unknown").json()["total"] == 4
        assert client.get("/api/datasets?modality=123").json()["total"] == 0
        assert client.get("/api/datasets?modality=text").json()["total"] == 1
        assert client.get("/api/datasets?license_class=unknown").json()["total"] == 5
        assert client.get("/api/datasets?license_class=permissive").json()["total"] == 1
        assert {row["key"]: row["count"] for row in dashboard["licenses"]}["unknown"] == 5


def test_psycopg_unknown_modality_binds_json_path_as_text_array():
    from sqlalchemy.dialects.postgresql.psycopg import dialect
    statement = select(Dataset.id).where(modality_filter(Dataset.modalities, "unknown", "postgresql"))
    compiled = str(statement.compile(dialect=dialect()))
    assert "#>> CAST(" in compiled and "AS TEXT[]" in compiled
    assert "#>> %(" not in compiled  # psycopg otherwise binds a scalar VARCHAR, which #>> rejects.


def test_dashboard_requires_authentication(tmp_path):
    app = create_app(str(tmp_path / "authenticated-dashboard.db"), dev_auth=False)
    with TestClient(app) as client:
        response = client.get("/api/overview/dashboard")
        assert response.status_code == 401
        assert "totals" not in response.json()
