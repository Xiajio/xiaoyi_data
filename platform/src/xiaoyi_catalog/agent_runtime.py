"""Bounded, checkpointed research workflow over existing catalog services.

The graph never receives approval/download tools. All side effects use stable call keys;
replaying a checkpoint reuses committed results and proposals retain their unique keys.
"""
from __future__ import annotations
import os
from contextlib import contextmanager
from typing import Callable, Literal, TypedDict
from sqlalchemy import or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from pydantic import AliasChoices, ConfigDict, Field, field_validator
from .db import session_scope
from .llm import Assessments, LLMClient, ModelResponse, ResearchPlan, StrictModel, PROMPT_VERSION
from .models import Dataset, DatasetVersion, Enrichment, Evidence, Pairing, Snapshot, Source, utcnow
from .models_runtime import AgentEvent, AgentRun, ToolCall


class RuntimeErrorBase(ValueError):
    pass


class RunCancelled(RuntimeErrorBase):
    pass


class LeaseLost(RuntimeErrorBase):
    pass


class BudgetExceeded(RuntimeErrorBase):
    pass


class HardConstraints(StrictModel):
    modalities: list[str] = Field(default_factory=list, max_length=20)
    task_categories: list[str] = Field(default_factory=list, max_length=30)
    languages: list[str] = Field(default_factory=list, max_length=30)
    license_class: list[str] = Field(default_factory=list, validation_alias=AliasChoices("license_class", "license_classes"))
    min_rows: int | None = Field(default=None, ge=0)
    max_rows: int | None = Field(default=None, ge=0)
    min_bytes: int | None = Field(default=None, ge=0)
    max_bytes: int | None = Field(default=None, ge=0)


