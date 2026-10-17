"""Bounded CPU processing of approved immutable assets into reproducible Parquet shards."""
from __future__ import annotations
import csv
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import tempfile
from collections import Counter
from contextlib import contextmanager, closing

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image
from sqlalchemy import delete, select
from .db import session_scope
from .downloads import digest_json, sha256_file, storage_root
from .models import Audit
from .models_assets import Asset
from .models_samples import (Sample, AssetReference, DatasetBuild, BuildSample, AnnotationSet,
                            EntityGroup, ProfileRun, MetricObservation)
from .recipes import Recipe

SCHEMA = pa.schema([(name, pa.string()) for name in
    ("sample_id", "content_hash", "modality", "content", "label", "group_key", "split", "source")])
MAX_ROW_BYTES = 4 * 1024 * 1024

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)

def checkpoint(guard, session=None):
    if guard:
        guard(session) if session is not None else guard()

def cached_operation(session, guard):
    """Publication receipt survives a crash before the worker updates Job.status."""
    job_id=getattr(guard,"ident",None)
    if job_id is None:
        return None
    checkpoint(guard,session)
    from .models_jobs import Artifact
    artifact=session.scalar(select(Artifact).where(Artifact.job_id==job_id))
    if artifact:
        if digest_json(artifact.manifest)!=artifact.content_hash:
            raise ValueError("job artifact receipt hash mismatch")
        return artifact.manifest
    return None

def remember_operation(session,guard,result):
    job_id=getattr(guard,"ident",None)
    if job_id is not None:
        from .models_jobs import Artifact
        if cached_operation(session,guard) is None:
            session.add(Artifact(job_id=job_id,content_hash=digest_json(result),manifest=result))

def asset_path(key: str, root=None) -> Path:
    root = (root or storage_root()).resolve()
    path = (root / key).resolve()
    if not re.fullmatch(r"sha256/[0-9a-f]{2}/[0-9a-f]{64}",key) or not path.is_relative_to(root) or not path.is_file():
        raise ValueError("missing or unsafe content-addressed asset")
    return path

def store_file(path: Path, root=None, guard=None):
    root = root or storage_root()
    if guard:
        from .derivation import _hash
        digest = _hash(path,guard)
    else:
        digest = sha256_file(path)
    key = f"sha256/{digest[:2]}/{digest}"
    target = root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if sha256_file(target) != digest:
            raise ValueError("content-addressed storage hash mismatch")
    else:
        # A complete temporary file is renamed atomically; concurrent identical writers are safe.
        fd, name = tempfile.mkstemp(prefix="artifact-", suffix=".partial", dir=target.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            if guard:
                with path.open("rb") as source, temporary.open("wb") as destination:
                    count=0
                    while chunk:=source.read(1024*1024):
                        destination.write(chunk);count+=1
                        if count%16==0:
                            checkpoint(guard)
            else:
                shutil.copyfile(path, temporary)
            checkpoint(guard)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return {"storage_key": key, "sha256": digest, "size_bytes": target.stat().st_size}

@contextmanager
def workspace():
    staging = storage_root() / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="samples-", dir=staging) as directory:
        yield Path(directory)

def source_record(asset):
    return {"asset_id": asset.id, "dataset_version_id": asset.dataset_version_id,
            "path": asset.path, "sha256": asset.sha256, "storage_key": asset.storage_key,
            "size_bytes": asset.size_bytes}

def validate_inputs(session, inputs):
    from .pairing import _approved_asset
    for item in inputs:
        asset = session.get(Asset,item["asset_id"])
        if not asset or source_record(asset) != item or not _approved_asset(session,asset):
            raise ValueError("source asset approval or pinned manifest is no longer valid")

def create_build(session, recipe, actor):
    recipe = Recipe.model_validate(recipe).model_dump()
    assets = list(session.scalars(select(Asset).where(Asset.id.in_(recipe["asset_ids"])).order_by(Asset.id)))
    if len(assets) != len(recipe["asset_ids"]):
        raise ValueError("all source asset IDs must exist")
    validate_inputs(session,[source_record(asset) for asset in assets])
    build = DatasetBuild(requested_by=actor, recipe=recipe, recipe_hash=digest_json(recipe),
                         input_manifest=[source_record(asset) for asset in assets])
    session.add(build)
    session.flush()
    session.add(Audit(actor=actor, action="build_requested", detail=f"build={build.id}; recipe={build.recipe_hash}"))
    return build

