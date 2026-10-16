"""Offline, annotation-aware comparison of baseline and agent dataset rankings.

Run: python -m xiaoyi_catalog.evaluation --input pilot.json --output report.json
No service calls, model calls, downloads, or inferred labels are performed.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

K = 5
METHODS = ("baseline", "agent")
MODALITIES = ("text", "table", "image", "3d")


class EvaluationError(ValueError):
    """Invalid or ambiguous evaluation input."""


def _object(value: Any, location: str) -> dict:
    if not isinstance(value, dict):
        raise EvaluationError(f"{location}: expected an object")
    return value


def _list(value: Any, location: str) -> list:
    if not isinstance(value, list):
        raise EvaluationError(f"{location}: expected an array")
    return value


def _id(value: Any, location: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise EvaluationError(f"{location}: expected a non-empty string or integer ID")
    return str(value).strip()


def _decision(value: Any, location: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise EvaluationError(f"{location}: use true, false, or null for a human label")
    return value


def _unique_ids(values: list, location: str) -> list[str]:
    result = [_id(value, f"{location}[{index}]") for index, value in enumerate(values)]
    if len(result) != len(set(result)):
        raise EvaluationError(f"{location}: duplicate IDs (numeric and string forms identify the same item)")
    return result


def _facts(items: Any, location: str) -> dict:
    facts = _list(items, location)
    seen: set[str] = set()
    missing: list[str] = []
    unresolved: list[str] = []
    results = []
    for index, raw in enumerate(facts):
        fact = _object(raw, f"{location}[{index}]")
        ident = _id(fact.get("fact_id"), f"{location}[{index}].fact_id")
        if ident in seen:
            raise EvaluationError(f"{location}: duplicate fact_id {ident}")
        seen.add(ident)
        version = _id(fact.get("dataset_version_id"), f"{location}[{index}].dataset_version_id")
        if not isinstance(fact.get("text"), str) or not fact["text"].strip():
            raise EvaluationError(f"{location}[{index}].text: required factual claim")
        evidence = _unique_ids(_list(fact.get("evidence_ids", []), f"{location}[{index}].evidence_ids"),
                               f"{location}[{index}].evidence_ids")
        supported = _decision(fact.get("supported"), f"{location}[{index}].supported")
        if supported is True and not evidence:
            raise EvaluationError(f"{location}: supported fact {ident} must cite at least one version-specific evidence ID")
        if "supported" not in fact:
            missing.append(ident)
        elif supported is None:
            unresolved.append(ident)
        results.append({"fact_id": ident, "dataset_version_id": version, "text": fact["text"],
                        "evidence_ids": evidence, "supported": supported})
    judged = sum(item["supported"] is not None for item in results)
    supported_count = sum(item["supported"] is True for item in results)
    total = len(results)
    return {"total": total, "judged": judged, "supported": supported_count,
            "accuracy": supported_count / total if total and judged == total else None,
            "judged_accuracy": supported_count / judged if judged else None,
            "annotation_coverage": judged / total if total else None,
            "missing_labels": missing, "unresolved_labels": unresolved, "facts": results}


def _ranking(method: dict, labels: dict[str, bool | None], complete: bool, location: str) -> dict:
    # null means no run has been recorded; [] means the run returned no candidates.
    raw_ranking = method.get("ranking")
    facts = _facts(method.get("facts", []), f"{location}.facts")
    if raw_ranking is None:
        return {"run_recorded": False, "top_5": None, "hit_at_5": None, "precision_at_5": None,
                "recall_at_5": None, "missing_relevance_labels": [], "unresolved_relevance_labels": [],
                "evidence": facts}
    ranking = _unique_ids(_list(raw_ranking, f"{location}.ranking"), f"{location}.ranking")
    top = ranking[:K]
    missing = [ident for ident in ranking if ident not in labels]
    unresolved = [ident for ident in ranking if ident in labels and labels[ident] is None]
    fully_judged = all(ident in labels and labels[ident] is not None for ident in top)
    positive = sum(labels.get(ident) is True for ident in top)
    relevant_total = sum(value is True for value in labels.values())
    universe_judged = complete and bool(labels) and all(value is not None for value in labels.values()) and not missing
    # A single human-confirmed hit proves Hit@5 even if other candidates are unjudged.
    has_judgments = any(value is not None for value in labels.values())
    hit = 1.0 if positive else (0.0 if fully_judged and has_judgments else None)
    precision = positive / K if fully_judged and has_judgments else None
    recall = positive / relevant_total if universe_judged and relevant_total and fully_judged else None
    return {"run_recorded": True, "top_5": top, "returned": len(ranking),
            "judged_top_5": sum(labels.get(ident) is not None for ident in top),
            "relevant_top_5": positive if fully_judged else None,
            "hit_at_5": hit, "precision_at_5": precision, "recall_at_5": recall,
            "known_relevant_total": relevant_total if labels else None,
            "recall_universe_complete": universe_judged,
            "missing_relevance_labels": missing, "unresolved_relevance_labels": unresolved,
            "evidence": facts}


def _mean(values: list[float | None]) -> dict:
    available = [value for value in values if value is not None]
    return {"value": math.fsum(available) / len(available) if available else None,
            "evaluated_tasks": len(available), "total_tasks": len(values)}


def evaluate(document: dict) -> dict:
    """Evaluate only explicit human annotations; preserve absent judgments as null."""
    document = _object(document, "input")
    if document.get("schema_version") != "1":
        raise EvaluationError("schema_version must be '1'")
    tasks = _list(document.get("tasks"), "tasks")
    if not tasks:
        raise EvaluationError("tasks must contain at least one research task")
    seen: set[str] = set()
    outputs = []
    for index, raw in enumerate(tasks):
        where = f"tasks[{index}]"
        task = _object(raw, where)
        ident = _id(task.get("task_id"), f"{where}.task_id")
        if ident in seen:
            raise EvaluationError(f"duplicate task_id {ident}")
        seen.add(ident)
        if task.get("modality") not in MODALITIES:
            raise EvaluationError(f"{where}.modality must be one of {MODALITIES}")
        if not isinstance(task.get("goal"), str) or not task["goal"].strip():
            raise EvaluationError(f"{where}.goal must describe the research task")
        complete = task.get("relevance_complete", False)
        if not isinstance(complete, bool):
            raise EvaluationError(f"{where}.relevance_complete must be a boolean")
        labels: dict[str, bool | None] = {}
        missing_decisions = []
        for label_index, raw_label in enumerate(_list(task.get("relevance_labels", []), f"{where}.relevance_labels")):
            label = _object(raw_label, f"{where}.relevance_labels[{label_index}]")
            version = _id(label.get("dataset_version_id"), f"{where}.relevance_labels[{label_index}].dataset_version_id")
            if version in labels:
                raise EvaluationError(f"{where}: duplicate relevance label for version {version}")
            labels[version] = _decision(label.get("relevant"), f"{where}.relevance_labels[{label_index}].relevant")
            if "relevant" not in label:
                missing_decisions.append(version)
        methods = _object(task.get("methods"), f"{where}.methods")
        if any(name not in methods for name in METHODS):
            raise EvaluationError(f"{where}.methods must explicitly contain baseline and agent; use ranking:null before a run")
        output = {"task_id": ident, "modality": task["modality"], "goal": task["goal"],
                  "label_count": len(labels), "missing_label_decisions": missing_decisions,
                  "methods": {name: _ranking(_object(methods[name], f"{where}.methods.{name}"), labels,
                                             complete, f"{where}.methods.{name}") for name in METHODS}}
        outputs.append(output)
    summary = {}
    for name in METHODS:
        summary[name] = {metric: _mean([task["methods"][name][metric] for task in outputs])
                         for metric in ("hit_at_5", "precision_at_5", "recall_at_5")}
        evidence = [task["methods"][name]["evidence"] for task in outputs]
        count = sum(item["total"] for item in evidence)
        judged = sum(item["judged"] for item in evidence)
        supported = sum(item["supported"] for item in evidence)
        summary[name]["evidence"] = {"total": count, "judged": judged, "supported": supported,
            "accuracy": supported / count if count and judged == count else None,
            "judged_accuracy": supported / judged if judged else None,
            "annotation_coverage": judged / count if count else None}
    return {"schema_version": "1", "evaluation_version": "pilot-evaluation/1", "k": K,
            "catalog": document.get("catalog", {}),
            "missing_pilot_modalities": [kind for kind in MODALITIES if not any(task["modality"] == kind for task in outputs)],
            "tasks": outputs, "summary": summary,
            "interpretation": "Human labels only. Null means unavailable or undefined; judged_accuracy describes only the annotated facts. No live pilot, authorization, or file integrity is verified by this evaluator."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare offline baseline and agent rankings using human relevance and evidence labels.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.input.resolve() == args.output.resolve():
        parser.error("--output must not overwrite the annotation input")
    try:
        report = evaluate(json.loads(args.input.read_text(encoding="utf-8-sig")))
    except (OSError, json.JSONDecodeError, EvaluationError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
