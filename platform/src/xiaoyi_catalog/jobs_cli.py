"""Authenticated task CLI: JSON requests are parameters, never executable programs."""
import argparse
import json
import os
from pathlib import Path
from .auth import resolve
from .db import create_engine_for,session_scope
from .jobs import managed,cancel,retry,to_dict
from .api_jobs import submit_public_job

def main(argv=None):
    parser=argparse.ArgumentParser(description="提交、查询或管理后台任务")
    parser.add_argument("command",choices=["submit","status","cancel","retry","build","experiment"])
    parser.add_argument("--db",default=os.getenv("XIAOYI_CATALOG_DB","./data/catalog.db"))
    parser.add_argument("--token-env",default="XIAOYI_CLI_TOKEN")
    parser.add_argument("--id",type=int)
    parser.add_argument("--request",type=Path)
    args=parser.parse_args(argv)
    engine=create_engine_for(args.db)
    try:
        with session_scope(engine) as session:
            actor=resolve(session,os.getenv(args.token_env,""))
            if actor is None:
                raise ValueError("CLI需要有效账号会话token")
            if args.command!="status" and actor.role not in {"researcher","reviewer","admin"}:
                raise ValueError("当前账号无操作权限")
            if args.command in {"submit","build","experiment"}:
                if not args.request or args.request.stat().st_size>65536:
                    raise ValueError("需要最多64KiB的JSON参数文件")
                body=json.loads(args.request.read_text(encoding="utf-8"))
                if args.command=="submit":
                    item=submit_public_job(session,body["kind"],body.get("payload",{}),actor,body.get("idempotency_key"))
                elif args.command=="build":
                    from .api_builds import submit_build_job
                    from .models_jobs import Job
                    submitted=submit_build_job(session,body.get("recipe",body),actor)
                    item=session.get(Job,submitted["job_id"])
                else:
                    from .api_builds import submit_experiment_job
                    from .models_jobs import Job
                    submitted=submit_experiment_job(session,body,actor)
                    item=session.get(Job,submitted["job_id"])
            else:
                if not args.id:
                    raise ValueError("需要--id")
                item={"status":managed,"cancel":cancel,"retry":retry}[args.command](session,args.id,actor)
            result=to_dict(item)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except (ValueError,KeyError,OSError) as exc:
        print(str(exc))
        return 2
    finally:
        engine.dispose()

if __name__=="__main__":
    raise SystemExit(main())
