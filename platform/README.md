# xiaoyi-catalog 0.5

采集、不可变版本、科研约束、认证、智能体 worker、下载审批和研究包业务服务。产品流程见 [主说明](../README.md)，服务部署见 [部署说明](../docs/deployment.md)。

## 安装与数据库

```powershell
python -m venv platform/.venv
platform/.venv/Scripts/python -m pip install -e "./platform[dev]"
$env:XIAOYI_CATALOG_DB = "$PWD/data/catalog.db"
platform/.venv/Scripts/python -m xiaoyi_catalog.cli init-db
```

数据库变量支持 SQLite 文件路径和 `postgresql+psycopg://...`。启动 API、worker 和 CLI 时运行 Alembic 正式迁移。迁移脚本位于 `src/xiaoyi_catalog/migrations`；不使用应用启动时的动态 `create_all` 替代迁移。PG 迁移建立 pgvector 扩展，数据库账号需有相应权限。升级前停止服务并备份。

历史版本缺少不可变元数据时标记 `legacy_unverified`，旧快照不被重新解释为可复现快照。重复采集不会自动升级历史证据；需要人工显式恢复：

```powershell
platform/.venv/Scripts/python -m xiaoyi_catalog.provenance --username curator recover --version 123
```

该命令要求人类 reviewer/admin，按原完整 HF commit 获取证据并留审计；不改已冻结快照。无法读取原 commit 的条目仍保留未验证状态。

## 账号与权限

```powershell
platform/.venv/Scripts/python -m xiaoyi_catalog.auth --username admin --role admin
platform/.venv/Scripts/python -m xiaoyi_catalog.auth --username researcher --role researcher
platform/.venv/Scripts/python -m xiaoyi_catalog.auth --username curator --role reviewer
platform/.venv/Scripts/python -m xiaoyi_catalog.auth --username catalog-agent --agent
```

人类密码通过交互输入；agent 命令仅在创建时输出机器 token，放入受保护的配置。默认 token 有效期 30 天，网页登录 12 小时。HTTP 会话可通过 logout 撤销。角色来自服务端，旧请求体 `role` 兼容保留但不授予权限。

MCP 使用 `XIAOYI_CATALOG_AGENT_TOKEN`；本地审批 CLI 使用人类会话变量 `XIAOYI_CLI_TOKEN` 或 `--username` 交互认证。服务器管理者拥有数据库访问权，需按管理员权限管理。

## 采集与目录

```powershell
platform/.venv/Scripts/python -m xiaoyi_catalog.cli collect --endpoint https://huggingface.co --task text-classification --language zh --limit 50
platform/.venv/Scripts/python -m xiaoyi_catalog.cli search --modality text --license-class permissive --query 情感
platform/.venv/Scripts/python -m xiaoyi_catalog.cli enrich
platform/.venv/Scripts/python -m xiaoyi_catalog.cli stats
```

卡片和元数据来自同一 revision。采集逐条持久化进度；有失败时不越过失败水位。行数与字节分开，检索在结构筛选结果中进行向量排序。

可选语义索引：安装 `./platform[embed]`，用 `cli embed` 生成真实嵌入，再配置 `XIAOYI_CATALOG_EMBED=1`。服务默认禁用嵌入以避免自动获取模型；`embed --hash` 仅用于离线测试，不代表真实语义模型。

## 人工来源和本地导入

先在工作台登记官方页面、明确版本、访问条件和许可。准备已授权文件；申请清单绑定实际哈希：

```json
{"dataset_version_id":123,"files":[{"path":"train.csv","size":11,"sha256":"<实际64位SHA-256>"}],"max_bytes":1024}
```

```powershell
platform/.venv/Scripts/python -m xiaoyi_catalog.asset_import --username curator request --manifest manifest.json
# 到工作台批准返回的 request_id；该命令不会自动批准。
platform/.venv/Scripts/python -m xiaoyi_catalog.asset_import --username curator --import-root ./data/import import --request 7 --files files.json
platform/.venv/Scripts/python -m xiaoyi_catalog.asset_import --username curator derive --asset 42
```

`files.json` 为已批准路径到本机文件路径的对象，例如 `{"train.csv":"D:/research/import/train.csv"}`，文件必须位于配置的受控导入目录。导入和派生要求 reviewer/admin；去重支持 CSV/TSV/JSONL/NDJSON，保留首次记录，产生新版本并记录输入哈希、程序版本与参数。

## HTTP、worker 与 MCP

```powershell
platform/.venv/Scripts/python -m uvicorn xiaoyi_catalog.service:create_app --factory --host 127.0.0.1 --port 5280
platform/.venv/Scripts/python -m xiaoyi_catalog.worker
platform/.venv/Scripts/python -m xiaoyi_catalog.mcp_server
```

新增 HTTP：`/api/auth/*`、`/api/agent-runs`、任务 events/cancel/retry、`/api/download-requests` 与 decision、版本 files/assets、asset preview、import-requests、derive、`/api/snapshots/{id}/bundle`、`/api/operations`。交互契约见服务的 `/docs`；核心业务仍由旧 REST 与 MCP 共用。

MCP 保留 `dataset_search / dataset_evidence / pairing_propose / snapshot_status / snapshot_export`，新增 `agent_run_create/status/cancel/retry`、`dataset_files`、`download_request`、`snapshot_bundle`。没有批准或发布工具。搜索和证据携带版本 ID。来源正文是输入数据，无法变更工具白名单、预算或身份。

SQLite checkpoint 默认是 `<数据库路径>.checkpoints.sqlite`，可用 `XIAOYI_CHECKPOINT_PATH` 指定；PostgreSQL checkpoint 保存于同一数据库。恢复包含数据库、checkpoint 与资产，见 [备份说明](../docs/backup-recovery.md)。

## 验证

```powershell
platform/.venv/Scripts/python -m pytest platform/tests -q
```

测试默认离线，不调用真实 HF 或模型。PG 测试需要独立可写测试数据库和 `XIAOYI_TEST_POSTGRES`，在隔离 schema 中执行。真实数据任务验收另见 [试点方案](../docs/pilot-evaluation.md)。


## 工作台 HF 采集

API 提交持久采集任务，worker 执行 Hugging Face 元数据、卡片采集及规则整理，不依赖模型。支持每日同步、逐项进度、取消、失败重试和重启恢复；默认每次 50 条，按最近更新时间排序。任务与计划由 Alembic `0005_collection_jobs` 管理。使用及接口见 [采集说明](../docs/huggingface-collection.md)。
