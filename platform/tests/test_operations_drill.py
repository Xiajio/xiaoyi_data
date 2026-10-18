"""The recovery drill must restore a real approved, independently loadable bundle."""
import hashlib
import json
import os
import subprocess
import zipfile

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from xiaoyi_catalog.auth import create_user, mint_session
from xiaoyi_catalog.db import create_engine_for, session_scope
from xiaoyi_catalog.downloads import sha256_file
from xiaoyi_catalog.models import Audit, Review, Snapshot
from xiaoyi_catalog.models_assets import AuthUser, DownloadRequest, ResearchBundle
from xiaoyi_catalog.operations_backup import create_backup, restore_backup
from xiaoyi_catalog.operations_drill import _create_bundle_fixture, _fixture_actor, _verify_restored_bundle


@pytest.fixture
def bundle_fixture(engine, tmp_path):
    root = tmp_path / "drill"
    root.mkdir()
    fixture = _create_bundle_fixture(engine, root)
    return engine, root, fixture


def test_real_bundle_restores_approval_hash_and_independent_loader(engine, tmp_path, monkeypatch):
    # The helper must restore configured paths, never touch the live storage root.
    live = tmp_path / "must-not-be-created"
    monkeypatch.setenv("XIAOYI_STORAGE_ROOT", str(live))
    monkeypatch.setenv("XIAOYI_IMPORT_ROOT", str(live / "inbox"))
    root = tmp_path / "drill"
    root.mkdir()
    fixture = _create_bundle_fixture(engine, root)
    assert os.environ["XIAOYI_STORAGE_ROOT"] == str(live)
    assert os.environ["XIAOYI_IMPORT_ROOT"] == str(live / "inbox")
    assert not live.exists()
    backup = root / "backup"
    create_backup(engine.url.database, root / "source-assets", backup, quiesced=True)
    db, assets = root / "restored.db", root / "restored-assets"
    restore_backup(backup, db, assets, quiesced=True)
    restored = create_engine_for(db)
    try:
        report = _verify_restored_bundle(restored, assets, root / "independent-load", fixture)
        assert report["research_bundle_count"] == 1
        bundle = report["research_bundles"][0]
        assert bundle["sha256"] == fixture["sha256"] == sha256_file(assets / fixture["storage_key"])
        expected = json.dumps(fixture["expected_rows"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        assert bundle["independent_load"] == {"verified": True, "independent_process": True,
            "asset_count": 1, "row_count": 2, "rows_sha256": hashlib.sha256(expected).hexdigest()}
        with session_scope(restored) as session:
            request = session.scalar(select(DownloadRequest))
            assert request.status == "completed" and request.requested_by == "drill-researcher"
            assert request.decided_by == "drill-reviewer" and request.approved_hash == request.manifest_hash
            review = session.scalar(select(Review))
            assert review.history_status == "native" and review.target_content_hash
            assert review.decision == "confirmed" and review.role == "reviewer"
            assert session.get(Snapshot, fixture["snapshot_id"]).status == "approved"
            assert session.scalar(select(AuthUser).where(AuthUser.username == "drill-reviewer")).kind == "human"
            actions = set(session.scalars(select(Audit.action).where(Audit.actor == "drill-reviewer")))
            assert {"download_approved", "pairing_review", "snapshot_freeze", "snapshot_approve"} <= actions
    finally:
        restored.dispose()


@pytest.mark.parametrize("inner_corruption", [False, True])
def test_bundle_recovery_rejects_zip_and_embedded_asset_tampering(bundle_fixture, inner_corruption):
    engine, root, fixture = bundle_fixture
    assets = root / "source-assets"
    path = assets / fixture["storage_key"]
    if not inner_corruption:
        path.write_bytes(path.read_bytes() + b"tampered")
        with pytest.raises(RuntimeError, match="SHA-256"):
            _verify_restored_bundle(engine, assets, root / "load-invalid", fixture)
        assert not (root / "load-invalid").exists()
    else:
        # Even if an operator recomputes the ZIP checksum, embedded asset hashes
        # must still fail in the bundle's standalone verifier.
        with zipfile.ZipFile(path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        data_name = next(name for name in members if name.startswith("data/"))
        members[data_name] = members[data_name].replace(b"restore alpha", b"damaged alpha")
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        fixture["sha256"] = sha256_file(path)
        with session_scope(engine) as session:
            session.get(ResearchBundle, fixture["bundle_id"]).sha256 = fixture["sha256"]
        with pytest.raises(subprocess.CalledProcessError) as error:
            _verify_restored_bundle(engine, assets, root / "load-invalid", fixture)
        assert "Checksum mismatch" in error.value.stderr


def test_drill_fixture_uses_persisted_human_authority(engine):
    with session_scope(engine) as session:
        agent = create_user(session, "agent-reviewer", "", "reviewer", "agent")
        reader = create_user(session, "human-reader", "fixture-password-123", "reader")
        agent_token, reader_token = mint_session(session, agent), mint_session(session, reader)
    for token, status in ((agent_token, 403), (reader_token, 403), ("invalid-token", 401)):
        with pytest.raises(HTTPException) as error:
            _fixture_actor(engine, token, {"reviewer", "admin"})
        assert error.value.status_code == status
