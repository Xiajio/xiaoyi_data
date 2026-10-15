# xiaoyi-catalog · 数据集目录平台（M1 + M2）

小亿数据集目录平台的采集、整理与智能体接口。设计依据：[../docs/dataset-platform-design.md](../docs/dataset-platform-design.md)。

- M1：目录库 schema（`source / dataset / dataset_version / evidence / run`）、HuggingFace 采集适配器、采集与检索 CLI。
- M2：整理流水线（schema 摘要 + 质量信号）、嵌入索引、混合检索、`idea / pairing / snapshot` 配对快照模型、MCP server 五工具。人工核验与策展界面在 M3。

## 环境

```sh
python -m venv .venv          # Python ≥ 3.12
.venv/Scripts/pip install -e ".[dev]"   # Windows；Linux 用 .venv/bin/pip
```

## 使用

```sh
# 建库（默认 ./data/catalog.db，可用 --db 或 XIAOYI_CATALOG_DB 覆盖）
.venv/Scripts/python -m xiaoyi_catalog.cli init-db

# 采集：默认走 HF 镜像端点（HF_ENDPOINT 可覆盖）；--task/--language 可重复
.venv/Scripts/python -m xiaoyi_catalog.cli collect --task text-classification --language zh --limit 50

# 检索：同一字段多值为 OR，字段之间为 AND；结果附带 missing 缺失清单
.venv/Scripts/python -m xiaoyi_catalog.cli search --modality text --license-class permissive --query 情感

# 概览
.venv/Scripts/python -m xiaoyi_catalog.cli stats

# M2：整理与嵌入（离线；embed 默认用 bge-small-zh，--hash 走无依赖冒烟嵌入器）
.venv/Scripts/python -m xiaoyi_catalog.cli enrich
.venv/Scripts/python -m xiaoyi_catalog.cli embed

# M2：配对与快照（提案落 pending；review/freeze/approve 为人工操作）
.venv/Scripts/python -m xiaoyi_catalog.cli propose --idea '{"external_id":"demo","title":"…","requirements":{"modalities":["text"]}}' --versions 1,2 --rationale "…"
.venv/Scripts/python -m xiaoyi_catalog.cli review --pairing 1 --decision confirmed --note "…"
.venv/Scripts/python -m xiaoyi_catalog.cli freeze --idea demo
.venv/Scripts/python -m xiaoyi_catalog.cli approve --snapshot 1
.venv/Scripts/python -m xiaoyi_catalog.cli export --snapshot 1 --out snapshot.json

# M2：MCP server（stdio，供小亿灵析等智能体接入）
XIAOYI_CATALOG_DB=./data/catalog.db .venv/Scripts/python -m xiaoyi_catalog.mcp_server
```

MCP 工具契约：`dataset_search / dataset_evidence / pairing_propose / snapshot_status / snapshot_export`。提案只能落 `pending`，确认与审批必须经人工（M3 策展 UI，M2 经 CLI）。嵌入模型可用 `XIAOYI_CATALOG_EMBED_MODEL` 覆盖，`XIAOYI_CATALOG_EMBED=0` 关闭语义排序。

依赖说明：`mcp` 锁定 `<2`（v2 将 FastMCP 改名 MCPServer，API 变动，后续再迁移）；`sentence-transformers` 在 `embed` extra 中，不装则只有哈希嵌入器可用。

采集幂等：指纹为 `sha256(kind:source_key:revision)`，重复采集零重复记录；同标识新版本会追加 `dataset_version` 并更新元数据。每次采集写入 `run`（输入范围、统计、失败分类），单项失败不中断批次。

## 测试

```sh
.venv/Scripts/python -m pytest        # 单元测试不访问网络（HF 全部 mock）
```

## 边界

- 只采集元数据与卡片原文，不下载数据集本体。
- 许可分级为映射表规则（permissive/copyleft/research-only/unknown），未识别一律 unknown，不推断。
- 模态来自来源 `modality:*` 标签或任务类别映射表；两者缺失时为空并在检索结果中标 `missing`。
