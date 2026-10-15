# 数据集目录与智能体配合平台 · 设计文档

状态：草案 v0.1（2026-10-15，待评审）
前置阅读：[README](../README.md)（演示版边界）、[PRODUCT](../PRODUCT.md)、本文第 8 节的演进映射

## 1. 背景与目标

现有 `xiaoyi-data-demo` 原为面向医院合作沟通的合成数据演示站，已完成通用数据场景改造。团队当前的真实需求是**内部开发测试**一个通用数据集管理平台：

- 数据来源以计算机科研类公开数据集为主，首期接入 HuggingFace，后续可扩展 ModelNet / ShapeNet / Objaverse 等三维数据源。
- 覆盖多种数据类型：自然语言文本、表格、图像照片、三维建模图形。
- 平台自动完成数据集**收集、整理、分类**，形成统一目录。
- 目录对**小亿灵析**（文献挖掘科研智能体）开放机器接口，配合完成"科研思路 → 数据集匹配 → 人工核验 → 组合冻结"的自动化流程。

本文档定义平台的架构、数据模型、采集与整理流水线、智能体接口和阶段计划。演示版的场景文案只是首版皮肤（医疗场景已改为通用科研数据），其"证据可追溯、候选与原文分离、人工核验后冻结发布"的内核原样保留。

## 2. 目标与非目标

### 2.1 目标

1. 一个可持续更新的**数据集目录（catalog）**，统一登记多来源、多模态数据集的元数据与证据。
2. 自动采集与整理流水线：采集 → 模态分类 → schema 摘要 → 去重 → 质量信号 → 嵌入索引。
3. 面向智能体的机器接口（MCP server），让小亿灵析可以检索数据集、读取证据、提交配对提案。
4. 人工策展界面：核验元数据与配对提案、冻结"思路 × 数据集"组合快照、审计留痕（由演示版前端演进）。
5. 全部组件可在内网离线部署（HF 可通过镜像端点采集一次，之后本地运行）。

### 2.2 非目标（本期不做）

- 不接入医院真实数据，不做任何临床用途；未来进院是另一个合规项目。
- 不下载完整数据集本体（默认只取元数据、卡片和少量样本预览）；大规模下载另行审批。
- 不做自动实验执行（训练 / 评测），配对冻结后的执行属于 Paper Lab 的范围。
- 不做真实身份认证与不可篡改审计；内部测试沿用演示版的角色视角与本地审计，进院前再替换。
- 不以自动分类结果替代人工核验；自动产出一律标记为候选。

## 3. 总体架构

```
┌────────────────────────────────────────────────────────────┐
│ 策展 UI（React，由 xiaoyi-data-demo 演进）                    │
│ 目录浏览 / 核验工作台 / 配对审批与快照 / 运行与审计             │
└──────────────▲─────────────────────────────────────────────┘
               │ REST（同源部署）                              │
┌──────────────┴──────────────┐   ┌──────────────────────────┐
│ 目录服务 catalog-service     │   │ 智能体接口 agent-bridge    │
│ (FastAPI, 目录 CRUD + 检索)  │   │ (MCP server，工具见第 6 节)│
└──────────────▲──────────────┘   └────────────▲─────────────┘
               │ 同一数据库                      │ 小亿灵析调用
┌──────────────┴───────────────────────────────┴─────────────┐
│ 目录存储：SQLite + 向量索引（内部测试）                       │
│ 表：source / dataset / dataset_version / evidence /         │
│ enrichment / review / idea / pairing / snapshot / audit     │
└──────────────▲─────────────────────────────────────────────┘
               │
┌──────────────┴──────────────┐
│ 采集与整理流水线 pipeline     │
│ 采集器(适配器) → 整理(分类/摘要/│
│ 去重/质量/嵌入) → 写入候选     │
└──────────────▲──────────────┘
               │
   HuggingFace（镜像端点） · ModelNet / ShapeNet 登记适配器 · 后续源
```

部署形态：三台进程（pipeline、catalog-service、agent-bridge）+ 静态前端，内部测试可全部跑在一台机器上；数据库先 SQLite，数据量或并发上来后迁 PostgreSQL + pgvector，schema 不变。

## 4. 数据模型（catalog schema）

