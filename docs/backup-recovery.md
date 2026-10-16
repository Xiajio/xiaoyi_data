# 备份与恢复演练

`xiaoyi_catalog.operations_backup` 只备份目录数据库、持久对象、研究包和任务检查点。服务配置、模型 API 密钥与其他环境配置不进入备份。SQLite 使用 `sqlite3.backup`；PostgreSQL 使用自定义格式的 `pg_dump` / `pg_restore`，连接密码只通过子进程环境传递，不写入命令参数或清单。

## 操作前提

先停止 API、worker，以及所有本地导入和派生进程。确认它们已经停止后，命令必须包含 `--quiesced`。该参数是操作者对停服状态的明确声明，工具不会自动停止或检测所有服务；不能在持续写入时把它当作一致性保证。

资产根目录采用项目规定的 `sha256/` 和 `bundles/` 布局。`staging/` 与未完成的 `*.partial` 文件不备份；存储根目录出现其他项会拒绝操作，以免把配置文件混入归档。符号链接、目录联接和越界路径均被拒绝。

备份目标必须是全新目录。备份成功时最后写入 `manifest.json`，记录每个数据库、检查点和资产文件的长度与 SHA-256。没有有效清单的未完成目录不能恢复。

## SQLite：备份

以下命令从项目根目录执行，使用已安装 `xiaoyi-catalog` 的 Python 环境；Windows 可将 `python` 替换为 `platform/.venv/Scripts/python.exe`。

```text
python -m xiaoyi_catalog.operations_backup backup --db ./data/catalog.db --storage ./data/assets --destination ./backups/catalog-2026-10-15 --quiesced
```

默认包括旁边的 `catalog.db.checkpoints.sqlite`。如 worker 使用了 `XIAOYI_CHECKPOINT_PATH`，工具会读取该配置；也可显式提供 `--checkpoint ./data/checkpoints.sqlite`。尚未执行任务、没有检查点文件的目录库可以备份，清单会明确记录没有独立检查点。

## SQLite：恢复到新位置

```text
python -m xiaoyi_catalog.operations_backup restore --backup ./backups/catalog-2026-10-15 --db ./recovery/catalog.db --storage ./recovery/assets --quiesced
```

恢复前会验证整份清单、所有文件的 SHA-256 和 SQLite 完整性。任何文件损坏或缺失都会在创建目标数据库、资产目录之前拒绝操作。目标数据库及其 WAL/SHM、目标检查点都必须不存在；资产目录必须不存在或为空。原数据库、原资产和备份内容均不删除、不覆盖。

如果此前设置过 `XIAOYI_CHECKPOINT_PATH`，恢复时显式指定新的路径，例如 `--checkpoint ./recovery/checkpoints.sqlite`，避免仍指向原检查点。随后启动恢复实例时，把 `XIAOYI_CATALOG_DB`、`XIAOYI_STORAGE_ROOT` 和检查点配置同时指向新位置；先核对目录数量、已批准快照、研究包哈希与任务状态，再恢复正常访问。

发生磁盘、权限或连接故障时，工具保留新目标中的部分结果用于排查，不会自动清空。排查后选用另一组全新路径重试。不要把备份或数据库文件放进资产根目录，备份目录与资产目录也不得互相包含。

## PostgreSQL

需要可访问的 PostgreSQL 服务，以及与服务器兼容的 `pg_dump`、`pg_restore` 客户端；缺少任一所需工具时会明确失败。模型配置无需提供。

备份使用现有的 `XIAOYI_CATALOG_DB` 环境变量；不要把含密码的连接串直接粘进终端命令或脚本。目标库内的 LangGraph 检查点表随整库备份，不另复制 SQLite 检查点。

```text
python -m xiaoyi_catalog.operations_backup backup --storage /data/assets --destination /backups/catalog-2026-10-15 --quiesced
```

恢复前将当前命令环境中的 `XIAOYI_CATALOG_DB` 配置为**新数据库名**的连接地址，并提供空资产目录：

```text
python -m xiaoyi_catalog.operations_backup restore --backup /backups/catalog-2026-10-15 --storage /recovery/assets --quiesced
```

恢复账号必须能连接 `postgres` 维护库并具备 `CREATEDB` 权限。工具会先确认目标数据库不存在，再创建并恢复；不会运行 `DROP` 或 `pg_restore --clean`。目标数据库已存在时即使为空也拒绝覆盖。备份中的业务账号和权限记录保留；数据库对象使用恢复账号拥有，数据库级授权由部署负责人另行配置。跨 SQLite/PostgreSQL 的迁移不属于该命令。

## 本地验证记录

`platform/tests/test_backup.py` 覆盖数据库、资产、研究包和检查点的 SQLite 恢复，验证恢复副本不会影响原数据；覆盖真实 LangGraph 在步骤中断后备份、恢复并继续执行，以及损坏文件、越界清单、既有目标、配置混入和未声明停服时的拒绝行为。PostgreSQL 命令与凭据隔离使用替身测试验证；实际 PostgreSQL 备份恢复需在部署环境再演练。

运行测试：

```text
python -m pytest platform/tests/test_backup.py -q --basetemp=output/backup-drill-tests
```

测试临时目录请选择项目内新的专用位置，避免和其他正在运行的 pytest 共用。若 Windows 账号不能创建符号链接，该平台特定测试会跳过；Linux CI 会执行该项。
