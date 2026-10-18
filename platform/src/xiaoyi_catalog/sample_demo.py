"""Isolated synthetic acceptance demo and opt-in million-row benchmark.

Never connects to the configured production database. The output directory must not exist.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from collections import Counter, defaultdict

from sqlalchemy import select,func
from .db import create_engine_for, init_db, session_scope
from .downloads import digest_json
from .models import Source,Dataset,DatasetVersion
from .models_assets import Asset,DownloadRequest
from .models_samples import DatasetBuild
from .sample_engine import create_build,execute_build,iter_build_rows,store_file,canonical
from .experiments import create_experiment,execute_experiment_import,compare_experiments
from .mining import execute_mining


def initialize(directory):
    directory=Path(directory).resolve()
    directory.mkdir(parents=True,exist_ok=False)
    os.environ["XIAOYI_STORAGE_ROOT"]=str(directory/"assets")
    engine=create_engine_for(directory/"fixture.sqlite")
    init_db(engine)
    with session_scope(engine) as session:
        source=Source(kind="manual",endpoint="synthetic-local-acceptance")
        session.add(source);session.flush()
        dataset=Dataset(source_id=source.id,source_key="synthetic-acceptance",name="Synthetic fixture; not research evidence",fingerprint="fixture-v1",license="CC0",license_class="permissive")
        session.add(dataset);session.flush()
        version=DatasetVersion(dataset_id=dataset.id,revision="synthetic/1",provenance_status="verified",metadata_json={"synthetic":True,"license":"CC0"})
        session.add(version);session.flush()
        version_id=version.id
    return engine,directory,version_id


def register_fixture(engine,version_id,path):
    artifact=store_file(path)
    manifest={"acquisition":"synthetic-fixture","files":[{"path":path.name,"size":artifact["size_bytes"],"sha256":artifact["sha256"]}]}
    fingerprint=digest_json(manifest)
    with session_scope(engine) as session:
        request=DownloadRequest(dataset_version_id=version_id,requested_by="synthetic-fixture-generator",status="completed",manifest=manifest,manifest_hash=fingerprint,approved_hash=fingerprint,max_bytes=max(1,artifact["size_bytes"]),decided_by="fixture-only-local-generated-content")
        session.add(request);session.flush()
        asset=Asset(dataset_version_id=version_id,download_request_id=request.id,path=path.name,**artifact)
        session.add(asset);session.flush()
        return asset.id


def run_build(engine,recipe):
    with session_scope(engine) as session:
        item=create_build(session,recipe,"synthetic-fixture");ident=item.id
    execute_build(engine,ident)
    with session_scope(engine) as session:
        item=session.get(DatasetBuild,ident)
        return ident,item.manifest,item.manifest_hash


def vector(text):
    return Counter(re.findall(r"[a-z]+",text.lower()))


def train_centroids(rows,seed=42,budget=80):
    chosen=sorted(rows,key=lambda row:digest_json([seed,row["sample_id"]]))[:budget]
    totals=defaultdict(Counter);counts=Counter()
    for row in chosen:
        label=json.loads(row["label"])
        totals[label].update(vector(json.loads(row["content"])["text"]))
        counts[label]+=1
    centroids={label:{token:value/counts[label] for token,value in values.items()} for label,values in totals.items()}
    return centroids,len(chosen)


def predict(centroids,text):
    values=vector(text)
    value_norm=math.sqrt(sum(v*v for v in values.values())) or 1
    def similarity(label):
        centroid=centroids[label]
        norm=math.sqrt(sum(v*v for v in centroid.values())) or 1
        return sum(v*centroid.get(token,0) for token,v in values.items())/(value_norm*norm)
    return max(sorted(centroids),key=similarity)


def run_demo(directory):
    engine,directory,version_id=initialize(directory)
    try:
        training=[]
        for i in range(120):
            label="positive" if i%2==0 else "negative"
            word="pleasant bright useful" if label=="positive" else "poor broken dull"
            training.append({"text":f"{word} product item {i}","label":label,"entity":f"train-{i}"})
        training+=training[:24]
        training += [{"text":f"poor broken dull mislabeled {i}","label":"invalid","entity":f"noise-{i}"} for i in range(12)]
        evaluation=[{"text":("pleasant bright useful" if i%2==0 else "poor broken dull")+f" heldout review {i}","label":"positive" if i%2==0 else "negative"} for i in range(40)]
        train_path,eval_path=directory/"training.jsonl",directory/"evaluation.jsonl"
        train_path.write_text("".join(canonical(row)+"\n" for row in training),encoding="utf-8")
        eval_path.write_text("".join(canonical(row)+"\n" for row in evaluation),encoding="utf-8")
        train_asset=register_fixture(engine,version_id,train_path)
        eval_asset=register_fixture(engine,version_id,eval_path)
        evaluation_id,evaluation_manifest,evaluation_hash=run_build(engine,{"asset_ids":[eval_asset],"modality":"text","label_column":"label","splits":{"test":1},"seed":42})
        common={"asset_ids":[train_asset],"modality":"text","label_column":"label","group_column":"entity","splits":{"train":1},"seed":42}
        configurations={"raw":{**common,"exact_dedup":False},
                        "cleaned":{**common,"exact_dedup":False,"allowed_labels":["positive","negative"]},
                        "deduplicated":{**common,"exact_dedup":True,"allowed_labels":["positive","negative"]},
                        "selected":{**common,"exact_dedup":True,"allowed_labels":["positive","negative"],"sample_budget":80}}
        experiment_ids=[];runs=[]
        for name,recipe in configurations.items():
            started=time.perf_counter()
            ident,manifest,manifest_hash=run_build(engine,recipe)
            centroids,used=train_centroids(list(iter_build_rows(manifest)),seed=42,budget=80)
            prediction_file=directory/f"predictions-{name}.jsonl"
            with prediction_file.open("w",encoding="utf-8",newline="\n") as stream:
                for row in iter_build_rows(evaluation_manifest):
                    result=predict(centroids,json.loads(row["content"])["text"])
                    stream.write(canonical({"build_id":evaluation_id,"sample_id":row["sample_id"],"prediction":result})+"\n")
            prediction_asset=register_fixture(engine,version_id,prediction_file)
            payload={"build_id":ident,"prediction_asset_id":prediction_asset,
                     "model":{"name":"word-count-nearest-centroid","version":"cpu-fixture/1","training_budget":"one pass, at most 80 fixed-rank samples","seed":42},
                     "protocol":{"evaluation_build_id":evaluation_id,"split":"test","require_complete":True}}
            with session_scope(engine) as session:
                experiment=create_experiment(session,payload,"synthetic-fixture");experiment_ids.append(experiment.id)
            metrics=execute_experiment_import(engine,{"experiment_id":experiment_ids[-1]})["metrics"]
            runs.append({"configuration":name,"build_id":ident,"manifest_hash":manifest_hash,"training_samples":used,"counts":manifest["counts"],"metrics":metrics,"seconds":time.perf_counter()-started})
        with session_scope(engine) as session:
            comparison=compare_experiments(session,experiment_ids)
        mining=execute_mining(engine,{"build_id":runs[0]["build_id"],"benchmark_build_id":evaluation_id})
        result={"evidence":"synthetic acceptance fixture; no claim of real research gains", "seed":42,
                "evaluation_build_id":evaluation_id,"evaluation_manifest_hash":evaluation_hash,
                "inputs":[{"path":p.name,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()} for p in (train_path,eval_path)],
                "runs":runs,"comparison":comparison,"mining":mining}
        (directory/"acceptance.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        return result
    finally:
        engine.dispose()


def peak_rss_bytes():
    if os.name!="nt":
        import resource,sys
        value=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value if sys.platform=="darwin" else value*1024
    import ctypes
    from ctypes import wintypes
    class MemoryInfo(ctypes.Structure):
        _fields_=[("cb",wintypes.DWORD),("PageFaultCount",wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in
            ("PeakWorkingSetSize","WorkingSetSize","QuotaPeakPagedPoolUsage","QuotaPagedPoolUsage","QuotaPeakNonPagedPoolUsage","QuotaNonPagedPoolUsage","PagefileUsage","PeakPagefileUsage")]
    info=MemoryInfo();info.cb=ctypes.sizeof(info)
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    psapi=ctypes.WinDLL("psapi",use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(MemoryInfo),wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(info),info.cb):
        raise OSError(ctypes.get_last_error())
    return info.PeakWorkingSetSize



def source_version():
    """Hash local Python sources, including uncommitted implementation changes."""
    root=Path(__file__).parent
    hashes={path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*.py"))}
    return {"sha256":digest_json(hashes),"files":hashes}


def benchmark_hardware():
    import platform
    import subprocess
    result={"os":platform.platform(),"python":platform.python_version(),"sqlite":__import__("sqlite3").sqlite_version,
            "machine":platform.machine(),"logical_cpus":os.cpu_count(),"processor":platform.processor()}
    if os.name=="nt":
        command="[ordered]@{cpu=(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name);physical_cores=(Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum;memory_bytes=(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory} | ConvertTo-Json -Compress"
        try:
            detail=subprocess.run(["powershell","-NoProfile","-Command",command],capture_output=True,text=True,timeout=15,check=True)
            result.update(json.loads(detail.stdout))
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            result["hardware_detail_error"]=type(exc).__name__
    else:
        try:
            result["memory_bytes"]=os.sysconf("SC_PAGE_SIZE")*os.sysconf("SC_PHYS_PAGES")
        except (ValueError,OSError,AttributeError):
            pass
    return result


def seed_read_catalog(engine,total=300):
    with session_scope(engine) as session:
        source_id=session.scalar(select(Source.id))
        existing=session.scalar(select(func.count()).select_from(Dataset))
        for index in range(existing,total):
            metadata={"name":f"Synthetic catalog {index}","source_key":f"synthetic/catalog-{index}",
                      "modalities":["text"],"languages":["en"],"license":"CC0","license_class":"permissive", "synthetic":True}
            dataset=Dataset(source_id=source_id,fingerprint=digest_json(metadata),name=metadata["name"],
                            source_key=metadata["source_key"],modalities=["text"],languages=["en"],
                            card_text="Synthetic metadata for concurrent directory read acceptance.",
                            downloads=index,license="CC0",license_class="permissive")
            session.add(dataset);session.flush()
            session.add(DatasetVersion(dataset_id=dataset.id,revision="synthetic/1",provenance_status="verified",metadata_json=metadata))


def _benchmark_http_server(database,connection,stop):
    """Separate API process, bound to an OS-selected localhost-only port."""
    import socket
    import threading
    import uvicorn
    from .service import create_app
    listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1",0));listener.listen(128)
        server=uvicorn.Server(uvicorn.Config(create_app(str(database),dev_auth=True),log_level="error",access_log=False))
        def request_stop():
            stop.wait()
            server.should_exit=True
        threading.Thread(target=request_stop,daemon=True).start()
        connection.send({"port":listener.getsockname()[1]})
        server.run(sockets=[listener])
        connection.send({"server_peak_rss_bytes":peak_rss_bytes()})
    except BaseException as exc:
        connection.send({"error":f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        listener.close();connection.close()


class ConcurrentCatalogReads:
    def __init__(self,database,concurrency):
        self.database,self.concurrency=database,concurrency
        self.latencies,self.failures=[],[]
        self.errors,self.attempts=0,0
        self.report={"read_concurrency":concurrency,"read_count":0,"read_errors":0,"read_p95_ms":None,
                     "read_transport":"real localhost HTTP; independent Uvicorn process" if concurrency else "disabled",
                     "read_endpoint":"/api/datasets?page=1&size=20","read_wave_pause_seconds":0.2,
                     "read_latency_scope":"HTTP response body received and validated; successful requests only"}

    def __enter__(self):
        if not self.concurrency:
            return self
        import httpx
        import multiprocessing
        import threading
        context=multiprocessing.get_context("spawn")
        self.parent_connection,child_connection=context.Pipe()
        self.server_stop=context.Event()
        self.process=context.Process(target=_benchmark_http_server,args=(self.database,child_connection,self.server_stop),daemon=True)
        self.stop=threading.Event()
        self.load_ready=threading.Event()
        self.load_begin=threading.Event()
        self.process.start();child_connection.close()
        try:
            if not self.parent_connection.poll(30):
                raise RuntimeError("benchmark HTTP server did not start")
            handshake=self.parent_connection.recv()
            if "error" in handshake:
                raise RuntimeError(handshake["error"])
            self.url=f"http://127.0.0.1:{handshake['port']}"
            deadline=time.monotonic()+30
            with httpx.Client(timeout=1,trust_env=False) as client:
                while True:
                    try:
                        response=client.get(self.url+"/api/health")
                        response.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if time.monotonic()>deadline:
                            raise RuntimeError("benchmark HTTP server readiness timed out")
                        time.sleep(0.05)
            self.thread=threading.Thread(target=self._read_waves,name="benchmark-directory-reads",daemon=True)
            self.thread.start()
            if not self.load_ready.wait(30):
                raise RuntimeError("benchmark load generator did not start")
            self.started=time.perf_counter()
            self.load_begin.set()
            return self
        except BaseException:
            self._stop_server()
            raise

    def _read_waves(self):
        import httpx
        from concurrent.futures import ThreadPoolExecutor
        clients=[httpx.Client(timeout=30,trust_env=False) for _ in range(self.concurrency)]
        self.load_ready.set()
        self.load_begin.wait()
        def read(client):
            started=time.perf_counter()
            try:
                response=client.get(self.url+self.report["read_endpoint"])
                response.raise_for_status()
                body=response.json()
                if body.get("total")!=300 or len(body.get("items",[]))!=20:
                    raise ValueError("unexpected catalog fixture response")
                return (time.perf_counter()-started)*1000,None
            except Exception as exc:
                return None,f"{type(exc).__name__}: {exc}"[:500]
        try:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                while not self.stop.is_set():
                    for latency,error in pool.map(read,clients):
                        self.attempts+=1
                        if error:
                            self.errors+=1
                            if len(self.failures)<20:
                                self.failures.append(error)
                        else:
                            self.latencies.append(latency)
                    self.stop.wait(0.2)
        except BaseException as exc:
            self.errors+=1
            self.failures.append(f"load generator: {type(exc).__name__}: {exc}"[:500])
        finally:
            for client in clients:
                client.close()

    def _stop_server(self):
        self.server_stop.set()
        self.process.join(timeout=10)
        if self.process.is_alive():
            self.process.terminate();self.process.join(timeout=5)
            self.report["server_shutdown_forced"]=True
        if self.parent_connection.poll():
            try:
                self.report.update(self.parent_connection.recv())
            except EOFError:
                pass
        self.parent_connection.close()

    def __exit__(self,*args):
        if not self.concurrency:
            return
        self.stop.set()
        self.thread.join(timeout=35)
        self.report.update(read_window_seconds=time.perf_counter()-self.started,read_count=len(self.latencies),
                           read_attempts=self.attempts,read_errors=self.errors,read_error_examples=self.failures,
                           read_p95_ms=sorted(self.latencies)[math.ceil(len(self.latencies)*0.95)-1] if self.latencies else None,
                           read_max_ms=max(self.latencies) if self.latencies else None,
                           read_wave_completion="in-flight requests finish after the build if necessary")
        if self.thread.is_alive():
            self.report["load_generator_shutdown_timeout"]=True
        self._stop_server()


def run_benchmark(directory,rows=1000000,read_concurrency=0):
    engine,directory,version_id=initialize(directory)
    code_before=source_version()
    hardware=benchmark_hardware()
    try:
        if read_concurrency:
            seed_read_catalog(engine)
        start=time.perf_counter()
        file=directory/"million.csv"
        with file.open("w",encoding="utf-8",newline="") as stream:
            writer=csv.writer(stream);writer.writerow(["feature","label","group"])
            for index in range(rows):
                writer.writerow([f"record-{index}",str(index%2),f"entity-{index//5}"])
        generated=time.perf_counter()-start
        aid=register_fixture(engine,version_id,file)
        with ConcurrentCatalogReads(directory/"fixture.sqlite",read_concurrency) as reads:
            start=time.perf_counter()
            ident,manifest,digest=run_build(engine,{"asset_ids":[aid],"label_column":"label","group_column":"group","shard_rows":2000})
            elapsed=time.perf_counter()-start
        code_after=source_version()
        memory=peak_rss_bytes()
        result={"evidence":"synthetic fixture on the recorded local OS/hardware and SQLite; not Linux/PostgreSQL production acceptance",
                "rows":rows,"output_rows":manifest["counts"]["output_rows"],"build_id":ident,"manifest_hash":digest,
                "generation_seconds":generated,"build_seconds":elapsed,"rows_per_second":rows/max(elapsed,1e-9),
                "peak_rss_bytes":memory,"under_8_gib":memory<8*1024**3,
                "memory_scope":"build and HTTP load generator process; API process peak reported separately",
                "database":"isolated SQLite fixture.sqlite","catalog_metadata_rows":300 if read_concurrency else 1,
                "hardware":hardware,"code_version":code_before,"code_changed_during_run":code_before!=code_after,
                "code_sha256_after":code_after["sha256"],**reads.report}
        (directory/"benchmark.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
        return result
    finally:
        engine.dispose()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=["demo","benchmark"])
    parser.add_argument("--output",required=True,help="new isolated output directory, must not exist")
    parser.add_argument("--rows",type=int,default=1000000)
    parser.add_argument("--read-concurrency",type=int,default=0,help="concurrent localhost directory reads during benchmark; 0 disables server")
    args=parser.parse_args()
    if not 1<=args.rows<=100000000:
        parser.error("--rows must be between 1 and 100000000")
    if not 0<=args.read_concurrency<=50:
        parser.error("--read-concurrency must be between 0 and 50")
    if args.mode=="demo" and args.read_concurrency:
        parser.error("--read-concurrency is only supported in benchmark mode")
    result=run_demo(args.output) if args.mode=="demo" else run_benchmark(args.output,args.rows,args.read_concurrency)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=="__main__":
    main()