所有表含 `id`、`created_at`、`updated_at`。证据与候选分离、自动与人工分离是贯穿性约束：**自动流水线只能写候选值与证据，人工核验才改确认值。**

### 4.1 来源与数据集

| 表 | 关键字段 | 说明 |
|---|---|---|
| `source` | `kind`(huggingface/modelnet/shapenet/manual)、`endpoint`、`enabled`、`rate_limit` | 数据源适配器注册表 |
| `dataset` | `source_id`、`source_key`(如 HF repo id `squad_v2`)、`name`、`author`、`url`、`license`、`license_class`(permissive/copyleft/research-only/unknown)、`modalities`(json 数组:text/table/image/3d)、`task_categories`、`languages`、`size_bytes`、`size_rows`、`downloads`、`likes`、`card_text`(原文)、`fingerprint`(去重指纹)、`status`(active/hidden/merged)、`first_seen_at`、`last_seen_at` | 统一目录记录 |
| `dataset_version` | `dataset_id`、`revision`(git sha 或快照时间)、`changelog` | 版本钉住，配对与快照引用具体版本 |
| `evidence` | `dataset_id`、`kind`(card/readme/sample/schema)、`excerpt`、`location`(段落或字段路径)、`fetched_at` | 证据片段，所有自动结论必须能指到一条证据 |

### 4.2 整理与核验

| 表 | 关键字段 | 说明 |
|---|---|---|
| `enrichment` | `dataset_id`、`kind`(modality/schema/quality/embedding)、`producer`(pipeline 版本)、`value`(json)、`confidence`(low/medium/high，仅分级，不伪装统计置信度)、`evidence_ids`、`status`(candidate/confirmed/rejected/superseded) | 自动整理结果，一律候选状态 |
| `review` | `target_kind`(enrichment/pairing)、`target_id`、`decision`(confirm/correct/missing/needs-info/reject)、`previous_value`、`value`、`note`、`role`、`created_at` | 人工核验留痕，语义沿用演示版 `ReviewEntry` |
| `run` | `kind`(collect/enrich)、`adapter`、`status`(running/completed/failed/interrupted)、`input_scope`(json)、`stats`、`started_at`、`finished_at`、`retry_of` | 流水线执行记录，对应演示版 `Run` |

### 4.3 思路配对与快照

| 表 | 关键字段 | 说明 |
|---|---|---|
| `idea` | `external_id`(灵析侧 id)、`title`、`statement`、`requirements`(json，见 6.4)、`source_refs`(论文 DOI 数组)、`imported_at` | 来自小亿灵析的思路卡 |
| `pairing` | `idea_id`、`dataset_version_id`、`rationale`、`proposed_by`(agent/human)、`evidence_ids`、`status`(pending/confirmed/rejected/superseded) | 思路 × 数据集提案，进入人工核验队列 |
| `snapshot` | `idea_id`、`version`、`pairing_ids`、`content`(冻结的完整 json)、`status`(pending/approved/invalidated)、`approved_at`、`note` | 冻结组合版本；范围或核验变化即失效，语义沿用演示版 |
| `audit` | `actor`(角色或 agent id)、`action`、`detail`、`created_at` | 操作审计 |

关键规则（从演示版继承并泛化）：

- `pairing` 只有在对应 `enrichment` 已核验、数据集 `license_class` 满足思路约束时才可 confirmed（发布阻断器 `getFieldBlockers` 的泛化）。
- `snapshot` 冻结时深拷贝全部引用内容，后续修订不改变已批准版本；同一 `idea` 同时最多一个 pending 快照。
- 任何字段缺失时使用显式状态（`unknown` / `needs-info`），**不允许把缺失推断为否定或默认值**——这是演示版 R-04 字段确立的原则。

## 5. 采集与整理流水线

### 5.1 采集器适配器接口

```python
class SourceAdapter(Protocol):
    kind: str
    def list(self, scope: CollectScope) -> Iterator[DatasetRef]: ...
    def fetch_detail(self, ref: DatasetRef) -> RawDataset: ...   # 含卡片原文
    def fingerprint(self, raw: RawDataset) -> str: ...
```

