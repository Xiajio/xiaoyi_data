"""Versioned, deterministic task constraints. No source code or model evaluation."""
from __future__ import annotations

from copy import deepcopy
import json
from dataclasses import dataclass, field

GROUPS = {"all_of", "any_of", "optional"}
SETS = {"modalities": "模态", "task_categories": "任务", "languages": "语言",
        "license_class": "许可分级", "license_classes": "许可分级"}
NUMBERS = {"min_rows": ("size_rows", True), "max_rows": ("size_rows", False),
           "min_bytes": ("size_bytes", True), "max_bytes": ("size_bytes", False)}
META = {"schema_version", "limit"}
STATES = ("pass", "fail", "unknown", "unsupported")


def validate_task_spec(value: dict, *, _depth=0) -> dict:
    """Validate shape/types; preserve unsupported fields so they cannot disappear."""
    if not isinstance(value, dict) or _depth > 8 or len(value) > 64:
        raise ValueError("TaskSpec 必须为有界对象（最多 8 层、每层 64 个字段）")
    if _depth == 0:
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError("TaskSpec 必须为有限 JSON 对象") from exc
        if len(encoded.encode("utf-8")) > 64000:
            raise ValueError("TaskSpec 超过 64 KB 上限")
    if any(not isinstance(key, str) or not key or len(key) > 128 for key in value):
        raise ValueError("TaskSpec 字段名必须为 1–128 字符")
    if "schema_version" in value and (type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise ValueError("仅支持 TaskSpec schema_version=1")
    if "license_class" in value and "license_classes" in value:
        raise ValueError("license_class 与 license_classes 不能同时出现")
    for key, item in value.items():
        if key in GROUPS:
            if not isinstance(item, list) or len(item) > 64 or (key == "any_of" and not item):
                raise ValueError(f"{key} 必须为最多 64 项的数组；any_of 不可为空")
            for child in item:
                validate_task_spec(child, _depth=_depth + 1)
        elif key in SETS:
            values = [item] if isinstance(item, str) else item
            if not isinstance(values, list) or len(values) > 64 or any(not isinstance(x, str) or not x.strip() or len(x) > 256 for x in values):
                raise ValueError(f"{key} 必须为非空字符串或字符串数组")
        elif key in NUMBERS:
            if item is not None and (type(item) is not int or item < 0):
                raise ValueError(f"{key} 必须为非负整数")
        elif key == "limit":
            if type(item) is not int or not 1 <= item <= 100:
                raise ValueError("limit 必须在 1–100 之间")
    for lower, upper in (("min_rows", "max_rows"), ("min_bytes", "max_bytes")):
        if value.get(lower) is not None and value.get(upper) is not None and value[lower] > value[upper]:
            raise ValueError(f"{lower} 不能大于 {upper}")
    return deepcopy(value)


def _combine(states, any_of=False):
    if not states:
        return "pass"
    if any_of:
        if "pass" in states:
            return "pass"
        if "unsupported" in states:
            return "unsupported"
        return "unknown" if "unknown" in states else "fail"
    if "fail" in states:
        return "fail"
    if "unsupported" in states:
        return "unsupported"
    return "unknown" if "unknown" in states else "pass"


def evaluate_task_spec(item: dict, requirements: dict) -> dict:
    spec = validate_task_spec(requirements)

    def evaluate(node, path="$", optional=False):
        checks, hard_states = [], []
        for key, wanted in node.items():
            if key in META:
                continue
            current = f"{path}.{key}"
            if key in GROUPS:
                children = [evaluate(child, f"{current}[{i}]", optional or key == "optional")
                            for i, child in enumerate(wanted)]
                state = _combine([child["status"] for child in children], key == "any_of")
                checks.append({"path": current, "status": state, "optional": optional or key == "optional",
                               "children": children, "message": f"{key}: {state}"})
                if key != "optional":
                    hard_states.append(state)
                continue
            state, message = "unsupported", f"暂不支持的硬约束：{key}"
            if key in SETS:
                values = [wanted] if isinstance(wanted, str) else wanted
                if not values:
                    continue
                source_key = "license_class" if key.startswith("license_class") else key
                actual = item.get(source_key)
                actuals = [actual] if isinstance(actual, str) else actual
                if not actuals or actuals == ["unknown"]:
                    state = "unknown"
                    message = f"{SETS[key]}未知，无法确认满足要求"
                elif set(values).intersection(actuals):
                    state, message = "pass", f"{SETS[key]}符合要求"
                else:
                    state = "fail"
                    message = f"{SETS[key]}不匹配：需要 {'/'.join(sorted(values))}"
            elif key in NUMBERS:
                if wanted is None:
                    continue
                source_key, minimum = NUMBERS[key]
                actual = item.get(source_key)
                if actual is None:
                    state = "unknown"
                    message = "规模未知，无法确认满足最小行数" if key == "min_rows" else f"规模未知，无法确认 {key}"
                elif (actual >= wanted if minimum else actual <= wanted):
                    state, message = "pass", f"{key} 符合要求"
                else:
                    state = "fail"
                    direction = "低于需求" if minimum else "超过上限"
                    message = f"规模 {actual} {direction} {wanted} ({source_key})"
            checks.append({"path": current, "field": key, "status": state, "optional": optional,
                           "message": message})
            hard_states.append(state)
        return {"status": _combine(hard_states), "checks": checks}

    result = evaluate(spec)
    return {"schema_version": 1, **result}


def requirement_gaps(item: dict, requirements: dict) -> list[str]:
    result = evaluate_task_spec(item, requirements)
    if result["status"] == "pass":
        return []
    def messages(node):
        found = []
        for check in node["checks"]:
            if check["optional"] or check["status"] == "pass":
                continue
            if "children" in check:
                for child in check["children"]:
                    found.extend(messages(child))
            else:
                found.append(check["message"])
        return found
    return messages(result)


@dataclass(frozen=True)
class TaskSpec:
    """Serializable facade used by CLI/API clients; legacy objects remain valid."""
    requirements: dict = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self):
        if self.schema_version != 1:
            raise ValueError("仅支持 TaskSpec schema_version=1")
        validate_task_spec(self.requirements)

    def evaluate(self, item: dict) -> dict:
        return evaluate_task_spec(item, self.requirements)
