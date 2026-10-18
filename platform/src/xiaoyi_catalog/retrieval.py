"""Bounded version-aware retrieval shared by API, MCP, agents and catalog Q&A."""
from __future__ import annotations

from collections import defaultdict
import re

from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.orm import aliased

from .models import Dataset, DatasetVersion, Evidence, Source
from .search import SearchFilter, dataset_to_dict, version_to_dict
from .task_spec import evaluate_task_spec, validate_task_spec

RETRIEVAL_VERSION = "catalog-retrieval/1"
CANDIDATE_LIMIT = 500
RRF_K = 60


def _terms(queries):
    result = []
    for query in queries:
        query = query.strip()[:200]
        if not query:
            continue
        result.append(query)
        # Punctuation is literal. It must not accidentally become a wildcard or a query language.
        if re.fullmatch(r"[\w\s-]+", query, flags=re.UNICODE):
            result.extend(token for token in re.split(r"[\s-]+", query) if len(token) >= 2)
    return list(dict.fromkeys(result))[:24]


def _filter_conditions(session, filt, sources):
    from .dashboard import modality_filter, license_filter
    conditions = [Dataset.status == "active"]
    if sources:
        conditions.append(Dataset.source_id.in_(select(Source.id).where(Source.kind.in_(sources))))
    for field, values in (("modalities", filt.modalities), ("task_categories", filt.task_categories),
                          ("languages", filt.languages)):
        if values:
            conditions.append(or_(*(modality_filter(getattr(Dataset, field), value, session.bind.dialect.name)
                                    for value in values)))
    if filt.license_classes:
        conditions.append(or_(*(license_filter(Dataset.license_class, value) for value in filt.license_classes)))
    for column, minimum, maximum in ((Dataset.size_rows, filt.min_rows, filt.max_rows),
                                     (Dataset.size_bytes, filt.min_bytes, filt.max_bytes)):
        if minimum is not None:
            conditions.append(column.is_not(None) & (column >= minimum))
        if maximum is not None:
            conditions.append(column.is_not(None) & (column <= maximum))
    return conditions


def _task_pushdown(session, spec):
    """A safe superset on captured metadata; final four-state evaluation remains authoritative."""
    from .dashboard import modality_filter
    version = aliased(DatasetVersion)
    def node(value):
        parts = []
        for key, wanted in value.items():
            if key in {"schema_version", "limit", "optional"}:
                continue
            if key in {"all_of", "any_of"}:
                children = [node(child) for child in wanted]
                parts.append(and_(*children) if key == "all_of" and children else
                             or_(*children) if children else literal(True))
            elif key in {"modalities", "task_categories", "languages"}:
                values = [wanted] if isinstance(wanted, str) else wanted
                if values:
                    parts.append(or_(*(modality_filter(version.metadata_json[key], item, session.bind.dialect.name)
                                       for item in values)))
            elif key in {"license_class", "license_classes"}:
                values = [wanted] if isinstance(wanted, str) else wanted
                if values:
                    parts.append(version.metadata_json["license_class"].as_string().in_(values))
            elif key in {"min_rows", "max_rows", "min_bytes", "max_bytes"} and wanted is not None:
                field = "size_rows" if key.endswith("rows") else "size_bytes"
                actual = version.metadata_json[field].as_integer()
                parts.append(actual >= wanted if key.startswith("min") else actual <= wanted)
            # Unsupported requirements are retained for explicit unsupported diagnostics.
        return and_(*parts) if parts else literal(True)
    latest = select(func.max(DatasetVersion.id)).where(DatasetVersion.dataset_id == Dataset.id,
            DatasetVersion.kind == "source").correlate(Dataset).scalar_subquery()
    return exists(select(literal(1)).select_from(version).where(
        version.dataset_id == Dataset.id, version.id == latest, node(spec)))


