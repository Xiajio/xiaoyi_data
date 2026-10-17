# 单台内网 Linux 部署与发布

本版本面向 5–20 人、百万级样本索引、约 100 GiB 资产的试点。使用 PostgreSQL 16 + pgvector、API、interactive/io/cpu 三个 worker 和 nginx；数据库与样本资产继续采用本机持久存储。建议试点主机 8 vCPU、32 GiB RAM、1 TiB SSD，最终容量与恢复时间以真实数据演练为准。无本机 GPU 要求。

## 构建与首次启动

需要 Linux Docker Engine 和支持 `service_completed_successfully`、`up --wait` 的 Compose v2。API/worker/ops 使用 Python 3.12；前端构建使用 Node 22。镜像 digest 已在 2026-10-18 向 Docker Hub 查询并固定，Python 的 `platform/requirements.lock` 来自真实 uv 解析，包含跨平台标记和包哈希。锁的更新命令：

```sh
uv pip compile platform/pyproject.toml --extra dev --universal --python-version 3.12 --generate-hashes --output-file platform/requirements.lock
```

生产环境使用已通过 CI 的提交；将完整 Git SHA 写入 `.env` 的 `XIAOYI_RELEASE`，保存构建出的镜像，后续环境复用这些镜像，不在上线时重新解析依赖。ops 镜像额外安装 PGDG 的 PostgreSQL 16 客户端；它的 apt 包小版本由构建记录和最终镜像 digest 固定。

```sh
cp .env.example .env
# 填写 URL-safe 随机数据库密码、模型服务、精确 origin 和 release SHA。
sudo install -d -o 10001 -g 10001 data/import backups recovery
docker compose --profile ops build
docker compose config --quiet
docker compose up -d --wait --wait-timeout 180
docker compose exec api python -m xiaoyi_catalog.auth --username admin --role admin
```

账户密码通过 CLI 提示输入。禁止使用开发身份。Compose 仅把 web 绑定到 `127.0.0.1:8080`；团队入口接已有 HTTPS 代理，设置精确 `XIAOYI_CATALOG_CORS`、`XIAOYI_SECURE_COOKIE=1`。API 与 PG 无宿主端口。模型地址应含其 API 前缀，例如 `/v1`；密钥仅在服务配置，不进入浏览器或报告。

Compose 先启动数据库，再执行一次性 `python -m xiaoyi_catalog.operations migrate`。API/worker 运行期间只校验版本，发现 schema 不匹配则拒绝启动；健康探针不会执行迁移。显式迁移成功后才启动 API 和三个 worker。服务配置含重启策略、内存/CPU 上限及每服务 5 × 10 MiB 日志轮转。

`XIAOYI_CATALOG_AUTO_SUMMARY` 已传入 API 与 worker。嵌入模型默认关闭；需要该能力时另建包含 embed 可选依赖和固定模型的已验证镜像，不直接修改生产容器。

## 数据与进程边界

- `catalog-db`：业务表、队列、审核、迁移记录及 PostgreSQL LangGraph checkpoint。
- `catalog-assets`：`sha256/` 内容寻址资产、`bundles/` 研究包；`staging/` 为可重建中间产物，不进入备份。
- `data/import`：只读导入源；配置、未导入原件和迁移 checkpoint 归档需另外保存，不应塞入资产根目录。
- interactive 处理短交互任务，io 处理采集/下载，cpu 处理样本计算。不同队列使用独立心跳，长下载不应阻塞交互队列。
- API/worker/ops 以 UID 10001 运行。备份和恢复目录必须允许该 UID 写入。默认资产配额 100 GiB、单次获取上限 10 GiB；暂存、研究包、备份和恢复副本需要额外磁盘。

启动后依次验收：登录与角色权限、历史目录/证据、待审批项、批准下载、固定版本研究包、三类队列心跳。先迁移并验证全部旧记录，再上线样本引擎；不能用空库上线替代全历史迁移。SQLite 迁移细则见 [历史迁移](history-migration.md)。

## 检查与告警