def insert_ignore(session, model, rows):
    if not rows:
        return
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as postgres_insert
    insert = postgres_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    # Bound the number of SQL parameters for SQLite and PostgreSQL.
    for start in range(0, len(rows), 100):
        session.execute(insert(model).values(rows[start:start+100]).on_conflict_do_nothing())

def iter_source(path, logical_path):
    suffix = Path(logical_path).suffix.lower()
    if suffix in {".csv", ".tsv"}:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t" if suffix == ".tsv" else ",")
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError("missing or duplicate column names")
            for index, row in enumerate(reader, 1):
                if None in row:
                    yield str(index), None, "column_count_mismatch"
                else:
                    yield str(index), row, None
    elif suffix in {".jsonl", ".ndjson", ".txt"}:
        with path.open(encoding="utf-8-sig") as stream:
            index = 0
            while True:
                line = stream.readline(MAX_ROW_BYTES + 1)
                if not line:
                    break
                index += 1
                if len(line.encode("utf-8")) > MAX_ROW_BYTES:
                    while line and not line.endswith("\n"):
                        line = stream.readline(MAX_ROW_BYTES + 1)
                    yield str(index), None, "row_too_large"
                    continue
                if suffix == ".txt":
                    yield str(index), {"text": line.rstrip("\r\n")}, None
                else:
                    try:
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise ValueError("row must be object")
                        yield str(index), value, None
                    except (ValueError, UnicodeError):
                        yield str(index), None, "invalid_json_object"
    elif suffix == ".parquet":
        index = 0
        for batch in pq.ParquetFile(path).iter_batches(batch_size=500):
            for row in batch.to_pylist():
                index += 1
                yield str(index), row, None
    else:
        raise ValueError(f"unsupported row file: {suffix}")

def split_for(key, recipe):
    value = int(hashlib.sha256(f"{recipe.seed}:{key}".encode()).hexdigest()[:16], 16) / 2**64
    running = 0.0
    for split in ("train", "validation", "test"):
        running += recipe.splits.get(split, 0)
        if value < running:
            return split
    return next(reversed(recipe.splits))

def _image_info(path):
    with Image.open(path) as im:
        im.verify()
    with Image.open(path) as im:
        return {"width": im.width, "height": im.height, "mode": im.mode}

def _sample(asset, position, row, recipe, image_assets):
    sid = digest_json([asset["dataset_version_id"], asset["path"], position])
    required = set(recipe.required_columns) | set(recipe.feature_columns)
    required.update(value for value in (recipe.label_column, recipe.group_column, recipe.text_column, recipe.image_path_column) if value)
    if any(key not in row or row[key] is None or row[key] == "" for key in required):
        raise ValueError("missing_required_field")
    label = recipe.labels.get(sid, recipe.labels.get(asset["path"], row.get(recipe.label_column) if recipe.label_column else None))
    if recipe.allowed_labels is not None and str(label) not in recipe.allowed_labels:
        raise ValueError("invalid_label")
    reference = asset
    if recipe.modality == "image":
        if recipe.image_path_column:
            name = str(row[recipe.image_path_column]).replace("\\", "/")
            reference = image_assets.get((asset["dataset_version_id"], name))
            if reference is None:
                raise ValueError("image_association_missing")
        info = _image_info(asset_path(reference["storage_key"]))
        content = {"image_sha256": reference["sha256"], "storage_key": reference["storage_key"], **info}
    elif recipe.modality == "text":
        text = row[recipe.text_column]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("invalid_text")
        content = {recipe.text_column: text}
    else:
        content = {key: row[key] for key in recipe.feature_columns} if recipe.feature_columns else {
            key: value for key, value in row.items() if key not in {recipe.label_column, recipe.group_column}}
        if not content:
            raise ValueError("empty_features")
    content_json = canonical(content)
    if len(content_json.encode("utf-8")) > MAX_ROW_BYTES:
        raise ValueError("row_too_large")
    content_hash = digest_json(content)
    group_key = digest_json(["group", recipe.group_column, row[recipe.group_column]]) if recipe.group_column else content_hash
    source = {**asset, "position": position, "referenced_asset_id": reference["asset_id"], "raw_content_hash": digest_json([canonical(row), reference["sha256"]]) if recipe.modality == "image" else digest_json(canonical(row))}
    return {"sample_id": sid, "content_hash": content_hash, "modality": recipe.modality,
            "content": content_json, "label": canonical(label), "group_key": group_key,
            "split": split_for(group_key, recipe), "source": canonical(source)}

