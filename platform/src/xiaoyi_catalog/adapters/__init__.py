"""适配器包：base 定义协议，huggingface 为首个自动来源实现。"""

from .base import CollectScope, CollectStats, DatasetRef, RawDataset, SourceAdapter

__all__ = ["CollectScope", "CollectStats", "DatasetRef", "RawDataset", "SourceAdapter"]
