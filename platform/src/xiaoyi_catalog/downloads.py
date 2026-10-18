"""Version-bound download approval, immutable content storage and resumable acquisition."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
from datetime import timedelta, timezone
from pathlib import Path, PurePosixPath

from huggingface_hub import HfApi
from sqlalchemy import select, func, update, or_
from .db import session_scope
from .models import Audit, Dataset, DatasetVersion, Source, utcnow
from .models_assets import Asset, DownloadRequest, QualityReport
from .profiling import profile_file, public_report, split_overlap

class DownloadError(ValueError):
    pass

def storage_root() -> Path:
    root=Path(os.environ.get("XIAOYI_STORAGE_ROOT","./data/assets")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root

def safe_path(name: str) -> str:
    p=PurePosixPath(name)
    if not name or "\\" in name or ":" in name or p.is_absolute() or any(x in {"..","."} for x in p.parts):
        raise DownloadError("无效文件路径")
    return str(p)

def digest_json(value) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()

def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()

def file_inventory(session, version_id: int, *, api=None) -> dict:
    version=session.get(DatasetVersion,version_id)
    if not version:
        raise DownloadError("数据集版本不存在")
    if version.provenance_status!="verified" or not version.metadata_json:
        from .provenance import RECOVERY_HELP
        raise DownloadError(RECOVERY_HELP)
    dataset=session.get(Dataset,version.dataset_id)
    source=session.get(Source,dataset.source_id)
    if source.kind!="huggingface":
        raise DownloadError("登记来源需由负责人取得文件后通过受控导入；暂不自动抓取外部下载链接")
    if not re.fullmatch(r"[0-9a-fA-F]{40}",version.revision):
        raise DownloadError("下载必须锁定完整 commit SHA")
    api=api or HfApi(endpoint=os.environ.get("HF_ENDPOINT","https://huggingface.co"))
    entries=[]
    for entry in api.list_repo_tree(repo_id=dataset.source_key,repo_type="dataset",revision=version.revision,recursive=True):
        if getattr(entry,"size",None) is not None:
            entries.append({"path":safe_path(entry.path),"size":int(entry.size)})
    return {"dataset_version_id":version.id,"revision":version.revision,"repo_id":dataset.source_key,
            "items":entries}

def request_download(session, payload: dict, actor: str, *, api=None) -> DownloadRequest:
    version=session.get(DatasetVersion,int(payload["dataset_version_id"]))
    if not version:
        raise DownloadError("数据集版本不存在")
    metadata=version.metadata_json or {}
    if not metadata.get("license") or metadata.get("license_class","unknown")=="unknown":
        raise DownloadError("许可依据缺失，补充核验后才能申请下载")
    files=sorted(set(safe_path(str(x)) for x in payload.get("files",[])))
    if not files or len(files)>10000:
        raise DownloadError("请选择 1 到 10000 个明确文件")
    max_bytes=int(payload.get("max_bytes",0))
    upper=int(os.environ.get("XIAOYI_DOWNLOAD_MAX_BYTES",str(10*1024**3)))
    if max_bytes<=0 or max_bytes>upper:
        raise DownloadError(f"下载上限须介于 1 与 {upper} 字节")
    inventory=file_inventory(session,version.id,api=api)
    available={x["path"]:x["size"] for x in inventory["items"]}
    if any(f not in available for f in files):
        raise DownloadError("申请包含版本清单外的文件")
    estimated=sum(available[f] for f in files)
    if estimated>max_bytes:
        raise DownloadError("预计下载体积超过申请上限")
    manifest={"dataset_version_id":version.id,"revision":version.revision,"repo_id":inventory["repo_id"],
              "files":[{"path":f,"size":available[f]} for f in files],
              "license":metadata["license"],"metadata_hash":digest_json(metadata)}
    fingerprint=digest_json(manifest)
    existing=session.scalar(select(DownloadRequest).where(DownloadRequest.manifest_hash==fingerprint,
        DownloadRequest.requested_by==actor,DownloadRequest.status.in_(["pending","approved","running","completed"])))
    if existing:
        return existing
    request=DownloadRequest(dataset_version_id=version.id,requested_by=actor,manifest=manifest,
            manifest_hash=fingerprint,max_bytes=max_bytes,estimated_bytes=estimated)
    session.add(request)
    session.flush()
    session.add(Audit(actor=actor,action="download_requested",detail=f"request={request.id}, manifest={fingerprint}"))
    return request

def decide_download(session, request_id, decision, actor, note=""):
    request=session.get(DownloadRequest,request_id,with_for_update=True)
    if not request:
        raise DownloadError("下载申请不存在")
    if decision not in {"approved","rejected"}:
        raise DownloadError("无效审批决定")
    if request.status==decision:
        return request
    if request.status!="pending":
        raise DownloadError("申请已处理；变更文件或重试失败下载需新建申请")
    if digest_json(request.manifest)!=request.manifest_hash:
        raise DownloadError("申请范围已变更，须重新申请")
    version=session.get(DatasetVersion,request.dataset_version_id)
    if digest_json(version.metadata_json)!=request.manifest["metadata_hash"]:
        raise DownloadError("版本证据发生变化，须重新核验")
    request.status=decision
    request.decided_by=actor
    request.note=note
    request.approved_hash=request.manifest_hash if decision=="approved" else None
    session.add(Audit(actor=actor,action="download_"+decision,detail=f"request={request.id}; {note}"))
    return request

def request_to_dict(r):
    return {"id":r.id,"dataset_version_id":r.dataset_version_id,"status":r.status,
            "manifest":r.manifest,"files":[f["path"] for f in r.manifest.get("files",[])], "revision":r.manifest.get("revision"), "license_basis":r.manifest.get("license"),"max_bytes":r.max_bytes,
            "estimated_bytes":r.estimated_bytes,"actual_bytes":r.actual_bytes,"requested_by":r.requested_by,
            "decided_by":r.decided_by,"note":r.note,"error":r.error,
            "created_at":r.created_at.isoformat() if r.created_at else None}

def asset_to_dict(a):
    return {"id":a.id,"dataset_version_id":a.dataset_version_id,"path":a.path,"sha256":a.sha256,
            "size_bytes":a.size_bytes,"source_url":a.source_url,"origin":a.origin,"recipe":a.recipe,
            "parent_asset_ids":a.parent_asset_ids}

def assets_for_version(session,version_id):
    rows=session.scalars(select(Asset).where(Asset.dataset_version_id==version_id)).all()
    reports=session.scalars(select(QualityReport).where(QualityReport.dataset_version_id==version_id)).all()
    names={a.id:a.path for a in rows}
    return {"items":[asset_to_dict(a) for a in rows],
            "reports":[{"id":r.id,"asset_id":r.asset_id,"value":public_report(r.value)} for r in reports],
            "overlap":split_overlap([(names.get(r.asset_id,str(r.asset_id)),r.value) for r in reports])}

def process_next_download(engine, *, fetcher=None, guard=None, request_id=None) -> bool:
    # One worker is the supported deployment; claim is still compare-and-set for accidental overlap.
    if guard: guard()
    now=utcnow()
    with session_scope(engine) as session:
        candidate=session.scalar(select(DownloadRequest).where(True if request_id is None else DownloadRequest.id == request_id, func.coalesce(DownloadRequest.manifest["acquisition"].as_string(), "huggingface") != "local", or_(DownloadRequest.status=="approved",
            (DownloadRequest.status=="running") & (True if request_id is not None else DownloadRequest.lease_until<now))).order_by(DownloadRequest.id).limit(1))
        if not candidate:
            return False
        ident=candidate.id
        result=session.execute(update(DownloadRequest).where(DownloadRequest.id==ident,
            func.coalesce(DownloadRequest.manifest["acquisition"].as_string(), "huggingface") != "local",
            or_(DownloadRequest.status=="approved",(DownloadRequest.status=="running") & (True if request_id is not None else DownloadRequest.lease_until<now)))
            .values(status="running",lease_until=now+timedelta(hours=1)).execution_options(synchronize_session=False))
        if not result.rowcount:
            return False
    try:
        _execute_download(engine,ident,fetcher=fetcher,guard=guard)
    except Exception as exc:
        # A worker that lost ownership must leave durable work for its successor.
        if guard: guard()
        with session_scope(engine) as session:
            request=session.get(DownloadRequest,ident)
            request.status="failed"
            request.error=f"{type(exc).__name__}: {str(exc)[:400]}"
            request.lease_until=None
            session.add(Audit(actor="download-worker",action="download_failed",detail=f"request={ident}; {request.error}"))
    return True

def _execute_download(engine,ident,*,fetcher=None,guard=None):
    if guard: guard()
    root=storage_root()
    with session_scope(engine) as session:
        request=session.get(DownloadRequest,ident)
        manifest=json.loads(json.dumps(request.manifest))
        if request.approved_hash!=digest_json(manifest) or not request.decided_by:
            raise DownloadError("下载审批缺失或范围已变更")
        version=session.get(DatasetVersion,request.dataset_version_id)
        if digest_json(version.metadata_json)!=manifest["metadata_hash"]:
            raise DownloadError("版本证据发生变化")
        version_id=version.id
        limit=request.max_bytes
        stored=session.scalar(select(func.coalesce(func.sum(Asset.size_bytes),0))) or 0
        quota=int(os.environ.get("XIAOYI_STORAGE_QUOTA_BYTES",str(100*1024**3)))
        present={a.path:a for a in session.scalars(select(Asset).where(Asset.dataset_version_id==version_id))}
        missing_bytes=sum(item["size"] for item in manifest["files"] if item["path"] not in present)
        if stored+missing_bytes>quota:
            raise DownloadError("存储配额不足")
        if shutil.disk_usage(root).free < missing_bytes * 2:
            raise DownloadError("磁盘剩余空间不足以暂存并验证下载")
    api=HfApi(endpoint=os.environ.get("HF_ENDPOINT","https://huggingface.co")) if fetcher is None else None
    total=0
    for item in manifest["files"]:
        if guard: guard()
        name=safe_path(item["path"])
        with session_scope(engine) as session:
            request=session.get(DownloadRequest,ident)
            if request.status!="running" or request.approved_hash!=digest_json(manifest):
                raise DownloadError("下载已停止或审批失效")
            prior=session.scalar(select(Asset).where(Asset.dataset_version_id==version_id,Asset.path==name))
            if prior:
                existing_path=root/prior.storage_key
                if existing_path.is_file() and sha256_file(existing_path)==prior.sha256:
                    if prior.size_bytes != item["size"] or total+prior.size_bytes>limit:
                        raise DownloadError("已有文件与批准范围大小不符")
                    previous_request=session.get(DownloadRequest,prior.download_request_id)
                    if previous_request.status!="completed":
                        prior.download_request_id=ident
                    total+=prior.size_bytes
                    request.actual_bytes=total
                    continue
        staging=root/"staging"/str(ident)
        staging.mkdir(parents=True,exist_ok=True)
        local=Path(fetcher(manifest,name,staging) if fetcher else api.hf_hub_download(
            repo_id=manifest["repo_id"],repo_type="dataset",revision=manifest["revision"],
            filename=name,local_dir=staging))
        if guard: guard()
        size=local.stat().st_size
        if size!=item["size"] or total+size>limit:
            raise DownloadError("实际文件大小与批准清单不符，停止下载")
        digest=sha256_file(local)
        key=f"sha256/{digest[:2]}/{digest}"
        destination=root/key
        destination.parent.mkdir(parents=True,exist_ok=True)
        if destination.exists():
            if sha256_file(destination)!=digest:
                raise DownloadError("存储文件校验失败")
        else:
            temp=destination.with_suffix(f".{ident}.partial")
            shutil.copyfile(local,temp)
            if guard: guard()
            os.replace(temp,destination)
        report=profile_file(destination,name)
        if guard: guard()
        with session_scope(engine) as session:
            prior=session.scalar(select(Asset).where(Asset.dataset_version_id==version_id,Asset.path==name))
            if prior and prior.sha256!=digest:
                raise DownloadError("同版本文件内容发生变化")
            if not prior:
                asset=Asset(dataset_version_id=version_id,download_request_id=ident,path=name,sha256=digest,
                            size_bytes=size,storage_key=key,
                            source_url=f"https://huggingface.co/datasets/{manifest['repo_id']}/resolve/{manifest['revision']}/{name}")
                session.add(asset)
                session.flush()
                session.add(QualityReport(asset_id=asset.id,dataset_version_id=version_id,value=report))
            request=session.get(DownloadRequest,ident)
            total+=size
            request.actual_bytes=total
            request.lease_until=utcnow()+timedelta(hours=1)
        # Only remove the exact just-downloaded file inside this request's verified staging root.
        if local.resolve().is_relative_to(staging.resolve()):
            local.unlink(missing_ok=True)
    if guard: guard()
    with session_scope(engine) as session:
        request=session.get(DownloadRequest,ident)
        request.status="completed"
        request.actual_bytes=total
        request.lease_until=None
        session.add(Audit(actor="download-worker",action="download_completed",detail=f"request={ident}; bytes={total}"))
