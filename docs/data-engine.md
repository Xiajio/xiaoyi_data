# 样本处理、挖掘与实验回流（CPU 首版）

该实现将已获批准的实际资产转为不可变样本构建，所有重操作在任务 worker 中执行。默认部署只需现有 Python、SQLAlchemy、PyArrow、Pillow 和 PostgreSQL；开发夹具支持 SQLite。目录元数据不会被转换成虚构样本。

## 数据与执行约定

- `Sample` 的 ID 固定为来源版本、来源路径与行位置的摘要；行内容摘要单独保存。同一来源版本/位置再次出现不同原始内容会使构建失败，必须创建新的来源版本，禁止 upsert 静默跳过。标签改动通过新配方的 `labels` 覆盖生成新构建，保留原样本 ID、旧构建和旧审核。`AssetReference` 保留图像标签文件及实际图像两条来源。
- `DatasetBuild` 固定输入清单与配方，输出 `sha256/` 下的分片 Parquet、全部排除原因清单、每步数量、切分分布与算法版本。原始文件仅引用；导出可携式包时按用户明确要求包含原始文件。
- `BuildSample` 提供构建成员、分片位置和分页索引。`AnnotationSet` 固定标签提取版本，`EntityGroup` 固定分组。完整字段、标签、特征和预测保留于 Parquet，不写入业务表大字段。
- 每个配方最多显式固定 10,000 个源资产；百万级处理适用于这些资产中的行样本，百万个独立图像文件需要后续的资产清单流式选择扩展。输入 CSV、TSV、JSONL、TXT、Parquet 按批读取；图像支持 PNG/JPEG/WebP/BMP/TIFF/GIF。缺失必需字段、损坏图像、缺失关联、标签不在允许集合的行进入排除清单，`invalid_policy=fail` 则使构建失败。
- 去重以内容与标签共同摘要为准，保留同内容不同标签以供冲突审阅。默认分组使用内容摘要；显式 `group_column` 时同一实体只能属于一个切分。切分用固定 seed 与组摘要；预算选择用固定 seed 与样本摘要排序。
- SQLite 临时磁盘索引负责全量去重与选择，临时表内存缓存 16 MiB，Parquet 默认每 2,000 行或累计序列化内容 16 MiB 切片；每行内容最大 4 MiB。单图解码受 Pillow 解压缩炸弹保护。上述为批处理约束，8 GiB 总 RSS 目标仍需在生产规格下测量。
- 旧 CSV/TSV/JSONL 派生已切换到事务外磁盘去重：批准输入捕获、重计算、最终短事务发布分离，磁盘摘要索引替代全量内存集合。保留同步 Python API 兼容；HTTP/CLI/MCP 使用后台任务。默认单行 4 MiB、总行数上限由 `XIAOYI_DERIVE_MAX_ROWS` 控制。
- 写入批次与最终结果提交均执行任务所有者/执行代号校验。部分产物不可见于就绪构建；重试重建成员索引，已完成构建返回原结果。失去租约的进程不得发布或改写失败状态。构建、画像、挖掘、派生、实验与导出在领域结果提交同一事务内保存 Job Artifact 完成凭据，覆盖结果提交后、Job.finish 前的崩溃和晚到取消窗口。
- `0010_samples` 只新增表，旧 QualityReport 原样复制到 ProfileRun，保留抽样、截断及覆盖字段。新画像追加 ProfileRun/MetricObservation，不覆盖旧报告。

## 提交构建、审核与交付

所有 API 使用现有认证。研究员及以上可提交，只有人工 reviewer/admin 可发布。客户端 payload 不得指定执行者或角色。

```http
POST /api/builds
Content-Type: application/json
Idempotency-Key: optional-explicit-new-run-key

{"recipe":{"version":"1","operator_version":"cpu/1","asset_ids":[101],"modality":"text","text_column":"text","label_column":"label","group_column":"patient_id","required_columns":["text","label","patient_id"],"allowed_labels":["positive","negative"],"exact_dedup":true,"seed":42,"splits":{"train":0.8,"validation":0.1,"test":0.1},"sample_budget":100000,"shard_rows":2000,"invalid_policy":"exclude"}}
```

返回 `202 {build_id,job_id,status}`。未提供幂等键时按当前用户与规范配方复用同一个任务；显式使用新键可生成独立复现构建。输入和执行阶段都核对原下载/本地导入的完整批准链与固定内容清单。

表格配方用 `modality=table`，可通过 `feature_columns` 限定特征；不指定则排除标签和组字段后保留所有字段。纯文本每行一个样本，默认字段名 `text`。图像直接使用图像资产 ID 列表及 `labels: {"images/a.png":"cat"}`，或把图像和标签表一起放入 `asset_ids`，使用 `image_path_column`、`label_column` 按同一来源版本内的路径关联。标注修订用 `labels: {"<sample_id>":"corrected-label"}` 并提交新的构建；不继承旧构建发布批准。

- `GET /api/builds`、`GET /api/builds/{id}` 查询构建状态；列表支持 `order=asc|desc`、`after_id` 与最多 100 条的 `limit`。
- `GET /api/builds/{id}/manifest` 返回固定清单及 SHA-256；非就绪返回 409。
- `GET /api/builds/{id}/samples?after_id=0&limit=50` 返回成员索引；最大 100 条，使用返回的 `next_after_id` 翻页。
- `POST /api/builds/{id}/publish {"manifest_hash":"..."}` 由人工审核者签认精确内容；重复请求不会覆盖首次批准人。
- `GET /api/builds/{id}/artifacts/{sha256}` 仅交付已人工发布的构建中的文件。
- 后台 `build.export` 处理器 `execute_export` 仅接受已人工发布且批准哈希一致的构建，流式生成确定性 ZIP。ZIP 包含 `load.py`、`manifest.json`、`manifest.sha256`、原始资产与 Parquet；通过 jobs API 下载已完成产物。