def _write_shard(rows, directory, name):
    path = directory / name
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), path, compression="zstd", version="2.6")
    return {**store_file(path), "rows": len(rows)}

def execute_build(engine, build_id, guard=None):
    with session_scope(engine) as session:
        checkpoint(guard, session)
        build = session.get(DatasetBuild, build_id, with_for_update=True)
        if not build:
            raise ValueError("build not found")
        if build.status in {"ready", "published"}:
            result={"build_id": build.id, "manifest_hash": build.manifest_hash, "sample_count": build.sample_count}
            remember_operation(session,guard,result)
            return result
        recipe = Recipe.model_validate(build.recipe)
        if digest_json(build.recipe) != build.recipe_hash:
            raise ValueError("recipe has changed")
        inputs = build.input_manifest
        validate_inputs(session,inputs)
        build.status, build.error = "running", ""
        session.execute(delete(BuildSample).where(BuildSample.build_id == build_id))
    try:
        result = _execute_build(engine, build_id, recipe, inputs, guard)
        return result
    except Exception as exc:
        # Failed or cancelled execution never becomes a usable build; stale workers cannot change state.
        try:
            with session_scope(engine) as session:
                checkpoint(guard, session)
                build = session.get(DatasetBuild, build_id, with_for_update=True)
                if build and build.status == "running":
                    build.status, build.error = "failed", str(exc)[:2000]
        except Exception:
            pass
        raise

