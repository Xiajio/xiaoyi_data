"""Disk-backed deterministic legacy derivation with short publication transactions."""
from __future__ import annotations
from copy import deepcopy
from contextlib import closing
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from sqlalchemy import func, select
from .db import session_scope
from .downloads import DownloadError,digest_json,asset_to_dict
from .models import Audit,DatasetVersion,Evidence
from .models_assets import Asset,QualityReport
from .models_samples import ProfileRun,MetricObservation
from .pairing import _approved_asset
from .profiling import profile_file
from .sample_engine import (asset_path,source_record,validate_inputs,workspace,store_file,
                            checkpoint,cached_operation,remember_operation)


def _capture(session,parent_asset_id,operation,*,lock=False):
    if operation!="deduplicate":
        raise DownloadError("首期仅支持确定性的 CSV/TSV/JSONL 去重")
    parent=session.get(Asset,parent_asset_id,with_for_update=lock,populate_existing=lock)
    if parent is None or not _approved_asset(session,parent):
        raise DownloadError("派生输入必须来自已完整批准的获取")
    suffix=Path(parent.path).suffix.lower()
    if suffix not in {".csv",".tsv",".jsonl",".ndjson"}:
        raise DownloadError("该格式不支持确定性去重")
    base=session.get(DatasetVersion,parent.dataset_version_id)
    if base is None:
        raise DownloadError("派生来源版本不存在")
    recipe={"operation":operation,"producer":"deduplicate/1","parameters":{"keep":"first","row_equality":"canonical_json"},
            "inputs":[{"asset_id":parent.id,"sha256":parent.sha256,"dataset_version_id":parent.dataset_version_id}]}
    return {"parent":source_record(parent),"source_url":parent.source_url,"download_request_id":parent.download_request_id,
            "base_id":base.id,"dataset_id":base.dataset_id,"base_metadata":deepcopy(base.metadata_json),
            "base_metadata_hash":digest_json(base.metadata_json),"recipe":recipe,"revision":"derived-"+digest_json(recipe),"suffix":suffix}


def _existing(session,state):
    version=session.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id==state["dataset_id"],DatasetVersion.revision==state["revision"]))
    if version is None:
        return None
    asset=session.scalar(select(Asset).where(Asset.dataset_version_id==version.id))
    if asset is None or not _approved_asset(session,asset):
        raise DownloadError("已有派生版本缺失完整产物或批准依据")
    return asset


def _hash(path,guard=None):
    checkpoint(guard)
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        count=0
        while chunk:=stream.read(1024*1024):
            digest.update(chunk);count+=1
            if count%16==0:
                checkpoint(guard)
    return digest.hexdigest()


def _json_rows(stream,max_bytes):
    while line:=stream.readline(max_bytes+1):
        if len(line.encode("utf-8"))>max_bytes:
            raise DownloadError("派生输入单行超过处理大小上限")
        if line.strip():
            yield json.loads(line)


def _compute(state,guard=None):
    source=asset_path(state["parent"]["storage_key"])
    if source.stat().st_size!=state["parent"]["size_bytes"] or _hash(source,guard)!=state["parent"]["sha256"]:
        raise DownloadError("派生输入校验失败")
    row_limit=int(os.environ.get("XIAOYI_DERIVE_MAX_ROWS","1000000"))
    max_bytes=int(os.environ.get("XIAOYI_DERIVE_MAX_ROW_BYTES",str(4*1024*1024)))
    with workspace() as directory, closing(sqlite3.connect(directory/"dedup.sqlite")) as index:
        index.execute("PRAGMA temp_store=FILE")
        index.execute("PRAGMA cache_size=-16384")
        index.execute("CREATE TABLE seen (digest BLOB PRIMARY KEY) WITHOUT ROWID")
        output=directory/("deduplicated"+state["suffix"])
        input_rows=output_rows=0
        with source.open(encoding="utf-8",newline="") as inp,output.open("w",encoding="utf-8",newline="") as out:
            if state["suffix"] in {".csv",".tsv"}:
                delimiter="\t" if state["suffix"]==".tsv" else ","
                reader=csv.DictReader(inp,delimiter=delimiter)
                if not reader.fieldnames or len(set(reader.fieldnames))!=len(reader.fieldnames):
                    raise DownloadError("CSV 列名缺失或重复")
                writer=csv.DictWriter(out,fieldnames=reader.fieldnames,delimiter=delimiter,lineterminator="\n")
                writer.writeheader();rows=reader
            else:
                rows=_json_rows(inp,max_bytes);writer=None
            for row in rows:
                if isinstance(row,dict) and None in row:
                    raise DownloadError("CSV 行列数量与表头不符")
                input_rows+=1
                if input_rows>row_limit:
                    raise DownloadError("派生输入超过配置的处理行数上限")
                canonical=json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(",",":"))
                if len(canonical.encode("utf-8"))>max_bytes:
                    raise DownloadError("派生输入单行超过处理大小上限")
                digest=hashlib.sha256(canonical.encode()).digest()
                added=index.execute("INSERT OR IGNORE INTO seen(digest) VALUES(?)",(digest,)).rowcount
                if added:
                    writer.writerow(row) if writer else out.write(canonical+"\n")
                    output_rows+=1
                if input_rows%500==0:
                    index.commit();checkpoint(guard)
        index.commit()
        # Detect mutation during streaming, before any publication transaction begins.
        if _hash(source,guard)!=state["parent"]["sha256"]:
            raise DownloadError("派生输入在处理期间改变")
        report=profile_file(output,state["parent"]["path"])
        report["derivation"]={"input_rows":input_rows,"output_rows":output_rows,"removed_rows":input_rows-output_rows}
        checkpoint(guard)
        artifact=store_file(output,guard=guard)
        return {"artifact":artifact,"report":report,"input_rows":input_rows,"output_rows":output_rows}