class RetrievalService:
    def __init__(self, session, *, embedder=None, candidate_limit=CANDIDATE_LIMIT):
        self.session, self.embedder = session, embedder
        self.candidate_limit = max(1, min(CANDIDATE_LIMIT, int(candidate_limit)))

    def search(self, filt=None, *, query_text="", queries=None, requirements=None, sources=None,
               include_source_keys=(), include_ineligible=False, order="downloads"):
        filt = filt or SearchFilter()
        spec = requirements if requirements is not None else getattr(filt, "task_spec", None)
        if spec is not None:
            spec = validate_task_spec(spec)
        limit = min(100, filt.limit if filt.limit is not None else 100)
        offset = max(0, min(CANDIDATE_LIMIT, filt.offset))
        terms = _terms(list(queries or [])[:5] or [query_text or filt.query])
        conditions = _filter_conditions(self.session, filt, sources)
        if spec and not include_ineligible and any(key not in {"schema_version", "limit", "optional"} for key in spec):
            conditions.append(_task_pushdown(self.session, spec))
        tie = (Dataset.first_seen_at.desc(), Dataset.id.desc()) if order == "recent" else (Dataset.downloads.desc(), Dataset.id)
        keyword_conditions = [or_(*(column.contains(term, autoescape=True)
                                   for term in terms for column in (Dataset.name, Dataset.source_key, Dataset.card_text)))] if terms else []
        matched_total = int(self.session.scalar(select(func.count()).select_from(Dataset)
                            .where(*conditions, *keyword_conditions)) or 0)
        if terms:
            relevance = sum(case((Dataset.name.contains(term, autoescape=True), 4), else_=0) +
                            case((Dataset.source_key.contains(term, autoescape=True), 2), else_=0) +
                            case((Dataset.card_text.contains(term, autoescape=True), 1), else_=0) for term in terms)
            lexical = list(self.session.scalars(select(Dataset.id).where(*conditions, *keyword_conditions)
                           .order_by(relevance.desc(), *tie).limit(self.candidate_limit)))
        else:
            lexical = list(self.session.scalars(select(Dataset.id).where(*conditions)
                           .order_by(*tie).limit(self.candidate_limit)))
        branches = {"keyword" if terms else order: lexical}
        vector_scores, vector_note = {}, ""
        if terms and self.embedder is not None:
            from .embeddings import cosine_top_k
            scope = select(Dataset.id).where(*conditions)
            if self.session.bind.dialect.name != "postgresql":
                # SQLite is the bounded local fallback; PostgreSQL performs vector ordering in SQL.
                scope = list(self.session.scalars(scope.order_by(*tie).limit(self.candidate_limit)))
            ranked_vectors = cosine_top_k(self.session, self.embedder.embed([" ".join(terms)[:1000]])[0],
                                          self.candidate_limit, producer=self.embedder.model_name, dataset_ids=scope)
            vector_scores = dict(ranked_vectors)
            if ranked_vectors:
                branches["vector"] = list(dict.fromkeys(ident for ident, _ in ranked_vectors))
            else:
                vector_note = "嵌入索引为空，未做语义排序"
        if include_source_keys:
            branches["collected"] = list(self.session.scalars(select(Dataset.id).where(*conditions,
                Dataset.source_key.in_(list(include_source_keys)[:self.candidate_limit])).order_by(*tie).limit(self.candidate_limit)))
        scores, ranks = defaultdict(float), defaultdict(dict)
        for branch, ids in branches.items():
            for position, ident in enumerate(ids, 1):
                scores[ident] += 1.0 / (RRF_K + position)
                ranks[ident][branch] = position
        candidate_ids = sorted(scores, key=lambda ident: (-scores[ident], ident))[:self.candidate_limit]
        rows = {row.id: row for row in self.session.scalars(select(Dataset).where(Dataset.id.in_(candidate_ids)))}
        latest_ids = select(func.max(DatasetVersion.id)).where(DatasetVersion.kind == "source",
                     DatasetVersion.dataset_id.in_(candidate_ids)).group_by(DatasetVersion.dataset_id)
        versions = {version.dataset_id: version for version in self.session.scalars(
                    select(DatasetVersion).where(DatasetVersion.id.in_(latest_ids)))}
        accepted, counts = [], {state: 0 for state in ("pass", "fail", "unknown", "unsupported")}
        for ident in candidate_ids:
            row, version = rows[ident], versions.get(ident)
            frozen = version_to_dict(self.session, version) if version else {}
            evaluation = evaluate_task_spec(frozen, spec or {})
            counts[evaluation["status"]] += 1
            if not include_ineligible and evaluation["status"] != "pass":
                continue
            item = dataset_to_dict(row)
            # Metadata used for eligibility and returned as evidence is pinned to the source version.
            if version:
                item.update({key: frozen.get(key) for key in item if key not in {"downloads", "likes"}})
                item["id"] = row.id
            item.update(dataset_version_id=version.id if version else None,
                        revision=version.revision if version else None,
                        provenance_status=version.provenance_status if version else "legacy_unverified",
                        metadata_scope="captured_version" if version else "current_catalog_unversioned",
                        constraint_evaluation=evaluation, retrieval_score=round(scores[ident], 8),
                        retrieval_ranks=ranks[ident])
            if ident in vector_scores:
                item["similarity"] = round(vector_scores[ident], 4)
            accepted.append(item)
        page = accepted[offset:offset + limit]
        for item in page:
            evidence = self.session.scalars(select(Evidence).where(
                Evidence.dataset_version_id == item["dataset_version_id"]).order_by(Evidence.id).limit(4)).all() if item["dataset_version_id"] else []
            item["evidence"] = [{"id": row.id, "dataset_version_id": row.dataset_version_id,
                                "location": row.location, "excerpt": row.excerpt[:1000],
                                "truncated": len(row.excerpt) > 1000} for row in evidence]
        exact = spec is None and "vector" not in branches and not include_source_keys
        truncated = matched_total > self.candidate_limit or len(scores) > self.candidate_limit or any(
                    len(ids) == self.candidate_limit for ids in branches.values())
        ranking = "hybrid_rrf" if "vector" in branches else "keyword" if terms else order
        result = {"retrieval_version": RETRIEVAL_VERSION, "ranking": ranking, "results": page,
                  "constraint_summary": counts, "candidate_count": len(candidate_ids),
                  "candidate_limit": self.candidate_limit, "truncated": truncated,
                  "matched_total": matched_total if exact else len(accepted), "total_is_exact": exact,
                  "has_more": len(accepted) > offset + len(page) or truncated,
                  "scope": "local_catalog", "branches": list(branches)}
        if spec is not None:
            result["task_spec"] = {"schema_version": 1, **spec}
        if vector_note:
            result["note"] = vector_note
        return result