def _execute_build(engine, build_id, recipe, inputs, guard):
    root = storage_root()
    with workspace() as directory, closing(sqlite3.connect(directory / "work.sqlite")) as connection:
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA cache_size=-16384")
        connection.execute("CREATE TABLE rows (id TEXT PRIMARY KEY, dedup TEXT, rank TEXT, payload TEXT)")
        connection.execute("CREATE INDEX dedup_idx ON rows(dedup)")
        counts = Counter()
        error_path = directory / "excluded.jsonl"
        image_assets = {(a["dataset_version_id"], a["path"]): a for a in inputs}
        for asset in inputs:
            checkpoint(guard)
            path = asset_path(asset["storage_key"], root)
            if path.stat().st_size != asset["size_bytes"] or sha256_file(path) != asset["sha256"]:
                raise ValueError("source asset hash mismatch")
        with error_path.open("w", encoding="utf-8", newline="\n") as errors:
            for asset in inputs:
                checkpoint(guard)
                path = asset_path(asset["storage_key"], root)
                suffix = Path(asset["path"]).suffix.lower()
                is_image = suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
                if recipe.modality == "image" and recipe.image_path_column and is_image:
                    continue
                rows = [("image", {}, None)] if recipe.modality == "image" and not recipe.image_path_column else iter_source(path, asset["path"])
                for position, row, error in rows:
                    counts["input_rows"] += 1
                    if counts["input_rows"] % 500 == 0:
                        connection.commit()
                        checkpoint(guard)
                    try:
                        if error:
                            raise ValueError(error)
                        sample = _sample(asset, position, row, recipe, image_assets)
                        dedup = digest_json([sample["content_hash"], sample["label"]])
                        if recipe.exact_dedup and connection.execute("SELECT 1 FROM rows WHERE dedup=? LIMIT 1", (dedup,)).fetchone():
                            counts["exact_duplicates"] += 1
                            errors.write(canonical({"asset_id": asset["asset_id"], "position": position, "reason": "exact_duplicate"}) + "\n")
                            continue
                        rank = digest_json([recipe.seed, sample["sample_id"]])
                        connection.execute("INSERT INTO rows VALUES(?,?,?,?)", (sample["sample_id"], dedup, rank, canonical(sample)))
                    except (ValueError, OSError, Image.DecompressionBombError) as exc:
                        if recipe.invalid_policy == "fail":
                            raise ValueError(f"invalid source row {asset['path']}:{position}: {exc}") from exc
                        reason = str(exc)[:200]
                        counts["invalid_rows"] += 1
                        errors.write(canonical({"asset_id": asset["asset_id"], "position": position, "reason": reason}) + "\n")
        connection.commit()
        retained = connection.execute("SELECT count(*) FROM rows").fetchone()[0]
        if not retained:
            connection.close()
            raise ValueError("build contains no valid samples")
        counts["before_selection"] = retained
        if recipe.sample_budget and retained > recipe.sample_budget:
            with error_path.open("a",encoding="utf-8",newline="\n") as errors:
                for (sid,) in connection.execute("SELECT id FROM rows ORDER BY rank,id LIMIT -1 OFFSET ?",(recipe.sample_budget,)):
                    errors.write(canonical({"sample_id":sid,"reason":"sample_budget"})+"\n")
            connection.execute("DELETE FROM rows WHERE id NOT IN (SELECT id FROM rows ORDER BY rank,id LIMIT ?)", (recipe.sample_budget,))
            connection.commit()
            counts["budget_excluded"] = retained - recipe.sample_budget
        shards, pending = [], []
        pending_bytes = 0
        cursor = connection.execute("SELECT payload FROM rows ORDER BY id")
        splits = Counter()
        def flush():
            if not pending:
                return
            checkpoint(guard)
            shard_index = len(shards)
            shard = _write_shard(pending, directory, f"part-{shard_index:05}.parquet")
            with session_scope(engine) as session:
                checkpoint(guard, session)
                sample_rows, references, groups, memberships = [], [], [], []
                for index, row in enumerate(pending):
                    source = json.loads(row["source"])
                    sample_rows.append(dict(id=row["sample_id"], dataset_version_id=source["dataset_version_id"],
                        asset_id=source["asset_id"], source_position=source["position"], content_hash=source["raw_content_hash"], modality=row["modality"]))
                    references.extend(dict(sample_id=row["sample_id"], asset_id=aid, role="source")
                                      for aid in sorted({source["asset_id"], source["referenced_asset_id"]}))
                    groups.append(dict(id=row["group_key"], namespace=(recipe.group_column or "content_hash")[:64]))
                    memberships.append(dict(build_id=build_id, sample_id=row["sample_id"], split=row["split"], group_key=row["group_key"], shard=shard_index, row_index=index))
                insert_ignore(session, Sample, sample_rows)
                existing={row.id:row for row in session.scalars(select(Sample).where(Sample.id.in_([item["id"] for item in sample_rows])))}
                for item in sample_rows:
                    prior=existing.get(item["id"])
                    if prior and (prior.content_hash!=item["content_hash"] or prior.asset_id!=item["asset_id"] or prior.source_position!=item["source_position"] or prior.dataset_version_id!=item["dataset_version_id"]):
                        raise ValueError("source position already has different immutable sample content; create a new source version")
                insert_ignore(session, AssetReference, references)
                insert_ignore(session, EntityGroup, groups)
                insert_ignore(session, BuildSample, memberships)
            shards.append(shard)
            pending.clear()
        try:
            for (payload,) in cursor:
                row = json.loads(payload)
                pending.append(row)
                pending_bytes += len(payload.encode("utf-8"))
                splits[row["split"]] += 1
                counts["output_rows"] += 1
                if len(pending) >= recipe.shard_rows or pending_bytes >= 16*1024*1024:
                    flush()
                    pending_bytes = 0
            flush()
        finally:
            connection.close()
        exclusions = store_file(error_path)
        manifest = {"format": "xiaoyi.samples/1", "recipe": recipe.model_dump(), "inputs": inputs,
                    "shards": shards, "exclusions": exclusions, "counts": dict(counts), "splits": dict(splits),
                    "steps": [{"operator":"validate/1","input":counts["input_rows"],"excluded":counts["invalid_rows"]},
                              {"operator":"exact-dedup/1","excluded":counts["exact_duplicates"]},
                              {"operator":"budget-select/1","input":counts["before_selection"],"excluded":counts["budget_excluded"],"output":counts["output_rows"]},
                              {"operator":"group-split/1","output":dict(splits)}],
                    "algorithms": {"identity": "source-position-sha256/1", "dedup": "content-and-label-sha256/1", "split": "group-sha256/1", "selection": "seeded-sha256-rank/1"}}
        manifest_hash = digest_json(manifest)
        with session_scope(engine) as session:
            checkpoint(guard, session)
            build = session.get(DatasetBuild, build_id, with_for_update=True)
            if build.status != "running":
                raise ValueError("build is no longer running")
            build.manifest, build.manifest_hash = manifest, manifest_hash
            build.sample_count, build.status = counts["output_rows"], "ready"
            session.add(AnnotationSet(build_id=build_id, version_hash=digest_json([recipe.label_column, recipe.labels, shards]),
                                      extraction={"label_column": recipe.label_column, "overrides": recipe.labels}))
            profile = ProfileRun(build_id=build_id, producer="cpu-profile/1", summary={"counts": dict(counts), "splits": dict(splits), "coverage": "all input rows", "truncated": False})
            session.add(profile)
            session.flush()
            session.add(MetricObservation(profile_run_id=profile.id, name="sample_counts", value=dict(counts)))
            result={"build_id": build_id, "manifest_hash": manifest_hash, "sample_count": counts["output_rows"]}
            remember_operation(session,guard,result)
        return result

