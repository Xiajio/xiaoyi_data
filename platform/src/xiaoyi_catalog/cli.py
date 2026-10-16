"""命令行入口：init-db / collect / search / stats。

示例：
  python -m xiaoyi_catalog.cli init-db
  python -m xiaoyi_catalog.cli collect --task text-classification --language zh --limit 50
  python -m xiaoyi_catalog.cli search --modality text --license-class permissive --query 情感
  python -m xiaoyi_catalog.cli stats
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .adapters.base import CollectScope
from .adapters.huggingface import DEFAULT_ENDPOINT, HuggingFaceAdapter
from .collect import collect
from .db import create_engine_for, init_db, session_scope
from .embeddings import HashEmbedder, SentenceTransformerEmbedder, embed_all
from .enrich import enrich_all
from .summary_runtime import backfill_summaries
from .pairing import (PairingError, approve_snapshot, export_snapshot, freeze_snapshot,
                      propose_pairing, review_pairing, snapshot_status)
from .search import SearchFilter, catalog_stats, search_datasets

DEFAULT_DB = "./data/catalog.db"
MIRROR_ENDPOINT = "https://hf-mirror.com"


def _resolve_db(args: argparse.Namespace) -> str:
    return args.db or os.environ.get("XIAOYI_CATALOG_DB", DEFAULT_DB)


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_init_db(args: argparse.Namespace) -> int:
    engine = create_engine_for(_resolve_db(args))
    init_db(engine)
    print(f"目录库已初始化：{_resolve_db(args)}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    engine = create_engine_for(_resolve_db(args))
    init_db(engine)
    endpoint = args.endpoint or os.environ.get("HF_ENDPOINT", MIRROR_ENDPOINT)
    adapter = HuggingFaceAdapter(endpoint)
    scope = CollectScope(
        search=args.search or "",
        task_categories=tuple(args.task),
        languages=tuple(args.language),
        author=args.author or "",
        limit=args.limit,
    )
    print(f"采集来源：{endpoint} · 范围：{json.dumps(asdict_safe(scope), ensure_ascii=False)}", file=sys.stderr)
    run = collect(engine, adapter, scope)
    if args.json:
        _print_json({"run_id": run.id, "status": run.status, "error_kind": run.error_kind, "stats": run.stats})
    else:
        stats = run.stats
        print(f"run #{run.id} · {run.status}：发现 {stats.get('found', 0)}，新增 {stats.get('new', 0)}，"
              f"更新 {stats.get('updated', 0)}，重复 {stats.get('duplicates', 0)}，跳过旧水位 {stats.get('skipped_stale', 0)}，失败 {stats.get('failed', 0)}")
        if run.error_kind:
            print(f"失败分类：{run.error_kind}", file=sys.stderr)
        for failure in stats.get("failures", [])[:5]:
            print(f"  失败：{failure['source_key'] or '(list)'} · {failure['error_kind']} · {failure['message']}", file=sys.stderr)
    return 0 if run.status == "completed" else 1


def cmd_search(args: argparse.Namespace) -> int:
    engine = create_engine_for(_resolve_db(args))
    with session_scope(engine) as session:
        results = search_datasets(session, SearchFilter(
            modalities=tuple(args.modality),
            task_categories=tuple(args.task),
            languages=tuple(args.language),
            license_classes=tuple(args.license_class),
            query=args.query or "",
            min_rows=args.min_rows,
            limit=args.limit,
            offset=args.offset,
        ))
    if args.json:
        _print_json(results)
    else:
        if not results:
            print("没有匹配的数据集。")
        for item in results:
            missing = f" · 缺失：{','.join(item['missing'])}" if item["missing"] else ""
            rows = f" · {item['size_rows']} 行" if item["size_rows"] is not None else ""
            print(f"[{item['id']}] {item['source_key']} · {','.join(item['modalities']) or 'unknown'}"
                  f" · {item['license_class']}{rows} · ↓{item['downloads']}{missing}")
            print(f"    {item['url']}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    engine = create_engine_for(_resolve_db(args))
    with session_scope(engine) as session:
        stats = catalog_stats(session)
    if args.json:
        _print_json(stats)
    else:
        print(f"数据集 {stats['datasets']} · 证据 {stats['evidence']} · 采集任务 {stats['runs']}")
        print(f"许可分级：{json.dumps(stats['by_license_class'], ensure_ascii=False)}")
        print(f"模态分布：{json.dumps(stats['by_modality'], ensure_ascii=False)}")
    return 0


def asdict_safe(scope: CollectScope) -> dict:
    return {"search": scope.search, "task_categories": list(scope.task_categories),
            "languages": list(scope.languages), "author": scope.author, "limit": scope.limit}


def _split_ids(text: str) -> list[int]:
    return [int(item) for item in text.split(",") if item.strip()]


def _cli_human(session, args):
    from getpass import getpass
    from sqlalchemy import select
    from .auth import resolve, check_password, Principal
    from .models_assets import AuthUser
    token = os.environ.get(getattr(args, "token_env", "XIAOYI_CLI_TOKEN"), "")
    actor = resolve(session, token) if token else None
    if not token and getattr(args, "username", None):
        user = session.scalar(select(AuthUser).where(AuthUser.username == args.username, AuthUser.enabled.is_(True)))
        password = getpass("Password: ")
        if user and check_password(password, user.password_hash):
            actor = Principal(user.id, user.username, user.role, user.kind)
    if actor is None or actor.kind != "human" or actor.role not in {"reviewer", "admin"}:
        raise PermissionError("此人工操作须通过本地人类 reviewer/admin 账号认证；使用 --username 或 XIAOYI_CLI_TOKEN")
    return actor


def _guarded(args: argparse.Namespace, action, *, audit_action: str | None = None) -> int:
    engine = create_engine_for(_resolve_db(args))
    init_db(engine)
    try:
        with session_scope(engine) as session:
            actor = _cli_human(session, args) if audit_action else None
            result = action(session)
            if actor is not None:
                from .models import Audit, Review
                session.add(Audit(actor=actor.username, action=audit_action,
                                  detail=json.dumps(result, ensure_ascii=False)))
                if audit_action == "pairing_review_cli":
                    session.add(Review(target_kind="pairing", target_id=args.pairing,
                                       decision=args.decision, note=args.note, role=actor.role))
    except (PairingError, PermissionError) as exc:
        print(f"操作未完成：{exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    if getattr(args, "json", False):
        _print_json(result)
    elif result is not None:
        print(result)
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    engine = create_engine_for(_resolve_db(args))
    init_db(engine)
    try:
        stats = enrich_all(engine, only_missing=args.missing_only)
    finally:
        engine.dispose()
    print(f"整理完成：处理 {stats['processed']}，跳过 {stats['skipped']}")
    return 0


def cmd_summary_backfill(args: argparse.Namespace) -> int:
    engine = create_engine_for(_resolve_db(args))
    init_db(engine)
    try:
        stats = backfill_summaries(engine, limit=args.limit)
    finally:
        engine.dispose()
    print(f"智能体总结回填：新排队 {stats['submitted']}，扫描版本 {stats['scanned']}（由后台 worker 逐条生成）")
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    engine = create_engine_for(_resolve_db(args))
    init_db(engine)
    embedder = HashEmbedder() if args.hash else SentenceTransformerEmbedder(args.model)
    try:
        stats = embed_all(engine, embedder)
    finally:
        engine.dispose()
    print(f"嵌入完成：生成 {stats['embedded']}，跳过未变化 {stats['skipped']} · 模型 {embedder.model_name}")
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    try:
        idea = json.loads(args.idea)
    except json.JSONDecodeError:
        print("--idea 需要思路卡 JSON（含 external_id/title/requirements）", file=sys.stderr)
        return 1
    return _guarded(args, lambda session: propose_pairing(session, idea, _split_ids(args.versions), args.rationale))


def cmd_review(args: argparse.Namespace) -> int:
    return _guarded(args, lambda session: {"id": review_pairing(session, args.pairing, args.decision, args.note).id, "status": args.decision}, audit_action="pairing_review_cli")


def cmd_freeze(args: argparse.Namespace) -> int:
    pairing_ids = _split_ids(args.pairings) if args.pairings else None
    return _guarded(args, lambda session: {"snapshot_id": freeze_snapshot(session, args.idea, pairing_ids, note=args.note).id}, audit_action="snapshot_freeze_cli")


def cmd_approve(args: argparse.Namespace) -> int:
    return _guarded(args, lambda session: {"snapshot_id": approve_snapshot(session, args.snapshot).id, "status": "approved"}, audit_action="snapshot_approve_cli")


def cmd_status(args: argparse.Namespace) -> int:
    return _guarded(args, lambda session: snapshot_status(session, args.idea))


def cmd_export(args: argparse.Namespace) -> int:
    def _export(session):
        return export_snapshot(session, args.snapshot)
    engine = create_engine_for(_resolve_db(args))
    init_db(engine)
    try:
        with session_scope(engine) as session:
            result = _export(session)
    except PairingError as exc:
        print(f"操作未完成：{exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(f"已写入 {args.out}")
    else:
        _print_json(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xiaoyi-catalog", description="小亿数据集目录：采集与检索（M1）")
    parser.add_argument("--db", default="", help=f"SQLite 路径（默认 {DEFAULT_DB} 或 XIAOYI_CATALOG_DB）")
    parser.add_argument("--username", help="人工审核账号；需要交互输入密码，也可用 XIAOYI_CLI_TOKEN")
    parser.add_argument("--token-env", default="XIAOYI_CLI_TOKEN", help="会话 token 环境变量名")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="建库建表").set_defaults(func=cmd_init_db)

    collect_parser = sub.add_parser("collect", help="从数据源采集数据集元数据")
    collect_parser.add_argument("--endpoint", default="", help=f"HF 端点（默认 HF_ENDPOINT 或 {MIRROR_ENDPOINT}）")
    collect_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    collect_parser.add_argument("--search", default="", help="关键词搜索")
    collect_parser.add_argument("--task", action="append", default=[], help="任务类别，可重复，如 text-classification")
    collect_parser.add_argument("--language", action="append", default=[], help="语言，可重复，如 zh")
    collect_parser.add_argument("--author", default="", help="限定作者/组织")
    collect_parser.add_argument("--limit", type=int, default=100, help="最多处理条目（默认 100）")
    collect_parser.set_defaults(func=cmd_collect)

    search_parser = sub.add_parser("search", help="结构化检索目录")
    search_parser.add_argument("--modality", action="append", default=[], help="模态，可重复：text/table/image/3d")
    search_parser.add_argument("--task", action="append", default=[], help="任务类别，可重复")
    search_parser.add_argument("--language", action="append", default=[], help="语言，可重复")
    search_parser.add_argument("--license-class", action="append", default=[], choices=["permissive", "copyleft", "research-only", "unknown"])
    search_parser.add_argument("--query", default="", help="名称 / 标识 / 卡片全文关键词")
    search_parser.add_argument("--min-rows", type=int, default=None, help="最小行数")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--offset", type=int, default=0)
    search_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    search_parser.set_defaults(func=cmd_search)

    stats_parser = sub.add_parser("stats", help="目录概览")
    stats_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    stats_parser.set_defaults(func=cmd_stats)

    enrich_parser = sub.add_parser("enrich", help="离线整理：schema 摘要 + 质量信号（候选）")
    enrich_parser.add_argument("--missing-only", action="store_true", help="只处理尚无 schema 候选的记录")
    enrich_parser.set_defaults(func=cmd_enrich)
    summary_parser = sub.add_parser("summary-backfill", help="存量回填：为尚无智能体总结的版本排队生成")
    summary_parser.add_argument("--limit", type=int, default=200, help="最多扫描的版本数")
    summary_parser.set_defaults(func=cmd_summary_backfill)

    embed_parser = sub.add_parser("embed", help="生成嵌入索引")
    embed_parser.add_argument("--model", default="BAAI/bge-small-zh-v1.5", help="嵌入模型（默认 bge-small-zh-v1.5）")
    embed_parser.add_argument("--hash", action="store_true", help="使用无依赖的哈希嵌入器（离线冒烟）")
    embed_parser.set_defaults(func=cmd_embed)

    propose_parser = sub.add_parser("propose", help="提交思路 × 数据集配对提案（落 pending）")
    propose_parser.add_argument("--idea", required=True, help="思路卡 JSON：external_id/title/requirements")
    propose_parser.add_argument("--versions", required=True, help="逗号分隔的 dataset_version id")
    propose_parser.add_argument("--rationale", required=True, help="配对理由")
    propose_parser.add_argument("--json", action="store_true")
    propose_parser.set_defaults(func=cmd_propose)

    review_parser = sub.add_parser("review", help="人工核验配对（confirmed/rejected）")
    review_parser.add_argument("--pairing", type=int, required=True)
    review_parser.add_argument("--decision", required=True, choices=["confirmed", "rejected", "needs-info"])
    review_parser.add_argument("--note", default="")
    review_parser.add_argument("--json", action="store_true")
    review_parser.add_argument("--username", default=argparse.SUPPRESS, help="人工审核账号")
    review_parser.set_defaults(func=cmd_review)

    freeze_parser = sub.add_parser("freeze", help="冻结思路下已确认配对为快照")
    freeze_parser.add_argument("--idea", required=True, help="思路卡 external_id")
    freeze_parser.add_argument("--pairings", default="", help="逗号分隔配对 id；缺省纳入全部已确认")
    freeze_parser.add_argument("--note", default="")
    freeze_parser.add_argument("--json", action="store_true")
    freeze_parser.add_argument("--username", default=argparse.SUPPRESS, help="人工审核账号")
    freeze_parser.set_defaults(func=cmd_freeze)

    approve_parser = sub.add_parser("approve", help="批准待审批快照")
    approve_parser.add_argument("--snapshot", type=int, required=True)
    approve_parser.add_argument("--json", action="store_true")
    approve_parser.add_argument("--username", default=argparse.SUPPRESS, help="人工审核账号")
    approve_parser.set_defaults(func=cmd_approve)

    status_parser = sub.add_parser("status", help="查询思路的配对与快照状态")
    status_parser.add_argument("--idea", required=True, help="思路卡 external_id")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)

    export_parser = sub.add_parser("export", help="导出已批准快照的冻结内容")
    export_parser.add_argument("--snapshot", type=int, required=True)
    export_parser.add_argument("--out", default="", help="写入文件；缺省打印 JSON")
    export_parser.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
