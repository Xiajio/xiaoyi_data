from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import zipfile
import pytest
from xiaoyi_catalog import operations_backup as backup


@pytest.fixture
def source(tmp_path):
    db = tmp_path / "catalog.db"
    checkpoint = Path(str(db) + ".checkpoints.sqlite")
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE research (id INTEGER PRIMARY KEY, goal TEXT)")
        connection.execute("INSERT INTO research VALUES (1, 'same research inputs')")
    with sqlite3.connect(checkpoint) as connection:
        connection.execute("CREATE TABLE checkpoints (thread_id TEXT, state TEXT)")
        connection.execute("INSERT INTO checkpoints VALUES ('research-run-1', '{\"next\":\"assess\"}')")
    storage = tmp_path / "assets"
    (storage / "sha256" / "ab").mkdir(parents=True)
    (storage / "sha256" / "ab" / "fixture").write_bytes(b"text,label\nhello,1\n")
    (storage / "bundles").mkdir()
    with zipfile.ZipFile(storage / "bundles" / "research.zip", "w") as archive:
        archive.writestr("manifest.json", '{"format_version":1}')
    (storage / "staging").mkdir()
    (storage / "staging" / ".env").write_text("XIAOYI_LLM_API_KEY=must-not-copy")
    (storage / "sha256" / "ab" / "interrupted.partial").write_bytes(b"unfinished")
    return db, checkpoint, storage


def test_sqlite_backup_restore_drill_with_assets_bundle_and_checkpoint(source, tmp_path, monkeypatch):
    db, checkpoint, storage = source
    monkeypatch.setenv("XIAOYI_LLM_API_KEY", "environment-secret-never-written")
    target = tmp_path / "backup"
    manifest_path = backup.create_backup(db, storage, target, quiesced=True)
    manifest = backup.verify_backup(target)
    assert manifest_path == target / "manifest.json"
    assert manifest["database"]["checkpoint"] == "checkpoints.sqlite"
    assert len(manifest["files"]) == 4
    assert not (target / "assets" / "staging").exists()
    assert not any(path.name.endswith(".partial") for path in target.rglob("*"))
    assert "environment-secret-never-written" not in manifest_path.read_text(encoding="utf-8")
    restored_db = tmp_path / "restored" / "catalog.db"
    restored_assets = tmp_path / "restored" / "assets"
    result = backup.restore_backup(target, restored_db, restored_assets, quiesced=True)
    assert result["checkpoint_restored"] is True
    with sqlite3.connect(restored_db) as connection:
        assert connection.execute("SELECT goal FROM research").fetchone()[0] == "same research inputs"
        connection.execute("UPDATE research SET goal='only restored copy'")
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT goal FROM research").fetchone()[0] == "same research inputs"
    with sqlite3.connect(str(restored_db) + ".checkpoints.sqlite") as connection:
        assert connection.execute("SELECT state FROM checkpoints").fetchone()[0] == '{"next":"assess"}'
    for namespace in ("sha256", "bundles"):
        for file in (restored_assets / namespace).rglob("*"):
            if file.is_file():
                assert file.read_bytes() == (storage / file.relative_to(restored_assets)).read_bytes()


def test_corrupted_backup_is_rejected_before_any_destination_write(source, tmp_path):
    db, _, storage = source
    target = tmp_path / "backup"
    backup.create_backup(db, storage, target, quiesced=True)
    damaged = target / "assets" / "sha256" / "ab" / "fixture"
    damaged.write_bytes(b"tampered")
    restored_db, restored_assets = tmp_path / "new.db", tmp_path / "new-assets"
    with pytest.raises(backup.BackupError, match="校验"):
        backup.restore_backup(target, restored_db, restored_assets, quiesced=True)
    assert not restored_db.exists() and not restored_assets.exists()


def test_explicit_quiesced_confirmation_is_required(source, tmp_path):
    db, _, storage = source
    with pytest.raises(backup.BackupError, match="quiesced"):
        backup.create_backup(db, storage, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
    with pytest.raises(backup.BackupError, match="quiesced"):
        backup.restore_backup(tmp_path / "missing", tmp_path / "new.db", tmp_path / "new-assets")


def test_never_overwrites_database_storage_or_backup_directory(source, tmp_path):
    db, _, storage = source
    target = tmp_path / "backup"
    backup.create_backup(db, storage, target, quiesced=True)
    original = db.read_bytes()
    with pytest.raises(backup.BackupError, match="已存在"):
        backup.create_backup(db, storage, target, quiesced=True)
    with pytest.raises(backup.BackupError, match="已存在"):
        backup.restore_backup(target, db, tmp_path / "new-assets", quiesced=True)
    assert db.read_bytes() == original
    new_db = tmp_path / "new.db"
    with pytest.raises(backup.BackupError, match="空资产"):
        backup.restore_backup(target, new_db, storage, quiesced=True)
    assert not new_db.exists()


def test_restore_rejects_existing_checkpoint_without_touching_targets(source, tmp_path):
    db, _, storage = source
    target = tmp_path / "backup"
    backup.create_backup(db, storage, target, quiesced=True)
    new_db = tmp_path / "new.db"
    Path(str(new_db) + ".checkpoints.sqlite").write_bytes(b"existing")
    with pytest.raises(backup.BackupError, match="检查点"):
        backup.restore_backup(target, new_db, tmp_path / "new-assets", quiesced=True)
    assert not new_db.exists()


def test_traversal_in_manifest_is_rejected(source, tmp_path):
    db, _, storage = source
    target = tmp_path / "backup"
    backup.create_backup(db, storage, target, quiesced=True)
    path = target / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../outside.db"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(backup.BackupError, match="路径"):
        backup.restore_backup(target, tmp_path / "new.db", tmp_path / "new-assets", quiesced=True)
    assert not (tmp_path / "new.db").exists()


def test_configuration_is_not_accidentally_archived(source, tmp_path):
    db, _, storage = source
    (storage / ".env").write_text("SECRET=do-not-archive")
    with pytest.raises(backup.BackupError, match="非标准"):
        backup.create_backup(db, storage, tmp_path / "backup", quiesced=True)
    assert not (tmp_path / "backup").exists()


def test_symlink_is_rejected_when_platform_permits_creation(source, tmp_path):
    db, _, storage = source
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    link = storage / "sha256" / "ab" / "link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create symlinks")
    with pytest.raises(backup.BackupError, match="链接"):
        backup.create_backup(db, storage, tmp_path / "backup", quiesced=True)


