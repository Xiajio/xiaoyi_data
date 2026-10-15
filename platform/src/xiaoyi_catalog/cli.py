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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xiaoyi-catalog", description="小亿数据集目录：采集与检索（M1）")
    parser.add_argument("--db", default="", help=f"SQLite 路径（默认 {DEFAULT_DB} 或 XIAOYI_CATALOG_DB）")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
