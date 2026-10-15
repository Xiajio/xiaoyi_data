"""来源元数据 → 目录字段的确定性归一化。

原则（对应设计文档）：只使用来源自带的标签与映射表，无标签时返回 unknown，
不做模型推断。所有映射表集中在本文件，改动需要同步单测。
"""

from __future__ import annotations

# 模态词表与前端 AssetKind 对齐：text/table/image/3d；HF 上另有 audio/video 等，
# 目录如实记录，不强行塞进四类。
KNOWN_MODALITIES = ("text", "table", "image", "audio", "video", "3d")

# modality 标签别名：HF 用 tabular，目录词表与前端 AssetKind 对齐用 table。
_MODALITY_TAG_ALIAS = {"tabular": "table"}

# task_categories → 模态的确定性映射（前缀优先，精确匹配兜底）。
_TASK_PREFIX_TO_MODALITY = {
    "text": "text",
    "token-classification": "text",
    "question-answering": "text",
    "translation": "text",
    "summarization": "text",
    "fill-mask": "text",
    "sentence-similarity": "text",
    "feature-extraction": "text",
    "tabular": "table",
    "time-series": "table",
    "image": "image",
    "object-detection": "image",
    "image-segmentation": "image",
    "text-to-image": "image",
    "visual-question-answering": "image",
    "audio": "audio",
    "automatic-speech-recognition": "audio",
    "text-to-speech": "audio",
    "video": "video",
}
_TASK_EXACT_TO_MODALITY = {
    "text-to-3d": "3d",
    "image-to-3d": "3d",
}

_PERMISSIVE = {
    "mit", "mit-0", "apache-2.0", "bsd", "bsd-2-clause", "bsd-3-clause", "isc",
    "cc0-1.0", "cc-by-4.0", "cc-by-3.0", "unlicense", "zlib", "ofl-1.1", "wtfpl",
    "pddl", "odc-by", "afl-3.0", "ms-pl", "eupl-1.1", "lpl-1.02",
}
_COPYLEFT = {
    "gpl", "gpl-2.0", "gpl-3.0", "gpl-3.0+", "lgpl", "lgpl-2.1", "lgpl-3.0",
    "agpl-3.0", "mpl-2.0", "epl-2.0", "cc-by-sa-3.0", "cc-by-sa-4.0", "odbl",
    "eupl-1.2", "cpol",
}
_LICENSE_CLASSES = ("permissive", "copyleft", "research-only", "unknown")


def derive_modalities(modality_tags: tuple[str, ...] | list[str], task_categories: tuple[str, ...] | list[str]) -> tuple[list[str], str]:
    """返回 (模态列表, 推导来源)。来源 ∈ tag / task-map / unknown。

    优先使用来源的 modality:* 标签；缺失时按任务类别映射表推导；
    两者都没有时返回空列表与 unknown——空列表在检索侧展示为缺失。
    """
    direct = sorted({_MODALITY_TAG_ALIAS.get(tag.split(":", 1)[1].strip().lower(), tag.split(":", 1)[1].strip().lower()) for tag in modality_tags if tag.startswith("modality:") and tag.split(":", 1)[1].strip()})
    if direct:
        return direct, "tag"
    derived: set[str] = set()
    for task in task_categories:
        task = task.strip().lower()
        if not task:
            continue
        if task in _TASK_EXACT_TO_MODALITY:
            derived.add(_TASK_EXACT_TO_MODALITY[task])
            continue
        for prefix, modality in _TASK_PREFIX_TO_MODALITY.items():
            if task == prefix or task.startswith(prefix + "-"):
                derived.add(modality)
                break
    if derived:
        return sorted(derived), "task-map"
    return [], "unknown"


def classify_license(license_id: str) -> str:
    """license 标签 → permissive/copyleft/research-only/unknown。未识别一律 unknown。"""
    value = (license_id or "").strip().lower()
    if not value or value in {"unknown", "other", "none"}:
        return "unknown"
    if value in _PERMISSIVE:
        return "permissive"
    if value in _COPYLEFT:
        return "copyleft"
    if "-nc" in value or value.startswith("cc-by-nc") or "non-commercial" in value or "research-only" in value or "research_only" in value:
        return "research-only"
    return "unknown"


def missing_fields(modalities: list[str], license_class: str, card_text: str) -> list[str]:
    """检索结果附带的缺失清单：让调用方（含智能体）看到"哪些属性是未知的"。"""
    missing: list[str] = []
    if not modalities:
        missing.append("modalities")
    if license_class not in _LICENSE_CLASSES[:-1]:
        missing.append("license")
    if not card_text.strip():
        missing.append("card")
    return missing
