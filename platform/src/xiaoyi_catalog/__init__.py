"""小亿数据集目录平台（内部开发测试版）。

分层：采集适配器（adapters）→ 采集编排（collect）→ 目录存储（models/db）→ 检索（search）→ CLI。
约束（对应 docs/dataset-platform-design.md 第 4 节）：自动流水线只写候选与证据；
字段缺失使用显式 unknown，不做无证据推断。
"""

__version__ = "0.1.0"
