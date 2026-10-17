# 全历史迁移：SQLite 0006 → PostgreSQL

保留全部业务历史、原 ID、引用、JSON、审核、批准清单、账号、问答、任务事件和工具结果。源库与独立 checkpoint 的原始文件另作只读归档。迁移器只接受固定基线 `0006_catalog_qa` 的一致备份；其他版本必须先在副本中用对应旧发行版迁移到该基线并重新验收，不能修改版本号冒充已迁移。

已有 PostgreSQL 使用同后端备份恢复；本工具不替代它。现有 `operations_backup restore` 明确拒绝 SQLite/PG 混用。

## 维护窗口前

保留一份原始全量备份及旧镜像，盘点主库、`XIAOYI_CHECKPOINT_PATH`（若设置）、资产根、导入源、配置和存储用量。演练仅操作副本。暂停采集计划并阻止新任务；先排空：

- 研究与 QA 的 queued/running；
- 采集的 queued/running，旧采集执行表的 running；
- 摘要 generating；
- 下载/导入 approved/running。

人工 pending、研究 waiting_approval、既有 completed/failed/cancelled 可以保留。排空失败就保留原环境并取消本次切换，不能跳过检查。全部写入停止后才声明 `--quiesced` 并生成最终备份。源备份文件清单不包含导入根、密钥或部署配置，这些须单独归档。

```sh
python -m xiaoyi_catalog.operations_backup backup --db /source/catalog.db --storage /source/assets --destination /backups/before-pg --quiesced
python -m xiaoyi_catalog.operations_migrate --backup /backups/before-pg --check-only
```

预检只读打开备份，检查 schema、未知表/列、SQLite 完整性、外键及已知 JSON 引用、审批清单 hash、全部登记资产/包的大小与 SHA-256。任何未知表或引用损坏都必须处理后重试，不能只迁移“认识的表”。

## 迁入新数据库

把目标连接串安全注入 `XIAOYI_MIGRATION_TARGET_DB`，要求全新数据库名、可连接 postgres 维护库及 CREATEDB 权限。不得把密码放进参数、工单或迁移报告。资产与归档目标路径必须尚不存在且与备份互不包含；预先挂载可写父目录即可。

```sh
# 环境中已安全提供 XIAOYI_MIGRATION_TARGET_DB。
docker compose run --rm -e XIAOYI_MIGRATION_TARGET_DB ops python -m xiaoyi_catalog.operations_migrate \
  --backup /backups/before-pg --storage /recovery/pg-assets --archive /recovery/legacy-archive --quiesced
```

工具创建新 PG，用 Alembic 建立当前目标 schema，按依赖顺序转移全部旧业务表并保留 ID。新增信任字段采用迁移的服务器默认值 `legacy_unverified`，不凭空补造旧审核证据。自引用分两步写入并保持旧时间戳；导入后在同一事务内比较每表数量和规范化行摘要，校正 PG sequence。唯一有意更改的原运行字段是过期 worker lease，变更在报告说明。全部 quality_report 另按 0010 的同一规则回填为 profile_run，保留原内容、producer 和时间戳，并在报告记录新增数量；旧报告仍保留。

向量从 SQLite JSON 转为 PG vector（float32），对账使用相同 float32 规范化；原始 JSON 仍逐字节保留在归档 SQLite。所有资产原字节复制，hash 不变。失败时不覆盖、不删除源，不把部分目标当作成功实例；保留排查并换全新目标重试。

成功生成 `migration-report.json`，含每表记录数与摘要、原库 SHA-256、归档文件 hash、资产数量和迁移 ID；数据库内同时写入迁移记录。归档库、checkpoint、报告设置只读权限，应进一步放入受控只读备份存储。报告写入失败时也不放行流量。

## 历史执行语义

waiting_approval 表示研究 graph 已运行到 END，审批靠业务表推进，不是挂起的 LangGraph 节点。它可以在 PG 用原 ID 继续核验，已有下载/发布批准也只覆盖原清单。

旧 SQLite checkpoint 不转换成 PostgresSaver。迁入的研究任务标记为历史归档；原位 retry 被禁止。重新发起应创建新任务并关联原 ID，新任务不得继承下载或发布批准。旧失败与取消记录、用量、工具结果保持原样。QA 的 state/observations 在业务表中，随业务数据保留；新 PG 研究任务建立新的 checkpoint。

若要求旧挂起研究任务从原节点原位接续，需要单独实现并测试 checkpoint、父链、pending writes 与序列化兼容迁移。只读保存旧 executor 不能代替该能力，也不能启动两个执行器对同批审批并行工作。

## 放行与回滚

放行前核对每表对账报告、全部资产/包 hash、账号/权限、私有 QA 范围、历史事件与审批。验证 pending 未变 approved，waiting_approval 能继续，旧 failed 研究任务无法原位重放，新任务能写入 PG checkpoint。恢复导入时按原相对清单映射新 import root，大小/hash 必须相同，不能为了路径变化修改批准清单。

先启动 API 验收，后启动 worker，最后恢复计划和新写入。先保留只读旧环境全量副本，确认新环境通过完整验收再进入日常运行。回滚只使用成套旧镜像/配置/主库/checkpoint/资产；不把新 PG 的批准结果拼接到旧 SQLite executor。
