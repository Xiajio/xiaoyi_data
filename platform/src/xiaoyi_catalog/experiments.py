"""External classification predictions bound to immutable train/evaluation builds."""
from __future__ import annotations
import json
import sqlite3
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from .db import session_scope
from .downloads import sha256_file
from .models_assets import Asset
from .models_samples import Experiment
from .sample_engine import (asset_path, canonical, checkpoint, iter_source, iter_build_rows,
                            ready_build, store_file, workspace, source_record, validate_inputs, remember_operation)

class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1,max_length=256)
    version: str = Field(min_length=1,max_length=256)
    training_budget: str = Field(min_length=1,max_length=256)
    seed: int = Field(default=42,ge=0)

class EvaluationProtocol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["1"] = "1"
    task: Literal["classification"] = "classification"
    evaluation_build_id: int = Field(gt=0)
    split: Literal["train","validation","test"] = "test"
    require_complete: bool = True

class ExperimentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build_id: int = Field(gt=0)
    prediction_asset_id: int = Field(gt=0)
    model: ModelSpec
    protocol: EvaluationProtocol

def create_experiment(session, payload, actor):
    spec = ExperimentSpec.model_validate(payload)
    ready_build(session, spec.build_id)
    ready_build(session, spec.protocol.evaluation_build_id)
    asset = session.get(Asset,spec.prediction_asset_id)
    if not asset or not asset.path.lower().endswith((".jsonl",".ndjson",".parquet")):
        raise ValueError("predictions must be an approved JSONL or Parquet asset")
    validate_inputs(session,[source_record(asset)])
    experiment = Experiment(build_id=spec.build_id,requested_by=actor,model=spec.model.model_dump(),
        protocol=spec.protocol.model_dump(),prediction_asset_id=asset.id,prediction_sha256=asset.sha256)
    session.add(experiment)
    session.flush()
    return experiment

def execute_experiment_import(engine, payload, guard=None):
    experiment_id = int(payload["experiment_id"])
    with session_scope(engine) as session:
        checkpoint(guard,session)
        experiment = session.get(Experiment,experiment_id,with_for_update=True)
        if not experiment:
            raise ValueError("experiment not found")
        if experiment.status == "completed":
            result={"experiment_id":experiment.id,"metrics":experiment.metrics}
            remember_operation(session,guard,result)
            return result
        build = ready_build(session,experiment.build_id)
        protocol = EvaluationProtocol.model_validate(experiment.protocol)
        evaluation = ready_build(session,protocol.evaluation_build_id)
        evaluation_manifest = evaluation.manifest
        build_hash,evaluation_hash = build.manifest_hash,evaluation.manifest_hash
        asset = session.get(Asset,experiment.prediction_asset_id)
        validate_inputs(session,[source_record(asset)])
        if asset.sha256 != experiment.prediction_sha256:
            raise ValueError("prediction source pin changed")
        source = {"path":asset.path,"storage_key":asset.storage_key,"sha256":experiment.prediction_sha256}
        experiment.status,experiment.error = "running",""
    try:
        return _import(engine,experiment_id,protocol,source,evaluation_manifest,build_hash,evaluation_hash,guard)
    except Exception as exc:
        try:
            with session_scope(engine) as session:
                checkpoint(guard,session)
                item = session.get(Experiment,experiment_id,with_for_update=True)
                if item and item.status == "running":
                    item.status,item.error = "failed",str(exc)[:2000]
        except Exception:
            pass
        raise

