"""Stable research bundles and a dependency-light verifier/loader."""
from __future__ import annotations
import io
import json
import hashlib
import zipfile
from pathlib import Path
from sqlalchemy import select, update
from .models import DatasetVersion, Evidence, Review, Snapshot, Audit
from .models_assets import Asset, QualityReport, ResearchBundle
from .downloads import storage_root, sha256_file, digest_json, asset_to_dict, DownloadError
from .profiling import public_report
from .pairing import export_snapshot

LOADER = '''"""Verify and load the exact approved research inputs. No network or generated code."""
import csv, hashlib, json
from pathlib import Path

def verify(root):
    root=Path(root).resolve()
    manifest=json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    for asset in manifest["assets"]:
        path=(root/asset["bundle_path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Path outside bundle")
        if not path.is_file() or path.stat().st_size!=asset["size_bytes"]:
            raise ValueError("Missing or wrong-size asset: "+asset["path"])
        digest=hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda:stream.read(1048576),b""): digest.update(chunk)
        if digest.hexdigest()!=asset["sha256"]:
            raise ValueError("Checksum mismatch: "+asset["path"])
    return manifest

def load(root, asset_id):
    manifest=verify(root)
    asset=next(a for a in manifest["assets"] if a["id"]==asset_id)
    path=Path(root)/asset["bundle_path"]
    suffix=Path(asset["path"]).suffix.lower()
    if suffix in {".csv",".tsv"}:
        with path.open(encoding="utf-8",newline="") as stream:
            return list(csv.DictReader(stream,delimiter="\\t" if suffix==".tsv" else ","))
    if suffix in {".jsonl",".ndjson"}:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if suffix==".json": return json.loads(path.read_text(encoding="utf-8"))
    if suffix==".txt": return path.read_text(encoding="utf-8")
    if suffix==".parquet":
        import pyarrow.parquet as pq
        return pq.read_table(path)
    return path  # Images/3D remain immutable files for the caller's chosen framework.

if __name__=="__main__":
    import argparse
    p=argparse.ArgumentParser(); p.add_argument("root",nargs="?",default=".")
    manifest=verify(p.parse_args().root)
    print("Verified",len(manifest["assets"]),"assets")
'''

