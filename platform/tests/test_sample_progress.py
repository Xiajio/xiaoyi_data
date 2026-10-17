"""Progress reports reflect measured work, including rejected/duplicate rows."""
import json
from xiaoyi_catalog.auth import Principal
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.models_samples import DatasetBuild
from xiaoyi_catalog.sample_engine import create_build,execute_build,execute_export,publish_build
from xiaoyi_catalog.asset_import import execute_derive
from test_sample_engine import source


class RecordingGuard:
    def __init__(self):
        self.records=[]
    def __call__(self,session=None):
        pass
    def report_progress(self,stage,processed,total=None,**counts):
        self.records.append({"stage":stage,"processed":processed,"total":total,**counts})
    def stage(self,name):
        return [row for row in self.records if row["stage"]==name]


def test_build_progress_counts_read_rejections_selection_and_committed_shards(engine,source):
    records=[{"text":f"row {index}","label":"valid"} for index in range(501)]
    records += [records[0],{"text":"","label":"valid"}]
    aid=source("progress.jsonl","".join(json.dumps(row)+"\n" for row in records))
    with session_scope(engine) as session:
        build=create_build(session,{"asset_ids":[aid],"modality":"text","label_column":"label","sample_budget":7,"shard_rows":3},"fixture")
        ident=build.id
    guard=RecordingGuard()
    result=execute_build(engine,ident,guard)
    reading=guard.stage("reading_rows")
    assert reading[-1]["processed"]==503
    assert any(row["processed"]==500 for row in reading)
    selection=guard.stage("selecting_rows")[-1]
    assert selection["processed"]==selection["total"]==501
    writing=guard.stage("writing_shards")
    assert writing[-1]["processed"]==writing[-1]["total"]==7
    assert [row["processed"] for row in writing]==sorted(row["processed"] for row in writing)
    completed=guard.stage("completed")[-1]
    assert completed["processed"]==completed["total"]==result["sample_count"]==7
    assert completed["input_rows"]==503
    assert completed["shards"]==3


def test_export_progress_counts_distinct_objects_and_actual_copied_bytes(engine,source):
    aid=source("export-progress.txt","first\nsecond\nthird\n")
    with session_scope(engine) as session:
        build=create_build(session,{"asset_ids":[aid],"modality":"text","shard_rows":2},"fixture")
        ident=build.id
    execute_build(engine,ident)
    with session_scope(engine) as session:
        build=session.get(DatasetBuild,ident)
        manifest=build.manifest
        publish_build(session,ident,Principal(1,"reviewer","reviewer"),build.manifest_hash)
    guard=RecordingGuard()
    execute_export(engine,ident,guard)
    objects={item["storage_key"]:item for item in manifest["inputs"]+manifest["shards"]+[manifest["exclusions"]]}
    copying=guard.stage("copying_objects")
    assert copying[-1]["processed"]==copying[-1]["total"]==len(objects)
    assert copying[-1]["bytes_copied"]==sum(item["size_bytes"] for item in objects.values())
    assert [row["processed"] for row in copying]==sorted(row["processed"] for row in copying)


def test_derive_progress_reports_input_output_and_removed_rows(engine,source):
    lines=[f"row-{index}" for index in range(501)]
    aid=source("derive-progress.csv","text\n"+"\n".join(lines+[lines[0],lines[1]])+"\n")
    guard=RecordingGuard()
    result=execute_derive(engine,aid,guard=guard,actor="reviewer")
    assert result["origin"]=="derived"
    deduplicating=guard.stage("deduplicating")
    assert any(row["processed"]==500 for row in deduplicating)
    final=deduplicating[-1]
    assert final["processed"]==503 and final["total"] is None
    assert final["output_rows"]==501 and final["removed_rows"]==2
    completed=guard.stage("completed")[-1]
    assert completed["processed"]==completed["total"]==503
    assert completed["output_rows"]==501
