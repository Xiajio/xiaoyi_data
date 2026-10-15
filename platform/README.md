# xiaoyi-catalog · 数据集目录平台（M1）

小亿数据集目录平台的采集与目录服务。设计依据：[../docs/dataset-platform-design.md](../docs/dataset-platform-design.md)。

M1 范围：目录库 schema（`source / dataset / dataset_version / evidence / run`）、HuggingFace 采集适配器、采集与检索 CLI。整理流水线（模态之外的 enrichment、嵌入索引）、MCP 智能体接口、策展 UI 分别在 M2 / M3。

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
```

采集幂等：指纹为 `sha256(kind:source_key:revision)`，重复采集零重复记录；同标识新版本会追加 `dataset_version` 并更新元数据。每次采集写入 `run`（输入范围、统计、失败分类），单项失败不中断批次。

## 测试

```sh
.venv/Scripts/python -m pytest        # 单元测试不访问网络（HF 全部 mock）
```

## 边界

- 只采集元数据与卡片原文，不下载数据集本体。
- 许可分级为映射表规则（permissive/copyleft/research-only/unknown），未识别一律 unknown，不推断。
- 模态来自来源 `modality:*` 标签或任务类别映射表；两者缺失时为空并在检索结果中标 `missing`。