def _croissant(manifest):
    # Explicit term mappings matter: conformsTo is Dublin Core, records/hash are
    # Croissant terms, while name/distribution/contentUrl belong to schema.org.
    context = {"@vocab": "http://schema.org/", "sc": "http://schema.org/",
        "cr": "http://mlcommons.org/croissant/", "dct": "http://purl.org/dc/terms/",
        "conformsTo": "dct:conformsTo",
        "dataType": {"@id": "cr:dataType", "@type": "@vocab"}}
    for term in ("recordSet", "field", "source", "fileObject", "extract", "column", "sha256"):
        context[term] = "cr:" + term
    formats = {".csv": "text/csv", ".tsv": "text/tab-separated-values",
        ".parquet": "application/vnd.apache.parquet", ".json": "application/json",
        ".jsonl": "application/x-ndjson", ".ndjson": "application/x-ndjson", ".txt": "text/plain",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gltf": "model/gltf+json", ".glb": "model/gltf-binary"}
    records, files = [], []
    reports = {r["asset_id"]: r["value"] for r in manifest.get("reports", [])}
    for asset in manifest["assets"]:
        file_id = f"asset-{asset['id']}"
        suffix = Path(asset["path"]).suffix.lower()
        entry = {"@type": "cr:FileObject", "@id": file_id, "name": asset["path"],
            "contentUrl": asset["bundle_path"], "contentSize": str(asset["size_bytes"]),
            "sha256": asset["sha256"]}
        if suffix in formats:
            entry["encodingFormat"] = formats[suffix]
        files.append(entry)
        report = reports.get(asset["id"], {})
        # Only describe columns actually observed by supported tabular parsers.
        # Semantic types, labels, keys and splits stay absent when unverified.
        if suffix not in {".csv", ".tsv", ".parquet"} or report.get("kind") != "table":
            continue
        columns = report.get("columns", [])
        if not columns or report.get("valid") is False:
            continue
        record_id = file_id + "/records"
        fields = []
        for index, column in enumerate(columns):
            field = {"@type": "cr:Field", "@id": record_id + f"/column-{index}", "name": column,
                "source": {"fileObject": {"@id": file_id}, "extract": {"column": column}}}
            if suffix in {".csv", ".tsv"}:
                field["dataType"] = "sc:Text"  # The fixed loader reads CSV/TSV cells as strings.
            fields.append(field)
        records.append({"@type": "cr:RecordSet", "@id": record_id, "name": asset["path"],
            "description": f"Observed columns; inspected {report.get('rows_scanned', 0)} rows; scope={report.get('scope', 'unknown')}. See manifest reports for coverage.",
            "field": fields})
    result = {"@context": context, "@type": "sc:Dataset",
        "name": manifest["snapshot"]["idea"]["title"],
        "description": manifest["snapshot"]["idea"].get("statement") or "Version-locked approved research inputs.",
        "version": str(manifest["snapshot"]["version"]), "conformsTo": "http://mlcommons.org/croissant/1.1",
        "license": manifest["snapshot"].get("licenses", []), "distribution": files}
    if records:
        result["recordSet"] = records
    return result


def build_bundle(session, snapshot_id: int) -> Path:
    # Hold a database lock until the caller commits the bundle record. This also
    # works across API processes; a Python mutex would not protect multiple workers.
    if session.get_bind().dialect.name == "sqlite":
        session.execute(update(Snapshot).where(Snapshot.id == snapshot_id).values(id=Snapshot.id))
    else:
        session.execute(select(Snapshot.id).where(Snapshot.id == snapshot_id).with_for_update())
    root=storage_root()
    existing=session.scalar(select(ResearchBundle).where(ResearchBundle.snapshot_id==snapshot_id))
    if existing:
        path=root/existing.storage_key
        if not path.is_file() or sha256_file(path)!=existing.sha256:
            raise DownloadError("已发布研究包文件缺失或校验失败，需要从备份恢复")
        return path
    snapshot=export_snapshot(session,snapshot_id)
    version_ids=[]
    for item in snapshot.get("datasets",[]):
        ident=item.get("dataset_version_id") or item.get("dataset",{}).get("dataset_version_id")
        if not ident:
            raise DownloadError("旧快照缺少版本证据，不能生成可复现研究包")
        version=session.get(DatasetVersion,ident)
        if not version or version.provenance_status!="verified":
            raise DownloadError("快照包含未验证的历史版本")
        version_ids.append(ident)
    frozen_assets=snapshot.get("assets",[])
    if not frozen_assets or set(a["dataset_version_id"] for a in frozen_assets)!=set(version_ids):
        raise DownloadError("快照未冻结完整资产清单：先完成下载，再重新冻结科研快照；此快照仍可导出目录 JSON")
    assets=[]
    entries=[]
    from .models_assets import DownloadRequest
    for frozen in frozen_assets:
        asset=session.get(Asset,frozen["id"])
        if not asset or asset.sha256!=frozen["sha256"] or asset.path!=frozen["path"]:
            raise DownloadError("冻结的资产与存储记录不一致")
        request=session.get(DownloadRequest,asset.download_request_id)
        if not request or request.status!="completed" or request.approved_hash!=request.manifest_hash:
            raise DownloadError("冻结资产对应的批准下载未完整完成")
        path=root/asset.storage_key
        if not path.is_file() or sha256_file(path)!=asset.sha256:
            raise DownloadError("数据文件校验失败，无法生成研究包")
        assets.append(asset)
        entries.append({**frozen,"bundle_path":f"data/{asset.dataset_version_id}/{asset.path}"})
    manifest={"format":"xiaoyi-research-bundle","format_version":1,"snapshot":snapshot,"assets":entries,
              "evidence":[e for item in snapshot["datasets"] for e in item.get("evidence",[])],
              "reports":snapshot.get("reports",[]),"enrichments":snapshot.get("enrichments",[]),
              "reviews":snapshot.get("reviews",[]),
              "loader_version":"research-loader/1","reproduction":"Run python load_data.py before use; no network required."}
    key=f"bundles/snapshot-{snapshot_id}-{digest_json(manifest)[:16]}.zip"
    destination=root/key
    destination.parent.mkdir(parents=True,exist_ok=True)
    temp=destination.with_suffix(".partial")
    with zipfile.ZipFile(temp,"w",compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json",json.dumps(manifest,ensure_ascii=False,indent=2))
        z.writestr("croissant.json",json.dumps(_croissant(manifest),ensure_ascii=False,indent=2))
        z.writestr("load_data.py",LOADER)
        z.writestr("README.txt","科研输入已按版本冻结。先运行 python load_data.py 验证所有文件。\n完整度与内容质量分别记录；抽样结果不能解释为全量结论。\n许可及引用见 manifest.json。Parquet 加载需安装 pyarrow。")
        for asset,entry in zip(assets,entries):
            z.write(root/asset.storage_key,entry["bundle_path"])
    temp.replace(destination)
    session.add(ResearchBundle(snapshot_id=snapshot_id,manifest=manifest,sha256=sha256_file(destination),storage_key=key))
    session.add(Audit(actor="bundle-service",action="bundle_created",detail=f"snapshot={snapshot_id}"))
    return destination
