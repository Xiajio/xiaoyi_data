"""Authenticated stdio MCP tools for research agents.

Set XIAOYI_CATALOG_AGENT_TOKEN to a scoped machine session created by the account CLI.
Approval, actual downloads and publication are deliberately absent from this tool set.
XIAOYI_CATALOG_DEV_AUTH=1 is only accepted for explicit local SQLite fixtures.
"""
from __future__ import annotations
import os
from contextlib import contextmanager
from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from .auth import Principal, resolve
from .db import create_engine_for, session_scope
from .embeddings import SentenceTransformerEmbedder, TextEmbedder
from .match import match_datasets, requirement_to_filter
from .models import Audit, Dataset, DatasetVersion, Evidence, Enrichment
from .pairing import export_snapshot as _export_snapshot
from .pairing import propose_pairing, snapshot_status as _snapshot_status
from .search import latest_version, version_to_dict

mcp = FastMCP("xiaoyi-catalog")
_embedder: TextEmbedder | None = None
_embedder_failed = False


def _get_embedder() -> TextEmbedder | None:
    global _embedder, _embedder_failed
    if os.environ.get("XIAOYI_CATALOG_EMBED", "1") == "0" or _embedder_failed:
        return None
    if _embedder is None:
        try:
            _embedder = SentenceTransformerEmbedder(os.environ.get("XIAOYI_CATALOG_EMBED_MODEL", "BAAI/bge-small-zh-v1.5"))
            _embedder.embed(["预热"])
        except Exception:
            _embedder_failed = True
            return None
    return _embedder


def _engine():
    return create_engine_for(os.environ.get("XIAOYI_CATALOG_DB", "./data/catalog.db"))


@contextmanager
def _authorized_session(*, write=False):
    token = os.environ.get("XIAOYI_CATALOG_AGENT_TOKEN", "")
    development = os.environ.get("XIAOYI_CATALOG_DEV_AUTH") == "1"
    if not token and not development:
        raise PermissionError("MCP 需要配置有效的 XIAOYI_CATALOG_AGENT_TOKEN")
    engine = _engine()
    try:
        with session_scope(engine) as session:
            if development:
                if engine.dialect.name != "sqlite":
                    raise PermissionError("DEV_AUTH 仅限本地 SQLite 测试夹具")
                actor = Principal(0, "mcp-fixture", "researcher", "agent")
            else:
                actor = resolve(session, token)
                if actor is None or actor.kind != "agent":
                    raise PermissionError("MCP 凭据无效、已过期或不是独立智能体账号")
            if write and actor.role not in {"researcher", "reviewer", "admin"}:
                raise PermissionError("该智能体凭据只具备读取权限")
            yield session, actor
    finally:
        engine.dispose()


@mcp.tool()
def dataset_search(requirements: dict, query: str = "") -> dict:
    """Search catalog candidates using explicit hard constraints. Results include dataset_version_id."""
    filt = requirement_to_filter({**requirements, "limit": max(1, min(100, int(requirements.get("limit") or 10)))})
    with _authorized_session() as (session, _actor):
        return match_datasets(session, filt, query_text=query, embedder=_get_embedder())


@mcp.tool()
def dataset_evidence(dataset_id: int, dataset_version_id: int | None = None) -> dict:
    """Read evidence and candidate enrichments for one pinned version (latest if omitted)."""
    with _authorized_session() as (session, _actor):
        dataset = session.get(Dataset, dataset_id)
        if dataset is None:
            return {"error": f"数据集 {dataset_id} 不存在"}
        version = session.get(DatasetVersion, dataset_version_id) if dataset_version_id is not None else latest_version(session, dataset_id)
        if version is None or version.dataset_id != dataset_id:
            return {"error": "数据集版本不存在或不属于该数据集"}
        evidence = session.scalars(select(Evidence).where(Evidence.dataset_version_id == version.id)).all()
        enrichments = session.scalars(select(Enrichment).where(Enrichment.dataset_version_id == version.id,
            Enrichment.status != "superseded")).all()
        return {"dataset_version_id": version.id, "revision": version.revision,
            "provenance_status": version.provenance_status, "dataset": version_to_dict(session, version),
            "evidence": [{"id": e.id, "dataset_version_id": e.dataset_version_id, "kind": e.kind,
                          "location": e.location, "excerpt": e.excerpt} for e in evidence],
            "enrichment": [{"id": en.id, "dataset_version_id": en.dataset_version_id, "kind": en.kind,
                            "producer": en.producer, "status": en.status, "value": en.value,
                            "evidence_ids": en.evidence_ids} for en in enrichments]}