`CollectScope` 支持按任务类别、模态、关键词、更新时间下限圈定范围；每次采集产生一条 `run`，记录输入范围与统计，可中断、可重试（沿用演示版 scan/run 状态机语义）。

### 5.2 HuggingFace 适配器（首期唯一自动源）

- 使用 `huggingface_hub.HfApi`：`list_datasets(search, author, filter)` 做增量发现；`dataset_info(id, files_metadata=True)` 取标签、文件列表、`lastModified`；卡片原文取 README。
- **网络**：支持 `HF_ENDPOINT` 环境变量切换到镜像端点（如 `https://hf-mirror.com`），并显式读取 `HTTPS_PROXY`；内网部署时先由有外网的机器采集入库，运行期不依赖外网。速率遵守目标端点限制，失败按"超时/限流/权限"分类记录，不静默跳过。
- 增量：以 `lastModified` 大于上次水位为条件；指纹 = `source_key + git sha`，重复采集不产生重复记录。
- 模态与任务：直接采用 HF 的 `tags`（`task_categories:`、`modality:`、`size_categories:`、`language:`、`license:` 前缀），无标签时记 `unknown`，不猜测。

### 5.3 三维数据源（登记型适配器）

ModelNet / ShapeNet / Objaverse 等三维数据集多为申请制或独立站点，不适合自动抓取。首期提供 `manual` 适配器：人工或脚本按统一格式登记元数据与官方页面链接作为证据，`producer` 标记为 `manual-registration`。这保证三维数据进入同一目录与配对流程，同时不伪造自动采集能力。后续如需自动源，再实现对应适配器。

### 5.4 整理流水线（enrich）

按序执行，每步产出 `enrichment` 候选记录并挂证据：

1. **模态与 schema 摘要**：文本/表格类调用 HF datasets-server 的 `/splits`、`/first-rows` 取样（不下载全量），记录列名、类型、规模；图像与三维只登记格式与规模。
2. **去重与合并**：指纹相同则合并来源；疑似 fork（同名称不同作者）互相链接，`status=merged` 的主记录唯一。
3. **质量信号**：卡片完整度（必填段落存在性）、下载量、最近更新、许可存在性，输出 low/medium/high 分级与依据，**不合成无来源的评分**。
4. **嵌入索引**：对卡片摘要生成向量，写入向量索引（SQLite 下用 sqlite-vec 或独立 faiss 文件），供语义检索；嵌入模型与版本记录在 `producer` 字段。
5. **许可分类**：`license` 标签映射到 `license_class`；映射表人工维护，未识别一律 `unknown` 并在核验台提示。

## 6. 智能体接口（agent-bridge / MCP）

MCP server 与 catalog-service 共用数据库，对小亿灵析暴露五个工具。所有返回都带证据指针；凡需写入的操作只产生**候选**，不改变已确认内容。

### 6.1 `dataset_search`

输入（结构化数据需求）：

```json
{
  "modalities": ["text", "table"],
  "task_categories": ["question-answering"],
  "languages": ["zh"],
  "min_rows": 10000,
  "license_class": ["permissive", "unknown"],
  "query": "中文阅读理解 抽取式",
  "limit": 10
}
```

输出：候选数据集列表，每项含 `dataset_id`、`name`、匹配到的 `evidence_ids`、质量分级、许可、`missing`(未知字段清单)。排序 = 结构化过滤 + 向量相似度，排序理由随结果返回。

### 6.2 `dataset_evidence`

输入 `dataset_id`；返回卡片摘录、schema 摘要、质量信号、缺失项——供灵析在回答中引用，满足其"证据先于结论"的产品原则。

### 6.3 `pairing_propose`

输入 `idea`(思路卡 json，见 6.4) + `dataset_version_ids` + `rationale`；幂等创建 `idea`（按 `external_id`）与若干 `pairing(status=pending)`，返回配对 id 列表。提案进入策展 UI 的核验队列，**agent 不能自我确认**。

### 6.4 思路卡契约（灵析 → 平台）

```json
{
  "external_id": "lingxi-idea-20261015-001",
  "title": "基于表格证据的中文抽取式问答增强",
  "statement": "问题陈述与预期贡献……",
  "requirements": { "modalities": ["text","table"], "min_rows": 10000, "languages": ["zh"], "license_class": ["permissive"] },
  "source_refs": ["10.xxxx/xxxx"],
  "uncertainty": "哪些需求是硬约束、哪些可放宽的说明"
}
```