def _publish(session,state,result,actor):
    # Serialize publication for the same parent, recheck source authority after the long computation.
    current=_capture(session,state["parent"]["asset_id"],state["recipe"]["operation"],lock=True)
    if current!=state:
        raise DownloadError("派生输入或来源版本在处理期间改变")
    existing=_existing(session,state)
    if existing is not None:
        return existing
    artifact=result["artifact"]
    stored=session.scalar(select(func.coalesce(func.sum(Asset.size_bytes),0))) or 0
    if stored+artifact["size_bytes"]>int(os.environ.get("XIAOYI_STORAGE_QUOTA_BYTES",str(100*1024**3))):
        raise DownloadError("派生文件超过存储配额")
    metadata=deepcopy(state["base_metadata"])
    parent=state["parent"]
    metadata.update(size_rows=result["output_rows"],size_bytes=artifact["size_bytes"],derivation=state["recipe"],downloads=0,likes=0,
        card_text=f"# Deterministic derived dataset\nProducer: deduplicate/1\nInput asset: {parent['asset_id']}\nInput SHA-256: {parent['sha256']}\nRows: {result['output_rows']}\nSource card is available on parent version {state['base_id']}.")
    version=DatasetVersion(dataset_id=state["dataset_id"],revision=state["revision"],kind="derived",parent_version_id=state["base_id"],metadata_json=metadata,provenance_status="verified")
    session.add(version);session.flush()
    asset=Asset(dataset_version_id=version.id,download_request_id=state["download_request_id"],path=parent["path"],
        source_url=state["source_url"],origin="derived",parent_asset_ids=[parent["asset_id"]],recipe=state["recipe"],**artifact)
    session.add(asset);session.flush()
    session.add(QualityReport(asset_id=asset.id,dataset_version_id=version.id,value=result["report"]))
    session.add(Evidence(dataset_id=version.dataset_id,dataset_version_id=version.id,kind="derivation",
        excerpt=json.dumps(state["recipe"],ensure_ascii=False),location=f"asset:{parent['asset_id']}@sha256:{parent['sha256']}"))
    session.add(Audit(actor=actor,action="asset_derived",detail=f"asset={asset.id}; parent={parent['asset_id']}; version={version.id}"))
    profile=ProfileRun(producer="deduplicate/1",manifest={"asset_id":asset.id,"recipe":state["recipe"]},summary=result["report"])
    session.add(profile);session.flush()
    session.add(MetricObservation(profile_run_id=profile.id,name="derivation_counts",value=result["report"]["derivation"]))
    return asset


def derive_in_session(session,parent_asset_id,operation="deduplicate",actor="reviewer"):
    """Legacy synchronous API; retains caller transaction ownership and output identity."""
    state=_capture(session,parent_asset_id,operation)
    existing=_existing(session,state)
    if existing is not None:
        return existing
    result=_compute(state)
    return _publish(session,state,result,actor)


def execute_derive(engine,parent_asset_id,operation="deduplicate",guard=None,actor=None):
    with session_scope(engine) as session:
        checkpoint(guard,session)
        previous=cached_operation(session,guard)
        if previous is not None:
            return previous
        state=_capture(session,parent_asset_id,operation)
        existing=_existing(session,state)
        if existing is not None:
            result=asset_to_dict(existing)
            remember_operation(session,guard,result)
            return result
    computed=_compute(state,guard)
    with session_scope(engine) as session:
        checkpoint(guard,session)
        asset=_publish(session,state,computed,actor or "derived-worker")
        result=asset_to_dict(asset)
        remember_operation(session,guard,result)
        return result
