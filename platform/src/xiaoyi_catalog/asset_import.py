"""Approved local file imports and deterministic CSV/JSONL derivations.

Local paths are CLI-only, confined to XIAOYI_IMPORT_ROOT. Approval binds content hashes,
never a mutable pathname. Derived assets keep the original dataset's current source version.
"""
from __future__ import annotations
from copy import deepcopy
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from sqlalchemy import select, func
from .db import session_scope
from .models import Dataset, DatasetVersion, Source, Evidence, Audit
from .models_assets import Asset, DownloadRequest, QualityReport
from .downloads import DownloadError, digest_json, safe_path, sha256_file, storage_root
from .profiling import profile_file


def request_local_import(session, payload: dict, actor: str) -> DownloadRequest:
    version = session.get(DatasetVersion, int(payload["dataset_version_id"]))
    if version is None or version.provenance_status != "verified":
        raise DownloadError("人工来源版本未验证")
    dataset = session.get(Dataset, version.dataset_id)
    if session.get(Source, dataset.source_id).kind != "manual":
        raise DownloadError("本地导入只适用于人工登记来源")
    metadata = version.metadata_json
    if not metadata.get("license") or metadata.get("license_class") == "unknown":
        raise DownloadError("许可依据缺失，不能申请导入")
    files = []
    for item in payload.get("files", []):
        name = safe_path(str(item["path"]))
        size = int(item["size"])
        digest = str(item["sha256"]).lower()
        if size < 0 or not re.fullmatch("[0-9a-f]{64}", digest):
            raise DownloadError("导入清单必须包含有效大小和 SHA-256")
        files.append({"path": name, "size": size, "sha256": digest})
    if not files or len(files) > 10000 or len({f["path"] for f in files}) != len(files):
        raise DownloadError("导入清单需含 1 到 10000 个不重名文件")
    total = sum(f["size"] for f in files)
    limit = int(payload.get("max_bytes", total))
    if limit <= 0 or total > limit or limit > int(os.environ.get("XIAOYI_DOWNLOAD_MAX_BYTES", str(10*1024**3))):
        raise DownloadError("导入体积超过允许上限")
    manifest = {"acquisition": "local", "dataset_version_id": version.id, "revision": version.revision,
                "files": sorted(files, key=lambda f: f["path"]), "license": metadata["license"],
                "source_url": metadata["url"], "metadata_hash": digest_json(metadata)}
    fingerprint = digest_json(manifest)
    prior = session.scalar(select(DownloadRequest).where(DownloadRequest.manifest_hash == fingerprint,
        DownloadRequest.requested_by == actor, DownloadRequest.status.in_(["pending", "approved", "running", "completed"])))
    if prior:
        return prior
    request = DownloadRequest(dataset_version_id=version.id, requested_by=actor, manifest=manifest,
                              manifest_hash=fingerprint, max_bytes=limit, estimated_bytes=total)
    session.add(request)
    session.flush()
    session.add(Audit(actor=actor, action="local_import_requested", detail=f"request={request.id}; manifest={fingerprint}"))
    return request


