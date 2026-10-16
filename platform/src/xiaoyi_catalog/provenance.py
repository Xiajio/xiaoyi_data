"""Explicit, audited human recovery of legacy metadata at an exact upstream commit."""
from __future__ import annotations
import argparse
from getpass import getpass
import json
import os
import re
import sys
from sqlalchemy import select
from .adapters.base import DatasetRef
from .adapters.huggingface import HuggingFaceAdapter
from .auth import Principal, check_password, resolve
from .collect import captured_metadata, CARD_EVIDENCE_EXCERPT
from .db import create_engine_for, init_db, session_scope
from .models import Audit, Dataset, DatasetVersion, Evidence, Source
from .models_assets import AuthUser

RECOVERY_HELP = "历史版本未验证；由人工审核者执行 python -m xiaoyi_catalog.provenance --db DATABASE --username USER recover --version ID（仅支持可获取的完整 HF commit）"


def recover_version(engine, version_id: int, actor: Principal, *, adapter=None) -> dict:
    if actor.kind != "human" or actor.role not in {"reviewer", "admin"}:
        raise ValueError("历史来源恢复只允许人类 reviewer/admin")
    with session_scope(engine) as session:
        version = session.get(DatasetVersion, version_id)
        if version is None:
            raise ValueError("数据集版本不存在")
        if version.provenance_status != "legacy_unverified" or version.metadata_json:
            raise ValueError("只允许显式恢复无元数据的旧版本；已验证内容不可重写")
        dataset = session.get(Dataset, version.dataset_id)
        source = session.get(Source, dataset.source_id)
        if source.kind != "huggingface" or not re.fullmatch(r"[0-9a-fA-F]{40}", version.revision):
            raise ValueError("恢复要求完整 Hugging Face commit SHA；其他历史来源须人工登记新的明确版本")
        key, revision, endpoint = dataset.source_key, version.revision, source.endpoint
    adapter = adapter or HuggingFaceAdapter(endpoint or "https://huggingface.co")
    raw = adapter.fetch_detail(DatasetRef(key, revision))
    if raw.ref.source_key != key or raw.ref.revision != revision:
        raise ValueError("来源返回的版本与固定 commit 不一致，未恢复任何记录")
    with session_scope(engine) as session:
        version = session.get(DatasetVersion, version_id, with_for_update=True)
        if version.provenance_status != "legacy_unverified" or version.metadata_json:
            raise ValueError("版本已由其他操作恢复；拒绝覆写")
        metadata = captured_metadata(raw, version.dataset_id)
        session.info["recover_legacy_version"] = version.id
        try:
            version.metadata_json = metadata
            version.provenance_status = "verified"
            session.flush()
        finally:
            session.info.pop("recover_legacy_version", None)
        evidence = []
        if raw.card_text.strip():
            evidence.append(Evidence(dataset_id=version.dataset_id, dataset_version_id=version.id,
                kind="card", excerpt=raw.card_text[:CARD_EVIDENCE_EXCERPT], location=f"{revision}/README.md"))
        evidence.append(Evidence(dataset_id=version.dataset_id, dataset_version_id=version.id, kind="metadata",
            excerpt=json.dumps({k:v for k,v in metadata.items() if k != "card_text"}, ensure_ascii=False),
            location=f"{raw.url}@{revision}:metadata"))
        session.add_all(evidence)
        session.add(Audit(actor=actor.username, action="legacy_provenance_recovered",
            detail=f"version={version.id}; revision={revision}; previous=legacy_unverified; frozen snapshots unchanged"))
        session.flush()
        return {"dataset_version_id": version.id, "revision": revision, "provenance_status":"verified",
                "evidence_ids":[e.id for e in evidence], "snapshots_changed": False,
                "note":"旧快照保持原样；新科研发布需重新人工核验并冻结"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="显式人工恢复旧 HF 版本来源证据，不改变历史快照")
    parser.add_argument("--db", default=os.environ.get("XIAOYI_CATALOG_DB", "./data/catalog.db"))
    parser.add_argument("--token-env", default="XIAOYI_CLI_TOKEN")
    parser.add_argument("--username")
    commands = parser.add_subparsers(dest="command", required=True)
    recover = commands.add_parser("recover")
    recover.add_argument("--version", type=int, required=True)
    args = parser.parse_args(argv)
    engine = create_engine_for(args.db)
    try:
        init_db(engine)
        with session_scope(engine) as session:
            token = os.environ.get(args.token_env, "")
            actor = resolve(session, token) if token else None
            if actor is None and args.username:
                user = session.scalar(select(AuthUser).where(AuthUser.username == args.username, AuthUser.enabled.is_(True)))
                password = getpass("Password: ")
                if user and check_password(password, user.password_hash):
                    actor = Principal(user.id, user.username, user.role, user.kind)
            if actor is None:
                raise ValueError("请提供有效人类会话 token 或本地账号密码")
        print(json.dumps(recover_version(engine, args.version, actor), ensure_ascii=False))
        return 0
    except (ValueError, OSError) as exc:
        print(f"恢复未完成：{exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
