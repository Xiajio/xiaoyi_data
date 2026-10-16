import { existsSync } from 'node:fs';
import { spawn, spawnSync } from 'node:child_process';
const windows = process.platform === 'win32';
const local = windows ? 'platform/.venv/Scripts/python.exe' : 'platform/.venv/bin/python';
const python = process.env.PYTHON || (existsSync(local) ? local : 'python');
const env = {...process.env, XIAOYI_CATALOG_DB:'platform/data/e2e-catalog.db', XIAOYI_CATALOG_DEV_AUTH:'1',
  XIAOYI_CATALOG_CORS:'http://127.0.0.1:4173', XIAOYI_CATALOG_EMBED:'0', PYTHONIOENCODING:'utf-8'};
const fixture=spawnSync(python,['platform/tests/fixtures/make_e2e_db.py',env.XIAOYI_CATALOG_DB],{env,stdio:'inherit'});
if(fixture.status!==0) process.exit(fixture.status??1);
const server=spawn(python,['-m','uvicorn','xiaoyi_catalog.service:create_app','--factory','--host','127.0.0.1','--port','5281'],{env,stdio:'inherit'});
for(const signal of ['SIGINT','SIGTERM']) process.on(signal,()=>{ server.kill(); });
server.on('exit',code=>process.exit(code??0));
