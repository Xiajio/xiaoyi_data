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

## 每日维护备份的安装入口

仓库提供 `deploy/backup-maintenance.sh`、`deploy/backup-maintenance.env.example` 和 `deploy/systemd/xiaoyi-backup.{service,timer}`。它们不会自动安装。完成真实恢复演练、确认每天 02:30 的停写窗口后，由管理员安装；时间使用服务器时区，漏过窗口不在开机时补跑。

先在独立备份主机导出 NFS/CIFS 挂载，或配置明确用于离机轮换的独立介质。准备挂载点与备份目录，读取 `findmnt -n -o SOURCE --target /mnt/xiaoyi-offsite/catalog`，把精确结果填入配置。脚本同时核对挂载点、预期来源、与本地备份不同的文件系统；缺失配置、挂载未就绪、来源变化均在停服前失败。单独的本机目录不符合要求。

维护约定：该 Compose 项目是数据库与资产的唯一写入者；所有人工迁移、导入、派生、恢复和其他 ops 命令都必须持有同一个 `XIAOYI_MAINTENANCE_LOCK`。脚本拒绝仍在运行的 migrate/ops 或未知项目容器，停掉 web/API/三个 worker，再确认只剩数据库运行。外部直连数据库的程序不受 Docker 控制，启用 timer 前必须停止或纳入同锁约定。

```sh
# 以下是管理员安装示例；先编辑项目路径、已有Compose项目名和离机挂载配置。
sudo install -d -m 0700 /etc/xiaoyi
sudo install -m 0600 deploy/backup-maintenance.env.example /etc/xiaoyi/backup-maintenance.env
# 编辑 /etc/xiaoyi/backup-maintenance.env；已有恢复挂载时保留相同 COMPOSE_FILE 配置。
# 编辑 service 中 ExecStart 的仓库路径、RequiresMountsFor；确认 timer 的维护窗口。
sudo install -m 0644 deploy/systemd/xiaoyi-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/xiaoyi-backup.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/xiaoyi-backup.service /etc/systemd/system/xiaoyi-backup.timer
sudo systemctl daemon-reload
# 在已确认的维护窗口先手工执行一次，核对异机副本与服务恢复后才启用 timer。
sudo systemctl start xiaoyi-backup.service
sudo journalctl -u xiaoyi-backup.service -n 100 --no-pager
sudo systemctl enable --now xiaoyi-backup.timer
```

宿主机需要 Bash、Docker Compose v2、util-linux（flock/findmnt/mountpoint）和 GNU coreutils。提前加载验收通过的 ops 镜像并准备 UID 10001 可写的项目 `backups/`；脚本不会在维护窗口构建或拉取镜像。外部副本以执行脚本的账号写入，校验容器使用同 UID/GID，只读挂载外部目录，因此要为该账号配置 NFS/CIFS 写权限。

脚本用 `flock -n` 防止重入，保存原先在线的服务集合，执行本地 backup → verify → 外部复制 → sync → 外部 verify。停止或复制失败会保留全部文件与本地日志，并尝试只恢复原先在线的服务；恢复服务失败也返回非零。成功与失败均不自动删除任何备份。日志位于 `backups/daily-*.log`，服务日志在 journal；监控系统应告警 systemd unit 的失败状态。宿主断电或 SIGKILL 无法执行退出清理，管理员恢复主机后按日志启动原服务。

人工维护示例使用同一把锁，防止与每日备份重叠：

```sh
sudo flock -n /run/lock/xiaoyi-catalog-maintenance.lock \
  docker compose run --rm ops python -m xiaoyi_catalog.operations check
```

测试 `platform/tests/test_backup_maintenance.py` 用假 Docker/挂载命令验证停写顺序、原服务恢复、复制失败、挂载错误、并发锁及恢复失败；它不会停止实际服务。CI 另外执行 Bash 语法与 ShellCheck。上线验收仍需使用真实外部挂载演练一次。
