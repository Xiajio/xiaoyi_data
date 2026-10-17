"""Bounded candidate mining; findings are evidence, never automatic deletions."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from collections import Counter
import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert
from .db import session_scope
from .models_samples import ProfileRun, MetricObservation, SampleRelation
from .sample_engine import (asset_path, canonical, checkpoint, iter_build_rows, ready_build,
                            store_file, workspace, cached_operation, remember_operation)

RELATION_INDEX_LIMIT = 10000

EXACT_CANDIDATES_SQL = "SELECT id,benchmark,label,split FROM features WHERE content=? ORDER BY id,benchmark LIMIT ?"
BAND_CANDIDATES_SQL = "SELECT f.id,f.benchmark,f.signature,f.modality FROM (SELECT id,benchmark FROM bands WHERE band=? AND value=? ORDER BY id,benchmark LIMIT ?) b JOIN features f ON f.id=b.id AND f.benchmark=b.benchmark"

class MiningSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build_id: int = Field(gt=0)
    benchmark_build_id: int | None = Field(default=None, gt=0)
    max_hamming: int = Field(default=3, ge=0, le=3)
    bucket_limit: int = Field(default=128, ge=1, le=1000)
    max_findings: int = Field(default=100000, ge=1, le=1000000)

def text_fingerprint(text):
    # Word and character shingles keep Chinese and short texts meaningful.
    tokens = re.findall(r"\w+", text.casefold())
    compact = re.sub(r"\s+", "", text.casefold())
    features = tokens + [compact[i:i+3] for i in range(max(1, len(compact)-2))]
    vector = [0] * 64
    for token in features:
        number = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            vector[bit] += 1 if number & (1 << bit) else -1
    return sum(1 << bit for bit, value in enumerate(vector) if value >= 0)

def image_fingerprint(key):
    with Image.open(asset_path(key)) as image:
        pixels = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS).tobytes()
    return sum(1 << (y*8+x) for y in range(8) for x in range(8) if pixels[y*9+x] > pixels[y*9+x+1])

def execute_mining(engine, payload, guard=None):
    spec = MiningSpec.model_validate(payload)
    with session_scope(engine) as session:
        cached = cached_operation(session, guard)
        if cached is not None:
            return cached
        checkpoint(guard, session)
        build = ready_build(session, spec.build_id)
        manifest, build_hash = build.manifest, build.manifest_hash
        benchmark = ready_build(session, spec.benchmark_build_id) if spec.benchmark_build_id else None
        benchmark_manifest = benchmark.manifest if benchmark else None
        benchmark_hash = benchmark.manifest_hash if benchmark else None
    stats = Counter()
    with workspace() as directory:
        database = sqlite3.connect(directory / "mining.sqlite")
        database.execute("PRAGMA temp_store=FILE")
        database.execute("PRAGMA cache_size=-16384")
        database.execute("CREATE TABLE features (id TEXT, benchmark INTEGER, content TEXT, label TEXT, modality TEXT, split TEXT, signature TEXT, PRIMARY KEY(id,benchmark))")
        database.execute("CREATE INDEX content_idx ON features(content,id,benchmark)")
        database.execute("CREATE TABLE bands (band INTEGER, value TEXT, id TEXT, benchmark INTEGER)")
        database.execute("CREATE INDEX bands_idx ON bands(band,value,id,benchmark)")
        database.execute("CREATE TABLE findings (source TEXT, target TEXT, kind TEXT, score REAL, algorithm TEXT, PRIMARY KEY(source,target,kind))")
        def finding(source, target, kind, score, algorithm):
            if source == target and kind != "benchmark_contamination_candidate":
                return
            if stats["findings"] >= spec.max_findings:
                stats["finding_limit_reached"] = 1
                return
            a,b = (source,target) if kind == "benchmark_contamination_candidate" else sorted((source,target))
            cur = database.execute("INSERT OR IGNORE INTO findings VALUES(?,?,?,?,?)", (a,b,kind,score,algorithm))
            stats["findings"] += cur.rowcount
        def ingest(rows, is_benchmark):
            for row in rows:
                stats["benchmark_rows" if is_benchmark else "input_rows"] += 1
                if sum((stats["input_rows"], stats["benchmark_rows"])) % 500 == 0:
                    checkpoint(guard)
                    database.commit()
                sid, content, modality = row["sample_id"], json.loads(row["content"]), row["modality"]
                signature = None
                if modality == "text":
                    text = " ".join(str(value) for value in content.values())
                    if len(text)>100000:
                        stats["signature_inputs_truncated"] += 1
                    signature = text_fingerprint(text[:100000])
                elif modality == "image":
                    signature = image_fingerprint(content["storage_key"])
                if not is_benchmark:
                    exact = database.execute(EXACT_CANDIDATES_SQL, (row["content_hash"], spec.bucket_limit+1)).fetchall()
                    if len(exact) > spec.bucket_limit:
                        stats["truncated_buckets"] += 1
                    for target, other_benchmark, label, split in exact[:spec.bucket_limit]:
                        if other_benchmark:
                            finding(sid,target,"benchmark_contamination_candidate",1.0,"exact-content-sha256/1")
                        elif label != row["label"]:
                            finding(sid,target,"label_conflict",1.0,"exact-content-label/1")
                        if not other_benchmark and split != row["split"]:
                            finding(sid,target,"split_leakage",1.0,"exact-content-split/1")
                    candidates = {}
                    if signature is not None:
                        for band in range(4):
                            value = str((signature >> (band*16)) & 65535)
                            matches = database.execute(BAND_CANDIDATES_SQL, (band,value,spec.bucket_limit+1)).fetchall()
                            if len(matches) > spec.bucket_limit:
                                stats["truncated_buckets"] += 1
                            for target, other_benchmark, other_signature, other_modality in matches[:spec.bucket_limit]:
                                if modality == other_modality:
                                    candidates[(target,other_benchmark)] = int(other_signature,16)
                        for (target,other_benchmark), other in candidates.items():
                            distance = (signature ^ other).bit_count()
                            if distance <= spec.max_hamming:
                                kind = "benchmark_contamination_candidate" if other_benchmark else ("text_near_duplicate" if modality == "text" else "image_near_duplicate")
                                finding(sid,target,kind,1-distance/64,"simhash64/1" if modality == "text" else "dhash64/1")
                database.execute("INSERT OR IGNORE INTO features VALUES(?,?,?,?,?,?,?)", (sid,is_benchmark,row["content_hash"],row["label"],modality,row["split"],f"{signature:016x}" if signature is not None else None))
                if signature is not None:
                    database.executemany("INSERT INTO bands VALUES(?,?,?,?)", [(band,str((signature >> (band*16)) & 65535),sid,is_benchmark) for band in range(4)])
        try:
            if benchmark_manifest:
                ingest(iter_build_rows(benchmark_manifest), 1)
            ingest(iter_build_rows(manifest), 0)
            database.commit()
            checkpoint(guard)
            slices = [{"label": label, "split": split, "count": count} for label,split,count in database.execute("SELECT label,split,count(*) FROM features WHERE benchmark=0 GROUP BY label,split ORDER BY count(*) DESC,label,split LIMIT 101")]
            stats["slice_output_truncated"] = int(len(slices) > 100)
            schema = pa.schema([("source_sample_id",pa.string()),("target_sample_id",pa.string()),("kind",pa.string()),("score",pa.float64()),("algorithm",pa.string())])
            file = directory / "findings.parquet"
            with pq.ParquetWriter(file, schema, compression="zstd") as writer:
                cursor = database.execute("SELECT source,target,kind,score,algorithm FROM findings ORDER BY source,target,kind")
                while records := cursor.fetchmany(500):
                    checkpoint(guard)
                    writer.write_table(pa.Table.from_pylist([dict(zip(schema.names, row)) for row in records], schema=schema))
            artifact = store_file(file)
            result = {"build_id": spec.build_id, "findings": stats["findings"]}
            with session_scope(engine) as session:
                checkpoint(guard, session)
                profile = ProfileRun(build_id=spec.build_id, producer="candidate-mining/1", manifest={"findings":artifact,"spec":spec.model_dump(),"build_hash":build_hash,"benchmark_hash":benchmark_hash}, summary={**dict(stats),"slices":slices[:100],"relation_index_count":min(stats["findings"], RELATION_INDEX_LIMIT),"relation_index_truncated":stats["findings"] > RELATION_INDEX_LIMIT,"automatic_removal":False,"coverage":"all samples; capped candidate buckets","truncated":bool(stats["truncated_buckets"] or stats["finding_limit_reached"] or stats["signature_inputs_truncated"])})
                session.add(profile)
                session.flush()
                profile_id = profile.id
                session.add(MetricObservation(profile_run_id=profile.id, name="mining_counts", value=dict(stats)))
                # The immutable artifact is authoritative; the bounded relation index aids review.
                cursor = database.execute("SELECT source,target,kind,score,algorithm FROM findings ORDER BY source,target,kind LIMIT ?", (RELATION_INDEX_LIMIT,))
                while records := cursor.fetchmany(500):
                    session.execute(insert(SampleRelation), [dict(profile_run_id=profile_id,source_sample_id=a,target_sample_id=b,kind=k,score=s,algorithm=v) for a,b,k,s,v in records])
                result["profile_run_id"] = profile_id
                remember_operation(session, guard, result)
            return result
        finally:
            database.close()