### 6.5 `snapshot_status` / `snapshot_export`

查询某思路下配对与快照状态；快照批准后可导出冻结 JSON（含数据集版本、核验记录、证据指针），导出水印注明来源与许可约束清单。

## 7. 与小亿灵析的配合流程

```
灵析文献挖掘 → 产出思路卡(含来源论文)
   → 调 dataset_search 检索候选数据集(带证据)
   → 灵析综合后调 pairing_propose 提交提案
   → 策展 UI 人工核验（配对依据 + 数据集证据同屏）
   → 冻结快照 → 负责人审批 → 导出
   → 快照可回链灵析（后续接入 Paper Lab 复现）
```

约束：

- 灵析侧只看到候选与证据；确认、冻结、审批都在本平台留痕，职责分离。
- 灵析引用数据集时必须通过 `dataset_evidence` 取证据，不允许凭记忆描述数据集属性。
- 配对被驳回时回执原因（`needs-info` 等），灵析可据此调整思路卡重新提案。

## 8. 从演示版演进

### 8.1 保留

- `src/bridge/frame.ts` 宿主 iframe 协议（嵌入小亿画板 / 灵析前端时直接使用，协议名随产品重命名）。
- `src/components/` 页面骨架：核验工作台、发布（快照冻结/审批/导出）、运行与审计三个页面语义几乎不变，换数据源与文案。
- `src/domain/model.ts` 的状态机模式：review/freeze/approve/run/scan 的 action 语义平移到 catalog-service 的命令处理器。

### 8.2 替换

| 演示版 | 替换为 |
|---|---|
| `persistence.ts`（localStorage + 种子比对校验） | catalog-service REST 客户端；服务端以数据库约束替代 `assertState` |
| `scanning.ts` 固定清单 | 采集器适配器（第 5 节） |
| `seed.ts` 合成数据 | 真实目录数据；演示模式可作为独立 fixture 保留用于 UI 开发 |
| `App.tsx` 定时器推进的假流水线 | pipeline 真实任务 + 轮询/WebSocket 进度 |
| 角色下拉（无认证） | 内部测试保留；进院前接真实认证 |

### 8.3 删除

- `assertState` 中与种子数据逐字节比对、R-02/03/04 硬编码规则、数量上限（≤16 资料 / ≤6 字段）——全部是演示护栏。
- 导出水印中的"虚构演示数据"声明，替换为许可与来源声明。

## 9. 技术选型（内部测试）

- pipeline / catalog-service / agent-bridge：Python 3.12 + FastAPI + SQLAlchemy；MCP 用官方 `mcp` Python SDK。
- 数据库：SQLite + sqlite-vec（零运维起步）；PostgreSQL + pgvector 为迁移目标，ORM 层不绑定方言。
- 前端：演示版 React + TS + Vite 直接演进，沿用朱砂红主题与组件。
- 嵌入模型：内网可部署的开源模型，型号与版本写入 `enrichment.producer`；更换模型即重建索引并保留旧索引可回滚。

## 10. 阶段计划与验收

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| M1 采集与目录 | schema 建库 + HF 适配器 + CLI 检索 | 对 3 个指定 scope 采集 ≥100 个数据集；重复采集零重复记录；每条记录有指纹与卡片证据 |
| M2 整理与接口 | enrich 流水线 + 嵌入索引 + MCP 五工具 | 灵析侧真实调通 `dataset_search`/`pairing_propose`；返回均含证据指针；提案落库为 pending |
| M3 策展 UI | 演示版前端改造对接 catalog-service | 核验、驳回、冻结、审批、导出全链路可用；旧演示测试改造通过 |
| M4 闭环联调 | 思路卡 → 配对 → 快照端到端 + 三维登记适配器 | 用 1 个真实思路卡跑通闭环；三维数据集经登记适配器参与配对；审计记录完整 |

每个里程碑交付时同步记录：已实现 / 已部署 / 已测试 / 尚未验证的明确区分，以及输入来源、规则依据和放弃方案的理由。