def iter_build_rows(manifest, root=None, *, verify=True):
    for shard in manifest["shards"]:
        path = asset_path(shard["storage_key"], root)
        if verify and sha256_file(path) != shard["sha256"]:
            raise ValueError("build shard hash mismatch")
        for batch in pq.ParquetFile(path).iter_batches(batch_size=500):
            yield from batch.to_pylist()

def ready_build(session, build_id):
    build = session.get(DatasetBuild, build_id)
    if not build or build.status not in {"ready", "published"}:
        raise ValueError("build is not ready")
    if digest_json(build.manifest) != build.manifest_hash:
        raise ValueError("build manifest hash mismatch")
    return build

def publish_build(session, build_id, actor, manifest_hash):
    if actor.kind != "human" or actor.role not in {"reviewer", "admin"}:
        raise ValueError("human reviewer approval required")
    build = session.get(DatasetBuild,build_id,with_for_update=True)
    build = ready_build(session, build_id)
    if manifest_hash != build.manifest_hash:
        raise ValueError("approval must bind the current manifest hash")
    validate_inputs(session,build.input_manifest)
    if build.status == "published":
        return build
    build.status, build.approved_by, build.approved_hash = "published", actor.username, manifest_hash
    session.add(Audit(actor=actor.username, action="build_published", detail=f"build={build_id}; sha256={manifest_hash}"))
    return build

