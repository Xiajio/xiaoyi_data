"""Quiesced, checksum-verified catalog backups and non-destructive restores.

SQLite uses sqlite3.backup (including its separate LangGraph checkpoint database).
PostgreSQL uses pg_dump/pg_restore; passwords never appear in their command arguments.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
from sqlalchemy.engine import make_url


class BackupError(ValueError):
    pass


FORMAT = "xiaoyi-catalog-backup"
FORMAT_VERSION = 1


def _quiesced(value: bool) -> None:
    if not value:
        raise BackupError("必须先停止 API、worker 和导入/派生任务，再显式提供 --quiesced；本工具不会停止服务")


def _links_forbidden(path: Path) -> None:
    """Reject symlinks/junctions before resolving paths, including an existing parent."""
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise BackupError("备份与恢复路径不得包含符号链接或目录联接")
        if item.exists():
            info = item.lstat()
            if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise BackupError("备份与恢复路径不得包含重解析点")


def _path(value: str | Path) -> Path:
    path = Path(value).absolute()
    _links_forbidden(path)
    return path.resolve()


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise BackupError("清单包含无效文件路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("..", ".") for part in path.parts) or path.as_posix() != value:
        raise BackupError("清单路径必须是规范的相对路径，禁止越界")
    return value


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _files(root: Path):
    for child in sorted(root.iterdir()):
        _links_forbidden(child)
        if child.is_dir():
            yield from _files(child)
        elif child.is_file():
            yield child
        else:
            raise BackupError("备份目录中包含不支持的文件类型")


def _copy_exclusive(source: Path, destination: Path) -> None:
    _links_forbidden(source)
    _links_forbidden(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, destination.open("xb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)


def _database(value: str | Path) -> tuple[str, object]:
    text = str(value)
    if "://" not in text:
        return "sqlite", _path(text)
    try:
        url = make_url(text)
    except Exception as exc:
        raise BackupError("数据库地址格式无效") from exc
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:":
            raise BackupError("备份要求持久 SQLite 文件，不能使用内存数据库")
        return "sqlite", _path(url.database)
    if url.get_backend_name() == "postgresql" and url.database:
        return "postgresql", url
    raise BackupError("仅支持持久 SQLite 或 PostgreSQL 数据库")


def _sqlite_copy(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise BackupError("SQLite 源数据库不存在")
    _links_forbidden(source)
    _links_forbidden(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the new path exclusively before SQLite opens it. Never clobber an existing database.
    with destination.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            # LangGraph uses WAL; consolidate the backup into one closed, self-contained database file.
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst.execute("PRAGMA journal_mode=DELETE")
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("SQLite 完整性检查失败")
    except sqlite3.Error as exc:
        raise BackupError("SQLite 备份或完整性检查失败") from exc


def _program(name: str) -> str:
    binary = shutil.which(name)
    if not binary:
        raise BackupError(f"缺少 PostgreSQL 工具 {name}；请安装与服务器版本兼容的 PostgreSQL 客户端后重试")
    return binary


def _pg_environment(url) -> dict[str, str]:
    env = dict(os.environ)
    # No inherited service/model configuration is written into the backup or command line.
    env = {k: v for k, v in env.items() if not k.startswith("XIAOYI_LLM_")}
    if url.password is not None:
        env["PGPASSWORD"] = url.password
    if url.host:
        env["PGHOST"] = url.host
    if url.port:
        env["PGPORT"] = str(url.port)
    if url.username:
        env["PGUSER"] = url.username
    env["PGDATABASE"] = url.database
    for key, target in (("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT"),
                        ("sslcert", "PGSSLCERT"), ("sslkey", "PGSSLKEY"),
                        ("connect_timeout", "PGCONNECT_TIMEOUT")):
        if key in url.query:
            env[target] = str(url.query[key])
    env.setdefault("PGCONNECT_TIMEOUT", "15")
    return env


def _run_pg(argv: list[str], env: dict[str, str]) -> None:
    try:
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=3600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError("PostgreSQL 工具未完成；请检查客户端、连接与运行时间后重试") from exc
    if result.returncode:
        # Do not echo provider stderr, connection strings or credential-bearing process exceptions.
        raise BackupError(f"PostgreSQL 工具执行失败（退出码 {result.returncode}）；请检查连接、权限和客户端版本")


def _pg_create_fresh(url, env) -> None:
    """Connect to the maintenance database and CREATE a new database, never replace one."""
    import psycopg
    from psycopg import sql
    kwargs = {"dbname": "postgres", "connect_timeout": int(env.get("PGCONNECT_TIMEOUT", "15")), "autocommit": True}
    for key, value in (("host", env.get("PGHOST")), ("port", env.get("PGPORT")),
                       ("user", env.get("PGUSER")), ("password", env.get("PGPASSWORD")),
                       ("sslmode", env.get("PGSSLMODE")), ("sslrootcert", env.get("PGSSLROOTCERT")),
                       ("sslcert", env.get("PGSSLCERT")), ("sslkey", env.get("PGSSLKEY"))):
        if value:
            kwargs[key] = value
    try:
        with psycopg.connect(**kwargs) as connection:
            if connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (url.database,)).fetchone():
                raise BackupError("目标 PostgreSQL 数据库已存在；只允许恢复到全新数据库")
            connection.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(url.database)))
    except BackupError:
        raise
    except psycopg.Error as exc:
        raise BackupError("无法创建新的 PostgreSQL 数据库；请检查连接及 CREATEDB 权限") from exc


def create_backup(db: str | Path, storage: str | Path, destination: str | Path, *,
                  quiesced: bool = False, checkpoint_path: str | Path | None = None) -> Path:
    _quiesced(quiesced)
    kind, database = _database(db)
    source = _path(storage)
    target = _path(destination)
    if not source.is_dir():
        raise BackupError("资产存储目录不存在")
    if target.exists():
        raise BackupError("备份目标已存在；请选择全新的目录")
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise BackupError("备份目录与资产存储目录不能相互包含")
    if kind == "sqlite" and (not database.is_file() or database.is_relative_to(target)):
        raise BackupError("SQLite 源数据库不存在或位于备份目标内")
    pg_dump = _program("pg_dump") if kind == "postgresql" else None
    # Persisted storage is content-addressed objects and bundles. Configuration is never backed up.
    for child in source.iterdir():
        _links_forbidden(child)
        if child.name == "staging":
            continue
        if child.name not in {"sha256", "bundles"} or not child.is_dir():
            raise BackupError("资产目录含非标准项；仅支持 sha256/、bundles/ 和被排除的 staging/")
        _links_forbidden(child)
    objects = []
    for namespace in ("sha256", "bundles"):
        folder = source / namespace
        if folder.exists():
            objects.extend(path for path in _files(folder) if not path.name.endswith(".partial"))
    checkpoint = None
    if kind == "sqlite":
        checkpoint = _path(checkpoint_path or os.getenv("XIAOYI_CHECKPOINT_PATH") or str(database) + ".checkpoints.sqlite")
        if checkpoint.exists() and not checkpoint.is_file():
            raise BackupError("检查点路径不是文件")
    target.mkdir(parents=True, exist_ok=False)
    database_file = "database.sqlite" if kind == "sqlite" else "database.dump"
    if kind == "sqlite":
        _sqlite_copy(database, target / database_file)
    else:
        _run_pg([pg_dump, "--format=custom", "--no-owner", "--no-privileges", "--file", str(target / database_file)], _pg_environment(database))
        if not (target / database_file).is_file():
            raise BackupError("pg_dump 未生成备份文件")
    checkpoint_file = None
    if checkpoint is not None and checkpoint.is_file():
        _sqlite_copy(checkpoint, target / "checkpoints.sqlite")
        checkpoint_file = "checkpoints.sqlite"
    for file in objects:
        _copy_exclusive(file, target / "assets" / file.relative_to(source))
    entries = [{"path": file.relative_to(target).as_posix(), "size_bytes": file.stat().st_size, "sha256": _hash(file)}
               for file in _files(target)]
    manifest = {"format": FORMAT, "format_version": FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": {"kind": kind, "path": database_file, "checkpoint": checkpoint_file},
        "files": entries, "quiesced": True, "excluded": ["staging/", "*.partial", "service configuration and credentials"]}
    path = target / "manifest.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    return path


def verify_backup(backup: str | Path) -> dict:
    root = _path(backup)
    if not root.is_dir():
        raise BackupError("备份目录不存在")
    path = root / "manifest.json"
    _links_forbidden(path)
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise BackupError("备份清单超过限制")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("format") != FORMAT or manifest.get("format_version") != FORMAT_VERSION or manifest.get("quiesced") is not True:
            raise BackupError("不支持的备份清单或未声明停服备份")
        database = manifest["database"]
        kind = database["kind"]
        expected_database = "database.sqlite" if kind == "sqlite" else "database.dump"
        if kind not in {"sqlite", "postgresql"} or database["path"] != expected_database:
            raise BackupError("数据库备份描述无效")
        checkpoint = database.get("checkpoint")
        if checkpoint not in {None, "checkpoints.sqlite"} or (kind == "postgresql" and checkpoint):
            raise BackupError("检查点描述无效")
        entries = manifest["files"]
        if not isinstance(entries, list) or len(entries) > 1_000_000:
            raise BackupError("备份文件清单无效或超过限制")
        seen = set()
        for item in entries:
            relative = _relative(item["path"])
            if relative in seen or not (relative in {expected_database, checkpoint} or relative.startswith("assets/sha256/") or relative.startswith("assets/bundles/")):
                raise BackupError("备份清单含重复或不允许的路径")
            seen.add(relative)
            if not isinstance(item["size_bytes"], int) or item["size_bytes"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise BackupError("备份清单的大小或 SHA-256 无效")
            file = root / relative
            _links_forbidden(file)
            if not file.is_file() or file.stat().st_size != item["size_bytes"] or _hash(file) != item["sha256"]:
                raise BackupError(f"备份文件缺失或校验失败：{relative}")
        required = {expected_database} | ({checkpoint} if checkpoint else set())
        if not required.issubset(seen):
            raise BackupError("备份缺少数据库或检查点文件")
        actual = {file.relative_to(root).as_posix() for file in _files(root)} - {"manifest.json"}
        if actual != seen:
            raise BackupError("备份目录内容与清单不一致")
        if kind == "sqlite":
            for relative in required:
                with closing(sqlite3.connect((root / relative).as_uri() + "?mode=ro", uri=True)) as connection:
                    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise BackupError("SQLite 备份完整性检查失败")
        return manifest
    except BackupError:
        raise
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
        raise BackupError("备份清单不可读取或内容无效") from exc


def restore_backup(backup: str | Path, db: str | Path, storage: str | Path, *,
                   quiesced: bool = False, checkpoint_path: str | Path | None = None) -> dict:
    _quiesced(quiesced)
    # Verify every byte and path BEFORE creating either destination or database.
    manifest = verify_backup(backup)
    root = _path(backup)
    kind, database = _database(db)
    if kind != manifest["database"]["kind"]:
        raise BackupError("备份与恢复数据库类型必须相同；本工具不执行数据库迁移")
    target = _path(storage)
    if target == root or target.is_relative_to(root) or root.is_relative_to(target):
        raise BackupError("恢复存储与备份目录不能相互包含")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise BackupError("恢复只允许使用空资产存储目录")
    checkpoint = None
    if kind == "sqlite":
        if database.exists() or any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise BackupError("目标 SQLite 数据库或旁文件已存在，拒绝覆盖")
        if database.is_relative_to(root) or database.is_relative_to(target):
            raise BackupError("目标 SQLite 数据库不能位于备份或资产目录内")
        checkpoint = _path(checkpoint_path or os.getenv("XIAOYI_CHECKPOINT_PATH") or str(database) + ".checkpoints.sqlite")
        if checkpoint == database or checkpoint.is_relative_to(root) or checkpoint.is_relative_to(target):
            raise BackupError("恢复检查点不能与数据库、备份或资产目录冲突")
        if checkpoint.exists() or any(Path(str(checkpoint) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise BackupError("目标检查点数据库已存在，拒绝覆盖")
    else:
        pg_restore = _program("pg_restore")
        env = _pg_environment(database)
        _run_pg([pg_restore, "--list", str(root / manifest["database"]["path"])], env)
        _pg_create_fresh(database, env)
    target.mkdir(parents=True, exist_ok=True)
    if kind == "sqlite":
        _sqlite_copy(root / manifest["database"]["path"], database)
        if manifest["database"].get("checkpoint"):
            _sqlite_copy(root / manifest["database"]["checkpoint"], checkpoint)
    else:
        _run_pg([pg_restore, "--exit-on-error", "--no-owner", "--no-privileges", "--dbname", database.database, str(root / manifest["database"]["path"])], env)
    for item in manifest["files"]:
        if item["path"].startswith("assets/"):
            relative = PurePosixPath(item["path"]).relative_to("assets")
            source, destination = root / item["path"], target / relative
            _copy_exclusive(source, destination)
            if _hash(destination) != item["sha256"]:
                raise BackupError("恢复文件校验失败；新目标保留供排查，原数据未修改")
    return {"database_kind": kind, "files_restored": len(manifest["files"]),
            "checkpoint_restored": bool(manifest["database"].get("checkpoint"))}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="小亿数据停服备份与恢复（绝不覆盖已有数据库/文件）")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    restore = commands.add_parser("restore")
    for command in (backup, restore):
        command.add_argument("--db", default=os.getenv("XIAOYI_CATALOG_DB", "./data/catalog.db"))
        command.add_argument("--storage", default=os.getenv("XIAOYI_STORAGE_ROOT", "./data/assets"))
        command.add_argument("--checkpoint", default=None, help="SQLite 独立检查点路径；默认目录库旁文件")
        command.add_argument("--quiesced", action="store_true", help="确认 API、worker、导入和派生任务已经停止")
    backup.add_argument("--destination", required=True)
    restore.add_argument("--backup", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            path = create_backup(args.db, args.storage, args.destination, quiesced=args.quiesced, checkpoint_path=args.checkpoint)
            print(f"备份完成：{path}")
        else:
            result = restore_backup(args.backup, args.db, args.storage, quiesced=args.quiesced, checkpoint_path=args.checkpoint)
            print(f"恢复完成：{result['database_kind']}，{result['files_restored']} 个验证文件")
        return 0
    except (BackupError, OSError) as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