```sh
curl --fail http://127.0.0.1:8080/api/ready
docker compose exec -T api python -m xiaoyi_catalog.operations worker-health --queue interactive
docker compose exec -T api python -m xiaoyi_catalog.operations worker-health --queue io
docker compose exec -T api python -m xiaoyi_catalog.operations worker-health --queue cpu
docker compose logs --tail 200 api worker-interactive worker-io worker-cpu
```

`/api/health` 只表示 HTTP 进程存活；`/api/ready` 检查 DB、当前 schema 和资产路径实际写权限。worker 心跳独立于任务租约，空闲也更新；60 秒内无活跃心跳即失败。心跳证明进程可访问数据库，不能单独证明外部模型或下载服务有进展；同时监控各队列最老等待时间、运行任务最后进度、失败次数、磁盘剩余、备份最近成功时间。审批等待与执行卡住分开统计。

初始告警阈值：心跳 60 秒未更新、磁盘使用超过 80%、备份 26 小时未成功、交互排队超过 30 秒。阈值在试点压测后调整。默认每天维护窗口成套备份，目标 RPO 24 小时；RTO 4 小时为待真实 100 GiB 恢复演练验证的目标，不是已测结果。

## 升级、回滚与发布门禁

1. 暂停新任务和采集计划，排空执行任务；核对并停止所有人工导入/派生 CLI。
2. 停止 web/API/三个 worker，保留 PG；保存旧镜像、完整 Compose 配置和独立保管的机密配置，完成成套备份并验证。
3. 先在隔离数据库/空资产目录恢复演练。再对生产执行一次显式迁移并启动新镜像。
4. 检查用户/角色、历史数量、审批清单 hash、既有包 hash、队列与样本读取，通过后开放写入。
5. 迁移失败或验证失败时停止新实例，保留失败副本；用旧完整配置、旧镜像和迁移前成套备份恢复到新位置，再切换入口。不要让新旧 worker 同时执行。

Alembic downgrade 明确不支持删除历史。存在 schema 变化时不能只回退镜像；全量恢复会回到备份时点，上线开放写入后的记录不能宣称自动零损回退。切换写入前完成验收，以缩小这个窗口。恢复路径的 Compose 覆盖示例为 `deploy/compose.recovery.yaml`；回退到旧发行版应使用该发行版的完整原配置，而不是把旧镜像混入新拓扑。

CI 的 `check` 覆盖单元、真实 PG、跨库迁移、前端与浏览器；`compose-and-recovery` 构建真实镜像、启动完整 Compose、检查三队列并执行真实 PG16 备份恢复及 checkpoint 接续。本机 Windows 无 Docker/PG，不能把本机通过的单元测试当作上述门禁已通过；以本次提交 CI 运行结果为准。

## 验收镜像交付

CI 的 `compose-and-recovery` 只有在部署健康检查、三队列检查和 PostgreSQL 恢复演练全部成功后，才生成 `release-images` artifact。包内包含 API/web/ops 三个实际验收的镜像、`release.txt`、源码提交、镜像 ID/平台与 SHA-256 清单；不会推送镜像 registry，也不包含容器环境或服务密钥。

PR 工作流使用 GitHub 的测试合并提交作为镜像标签，可能不同于 PR 分支的 HEAD。部署时必须以 artifact 内 `release.txt` 为准；push 工作流的标签对应该次 push SHA。不要根据 PR 页面上的提交号猜镜像标签。

```sh
# 在解压出的 release-images artifact 目录中：
sha256sum -c SHA256SUMS
docker image load --input release-images.tar.gz
cat release.txt
# 将该值写入部署 .env 的 XIAOYI_RELEASE；配置、迁移和验收仍按本手册完成。
# 使用加载的镜像启动，不在上线窗口重新构建：
docker compose up -d --no-build --pull never --wait
```

artifact 默认保留 14 天；发布负责人将验收包与对应源码、配置模板一起复制到受控长期存储，保留旧版本包用于整套恢复回滚。实际 .env/密钥另按受控配置备份规则保管。
