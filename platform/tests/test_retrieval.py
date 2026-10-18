"""Relevance and coverage comparisons for the shared bounded catalog retriever."""
import json
from sqlalchemy import select

from conftest import FakeAdapter, make_raw
from xiaoyi_catalog.adapters.base import CollectScope
from xiaoyi_catalog.collect import collect
from xiaoyi_catalog.db import session_scope
from xiaoyi_catalog.embeddings import embed_all
from xiaoyi_catalog.match import match_datasets, requirement_to_filter
from xiaoyi_catalog.models import DatasetVersion
from xiaoyi_catalog.qa_tools import execute_query
from xiaoyi_catalog.retrieval import RetrievalService
from xiaoyi_catalog.search import SearchFilter


def test_keyword_relevance_beats_popularity_without_model(engine):
    collect(engine, FakeAdapter([
        make_raw("lab/popular", downloads=9999, card="unrelated speech"),
        make_raw("lab/needle", downloads=1, card="needle segmentation"),
        make_raw("lab/card", downloads=100, card="needle"),
    ]), CollectScope())
    with session_scope(engine) as session:
        result = RetrievalService(session).search(query_text="needle", filt=SearchFilter(limit=2))
        assert [item["source_key"] for item in result["results"]] == ["lab/needle", "lab/card"]
        assert result["ranking"] == "keyword" and result["total_is_exact"]
        assert result["matched_total"] == 2
        for item in result["results"]:
            assert item["evidence"]
            assert all(e["dataset_version_id"] == item["dataset_version_id"] for e in item["evidence"])


def test_api_facade_and_qa_share_keyword_candidates(engine):
    collect(engine, FakeAdapter([make_raw("lab/needle", card="needle"), make_raw("lab/other", card="other")]), CollectScope())
    with session_scope(engine) as session:
        api = match_datasets(session, requirement_to_filter({}), query_text="needle")
        qa = json.loads(execute_query(session, "search", {"query": "needle"})[0]["content"])
        assert [item["dataset_version_id"] for item in api["results"]] == [item["dataset_version_id"] for item in qa["items"]]
        assert api["retrieval_version"] == qa["retrieval_version"]


def test_vector_adds_semantic_candidates_but_hard_filters_hold(engine):
    raws = [
        make_raw("lab/needle", tasks=("text-classification",), card="needle", downloads=1),
        make_raw("lab/related", tasks=("text-classification",), card="semantic match", downloads=2),
        make_raw("lab/forbidden", tasks=("image-classification",), card="semantic perfect", downloads=999),
    ]
    collect(engine, FakeAdapter(raws), CollectScope())
    class Embedder:
        model_name, dim = "retrieval-fixture", 2
        def embed(self, texts):
            return [[1.0, 0.0] if "semantic" in value or value == "needle" else [0.8, 0.6] for value in texts]
    embed_all(engine, Embedder())
    with session_scope(engine) as session:
        result = RetrievalService(session, embedder=Embedder()).search(
            requirements={"modalities": ["text"]}, query_text="needle")
        assert result["ranking"] == "hybrid_rrf"
        assert {item["source_key"] for item in result["results"]} == {"lab/needle", "lab/related"}
        assert all(item["constraint_evaluation"]["status"] == "pass" for item in result["results"])
        assert "keyword" in result["branches"] and "vector" in result["branches"]


def test_candidate_limit_is_explicit_and_keeps_exact_sql_total(engine):
    collect(engine, FakeAdapter([make_raw(f"lab/needle-{i}", card="needle") for i in range(35)]),
            CollectScope(limit=100))
    with session_scope(engine) as session:
        result = RetrievalService(session, candidate_limit=10).search(query_text="needle", filt=SearchFilter(limit=10))
        assert result["candidate_count"] == 10 and len(result["results"]) == 10
        assert result["truncated"] and result["has_more"]
        assert result["matched_total"] == 35 and result["total_is_exact"]


def test_hard_filter_pushdown_prevents_popular_nonmatches_consuming_candidate_budget(engine):
    collect(engine, FakeAdapter([
        *[make_raw(f"lab/image-{i}", tasks=("image-classification",), card="needle", downloads=100) for i in range(12)],
        make_raw("lab/text", tasks=("text-classification",), card="needle", downloads=1),
    ]), CollectScope())
    with session_scope(engine) as session:
        result = RetrievalService(session, candidate_limit=3).search(
            query_text="needle", requirements={"all_of": [{"modalities": ["text"]}]})
        assert [item["source_key"] for item in result["results"]] == ["lab/text"]


def test_derived_version_does_not_replace_source_evidence(engine):
    collect(engine, FakeAdapter([make_raw("lab/needle", revision="source", card="source evidence")]), CollectScope())
    with session_scope(engine) as session:
        source = session.scalar(select(DatasetVersion))
        session.add(DatasetVersion(dataset_id=source.dataset_id, revision="derived", kind="derived",
                    parent_version_id=source.id, provenance_status="verified", metadata_json={"name": "derived"}))
        session.flush()
        result = RetrievalService(session).search(query_text="needle")
        assert result["results"][0]["dataset_version_id"] == source.id
        assert result["results"][0]["evidence"][0]["excerpt"] == "source evidence"
