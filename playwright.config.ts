import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir:'./tests/browser', timeout:30000, fullyParallel:true,workers:process.env.CI?2:4,
  use:{baseURL:'http://127.0.0.1:4173',headless:true,viewport:{width:1440,height:1000},screenshot:'only-on-failure',trace:'retain-on-failure'},
  reporter:[['list'],['html',{open:'never'}]],
  webServer:[
    {command:'npm run preview -- --port 4173 --strictPort',url:'http://127.0.0.1:4173',reuseExistingServer:!process.env.CI,timeout:30000},
    // 先重建确定性 fixture 库，再启动目录服务（建库必须在服务占用文件之前，故同一命令串行）
    {command:'platform\\.venv\\Scripts\\python.exe platform\\tests\\fixtures\\make_e2e_db.py platform\\data\\e2e-catalog.db && platform\\.venv\\Scripts\\python.exe -m uvicorn xiaoyi_catalog.service:create_app --factory --host 127.0.0.1 --port 5280',
     url:'http://127.0.0.1:5280/api/overview',reuseExistingServer:!process.env.CI,timeout:60000,
     env:{XIAOYI_CATALOG_DB:'platform/data/e2e-catalog.db',XIAOYI_CATALOG_CORS:'http://127.0.0.1:4173',PYTHONIOENCODING:'utf-8'}},
  ],
});
