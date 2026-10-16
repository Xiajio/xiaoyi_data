from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from xiaoyi_catalog.evaluation import EvaluationError, evaluate, main


def document():
    return {"schema_version": "1", "tasks": [{
        "task_id": "text-task", "modality": "text", "goal": "比较有许可依据的中文新闻分类数据集",
        "relevance_complete": True,
        "relevance_labels": [{"dataset_version_id": value, "relevant": value in (1, 3, 6)} for value in range(1, 7)],
        "methods": {
            "baseline": {"ranking": [1, 2, 3, 4, 5, 6], "facts": []},
            "agent": {"ranking": [6, 1, 3, 2, 4], "facts": [
                {"fact_id": "f1", "dataset_version_id": 6, "text": "来源声明 MIT 许可", "evidence_ids": [10], "supported": True},
                {"fact_id": "f2", "dataset_version_id": 6, "text": "包含独立测试集", "evidence_ids": [11], "supported": False},
            ]},
        },
    }]}


def test_exact_top_five_metrics_and_fact_accuracy():
    report = evaluate(document())
    baseline = report["tasks"][0]["methods"]["baseline"]
    agent = report["tasks"][0]["methods"]["agent"]
    assert baseline["top_5"] == ["1", "2", "3", "4", "5"]
    assert baseline["hit_at_5"] == 1
    assert baseline["precision_at_5"] == pytest.approx(2 / 5)
    assert baseline["recall_at_5"] == pytest.approx(2 / 3)
    assert agent["precision_at_5"] == pytest.approx(3 / 5)
    assert agent["recall_at_5"] == 1
    assert agent["evidence"]["accuracy"] == 0.5
    assert report["summary"]["agent"]["evidence"]["accuracy"] == 0.5


def test_short_rankings_use_five_slots_not_only_the_returned_count():
    data = document()
    data["tasks"][0]["methods"]["baseline"]["ranking"] = [1, 3]
    result = evaluate(data)["tasks"][0]["methods"]["baseline"]
    assert result["returned"] == 2
    assert result["precision_at_5"] == pytest.approx(2 / 5)
    assert result["recall_at_5"] == pytest.approx(2 / 3)


def test_missing_relevance_is_reported_and_never_counted_as_negative():
    data = document()
    data["tasks"][0]["relevance_labels"] = [{"dataset_version_id": 1, "relevant": True}]
    result = evaluate(data)["tasks"][0]["methods"]["baseline"]
    assert result["missing_relevance_labels"] == ["2", "3", "4", "5", "6"]
    assert result["hit_at_5"] == 1  # The confirmed hit is enough to determine this metric.
    assert result["precision_at_5"] is None
    assert result["recall_at_5"] is None
    assert result["relevant_top_5"] is None


def test_null_and_absent_relevance_decisions_are_distinct_gaps():
    data = document()
    data["tasks"][0]["relevance_labels"] = [{"dataset_version_id": 1}, {"dataset_version_id": 2, "relevant": None}]
    result = evaluate(data)["tasks"][0]
    assert result["missing_label_decisions"] == ["1"]
    assert result["methods"]["baseline"]["unresolved_relevance_labels"] == ["1", "2"]
    assert result["methods"]["baseline"]["hit_at_5"] is None


def test_partial_fact_annotations_do_not_invent_zero_accuracy():
    data = document()
    facts = data["tasks"][0]["methods"]["agent"]["facts"]
    facts[0].pop("supported")
    facts[1]["supported"] = None
    result = evaluate(data)["tasks"][0]["methods"]["agent"]["evidence"]
    assert result["accuracy"] is None
    assert result["judged_accuracy"] is None
    assert result["missing_labels"] == ["f1"]
    assert result["unresolved_labels"] == ["f2"]
    facts[0]["supported"] = True
    result = evaluate(data)["tasks"][0]["methods"]["agent"]["evidence"]
    assert result["accuracy"] is None
    assert result["judged_accuracy"] == 1
    assert result["annotation_coverage"] == 0.5