class RunRequest(StrictModel):
    goal: str = Field(min_length=3, max_length=8000)
    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
    preferences: dict = Field(default_factory=dict)
    sources: list[Literal["huggingface", "manual"]] = Field(default_factory=lambda: ["huggingface", "manual"], min_length=1)
    max_candidates: int = Field(default=100, ge=1, le=100)
    max_tool_calls: int = Field(default=20, ge=1, le=20)

    @field_validator("goal")
    @classmethod
    def nonblank_goal(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("科研目标至少需要 3 个字符")
        return value


def event(session: Session, run_id: int, kind: str, message: str, detail: dict | None = None) -> None:
    session.add(AgentEvent(run_id=run_id, kind=kind, message=message, detail=detail or {}))


def create_run(session: Session, payload: dict, actor: str = "api") -> AgentRun:
    request = RunRequest.model_validate(payload)
    constraints = request.hard_constraints
    if constraints.min_rows is not None and constraints.max_rows is not None and constraints.min_rows > constraints.max_rows:
        raise RuntimeErrorBase("min_rows 不能大于 max_rows")
    if constraints.min_bytes is not None and constraints.max_bytes is not None and constraints.min_bytes > constraints.max_bytes:
        raise RuntimeErrorBase("min_bytes 不能大于 max_bytes")
    actor_name = actor if isinstance(actor, str) else getattr(actor, "username", str(actor))
    run = AgentRun(actor=actor_name[:128], goal=request.goal, payload=request.model_dump(), prompt_version=PROMPT_VERSION)
    session.add(run)
    session.flush()
    event(session, run.id, "queued", "科研任务已排队；仅人工可批准下载和发布")
    return run


def run_to_dict(run: AgentRun) -> dict:
    return {"id": run.id, "actor": run.actor, "goal": run.goal, "payload": run.payload,
            "status": run.status, "stage": run.stage, "result": run.result or {}, "error": run.error,
            "tool_calls": run.tool_calls, "attempts": run.attempts, "model": run.model,
            "prompt_version": run.prompt_version, "usage": run.usage or {}, "cancel_requested": run.cancel_requested,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None}


def list_events(session: Session, run_id: int, after_id: int = 0) -> list[dict]:
    return [{"id": e.id, "run_id": e.run_id, "kind": e.kind, "message": e.message, "detail": e.detail,
             "created_at": e.created_at.isoformat()} for e in session.scalars(select(AgentEvent)
                .where(AgentEvent.run_id == run_id, AgentEvent.id > after_id).order_by(AgentEvent.id).limit(500))]


def _check_owner(run: AgentRun, actor) -> None:
    if actor is None:
        return  # Trusted internal callers; HTTP always supplies an authenticated actor.
    username = actor if isinstance(actor, str) else getattr(actor, "username", "")
    role = getattr(actor, "role", "")
    if run.actor != username and role not in ("reviewer", "admin"):
        raise RuntimeErrorBase("只能取消或重试自己创建的任务")


def cancel_run(session: Session, run_id: int, actor=None) -> AgentRun:
    run = session.get(AgentRun, run_id)
    if run is None:
        raise RuntimeErrorBase("任务不存在")
    _check_owner(run, actor)
    if run.status in ("completed", "failed", "cancelled"):
        return run
    run.cancel_requested = True
    run.status, run.stage, run.finished_at = "cancelled", "cancelled", utcnow()
    event(session, run.id, "cancelled", "已取消任务；已提交的候选与核验记录保留")
    return run


def retry_run(session: Session, run_id: int, actor=None) -> AgentRun:
    run = session.get(AgentRun, run_id)
    if run is None:
        raise RuntimeErrorBase("任务不存在")
    _check_owner(run, actor)
    if run.status not in ("failed", "cancelled"):
        raise RuntimeErrorBase("只有失败或取消的任务可重试")
    if run.attempts >= 3:
        raise RuntimeErrorBase("任务已达到最多 3 次执行上限")
    if run.tool_calls >= run.payload["max_tool_calls"]:
        raise BudgetExceeded("任务工具预算已耗尽，请调整目标后新建任务")
    run.status, run.error, run.cancel_requested, run.finished_at = "queued", "", False, None
    event(session, run.id, "queued", "重试已排队，将复用已完成步骤和固定版本")
    return run


class GraphState(TypedDict, total=False):
    run_id: int
    plan: dict
    version_ids: list[int]
    candidates: list[dict]
    proposals: list[int]
    idea_id: int


class ResearchRunner:
    def __init__(self, engine: Engine, run_id: int, *, llm: LLMClient | None = None,
                 guard: Callable[[], None] | None = None, adapter_factory=None):
        self.engine, self.run_id, self.llm, self.guard = engine, run_id, llm, guard
        self.adapter_factory = adapter_factory
        with session_scope(engine) as session:
            run = session.get(AgentRun, run_id)
            if not run:
                raise RuntimeErrorBase("任务不存在")
            self.payload = dict(run.payload)

    def check(self) -> None:
        if self.guard:
            self.guard()
        with session_scope(self.engine) as session:
            run = session.get(AgentRun, self.run_id)
            if run is None or run.cancel_requested or run.status == "cancelled":
                raise RunCancelled("任务已取消")

    def stage(self, name: str, message: str, detail: dict | None = None) -> None:
        self.check()
        with session_scope(self.engine) as session:
            run = session.get(AgentRun, self.run_id)
            run.stage = name
            event(session, self.run_id, name, message, detail)

    def tool(self, key: str, name: str, action: Callable[[], dict]) -> dict:
        self.check()
        with session_scope(self.engine) as session:
            previous = session.scalar(select(ToolCall).where(ToolCall.run_id == self.run_id, ToolCall.call_key == key))
            if previous and previous.status == "completed":
                return previous.result
            run = session.get(AgentRun, self.run_id)
            if run.tool_calls >= self.payload["max_tool_calls"]:
                raise BudgetExceeded("任务工具调用预算已耗尽")
            if previous is None:
                previous = ToolCall(run_id=self.run_id, call_key=key, tool=name, attempts=0)
                session.add(previous)
            if previous.attempts >= 3:
                raise RuntimeErrorBase("同一步骤重试次数已耗尽")
            previous.attempts += 1
            previous.status, previous.error = "running", ""
            run.tool_calls += 1
            session.flush()
            call_id = previous.id
            event(session, self.run_id, "tool_started", name, {"call_key": key, "attempt": previous.attempts})
        try:
            output = action()
            # Preserve returned usage/results even if cancelled during a remote call.
            # They may be reused on retry; cancellation still blocks every subsequent step.
            if self.guard:
                self.guard()
            with session_scope(self.engine) as session:
                record = session.get(ToolCall, call_id)
                record.result, record.status, record.finished_at = output, "completed", utcnow()
                run = session.get(AgentRun, self.run_id)
                if output.get("model"):
                    run.model = output["model"]
                    run.usage = {k: (run.usage or {}).get(k, 0) + output.get("usage", {}).get(k, 0)
                                 for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
                event(session, self.run_id, "tool_completed", name, {"call_key": key})
            self.check()
            return output
        except (LeaseLost, RunCancelled):
            raise
        except Exception as exc:
            if self.guard:
                self.guard()
            with session_scope(self.engine) as session:
                record = session.get(ToolCall, call_id)
                record.status, record.error, record.finished_at = "failed", safe_error(exc), utcnow()
                event(session, self.run_id, "tool_failed", name, {"call_key": key, "error": record.error})
            raise

    def model_call(self, schema, instruction: str, data: dict) -> dict:
        client = self.llm or LLMClient.from_env()
        response = client.complete(schema, instruction, data)
        return {"value": response.value.model_dump(), "model": response.model, "usage": response.usage}

    def parse(self, state: GraphState) -> dict:
        self.stage("planning", "解析科研目标与检索策略")
        result = self.tool("plan", "parse_requirements", lambda: self.model_call(ResearchPlan,
            "Create short dataset search queries. User hard constraints are immutable; only explain a search strategy.",
            {"goal": self.payload["goal"], "hard_constraints": self.payload["hard_constraints"],
             "preferences": self.payload["preferences"]}))
        # Validate again on persisted results, rather than trusting checkpoint contents.
        plan = ResearchPlan.model_validate(result["value"]).model_dump()
        if any(not q.strip() or len(q) > 200 for q in plan["search_queries"]):
            raise RuntimeErrorBase("检索词为空或过长")
        with session_scope(self.engine) as session:
            session.get(AgentRun, self.run_id).result = {"plan": plan}
        return {"plan": plan}

    def discover(self, state: GraphState) -> dict:
        from .adapters.base import CollectScope
        from .adapters.huggingface import HuggingFaceAdapter
        from .collect import collect
        from .search import latest_version
        self.stage("discovering", "从已登记来源发现并采集固定版本")
        limit, sources = self.payload["max_candidates"], self.payload["sources"]
        queries = state["plan"]["search_queries"]

        def action():
            collected_keys: list[str] = []
            source_failures = []
            if "huggingface" in sources:
                adapter = self.adapter_factory() if self.adapter_factory else HuggingFaceAdapter(
                    endpoint=os.getenv("HF_ENDPOINT", "https://huggingface.co"))
                runner = self
                class CheckedAdapter:
                    kind, endpoint = adapter.kind, adapter.endpoint
                    def list(self, scope):
                        for position, ref in enumerate(adapter.list(scope)):
                            if position >= scope.limit:
                                break
                            runner.check()
                            collected_keys.append(ref.source_key)
                            yield ref
                    def fetch_detail(self, ref):
                        runner.check()
                        return adapter.fetch_detail(ref)
                for query in queries:
                    self.check()
                    remaining = limit - len(set(collected_keys))
                    if remaining <= 0:
                        break
                    result = collect(self.engine, CheckedAdapter(), CollectScope(search=query,
                        languages=tuple(self.payload["hard_constraints"].get("languages") or []),
                        task_categories=tuple(self.payload["hard_constraints"].get("task_categories") or []), limit=remaining))
                    if result.status == "failed" or (result.stats or {}).get("failed"):
                        source_failures.append({"query": query, "status": result.status, "stats": result.stats})
            self.check()
            with session_scope(self.engine) as session:
                stmt = select(Dataset).join(Source, Source.id == Dataset.source_id).where(
                    Source.kind.in_(sources), Dataset.status == "active")
                # Search existing registrations too. Missing metadata is never converted to affirmative facts.
                terms = [Dataset.source_key.in_(collected_keys)] if collected_keys else []
                for q in queries:
                    terms.extend([Dataset.card_text.ilike("%" + q + "%"), Dataset.name.ilike("%" + q + "%")])
                if terms:
                    stmt = stmt.where(or_(*terms))
                rows = session.scalars(stmt.order_by(Dataset.downloads.desc(), Dataset.id).limit(limit)).all()
                ids = []
                for row in rows:
                    version = latest_version(session, row.id)
                    if version:
                        ids.append(version.id)
                event(session, self.run_id, "candidates_found", f"发现 {len(ids)} 个候选版本",
                      {"dataset_version_ids": ids, "source_failures": source_failures})
            if not ids and source_failures:
                raise RuntimeErrorBase("来源采集失败且目录没有可用候选；请检查来源连接后重试")
            return {"version_ids": ids, "source_failures": source_failures}

        result = self.tool("discover", "discover_and_collect", action)
        return {"version_ids": result["version_ids"]}

    def enrich(self, state: GraphState) -> dict:
        from .enrich import enrich_dataset
        self.stage("enriching", "整理目录结构与信息完整度")
        def action():
            for version_id in state["version_ids"]:
                self.check()
                with session_scope(self.engine) as session:
                    version = session.get(DatasetVersion, version_id)
                    if not version or version.provenance_status != "verified":
                        continue
                    dataset = session.get(Dataset, version.dataset_id)
                    enrich_dataset(session, dataset, dataset_version_id=version.id)
            return {"processed": len(state["version_ids"])}
        self.tool("enrich", "enrich_versions", action)
        return {}

    def assess(self, state: GraphState) -> dict:
        from .search import version_to_dict
        from .match import missing_for_requirement
        self.stage("matching", "核对证据、硬约束与科研相关性")
        candidates: list[dict] = []
        evidence_by_version: dict[int, dict[int, dict]] = {}
        with session_scope(self.engine) as session:
            for version_id in state["version_ids"]:
                version = session.get(DatasetVersion, version_id)
                if not version:
                    continue
                item = version_to_dict(session, version)
                evidence = {e.id: {"id": e.id, "location": e.location, "excerpt": e.excerpt}
                            for e in session.scalars(select(Evidence).where(Evidence.dataset_version_id == version_id))}
                evidence_by_version[version_id] = evidence
                gaps = missing_for_requirement(item, self.payload["hard_constraints"])
                if version.provenance_status != "verified":
                    gaps = list(gaps) + ["版本证据尚未验证"]
                item.pop("card_text", None)
                candidates.append({"dataset_version_id": version_id, "dataset": item,
                    "hard_constraint_gaps": gaps, "evidence": list(evidence.values())[:8]})
        for offset in range(0, len(candidates), 10):
            self.check()
            batch = candidates[offset:offset + 10]
            result = self.tool(f"assess:{offset}", "assess_evidence", lambda batch=batch: self.model_call(Assessments,
                "Assess relevance for every supplied version. rationale is explicitly a model inference. "
                "Use claims only for facts grounded in exact evidence quotes. Never mark unknown facts as known. "
                "Determine relevance from the goal and evidence, independently of soft preferences. "
                "Assign rank_score from 0 to 1 to express preference fit based on supplied evidence; use 0 when no preferences are supplied. "
                "Preferences affect ordering only: unmet preferences must not become hard constraints or make an otherwise relevant dataset irrelevant.",
                {"goal": self.payload["goal"], "hard_constraints": self.payload["hard_constraints"],
                 "preferences": self.payload["preferences"], "candidates": batch}))
            parsed = Assessments.model_validate(result["value"])
            expected = {c["dataset_version_id"] for c in batch}
            returned = [a.dataset_version_id for a in parsed.candidates]
            if len(returned) != len(set(returned)) or set(returned) != expected:
                raise RuntimeErrorBase("模型返回了遗漏、重复或未提供的数据集版本")
            for assessment in parsed.candidates:
                item = next(c for c in batch if c["dataset_version_id"] == assessment.dataset_version_id)
                evidence = evidence_by_version[assessment.dataset_version_id]
                invalid = []
                for claim in assessment.claims:
                    cited = [evidence.get(eid) for eid in claim.evidence_ids]
                    if any(e is None for e in cited) or any(not quote.strip() or not any(
                            quote in e["excerpt"] for e in cited if e) for quote in claim.quotes):
                        invalid.append("引用了当前版本之外的证据或不存在的原文")
                if assessment.relevant and not assessment.claims:
                    invalid.append("相关性结论缺少可定位事实依据")
                item.update(assessment=assessment.model_dump(), fact_status="model_inferred",
                            evidence_validation="invalid" if invalid else "grounded",
                            evidence_errors=invalid, eligible=assessment.relevant and not invalid and not item["hard_constraint_gaps"])
                # Remove ungrounded model text from persisted facts; retain the failure reason for review.
                if invalid:
                    item["assessment"] = {"dataset_version_id": assessment.dataset_version_id, "relevant": False, "rank_score": 0,
                        "rationale": "证据检查未通过，需人工补充资料", "claims": [], "uncertainty": invalid}
        # Stable sorting preserves discovery order on ties. A soft preference never outranks
        # a deterministic hard-constraint/evidence failure, and only eligible items become proposals.
        candidates.sort(key=lambda item: (bool(item["eligible"]), bool(item["assessment"]["relevant"]),
                                         item["assessment"].get("rank_score", 0)), reverse=True)
        with session_scope(self.engine) as session:
            run = session.get(AgentRun, self.run_id)
            run.result = {**(run.result or {}), "candidates": candidates}
        return {"candidates": candidates}

    def propose(self, state: GraphState) -> dict:
        from .pairing import propose_pairing, upsert_idea
        self.stage("proposing", "生成待人工核验的配对提案")
        def action():
            self.check()
            idea_payload = {"external_id": f"research-run-{self.run_id}", "title": state["plan"]["title"],
                "statement": self.payload["goal"], "requirements": self.payload["hard_constraints"],
                "source_refs": [{"kind": "agent_run", "id": self.run_id}]}
            # Everything committed in a single short transaction, keyed by idea+version.
            with session_scope(self.engine) as session:
                idea = upsert_idea(session, idea_payload)
                ids = []
                for item in state["candidates"]:
                    if not item["eligible"]:
                        continue
                    assessment = item["assessment"]
                    result = propose_pairing(session, idea_payload, [item["dataset_version_id"]],
                        "模型推断（需人工核验）：" + assessment["rationale"], proposed_by="agent")
                    ids.extend(p["id"] for p in result["created"] + result["existing"])
                    version = session.get(DatasetVersion, item["dataset_version_id"])
                    prior = session.scalar(select(Enrichment).where(Enrichment.dataset_version_id == version.id,
                        Enrichment.kind == "llm_summary", Enrichment.producer == f"agent-run/{self.run_id}"))
                    if prior is None:
                        session.add(Enrichment(dataset_id=version.dataset_id, dataset_version_id=version.id,
                            kind="llm_summary", producer=f"agent-run/{self.run_id}", value=assessment,
                            confidence="model_inferred", evidence_ids=sorted({eid for claim in assessment["claims"]
                                for eid in claim["evidence_ids"]}), status="candidate"))
                return {"proposals": ids, "idea_id": idea.id}
        return self.tool("propose", "propose_pairings", action)

    def graph(self, checkpointer):
        from langgraph.graph import END, START, StateGraph
        builder = StateGraph(GraphState)
        for name, node in [("parse", self.parse), ("discover", self.discover), ("enrich", self.enrich),
                           ("assess", self.assess), ("propose", self.propose)]:
            builder.add_node(name, node)
        for source, target in [(START, "parse"), ("parse", "discover"), ("discover", "enrich"),
                               ("enrich", "assess"), ("assess", "propose"), ("propose", END)]:
            builder.add_edge(source, target)
        return builder.compile(checkpointer=checkpointer)


def safe_error(exc: BaseException) -> str:
    # A provider exception can include key-bearing URLs or response bodies. Only known local messages persist.
    from .llm import ModelConfigurationError, ModelOutputError
    if isinstance(exc, (RuntimeErrorBase, ModelConfigurationError, ModelOutputError)):
        return str(exc)[:1000]
    return f"{type(exc).__name__}: 执行失败，请检查服务连接或输入数据后重试"


@contextmanager
def checkpoint_store(engine: Engine):
    if engine.dialect.name == "postgresql":
        from langgraph.checkpoint.postgres import PostgresSaver
        dsn = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
        with PostgresSaver.from_conn_string(dsn) as saver:
            saver.setup()
            yield saver
    else:
        from langgraph.checkpoint.sqlite import SqliteSaver
        configured = os.getenv("XIAOYI_CHECKPOINT_PATH")
        database = engine.url.database
        if not configured and database in (None, "", ":memory:"):
            raise RuntimeErrorBase("持久任务需要文件数据库或 XIAOYI_CHECKPOINT_PATH")
        with SqliteSaver.from_conn_string(configured or database + ".checkpoints.sqlite") as saver:
            saver.setup()
            yield saver


def execute_run(engine: Engine, run_id: int, *, llm=None, guard=None, adapter_factory=None) -> None:
    runner = ResearchRunner(engine, run_id, llm=llm, guard=guard, adapter_factory=adapter_factory)
    try:
        runner.check()
        with session_scope(engine) as session:
            run = session.get(AgentRun, run_id)
            if run.status in ("completed", "waiting_approval"):
                return
            if run.attempts >= 3:
                raise RuntimeErrorBase("任务已达到最多 3 次执行上限")
            run.status, run.started_at, run.error = "running", run.started_at or utcnow(), ""
            run.attempts += 1
        # Fail explicitly before collecting source data when there is no model configuration.
        if llm is None:
            runner.llm = LLMClient.from_env()
        with checkpoint_store(engine) as saver:
            graph = runner.graph(saver)
            config = {"configurable": {"thread_id": f"research-run-{run_id}"}, "recursion_limit": 30}
            checkpoint = graph.get_state(config)
            if checkpoint.values and not checkpoint.next:
                result = checkpoint.values
            else:
                result = graph.invoke(None if checkpoint.values else {"run_id": run_id}, config)
        runner.check()
        with session_scope(engine) as session:
            run = session.get(AgentRun, run_id)
            run.result = {**(run.result or {}), "proposal_ids": result.get("proposals", []),
                "idea_id": result.get("idea_id"), "idea_external_id": f"research-run-{run_id}"}
            run.status = "waiting_approval" if result.get("proposals") else "completed"
            run.stage = "human_review" if result.get("proposals") else "completed"
            run.finished_at = None if result.get("proposals") else utcnow()
            event(session, run.id, run.status, "候选提案等待人工核验" if result.get("proposals") else "检索完成；没有满足证据与硬约束的提案")
    except LeaseLost:
        raise
    except RunCancelled:
        with session_scope(engine) as session:
            run = session.get(AgentRun, run_id)
            run.status, run.stage, run.finished_at = "cancelled", "cancelled", utcnow()
    except Exception as exc:
        with session_scope(engine) as session:
            run = session.get(AgentRun, run_id)
            if run.cancel_requested:
                run.status, run.stage = "cancelled", "cancelled"
            else:
                run.status, run.error = "failed", safe_error(exc)
                event(session, run.id, "failed", run.error)
            run.finished_at = utcnow()


def reconcile_reviews(session: Session) -> None:
    """Refresh human decisions and acquisition progress without giving the graph approval powers."""
    from .models_assets import DownloadRequest
    runs = session.scalars(select(AgentRun).where(AgentRun.status.in_(["waiting_approval", "completed"]))
        .order_by(AgentRun.id.desc()).limit(500))
    for run in runs:
        ids = (run.result or {}).get("proposal_ids", [])
        pairings = [session.get(Pairing, pid) for pid in ids]
        versions = [p.dataset_version_id for p in pairings if p is not None]
        decisions = {status: sum(p is not None and p.status == status for p in pairings)
                     for status in ("pending", "confirmed", "rejected", "needs-info")}
        downloads = [{"id": d.id, "dataset_version_id": d.dataset_version_id, "status": d.status,
                      "estimated_bytes": d.estimated_bytes, "actual_bytes": d.actual_bytes, "error": d.error}
                     for d in session.scalars(select(DownloadRequest).where(
                         DownloadRequest.dataset_version_id.in_(versions)).order_by(DownloadRequest.id))] if versions else []
        idea_id = (run.result or {}).get("idea_id")
        snapshots = [{"id": item.id, "version": item.version, "status": item.status}
                     for item in session.scalars(select(Snapshot).where(Snapshot.idea_id == idea_id))] if idea_id else []
        run.result = {**(run.result or {}), "review_progress": decisions,
                      "downloads": downloads, "snapshots": snapshots}
        if run.status == "waiting_approval" and ids and all(
                p is not None and p.status in ("confirmed", "rejected") for p in pairings):
            run.status, run.stage, run.finished_at = "completed", "completed", utcnow()
            event(session, run.id, "completed", "人工候选核验已完成；下载和发布仍需分别批准")