def export_build(manifest, destination, root=None):
    """Portable export includes original files only when explicitly exporting a build."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    objects = manifest["inputs"] + manifest["shards"] + [manifest["exclusions"]]
    for item in objects:
        source = asset_path(item["storage_key"], root)
        if sha256_file(source) != item["sha256"]:
            raise ValueError("export source hash mismatch")
        target = destination / item["storage_key"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (destination / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
    (destination / "manifest.sha256").write_text(digest_json(manifest), encoding="ascii")
    (destination / "load.py").write_text(PORTABLE_LOADER, encoding="utf-8")
    return destination

PORTABLE_LOADER = '''"""Run: python load.py (requires pyarrow). Yields fixed sample records."""
import hashlib, json
from pathlib import Path
import pyarrow.parquet as pq
ROOT = Path(__file__).resolve().parent

def load():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    raw = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if hashlib.sha256(raw.encode()).hexdigest() != (ROOT / "manifest.sha256").read_text().strip():
        raise ValueError("manifest hash mismatch")
    for item in manifest["inputs"] + manifest["shards"] + [manifest["exclusions"]]:
        path = (ROOT / item["storage_key"]).resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError("unsafe path")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for part in iter(lambda: stream.read(1024*1024), b""):
                digest.update(part)
        if digest.hexdigest() != item["sha256"]:
            raise ValueError("artifact hash mismatch")
    for item in manifest["shards"]:
        for batch in pq.ParquetFile(ROOT / item["storage_key"]).iter_batches(batch_size=500):
            yield from batch.to_pylist()

if __name__ == "__main__":
    print(json.dumps({"samples": sum(1 for _ in load()), "verified": True}))
'''


def execute_profile(engine, asset_id, guard=None):
    """Append a measured report while preserving existing QualityReport records verbatim."""
    from .profiling import profile_file
    with session_scope(engine) as session:
        asset = session.get(Asset,asset_id)
        if not asset:
            raise ValueError("asset not found")
        source = source_record(asset)
        validate_inputs(session,[source])
        previous=cached_operation(session,guard)
        if previous is not None:
            return previous
    checkpoint(guard)
    path = asset_path(source["storage_key"])
    if sha256_file(path) != source["sha256"]:
        raise ValueError("source asset hash mismatch")
    report = profile_file(path,source["path"])
    with session_scope(engine) as session:
        checkpoint(guard,session)
        profile = ProfileRun(producer=report.get("producer","profile/1"),manifest={"source":source},summary=report)
        session.add(profile)
        session.flush()
        session.add(MetricObservation(profile_run_id=profile.id,name="file_profile",value=report))
        result={"profile_run_id":profile.id,"asset_id":asset_id,"report":report}
        remember_operation(session,guard,result)
        return result


def execute_export(engine, build_id, guard=None):
    """Stream a portable approved build ZIP; no archive is served before atomic completion."""
    import zipfile
    with session_scope(engine) as session:
        checkpoint(guard,session)
        build=ready_build(session,build_id)
        if build.status!="published" or build.approved_hash!=build.manifest_hash:
            raise ValueError("portable export requires current human publication approval")
        validate_inputs(session,build.input_manifest)
        manifest,manifest_hash=build.manifest,build.manifest_hash
        previous=cached_operation(session,guard)
        if previous is not None:
            return previous
    with workspace() as directory:
        target=directory/"training-build.zip"
        def info(name):
            item=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0))
            item.compress_type=zipfile.ZIP_STORED
            item.create_system=3
            item.external_attr=0o100644<<16
            return item
        objects={item["storage_key"]:item for item in manifest["inputs"]+manifest["shards"]+[manifest["exclusions"]]}
        with zipfile.ZipFile(target,"w",allowZip64=True) as archive:
            archive.writestr(info("manifest.json"),canonical(manifest))
            archive.writestr(info("manifest.sha256"),manifest_hash)
            archive.writestr(info("load.py"),PORTABLE_LOADER)
            for key,item in sorted(objects.items()):
                checkpoint(guard)
                digest=hashlib.sha256()
                with asset_path(key).open("rb") as source, archive.open(info(key),"w",force_zip64=True) as destination:
                    count=0
                    while chunk:=source.read(1024*1024):
                        destination.write(chunk)
                        digest.update(chunk)
                        count+=1
                        if count%16==0:
                            checkpoint(guard)
                if digest.hexdigest()!=item["sha256"]:
                    raise ValueError("export artifact hash mismatch")
        checkpoint(guard)
        artifact=store_file(target)
    with session_scope(engine) as session:
        checkpoint(guard,session)
        build=ready_build(session,build_id)
        if build.status!="published" or build.approved_hash!=manifest_hash or build.manifest_hash!=manifest_hash:
            raise ValueError("build approval changed during export")
        validate_inputs(session,build.input_manifest)
        result={"build_id":build_id,**artifact}
        remember_operation(session,guard,result)
    return result