def test_source_and_backup_directories_must_not_overlap(source):
    db, _, storage = source
    with pytest.raises(backup.BackupError, match="包含"):
        backup.create_backup(db, storage, storage / "backup", quiesced=True)


def test_postgres_missing_tools_fails_clearly_before_writing(source, tmp_path, monkeypatch):
    _, _, storage = source
    monkeypatch.setattr(backup.shutil, "which", lambda name: None)
    with pytest.raises(backup.BackupError, match="pg_dump"):
        backup.create_backup("postgresql://catalog:password@localhost/catalog", storage, tmp_path / "backup", quiesced=True)
    assert not (tmp_path / "backup").exists()


def test_postgres_password_only_in_environment_and_restore_requests_new_db(source, tmp_path, monkeypatch):
    _, _, storage = source
    calls = []
    monkeypatch.setattr(backup.shutil, "which", lambda name: name)
    monkeypatch.setenv("XIAOYI_LLM_API_KEY", "model-secret")
    def command(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        if argv[0] == "pg_dump":
            Path(argv[argv.index("--file") + 1]).write_bytes(b"PGDMP-fixture")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(backup.subprocess, "run", command)
    fresh = []
    monkeypatch.setattr(backup, "_pg_create_fresh", lambda url, env: fresh.append(url.database))
    target = tmp_path / "backup"
    backup.create_backup("postgresql+psycopg://catalog:super-secret@localhost/original", storage, target, quiesced=True)
    result = backup.restore_backup(target, "postgresql://catalog:restore-secret@localhost/recovered", tmp_path / "new-assets", quiesced=True)
    assert result["database_kind"] == "postgresql" and fresh == ["recovered"]
    assert all("super-secret" not in " ".join(argv) and "restore-secret" not in " ".join(argv) for argv, env in calls)
    assert calls[0][1]["PGPASSWORD"] == "super-secret"
    assert calls[-1][1]["PGPASSWORD"] == "restore-secret"
    assert all("XIAOYI_LLM_API_KEY" not in env for _, env in calls)
    assert "--dbname" in calls[-1][0] and "recovered" in calls[-1][0]
    assert all("--clean" not in argv for argv, _ in calls)
    assert "secret" not in (target / "manifest.json").read_text(encoding="utf-8")


def test_cli_missing_quiesced_returns_nonzero_without_mutation(source, tmp_path, capsys):
    db, _, storage = source
    result = backup.main(["backup", "--db", str(db), "--storage", str(storage), "--destination", str(tmp_path / "backup")])
    assert result == 2 and "quiesced" in capsys.readouterr().out
    assert not (tmp_path / "backup").exists()

def test_langgraph_checkpoint_resumes_after_restore(source, tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import StateGraph, START, END
    db, _, storage = source
    live_checkpoint = tmp_path / "graph.sqlite"
    def make_graph(checkpointer, *, pause=False):
        builder = StateGraph(dict)
        builder.add_node("plan", lambda state: {"value": state["value"] + 1})
        builder.add_node("finish", lambda state: {"value": state["value"] * 10})
        builder.add_edge(START, "plan")
        builder.add_edge("plan", "finish")
        builder.add_edge("finish", END)
        return builder.compile(checkpointer=checkpointer, interrupt_after=["plan"] if pause else [])
    config = {"configurable": {"thread_id": "research-run-backup-drill"}}
    with SqliteSaver.from_conn_string(str(live_checkpoint)) as saver:
        graph = make_graph(saver, pause=True)
        graph.invoke({"value": 2}, config)
        assert graph.get_state(config).next == ("finish",)
    destination = tmp_path / "backup"
    backup.create_backup(db, storage, destination, quiesced=True, checkpoint_path=live_checkpoint)
    recovered_checkpoint = tmp_path / "recovered-graph.sqlite"
    backup.restore_backup(destination, tmp_path / "recovered.db", tmp_path / "recovered-assets", quiesced=True,
                          checkpoint_path=recovered_checkpoint)
    with SqliteSaver.from_conn_string(str(recovered_checkpoint)) as saver:
        graph = make_graph(saver)
        assert graph.get_state(config).next == ("finish",)
        assert graph.invoke(None, config) == {"value": 30}
        assert graph.get_state(config).next == ()
    with SqliteSaver.from_conn_string(str(live_checkpoint)) as saver:
        assert make_graph(saver).get_state(config).next == ("finish",)
