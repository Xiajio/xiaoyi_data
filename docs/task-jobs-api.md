# 任务契约、审核与后台作业接口

这些接口沿用会话认证与角色校验。API 使用 `/api` 前缀；任务输入是参数，不允许执行 Python、shell 或用户提供的算子代码。

## TaskSpec v1

科研任务的 `hard_constraints`、idea 的 `requirements` 和匹配入口采用同一约束解释器；旧扁平对象继续兼容。版本写作整数 `schema_version: 1`。

```json
{
  "schema_version": 1,
  "all_of": [
    {"modalities": ["text"]},
    {"license_class": ["permissive"], "min_rows": 1000},
    {"any_of": [{"languages": ["zh"]}, {"languages": ["en"]}]}
  ],
  "optional": [{"task_categories": ["text-classification"]}]
}
```

支持的字段是 modalities、task_categories、languages、license_class（兼容 license_classes）、min_rows/max_rows、min_bytes/max_bytes。集合字段中多个值表示任选其一，需要同时满足时用 all_of 分开表达。all_of 全部满足，any_of 至少一个分支满足；optional 只报告，不决定是否就绪。

单项和组合都输出 `pass / fail / unknown / unsupported`。源数据未知为 unknown，未实现字段为 unsupported，不能通过丢弃字段而变成 pass。只有硬约束整体 pass 才可确认配对、进入可用输入状态；例如 label_schema、时间切分等尚未支持的约束会明确阻断，不能由模型宣称满足。输入最多 64 KiB、8 层、每组 64 项；非法类型和未知版本拒绝。

目录、科研发现、问答调用共同 RetrievalService，输出固定版本来源、检索算法版本、覆盖与截断；硬约束最终由同一解释器核验，向量分数不替代硬约束。

## 不可变生成与审核

已完成的 Enrichment 内容、证据引用和哈希不能原位覆盖。重新生成或人工更正创建新结果 ID，并保留修订关系。审核记录绑定结果版本、内容快照与 SHA-256；工作台审核提交 `expected_content_hash`，读取后内容不一致则拒绝。

旧审核没有当时内容的不能补造哈希：迁移明确标记历史不可验证，原审核和已有批准快照/研究包继续保留。所有入口采用相同的非空引文及来源归属校验。

## Job v1

`POST /api/jobs` 请求格式：

```json
{"kind":"profile.run","payload":{"asset_id":101},"idempotency_key":"optional-client-key"}
```

返回 HTTP 202，JSON 包含 `job_id`、状态、队列、尝试次数、进度、结果及错误。公共直接操作为 `profile.run`、`asset.derive`、`bundle.build`、`build.export`；派生需人工 reviewer/admin，输入资产必须完整获批，导出要求发布批准。构建、实验、采集、科研、问答使用各自的业务入口，不能伪造底层 Job payload 绕过检查。

| 操作 | 接口 |
|---|---|
| 列出自己的任务 | GET /api/jobs?after_id=0&limit=50（上限100，支持order=asc/desc） |
| 查询任务 | GET /api/jobs/{job_id} |
| 读取事件 | GET /api/jobs/{job_id}/events?after_id=0 |
| 请求取消 | POST /api/jobs/{job_id}/cancel |
| 重新执行失败/取消任务 | POST /api/jobs/{job_id}/retry |
| 下载完成的训练包 | GET /api/jobs/{job_id}/artifact |

状态为 queued、running、succeeded、failed、cancelled。运行中取消先记录 cancel_requested，等待安全检查点；已经原子提交的产物不会因晚到取消而撤销。进度字段按操作提供真实 stage、processed、可选 total 与少量整数 counts，最多2KiB；采样、选择、分片、派生及导出通过既有15秒租约心跳批量写入，最终保存最后快照。旧所有者和取消后的执行不能更新进度。工作台显示阶段与实际计数，不推算未测量的百分比；历史业务阶段仍可从原入口读取。

幂等键按账号隔离；同键不同参数拒绝。构建和实验默认按规范参数复用任务；需要独立复现时显式提供新 Idempotency-Key。任务失败后显式重试增加 Attempt、保留事件与原输入；旧科研/问答/下载等业务重试仍走原入口，保留业务权限与审批逻辑。

领取采用 PostgreSQL FOR UPDATE SKIP LOCKED，短事务分配所有者和执行代号。执行在事务外进行；每批写入与最终提交都检查身份、租约和取消状态。Artifact 回执与业务产物同事务提交，避免在业务提交后、Job 完成前崩溃造成重复发布。过期所有者不能提交迟到结果。

## CLI 与 MCP

```sh
python -m xiaoyi_catalog.jobs_cli build --request recipe.json
python -m xiaoyi_catalog.jobs_cli experiment --request experiment.json
python -m xiaoyi_catalog.jobs_cli submit --request operation.json
python -m xiaoyi_catalog.jobs_cli status --id 12
python -m xiaoyi_catalog.jobs_cli cancel --id 12
python -m xiaoyi_catalog.jobs_cli retry --id 12
```

CLI 使用 `XIAOYI_CLI_TOKEN` 账号会话；数据库使用现有配置或 `--db`。这些命令不绕过角色或批准链。MCP 增加 job_submit/job_status/job_cancel/job_retry、dataset_build_create/dataset_build_status、experiment_import/experiments_compare；机器账号不能执行发布批准。

原资产 CLI 的 import/derive 现在返回 job_id，由对应队列执行。研究包 GET 不再触发打包：尚未完成返回 409，提交 `bundle.build` 后查询任务，再下载原包地址。

## 运维接口

`GET /api/health` 为存活检查；`GET /api/ready` 校验数据库、迁移版本和资产路径读写。`GET /api/operations` 供 reviewer/admin 查询每类队列等待/运行/失败、过期租约、最老等待时间与心跳，并独立报告人工审批等待、资产体积和磁盘可用。心跳不等于模型或下载的业务进展。