导出 ZIP 解压后，安装 PyArrow，运行 `python load.py` 即校验全部输入/产物哈希并输出样本数；训练代码可导入 `load()` 按批加载。更改清单、源文件或分片将使校验失败。

## 候选挖掘与实验

```http
POST /api/builds/12/mining

{"build_id":12,"benchmark_build_id":9,"max_hamming":3,"bucket_limit":128,"max_findings":100000}
```

返回后台任务。文本采用 Unicode 单词与字符三元组 SimHash-64；图像采用灰度 dHash-64；四个 16 位桶召回候选，最大汉明距离 3。文本指纹最多处理前 100,000 个字符，长文本计入 `signature_inputs_truncated`。每个桶默认取 128 条，最多输出 100,000 个关系，所有截断明确记入 ProfileRun。当前实现是有界近重复候选筛查，不能声称语义去重或证明不存在污染。

结果覆盖文本/图像近重复、相同内容标签冲突、相同内容跨切分泄漏、与固定基准构建重合的污染候选及标签/切分分布。每条关系保存双方 sample_id、算法版本和分数；`GET /api/profiles/{id}/findings` 分页读取，完整发现保存为 Parquet。所有样本都会被扫描，候选桶和分布输出可能截断；结果不自动删除任何样本。查看 `GET /api/builds/{id}/profiles` 获取覆盖和截断信息。

外部实验首版支持分类精确匹配，不声称实现回归、检测或分割评估。先通过原资产导入流程批准 JSONL/Parquet 预测文件，每条记录必须包含：

```json
{"build_id":9,"sample_id":"<fixed evaluation sample ID>","prediction":"positive"}
```

再提交：

```http
POST /api/experiments/import

{"build_id":12,"prediction_asset_id":204,"model":{"name":"classifier","version":"sha256:fixed-model","training_budget":"1 epoch, 80 samples","seed":42},"protocol":{"version":"1","task":"classification","evaluation_build_id":9,"split":"test","require_complete":true}}
```

外层 build_id 是训练构建；预测行 build_id 是固定评估构建。worker 将评估成员和已导入标记存临时磁盘表，拒绝未知样本、错误构建/切分、重复预测、缺失标签和不完整覆盖。逐样本预测、真值和错误候选保存为 Parquet，计算 accuracy/coverage/error_count。

- `GET /api/experiments` 支持与构建相同的游标分页和排序；`GET /api/experiments/{id}` 获取结果。`GET /api/experiments/{id}/errors` 供 researcher/reviewer/admin 下载内部错误审阅材料，不代表构建已获发布批准。
- 导入默认按当前用户与精确规范化实验 spec 幂等；提供新的 `Idempotency-Key` 才创建独立重复实验，复用相同键修改模型/协议将被拒绝。HTTP、CLI、MCP 共用提交函数。
- `POST /api/experiments/compare {"experiment_ids":[1,2,3,4]}` 以第一项为基线。模型版本、训练预算、seed、完整评估构建与协议必须全部一致；返回各构建指标和差值。
- 错误样本可用新配方的 sample_id 标签覆盖修订，然后重新构建、人工审核和实验。没有自动训练、自动发布或自动覆盖历史行为。

## 可复现验收与容量验证

从仓库根目录，使用安装了 platform 包的 Python。所有脚本必须提供一个不存在的新输出目录，不连接 `XIAOYI_CATALOG_DB`，不会操作现有数据。夹具自动批准只用于脚本在新隔离库里直接生成的合成内容，生产入口仍使用原人工审批流程。

```sh
python -m pytest platform/tests/test_sample_engine.py -q
python -m xiaoyi_catalog.sample_demo demo --output /tmp/xiaoyi-fixture-unique
python -m xiaoyi_catalog.sample_demo benchmark --rows 1000000 --output /tmp/xiaoyi-million-unique
```

演示固定 seed=42、词袋最近质心、每个方案最多 80 个固定排序训练样本、40 个独立固定评估样本，比较原始/清洗/精确去重/预算选择四个构建，真实生成预测并调用实验回流和比较。输出 `acceptance.json`、输入文件、固定清单、逐样本预测与 SQLite 库。该流程使用刻意含非法标签和重复项的合成数据，指标只证明处理闭环有效，不代表真实科研收益。

2026-10-18 本机验证：18 项样本引擎测试通过；相关派生/样本/jobs/API 联合回归 37 项通过，另覆盖事务外计算、磁盘满、损坏输入和崩溃重放；合成对照四组 accuracy 分别为 0.5、1.0、1.0、1.0，每组训练 80、评估 40、覆盖率 1.0。1000 行压测脚本烟测构建耗时约 0.30 秒、峰值 RSS 121,946,112 字节。百万样本、100 GB 文件、Linux PostgreSQL 恢复及 10 并发读取 P95 不在这组小样本结果的证明范围，仍需部署环境验收。百万脚本输出 `benchmark.json`，包含输入/输出行数、构建摘要、耗时、吞吐及进程峰值 RSS，不自动推定生产容量达标。