@mcp.tool()
def pairing_propose(idea: dict, dataset_version_ids: list[int], rationale: str) -> dict:
    """Submit pending idea × dataset-version proposals. This cannot confirm or publish them."""
    with _authorized_session(write=True) as (session, actor):
        result = propose_pairing(session, idea, dataset_version_ids, rationale, proposed_by="agent")
        session.add(Audit(actor=actor.username, action="mcp_pairing_proposed", detail=f"idea={result['idea_id']}"))
        return result


@mcp.tool()
def snapshot_status(idea_external_id: str) -> dict:
    """Read pairing and snapshot state for an idea."""
    with _authorized_session() as (session, _actor):
        return _snapshot_status(session, idea_external_id)


@mcp.tool()
def snapshot_export(snapshot_id: int) -> dict:
    """Read an already approved snapshot's immutable content. Unapproved snapshots are refused."""
    with _authorized_session() as (session, _actor):
        return _export_snapshot(session, snapshot_id)


@mcp.tool()
def agent_run_create(payload: dict) -> dict:
    """Queue a bounded research goal; payload goal/hard_constraints/preferences/sources/budgets."""
    from .agent_runtime import create_run, run_to_dict
    with _authorized_session(write=True) as (session, actor):
        return run_to_dict(create_run(session, payload, actor.username))


@mcp.tool()
def agent_run_status(run_id: int, after_event_id: int = 0) -> dict:
    """Read a research task, its candidates and progress events; does not execute work."""
    from .agent_runtime import run_to_dict, list_events
    from .models_runtime import AgentRun
    with _authorized_session() as (session, _actor):
        run = session.get(AgentRun, run_id)
        if run is None:
            return {"error": "科研任务不存在"}
        return {"run": run_to_dict(run), "events": list_events(session, run_id, after_event_id)}


@mcp.tool()
def agent_run_cancel(run_id: int) -> dict:
    """Cancel a task owned by this authenticated agent, preserving already recorded evidence."""
    from .agent_runtime import cancel_run, run_to_dict
    with _authorized_session(write=True) as (session, actor):
        return run_to_dict(cancel_run(session, run_id, actor.username))


@mcp.tool()
def agent_run_retry(run_id: int) -> dict:
    """Retry this agent's failed/cancelled task within its original execution and tool budgets."""
    from .agent_runtime import retry_run, run_to_dict
    with _authorized_session(write=True) as (session, actor):
        return run_to_dict(retry_run(session, run_id, actor.username))


@mcp.tool()
def dataset_files(dataset_version_id: int) -> dict:
    """List exact file names and byte estimates at a fixed HF revision, without downloading content."""
    from .downloads import file_inventory
    with _authorized_session() as (session, _actor):
        return file_inventory(session, dataset_version_id)


@mcp.tool()
def download_request(dataset_version_id: int, files: list[str], max_bytes: int) -> dict:
    """Request named files at a fixed version. Only a human may approve; this never downloads files."""
    from .downloads import request_download, request_to_dict
    with _authorized_session(write=True) as (session, actor):
        return request_to_dict(request_download(session, {"dataset_version_id": dataset_version_id,
            "files": files, "max_bytes": max_bytes}, actor.username))


@mcp.tool()
def snapshot_bundle(snapshot_id: int) -> dict:
    """Read the manifest of an existing approved research bundle; never creates or publishes a bundle."""
    from .models_assets import ResearchBundle
    with _authorized_session() as (session, _actor):
        frozen = _export_snapshot(session, snapshot_id)
        bundle = session.scalar(select(ResearchBundle).where(ResearchBundle.snapshot_id == snapshot_id))
        return {"snapshot": frozen, "manifest": bundle.manifest if bundle else None,
                "available": bundle is not None, "sha256": bundle.sha256 if bundle else None}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