**进度（2026-10-15）**：M1 已完成并验收——`platform/` 下采集与目录服务落地（schema 五表、HF 适配器、采集/检索 CLI）；真实镜像站（hf-mirror）三 scope 采集 117 个数据集，重复采集零重复记录，每条记录含指纹与卡片证据。实施中修正了两处设计细节：增量水位按 scope 维度记录（不同过滤条件互不共享），且结果疑似截断时不推进水位（防回填丢失）；`modality:tabular` 归一为目录词表 `table`。已知边界：卡片中的规模声明为作者自报（出现过 64 亿行的异常值），质量分级属 M2 范围。

**进度（2026-10-15，M2）**：M2 已完成开发并验证——新增 `enrichment / idea / pairing / snapshot` 四表（dataset 增量迁移 `source_modified_at`）；整理流水线离线解析卡片 frontmatter 产出 schema 摘要与质量信号候选（117/117 处理）；嵌入索引经 enrichment 表落地（HashEmbedder 冒烟 + bge-small-zh-v1.5 真实模型，镜像站下载可用）；混合检索为结构化硬过滤 + 余弦重排；配对提案只能落 pending，驳回联动快照失效；MCP server 按契约暴露五个工具（`mcp` 锁定 <2，v2 迁移留待后续）。集成链路"检索 → 提案 → 人工确认 → 冻结 → 审批 → 导出"在真实库上跑通，导出含许可清单与声明。灵析侧真实接入（思路卡出口）属灵析仓库改动，尚未进行。

**进度（2026-10-15，M4）**：M4 灵析接线已完成并闭环验证——灵析侧在 academic-research 技能新增 `scripts/dataset_catalog.py`（search/evidence/propose/status，REST 调用，配置缺失即停，沿用 task-api.local.json 与 _bypass_proxy 约定）+ 参考文档 + SKILL.md 路由行 + 变更日志（11 项单测过，HTTP 全 mock）；目录服务新增 `POST /api/match`、`POST /api/pairings`（提案只落 pending 并留审计）、`GET /api/ideas/{id}/status` 三端点。对部署服务（10.10.70.12:5280）的真实闭环已跑通：脚本检索中文文本候选 → evidence 核证 → 提案落 pending → 策展 REST 确认 → 冻结 → 批准 → 导出（`idea_dataset_snapshot` V1，许可清单 ['mit']）→ status 回读 approved。未决项：灵析网关的技能文件同步属其个人环境部署（非本机账户），前端 ResearchIdea 与思路卡的结构打通留待后续。

**进度（2026-10-15，M3）**：M3 已完成并端到端验收——新增 `review / audit` 留痕表与 FastAPI 目录服务（`/api/overview|datasets|review/enrichments|pairings|ideas|snapshots/*|runs|audit`）；演示版前端新增策展模式（`?mode=catalog`，构建期可用 `VITE_CATALOG_API` 固定），五个页面覆盖概览、数据目录、整理核验、配对与发布、运行审计，演示模式原样保留。验证：Python 侧 85 项测试（TestClient 覆盖全端点与错误路径）、前端 98 项单测、34 项浏览器测试（29 项演示回归 + 5 项策展链路：实时统计、目录筛选与证据、候选核验出队、配对确认→冻结→批准→导出、断网提示）全部通过。实施中修复：JSX 元素恒真值导致加载态短路（概览/详情空白）、Playwright webServer 的 Windows 路径与 fixture 建库时序、审批接口请求体模型误用。已知边界：MCP/REST 均无鉴权（内部测试约定）；策展模式暂无宿主嵌入入口。

## 11. 风险与开放问题

1. **HF 卡片质量参差**：大量数据集无 license 或无任务标签 → 以 `unknown` + 核验队列消化，不推断。
2. **三维数据源分散**：登记型适配器是过渡方案，自动化程度低，需要评估各源的使用条款。
3. **嵌入检索的可解释性**：向量排序必须叠加结构化过滤与证据返回，避免"相似但不可用"的黑盒推荐进入核验台。
4. **灵析侧配合改造**：思路卡契约需要灵析前端/后端各加一个出口，需与灵析团队对齐排期。
5. **许可合规债**：内部测试阶段就记录 license，避免未来进院或对外时返工。
