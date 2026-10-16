"""Bounded, deterministic content inspection. Source claims are never measurements."""
from __future__ import annotations
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from itertools import islice

ROW_LIMIT = 10000
BYTE_LIMIT = 32 * 1024 * 1024

def profile_file(path: Path, name: str) -> dict:
    suffix = Path(name).suffix.lower()
    report = {"producer": "profile/1", "origin": "measured", "format": suffix.lstrip("."),
              "scope": "file", "size_bytes": path.stat().st_size, "issues": []}
    try:
        if suffix in {".csv", ".tsv", ".jsonl", ".ndjson", ".txt", ".parquet", ".json"}:
            return {**report, **_table(path, suffix)}
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
            from PIL import Image
            with Image.open(path) as im:
                report.update(kind="image", width=im.width, height=im.height, mode=im.mode)
                im.verify()
            report["valid"] = True
        elif suffix == ".obj":
            vertices = faces = 0
            with path.open("rb") as stream:
                data = stream.read(BYTE_LIMIT + 1)
            for line in data[:BYTE_LIMIT].splitlines():
                vertices += line.startswith(b"v ")
                faces += line.startswith(b"f ")
            report.update(kind="3d", vertices=vertices, faces=faces, scope="sample" if len(data)>BYTE_LIMIT else "file",
                          truncated=len(data)>BYTE_LIMIT, bytes_inspected=min(len(data),BYTE_LIMIT))
        elif suffix == ".ply":
            with path.open("rb") as stream:
                header = stream.read(65536).split(b"end_header")[0].decode("ascii", errors="replace")
            if not header.startswith("ply"):
                raise ValueError("invalid PLY header")
            counts = {}
            for line in header.splitlines():
                words = line.split()
                if len(words)==3 and words[0]=="element":
                    counts[words[1]] = int(words[2])
            report.update(kind="3d", scope="header", declared_elements=counts,
                          note="仅核对文件头；几何数量为头部声明，未验证全部几何内容")
        elif suffix in {".gltf", ".glb"}:
            if path.stat().st_size > BYTE_LIMIT:
                return {**report, "kind":"3d", "scope":"metadata", "issues":["文件超过基础解析上限"]}
            data = path.read_bytes()
            if suffix == ".glb":
                import struct
                if data[:4] != b"glTF" or len(data)<20:
                    raise ValueError("invalid GLB")
                length, kind = struct.unpack("<II", data[12:20])
                if kind != 0x4E4F534A or length > len(data)-20:
                    raise ValueError("invalid GLB JSON chunk")
                data = data[20:20+length]
            gltf = json.loads(data)
            report.update(kind="3d", scope="structure", meshes=len(gltf.get("meshes",[])),
                          nodes=len(gltf.get("nodes",[])),
                          external_resources=sum(1 for b in gltf.get("buffers",[])+gltf.get("images",[])
                                                 if b.get("uri") and not b["uri"].startswith("data:")),
                          note="不解析或请求外部资源；不代表几何完整性认证")
        else:
            report.update(kind="file", scope="metadata", issues=["当前格式仅登记文件和校验和"])
    except Exception as exc:
        report.update(valid=False, issues=[f"内容检查失败：{type(exc).__name__}: {str(exc)[:160]}"])
    return report

def _table(path, suffix):
    size=path.stat().st_size
    if suffix == ".parquet":
        import pyarrow.parquet as pq
        f=pq.ParquetFile(path)
        batches=f.iter_batches(batch_size=min(ROW_LIMIT,1000))
        rows=list(islice((row for batch in batches for row in batch.to_pylist()),ROW_LIMIT))
        total=f.metadata.num_rows
        truncated=total>len(rows)
    else:
        with path.open("rb") as f:
            data=f.read(BYTE_LIMIT+1)
        clipped=len(data)>BYTE_LIMIT
        text=data[:BYTE_LIMIT].decode("utf-8",errors="replace")
        if clipped:
            text=text.rsplit("\n",1)[0]
        if suffix in {".csv",".tsv"}:
            import io
            all_rows=csv.DictReader(io.StringIO(text),delimiter="\t" if suffix==".tsv" else ",")
        elif suffix in {".jsonl",".ndjson"}:
            all_rows=(json.loads(line) for line in text.splitlines() if line.strip())
        elif suffix==".json":
            if clipped:
                return {"kind":"table","scope":"metadata","issues":["JSON 超过基础解析上限"],"truncated":True}
            doc=json.loads(text)
            all_rows=doc if isinstance(doc,list) else [doc]
        else:
            all_rows=({"text":line} for line in text.splitlines())
        limited=list(islice(all_rows,ROW_LIMIT+1))
        rows=limited[:ROW_LIMIT]
        truncated=clipped or len(limited)>ROW_LIMIT
        total=None if truncated else len(rows)
    rows=[r if isinstance(r,dict) else {"value":r} for r in rows]
    fields=sorted({str(k) for r in rows for k in r})
    missing={key:sum(r.get(key) is None or r.get(key)=="" for r in rows) for key in fields}
    hashes=[hashlib.sha256(json.dumps(r,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest() for r in rows]
    return {"kind":"table","scope":"sample" if truncated else "file","truncated":truncated,
            "rows_scanned":len(rows),"total_rows":total,"sample_method":"first_rows",
            "row_limit":ROW_LIMIT,"byte_limit":BYTE_LIMIT,"columns":fields,"missing_counts":missing,
            "duplicate_rows":len(hashes)-len(set(hashes)),"_row_hashes":sorted(set(hashes)),
            "issues":[]}

def public_report(value):
    return {key:val for key,val in value.items() if not key.startswith("_")}

def split_overlap(reports):
    results=[]
    for i,(name,a) in enumerate(reports):
        for other,b in reports[i+1:]:
            shared=len(set(a.get("_row_hashes",[])) & set(b.get("_row_hashes",[])))
            if shared:
                results.append({"left":name,"right":other,"identical_sample_rows":shared,
                                "scope":"inspected_rows","note":"相同记录提示；是否属于训练/测试切分需人工核对"})
    return results
