"""Preview only acquired assets; bounded reads, no external resources."""
from pathlib import Path
import base64
import csv
import io
import json
from itertools import islice
from .downloads import storage_root, sha256_file, DownloadError
from .models_assets import Asset, QualityReport
from .profiling import profile_file, public_report, BYTE_LIMIT
from sqlalchemy import select

def preview_asset(session,asset_id):
    asset=session.get(Asset,asset_id)
    if not asset: raise DownloadError("资产不存在")
    path=storage_root()/asset.storage_key
    if not path.is_file() or sha256_file(path)!=asset.sha256:
        raise DownloadError("资产校验失败")
    suffix=Path(asset.path).suffix.lower()
    if path.stat().st_size>BYTE_LIMIT and suffix!=".parquet":
        return {"kind":"file","note":"文件超过预览上限；请查看文件清单或研究包","sampled":True}
    if suffix in {".png",".jpg",".jpeg",".webp",".bmp",".tiff",".tif"}:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            if im.width*im.height>32_000_000:
                raise DownloadError("图像像素超过在线预览上限")
            width,height=im.size
            im=ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((640,640))
            out=io.BytesIO()
            im.save(out,format="PNG")
        return {"kind":"image","data_url":"data:image/png;base64,"+base64.b64encode(out.getvalue()).decode(),
                "width":width,"height":height,"sampled":True}
    if suffix==".parquet":
        import pyarrow.parquet as pq
        batches=pq.ParquetFile(path).iter_batches(batch_size=100)
        rows=list(islice((row for b in batches for row in b.to_pylist()),100))
        return {"kind":"table","rows":json.loads(json.dumps(rows,default=str)),"limit":100,"sampled":True}
    if suffix in {".csv",".tsv"}:
        with path.open(encoding="utf-8",errors="replace",newline="") as f:
            rows=list(islice(csv.DictReader(f,delimiter="\t" if suffix==".tsv" else ","),100))
        return {"kind":"table","rows":[{k:str(v)[:4096] for k,v in r.items()} for r in rows],"limit":100,"sampled":True}
    if suffix in {".jsonl",".ndjson"}:
        with path.open(encoding="utf-8") as f:
            rows=[json.loads(line) for line in islice((l for l in f if l.strip()),100)]
        rows=[row if isinstance(row,dict) else {"value":row} for row in rows]
        return {"kind":"table","rows":rows,"limit":100,"sampled":True}
    if suffix in {".txt",".md",".json"}:
        with path.open(encoding="utf-8",errors="replace") as f:
            text="".join(islice(f,100))[:20000]
        return {"kind":"text","text":text,"limit":100,"sampled":True}
    report=session.scalar(select(QualityReport).where(QualityReport.asset_id==asset.id))
    value=public_report(report.value) if report else profile_file(path,asset.path)
    return {"kind":value.get("kind","file"),"report":value,"note":"基础结构检查；未执行来源代码或加载外部资源"}
