# 小亿数据部署

## 本地验收环境

Python 3.12+、Node.js 22.13+。从仓库根目录安装依赖，参见 README。以下变量必须同时存在于 API、worker 和操作 CLI 的进程中；每个新 PowerShell 终端分别设置，或通过团队受保护的进程配置注入：

```powershell
$env:XIAOYI_CATALOG_DB = "$PWD/data/catalog.db"
$env:XIAOYI_STORAGE_ROOT = "$PWD/data/assets"
$env:XIAOYI_IMPORT_ROOT = "$PWD/data/import"
$env:HF_ENDPOINT = "https://huggingface.co"
```

先用 `python -m xiaoyi_catalog.auth --username admin --role admin` 创建账号，然后分别启动 API、worker 和前端。使用 README 中的虚拟环境 Python；前端开发 API 默认 `http://127.0.0.1:5280`。默认数据库路径相对进程工作目录，建议使用绝对路径避免启动到不同的库。

模型通过 `XIAOYI_LLM_BASE_URL`、`XIAOYI_LLM_MODEL`、`XIAOYI_LLM_API_KEY` 配置兼容 Chat Completions 的接口。BASE_URL 应包含服务的 API 前缀（例如 /v1）。不要将密钥写入仓库或浏览器变量。未配置时科研智能体任务保留失败状态和原因，配置后可在有限重试次数内重试。HF 采集与定时同步独立运行，不需要模型配置。数据问答复用上述配置，未配置时不会排队模型问题；配置变更后须同时重启 API 和 worker。问答功能与记录范围见 [问答说明](catalog-qa.md)。

`XIAOYI_CATALOG_DEV_AUTH=1` 仅用于 SQLite 本机测试夹具，固定身份 local-demo，不要用于团队部署。生产环境保留真实登录。

## 单机 Docker Compose

提供 database（PG16 + pgvector）、api、worker、web 四个服务。单 worker 使用数据库租约；API 请求只排队任务。配置文件为根目录 `compose.yaml`、`.env.example` 和 `deploy/`。

```sh
cp .env.example .env
# 编辑 .env：填写 URL-safe 随机数据库密码和现有模型配置
docker compose up -d --build
docker compose exec api python -m xiaoyi_catalog.auth --username admin --role admin
```

打开 `http://localhost:8080`。web 仅绑定服务器回环地址；团队访问使用已有 HTTPS 反向代理，并配置 `XIAOYI_CATALOG_CORS` 为实际精确 origin、`XIAOYI_SECURE_COOKIE=1`。根路径同源 /api 由 nginx 转发，不需构建时写入服务地址。不要将包含数据库密码的连接串复制到工单。

持久卷 `catalog-db` 保存业务和 checkpoint，`catalog-assets` 保存哈希文件与已发布包；`./data/import` 只读挂入容器供人工来源的批准导入。导入 CLI 可在 api 容器中运行，清单文件需在可访问的挂载路径中。应用容器以 uid 10001 运行。

默认每申请最大 10 GiB、资产总配额 100 GiB，可在 .env 中调整。容器有写入缓存与导出压缩包的额外磁盘需求；为暂存和研究包预留空间。数据库与文件必须成套备份，参见 [备份恢复](backup-recovery.md)。

## 升级与恢复

1. 停止 web/api/worker 的写入，保留数据库运行以完成一致备份。
2. 保存旧镜像版本、数据库、checkpoint、资产和独立保管的部署配置。
3. 重建并启动 api；Alembic 按版本升级，再启动 worker/web。
4. 检查登录、目录版本、待审批任务与一个既有研究包哈希。
5. 恢复演练使用全新数据库和空资产目录，核验后再切换流量；工具不覆盖现有数据库。

旧目录未保存版本证据的条目标记“历史版本未验证”。使用人工恢复命令补齐原 SHA 证据，再重新核验/冻结；已批准旧 JSON 快照继续保持原样，不自动成为可复现包。

## 运维检查

- `GET /api/health`：API 启动状态；数据库迁移在启动阶段完成。
- reviewer/admin 的 `GET /api/operations`：任务/下载状态计数、核验积压、登记资产字节、磁盘剩余。
- HF 采集页：查看持久同步计划、逐项进度、失败原因与版本证据；worker 持续运行才会触发计划，使用方法见 [采集说明](huggingface-collection.md)。
- 科研任务详情与事件：模型、提示版本、调用用量、单项失败和审批等待。
- `docker compose logs api worker`：进程运行日志；避免将环境变量或 token 放入自定义日志。
- worker 重启后，租约过期任务恢复；已批准范围不变，工具结果复用持久执行键。网络调用中断可能已计费，不能承诺外部 API 的 exactly-once 计费。
- 下载失败保留原因和已校验资产；重新申请与批准后可复用相同版本/哈希的文件，范围改变需要新审批。

## 本机验证边界

本次 Windows 环境无 Docker / PostgreSQL 客户端或服务，未运行容器启动和真实 PG 备份恢复。CI 已提供 PG16 + pgvector 集成任务；每次推送后的执行结果以 GitHub Actions 为准。模型端点和服务器信息按用户要求后续补齐。真实 100–300 条目录及四类任务试点按 [验收方案](pilot-evaluation.md) 执行。