def _import(engine,experiment_id,protocol,source,evaluation_manifest,build_hash,evaluation_hash,guard):
    source_path = asset_path(source["storage_key"])
    if sha256_file(source_path) != source["sha256"]:
        raise ValueError("prediction source hash mismatch")
    with workspace() as directory:
        db = sqlite3.connect(directory / "evaluation.sqlite")
        db.execute("PRAGMA temp_store=FILE")
        db.execute("PRAGMA cache_size=-16384")
        db.execute("CREATE TABLE expected (id TEXT PRIMARY KEY,label TEXT,seen INTEGER DEFAULT 0)")
        try:
            expected = 0
            for row in iter_build_rows(evaluation_manifest):
                if row["split"] != protocol.split:
                    continue
                if row["label"] == "null":
                    raise ValueError("evaluation samples require labels")
                db.execute("INSERT INTO expected(id,label) VALUES(?,?)", (row["sample_id"],row["label"]))
                expected += 1
                if expected % 500 == 0:
                    checkpoint(guard)
                    db.commit()
            db.commit()
            if not expected:
                raise ValueError("selected evaluation split is empty")
            schema = pa.schema([("sample_id",pa.string()),("prediction",pa.string()),("expected",pa.string()),("correct",pa.bool_())])
            all_path,error_path = directory/"predictions.parquet",directory/"errors.parquet"
            count,correct = 0,0
            batch,errors = [],[]
            with pq.ParquetWriter(all_path,schema,compression="zstd") as writer, pq.ParquetWriter(error_path,schema,compression="zstd") as error_writer:
                def flush():
                    if batch:
                        writer.write_table(pa.Table.from_pylist(batch,schema=schema))
                        batch.clear()
                    if errors:
                        error_writer.write_table(pa.Table.from_pylist(errors,schema=schema))
                        errors.clear()
                for position,row,error in iter_source(source_path,source["path"]):
                    if error or not isinstance(row,dict) or not {"sample_id","build_id","prediction"} <= row.keys():
                        raise ValueError(f"invalid prediction row {position}")
                    if isinstance(row["build_id"],bool) or row["build_id"] != protocol.evaluation_build_id:
                        raise ValueError("prediction build_id must match the fixed evaluation build")
                    sid = row["sample_id"]
                    if not isinstance(sid,str):
                        raise ValueError("sample_id must be a string")
                    match = db.execute("SELECT label,seen FROM expected WHERE id=?",(sid,)).fetchone()
                    if not match:
                        raise ValueError("prediction sample is not in the selected evaluation build/split")
                    if match[1]:
                        raise ValueError("duplicate prediction sample")
                    prediction = canonical(row["prediction"])
                    hit = prediction == match[0]
                    record = {"sample_id":sid,"prediction":prediction,"expected":match[0],"correct":hit}
                    batch.append(record)
                    if not hit:
                        errors.append(record)
                    count += 1
                    correct += int(hit)
                    db.execute("UPDATE expected SET seen=1 WHERE id=?",(sid,))
                    if len(batch) >= 500:
                        checkpoint(guard)
                        db.commit()
                        flush()
                flush()
            if not count or (protocol.require_complete and count != expected):
                raise ValueError("predictions do not cover the required evaluation split")
            metrics = {"accuracy":correct/count,"correct":correct,"predictions":count,"expected_samples":expected,
                       "coverage":count/expected,"error_count":count-correct,"producer":"classification-exact-match/1"}
            manifest = {"predictions":store_file(all_path),"errors":store_file(error_path),"source":source,
                        "training_build_hash":build_hash,"evaluation_build_hash":evaluation_hash,"protocol":protocol.model_dump()}
            with session_scope(engine) as session:
                checkpoint(guard,session)
                experiment = session.get(Experiment,experiment_id,with_for_update=True)
                if experiment.status != "running":
                    raise ValueError("experiment no longer running")
                experiment.manifest,experiment.metrics,experiment.status = manifest,metrics,"completed"
                result={"experiment_id":experiment_id,"metrics":metrics}
                remember_operation(session,guard,result)
            return result
        finally:
            db.close()

def compare_experiments(session, ids):
    if len(ids) < 2 or len(ids) > 20 or len(set(ids)) != len(ids):
        raise ValueError("comparison needs 2 to 20 distinct experiments")
    rows = [session.get(Experiment,i) for i in ids]
    if any(not row or row.status != "completed" for row in rows):
        raise ValueError("all experiments must be completed")
    baseline = rows[0]
    for row in rows:
        if row.model != baseline.model or row.protocol != baseline.protocol or row.manifest["evaluation_build_hash"] != baseline.manifest["evaluation_build_hash"] or row.metrics["coverage"] != 1:
            raise ValueError("comparison requires the same fixed model, budget, seed, complete evaluation set and protocol")
    return {"baseline_experiment_id":baseline.id,"evaluation_build_hash":baseline.manifest["evaluation_build_hash"],
            "items":[{"experiment_id":row.id,"build_id":row.build_id,"metrics":row.metrics,
                      "accuracy_delta":row.metrics["accuracy"]-baseline.metrics["accuracy"]} for row in rows]}
