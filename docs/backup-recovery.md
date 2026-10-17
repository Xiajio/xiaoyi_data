# 成套备份与恢复演练

`operations_backup` 备份业务数据库、持久资产、研究包和任务 checkpoint。SQLite 使用 `sqlite3.backup`；PG 使用 custom-format `pg_dump`/`pg_restore`。密码只通过子进程环境传递，不写入命令参数或清单。服务配置/模型密钥、导入目录的未导入原件、SQLite → PG 后的旧 checkpoint 归档要另外安全保存。

## 一致性与不覆盖边界

先暂停采集计划、停止入口，排空并停止 API、所有 worker、人工导入和派生进程。数据库保持运行。确认没有其他写入后才使用 `--quiesced`；该标记是操作者声明，不能代替停写，也不会自动检测所有进程。

资产根仅允许 `sha256/`、`bundles/`、`staging/`。后者及未完成 `*.partial` 不备份；未知目录、符号链接、目录联接及越界路径被拒绝。备份目录必须全新；成功时最后写入 `manifest.json`，记录每个文件长度和 SHA-256。缺失/损坏的清单不可恢复。格式目前上限为 100 万文件、512 MiB 清单；样本 Parquet 分片按文件备份，不把每个样本另写成一个备份文件。

恢复前验证全部文件及 SQLite 完整性；目标 DB 必须不存在、资产目录必须不存在或为空。原库、原资产、备份永不覆盖。磁盘/权限故障留下部分新目标用于排查，重试用另一组全新位置。备份、源资产、恢复资产路径不得互相包含。

## Compose 的 PostgreSQL 操作

ops profile 提供与服务器匹配的 PG16 客户端；数据库卷本身不是成套备份。准备 UID 10001 可写的 `backups/` 和 `recovery/`。

```sh
docker compose stop web api worker-interactive worker-io worker-cpu
# 同时核对所有人工导入/派生 CLI 已退出；保持 database 运行。
docker compose run --rm ops python -m xiaoyi_catalog.operations_backup backup --destination /backups/pre-release-20261018 --quiesced
docker compose run --rm ops python -m xiaoyi_catalog.operations_backup verify --backup /backups/pre-release-20261018
```

将当前 shell 的 `XIAOYI_CATALOG_DB` 从受保护配置注入为**新数据库名**，然后透传到一次性 ops 容器。不要在参数中贴含密码的 URI：

```sh
docker compose run --rm -e XIAOYI_CATALOG_DB ops python -m xiaoyi_catalog.operations_backup restore \
  --backup /backups/pre-release-20261018 --storage /recovery/restored-assets --quiesced
```

恢复账号必须能连接 postgres 维护库并有 CREATEDB 权限。目标库已存在即拒绝，即使为空；不使用 DROP 或 `--clean`。业务账号/审批记录随库保留；数据库对象所有者为恢复账号，数据库级授权由部署负责人重新核对。

恢复后先使用匹配该备份的程序镜像。检查 schema、历史记录数量、权限、待审批、资产及研究包 hash、checkpoint。验证通过后，把 `POSTGRES_DB` 指向新库，`XIAOYI_RECOVERY_ASSETS` 指向宿主机恢复资产的绝对路径，使用 `deploy/compose.recovery.yaml` 覆盖挂载；检查 `docker compose -f compose.yaml -f deploy/compose.recovery.yaml config --quiet` 再启用。跨发行版回滚使用保存的旧完整 Compose 配置，不能用旧镜像运行新迁移命令。

## SQLite 同后端备份恢复

```sh
python -m xiaoyi_catalog.operations_backup backup --db /source/catalog.db --storage /source/assets --destination /backups/sqlite-release --quiesced
python -m xiaoyi_catalog.operations_backup verify --backup /backups/sqlite-release
python -m xiaoyi_catalog.operations_backup restore --backup /backups/sqlite-release --db /recovery/catalog.db --storage /recovery/assets --quiesced
```

默认包含旁边的 `catalog.db.checkpoints.sqlite`；如果配置 `XIAOYI_CHECKPOINT_PATH`，工具读取该配置，也可显式 `--checkpoint`。恢复时应显式指定新的 checkpoint 路径，避免仍指向旧位置。不存在 checkpoint 的空任务库可以备份，清单如实记录。

同后端恢复不能转换 SQLite/PG。全历史跨库工具与执行兼容政策见 [历史迁移](history-migration.md)。

## 真实 PostgreSQL 演练

```sh
docker compose run --rm ops python -m xiaoyi_catalog.operations_drill --destination /recovery/drill-20261018
```

演练在两个随机命名的新数据库中生成明确的测试记录、资产和实际 LangGraph checkpoint，备份后恢复，比较所有表计数和资产字节，并从原 checkpoint 节点继续执行。最后只删除本次随机创建的两个测试库，保留报告及文件；不修改配置中的原数据库。该命令需要 CREATEDB 权限和 PG16 客户端。它证明工具流程，不能代替真实 100 GiB 数据恢复的容量/耗时验收。

CI `compose-and-recovery` 执行此演练并上传 `drill-report.json`。Python 测试另覆盖 SQLite 恢复、恢复副本不影响原库、未知表/损坏引用拒绝、全历史跨库行摘要、PG sequence 和 checkpoint 归档。

```sh
python -m pytest platform/tests/test_backup.py platform/tests/test_operations.py -q
```

本机无 PostgreSQL 时，真实跨库测试会跳过；Windows 无符号链接权限时也会明确跳过。发布必须等待真实 PG 和 Compose 两个 CI 门禁通过，并完成部署环境恢复演练。默认每天成套备份到另一存储位置、每周抽验恢复、每次升级前完整恢复演练；备份原件与迁移原始归档在试点阶段不自动删除。
