# 小亿数据 · 科研数据集平台

面向内网科研小团队的数据集发现、整理、人工核验与科研输入交付系统。React 工作台调用 FastAPI，LangGraph worker 执行受限的发现与整理任务，下载和发布必须由人类账号批准。

当前工作区实现 v0.5：版本证据固定、真实团队认证、科研任务、按范围批准下载、内容报告、研究包及 Python 加载入口。**本地验收完成情况与仍待联调的项目见 [实施验收](docs/research-platform.md)**。真实模型 API、PostgreSQL 容器及服务器试点需要部署配置后验收。

## 启动

需要 Python 3.12+、Node.js 22.13+。从仓库根目录运行：

```powershell
python -m venv platform/.venv
platform/.venv/Scripts/python -m pip install -e "./platform[dev]"
npm ci
$env:XIAOYI_CATALOG_DB = "$PWD/data/catalog.db"
platform/.venv/Scripts/python -m xiaoyi_catalog.auth --username admin --role admin
platform/.venv/Scripts/python -m uvicorn xiaoyi_catalog.service:create_app --factory --host 127.0.0.1 --port 5280
```

新终端运行 `npm run dev`，以刚创建的账号登录。API 和 worker 必须使用同一数据库、存储目录和模型环境配置；完整步骤见 [部署说明](docs/deployment.md)。

```powershell
$env:XIAOYI_CATALOG_DB = "$PWD/data/catalog.db"
platform/.venv/Scripts/python -m xiaoyi_catalog.worker
```

首页 **工作概览** 展示真实目录规模、14/30 天入库趋势、模态与许可分布、整理核验状态，以及采集任务和同步计划。点击模态或许可可进入相应目录筛选；未知信息、未完成交付如实显示。

工作台 **HF 采集** 支持一键采集、每日同步、进度和失败原因查看，默认每次最多 50 条，完全不依赖 LLM；详见 [采集说明](docs/huggingface-collection.md)。未配置模型时，科研智能体任务会明确失败并保留原因；目录浏览、人工登记、核验和已批准资产交付仍可使用。Linux 将 `platform/.venv/Scripts/python` 换为 `platform/.venv/bin/python`。

## 使用流程

1. 创建科研目标、硬约束和排序偏好。
2. worker 从 Hugging Face 和人工登记目录发现候选，固定版本证据，生成待人工核验的建议。
3. 对照证据核验候选，明确选择文件和体积上限，人工批准下载。
4. 查看已获取文件的样本、测量报告和派生记录。
5. 冻结并批准科研快照，导出目录 JSON 或带文件的研究包；在另一目录运行包内 `python load_data.py` 校验。

数据源自报、模型推断、人工确认、实际测量分别保存。未知许可、模态或规模不会被视为满足硬约束。模型和 MCP 凭据不能批准下载或发布。

## 工程与验收

```powershell
platform/.venv/Scripts/python -m pytest platform/tests -q
npm run check
```

浏览器测试自动建立独立测试库。若 Windows 临时目录不可写，为 pytest 指定一个新的仓库内 `--basetemp output/pytest-<本次名称>`。PostgreSQL 集成测试仅在提供 `XIAOYI_TEST_POSTGRES` 时执行；CI 已配置 pgvector/PG16 服务。

- [实现范围、架构与验收记录](docs/research-platform.md)
- [本地与 Docker Compose 部署](docs/deployment.md)
- [备份和恢复](docs/backup-recovery.md)
- [后端 CLI、MCP 与版本恢复](platform/README.md)
- [四类真实任务试点方案](docs/pilot-evaluation.md)

## 独立合成演示

`?mode=demo` 是原有七页面合成演示，`host.html` 是 iframe 宿主示例。其虚构研究对象、目录扫描、处理进度、浏览器角色和 localStorage 状态只用于演示，与真实策展库分别运行。

生产构建默认进入真实策展模式；同源部署默认使用 `/api`。构建期 `VITE_CATALOG_API` 可指定另一个 API origin。静态演示的子路径部署与宿主集成保留，见 [宿主协议](docs/embedding.md)、[演示操作](docs/demo-guide.md) 和 [演示仪表台](docs/dashboard.md)。