def _store_file(source: Path, expected_hash: str, root: Path) -> str:
    key = f"sha256/{expected_hash[:2]}/{expected_hash}"
    destination = root / key
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(destination) != expected_hash:
            raise DownloadError("内容存储校验失败")
        return key
    handle, temporary_name = tempfile.mkstemp(prefix="import-", suffix=".partial", dir=destination.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        if sha256_file(temporary) != expected_hash:
            raise DownloadError("文件内容在导入期间变化")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return key


def process_local_import(engine, request_id: int, files: dict[str, str | Path]) -> int:
    inbox = Path(os.environ.get("XIAOYI_IMPORT_ROOT", "./data/import")).resolve()
    root = storage_root()
    with session_scope(engine) as session:
        request = session.get(DownloadRequest, request_id, with_for_update=True)
        if request is None or request.manifest.get("acquisition") != "local":
            raise DownloadError("本地导入申请不存在")
        if request.status == "completed":
            return request.id
        if request.status not in {"approved", "running"} or not request.decided_by:
            raise DownloadError("本地导入必须先由人工批准")
        if request.approved_hash != request.manifest_hash or digest_json(request.manifest) != request.approved_hash:
            raise DownloadError("批准清单已改变")
        version = session.get(DatasetVersion, request.dataset_version_id)
        if digest_json(version.metadata_json) != request.manifest["metadata_hash"]:
            raise DownloadError("版本依据已变化")
        manifest = deepcopy(request.manifest)
        if set(files) != {f["path"] for f in manifest["files"]}:
            raise DownloadError("导入文件必须与批准清单完全一致")
        sources = {}
        missing_bytes = 0
        for item in manifest["files"]:
            source = Path(files[item["path"]]).resolve()
            if not source.is_relative_to(inbox) or not source.is_file():
                raise DownloadError("文件必须位于配置的导入目录内")
            if source.stat().st_size != item["size"] or sha256_file(source) != item["sha256"]:
                raise DownloadError("导入文件与批准的大小或 SHA-256 不符")
            sources[item["path"]] = source
            if session.scalar(select(Asset.id).where(Asset.dataset_version_id == version.id, Asset.path == item["path"])) is None:
                missing_bytes += item["size"]
        stored = session.scalar(select(func.coalesce(func.sum(Asset.size_bytes), 0))) or 0
        if stored + missing_bytes > int(os.environ.get("XIAOYI_STORAGE_QUOTA_BYTES", str(100*1024**3))):
            raise DownloadError("存储配额不足")
        if sum(item["size"] for item in manifest["files"]) > request.max_bytes:
            raise DownloadError("导入超过批准上限")
        request.status = "running"
    # A crash preserves approved/running and can be resumed with the same exact files.
    with session_scope(engine) as session:
        request = session.get(DownloadRequest, request_id, with_for_update=True)
        if request.status not in {"running", "completed"} or digest_json(request.manifest) != request.approved_hash:
            raise DownloadError("导入审批已失效")
        if request.status == "completed":
            return request.id
        for item in manifest["files"]:
            key = _store_file(sources[item["path"]], item["sha256"], root)
            prior = session.scalar(select(Asset).where(Asset.dataset_version_id == request.dataset_version_id, Asset.path == item["path"]))
            if prior is not None:
                if prior.sha256 != item["sha256"]:
                    raise DownloadError("同版本文件不能被覆盖")
                continue
            asset = Asset(dataset_version_id=request.dataset_version_id, download_request_id=request.id,
                path=item["path"], sha256=item["sha256"], size_bytes=item["size"], storage_key=key,
                source_url=manifest["source_url"], origin="imported")
            session.add(asset)
            session.flush()
            session.add(QualityReport(asset_id=asset.id, dataset_version_id=asset.dataset_version_id,
                value=profile_file(root/key, asset.path)))
        request.status, request.actual_bytes = "completed", sum(f["size"] for f in manifest["files"])
        session.add(Audit(actor="local-import", action="local_import_completed", detail=f"request={request.id}"))
    return request_id


def derive_asset(session, parent_asset_id: int, operation: str = "deduplicate", actor: str = "reviewer") -> Asset:
    """Compatibility API; workers use execute_derive to avoid long transactions."""
    from .derivation import derive_in_session
    return derive_in_session(session,parent_asset_id,operation,actor)


def execute_derive(engine, parent_asset_id: int, operation: str = "deduplicate", guard=None, actor=None):
    from .derivation import execute_derive as execute
    return execute(engine,parent_asset_id,operation,guard=guard,actor=actor)



def main(argv=None) -> int:
    """CLI operations require a human account; no command grants approval implicitly."""
    import argparse
    import sys
    from getpass import getpass
    from .auth import resolve, check_password, Principal
    from .models_assets import AuthUser
    from .db import create_engine_for, init_db
    parser = argparse.ArgumentParser(description="人工来源的批准导入与确定性派生（本机 CLI）")
    parser.add_argument("--db", default=os.environ.get("XIAOYI_CATALOG_DB", "./data/catalog.db"))
    parser.add_argument("--import-root", help="受控文件收件目录；默认 XIAOYI_IMPORT_ROOT 或 ./data/import")
    parser.add_argument("--storage-root", help="内容存储目录；默认 XIAOYI_STORAGE_ROOT 或 ./data/assets")
    parser.add_argument("--token-env", default="XIAOYI_CLI_TOKEN", help="存有人类会话 token 的环境变量名")
    parser.add_argument("--username", help="不用会话 token 时输入本地账号名，并交互输入密码")
    commands = parser.add_subparsers(dest="command", required=True)
    request_parser = commands.add_parser("request", help="读取 manifest JSON 并创建待批准导入申请")
    request_parser.add_argument("--manifest", type=Path, required=True)
    import_parser = commands.add_parser("import", help="导入已获人工批准且在受控目录中的文件")
    import_parser.add_argument("--request", type=int, required=True)
    import_parser.add_argument("--files", type=Path, required=True, help="JSON 映射：批准清单路径 -> 本机文件路径")
    derive_parser = commands.add_parser("derive", help="生成新的确定性去重版本，保留原始文件")
    derive_parser.add_argument("--asset", type=int, required=True)
    derive_parser.add_argument("--operation", choices=["deduplicate"], default="deduplicate")
    args = parser.parse_args(argv)
    if args.import_root:
        os.environ["XIAOYI_IMPORT_ROOT"] = str(Path(args.import_root).resolve())
    if args.storage_root:
        os.environ["XIAOYI_STORAGE_ROOT"] = str(Path(args.storage_root).resolve())
    engine = create_engine_for(args.db)
    try:
        init_db(engine)
        with session_scope(engine) as session:
            actor = None
            token = os.environ.get(args.token_env, "")
            if token:
                actor = resolve(session, token)
            elif args.username:
                user = session.scalar(select(AuthUser).where(AuthUser.username == args.username, AuthUser.enabled.is_(True)))
                password = getpass("Password: ")
                if user and check_password(password, user.password_hash):
                    actor = Principal(user.id, user.username, user.role, user.kind)
            if actor is None or actor.kind != "human":
                raise DownloadError("此 CLI 必须使用有效人类会话 token 或本地人类账号密码")
            roles = {"researcher", "reviewer", "admin"} if args.command == "request" else {"reviewer", "admin"}
            if actor.role not in roles:
                raise DownloadError("当前人类账号没有执行此操作的权限")
            if args.command == "request":
                payload = json.loads(args.manifest.read_text(encoding="utf-8"))
                request = request_local_import(session, payload, actor.username)
                result = {"request_id": request.id, "status": request.status,
                          "next": "在策展工作台核验文件清单并人工批准，再执行 import"}
            elif args.command == "derive":
                from .api_jobs import submit_public_job
                from .jobs import to_dict
                result = to_dict(submit_public_job(session,"asset.derive",{"asset_id":args.asset,"operation":args.operation},actor))
        if args.command == "import":
            files = json.loads(args.files.read_text(encoding="utf-8"))
            if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
                raise DownloadError("--files 必须是清单路径到本机路径的 JSON 对象")
            from .jobs import enqueue,to_dict
            with session_scope(engine) as session:
                request=session.get(DownloadRequest,args.request)
                if not request or request.manifest.get("acquisition")!="local" or request.status not in {"approved","running","completed"} or request.approved_hash!=request.manifest_hash:
                    raise DownloadError("本地导入必须先获得人工批准")
                result=to_dict(enqueue(session,"asset.import",{"request_id":args.request,"files":files},actor,f"asset.import:{args.request}"))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (DownloadError, ValueError, OSError, KeyError) as exc:
        print(f"操作未完成：{exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