def test_recall_requires_an_explicit_complete_relevance_universe():
    data = document()
    data["tasks"][0]["relevance_complete"] = False
    result = evaluate(data)["tasks"][0]["methods"]["agent"]
    assert result["precision_at_5"] == pytest.approx(0.6)
    assert result["recall_at_5"] is None
    data["tasks"][0]["relevance_complete"] = True
    for label in data["tasks"][0]["relevance_labels"]:
        label["relevant"] = False
    result = evaluate(data)["tasks"][0]["methods"]["agent"]
    assert result["hit_at_5"] == 0
    assert result["precision_at_5"] == 0
    assert result["recall_at_5"] is None  # Zero denominator is undefined.


def test_empty_rankings_differ_from_unexecuted_runs():
    data = document()
    data["tasks"][0]["methods"]["baseline"]["ranking"] = []
    data["tasks"][0]["methods"]["agent"]["ranking"] = None
    result = evaluate(data)["tasks"][0]["methods"]
    assert result["baseline"]["run_recorded"] is True
    assert result["baseline"]["hit_at_5"] == 0
    assert result["baseline"]["precision_at_5"] == 0
    assert result["baseline"]["recall_at_5"] == 0
    assert result["agent"]["run_recorded"] is False
    assert result["agent"]["hit_at_5"] is None


@pytest.mark.parametrize("case", ["ranking", "label", "fact", "task", "evidence"])
def test_duplicate_identifiers_are_rejected(case):
    data = document()
    task = data["tasks"][0]
    if case == "ranking": task["methods"]["baseline"]["ranking"] = [1, "1"]
    elif case == "label": task["relevance_labels"].append({"dataset_version_id": "1", "relevant": False})
    elif case == "fact": task["methods"]["agent"]["facts"].append(deepcopy(task["methods"]["agent"]["facts"][0]))
    elif case == "task": data["tasks"].append(deepcopy(task))
    else: task["methods"]["agent"]["facts"][0]["evidence_ids"] = [10, "10"]
    with pytest.raises(EvaluationError, match="duplicate"):
        evaluate(data)


def test_true_fact_without_locatable_evidence_and_non_boolean_labels_are_rejected():
    data = document()
    data["tasks"][0]["methods"]["agent"]["facts"][0]["evidence_ids"] = []
    with pytest.raises(EvaluationError, match="cite"):
        evaluate(data)
    data = document()
    data["tasks"][0]["relevance_labels"][0]["relevant"] = "yes"
    with pytest.raises(EvaluationError, match="true, false, or null"):
        evaluate(data)


def test_summary_reports_metric_coverage_and_does_not_fill_missing_tasks():
    data = document()
    pending = deepcopy(data["tasks"][0])
    pending.update(task_id="image-task", modality="image", relevance_complete=False, relevance_labels=[])
    pending["methods"] = {name: {"ranking": None, "facts": []} for name in ("baseline", "agent")}
    data["tasks"].append(pending)
    summary = evaluate(data)["summary"]["agent"]["precision_at_5"]
    assert summary == {"value": 0.6, "evaluated_tasks": 1, "total_tasks": 2}


def test_template_is_a_four_modality_pending_pilot_not_an_evaluation_result():
    path = Path(__file__).resolve().parents[2] / "docs" / "pilot-template.json"
    report = evaluate(json.loads(path.read_text(encoding="utf-8-sig")))
    assert len(report["tasks"]) == 4
    assert report["missing_pilot_modalities"] == []
    for method in report["summary"].values():
        assert method["precision_at_5"]["value"] is None
        assert method["precision_at_5"]["evaluated_tasks"] == 0
        assert method["evidence"]["accuracy"] is None


def test_cli_writes_a_reproducible_report_without_overwriting_input(tmp_path):
    source = tmp_path / "input.json"
    output = tmp_path / "report.json"
    source.write_text(json.dumps(document(), ensure_ascii=False), encoding="utf-8")
    assert main(["--input", str(source), "--output", str(output)]) == 0
    first = output.read_bytes()
    assert main(["--input", str(source), "--output", str(output)]) == 0
    assert output.read_bytes() == first
    with pytest.raises(SystemExit):
        main(["--input", str(source), "--output", str(source)])
