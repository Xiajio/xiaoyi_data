"""Owner-scoped endpoints for read-only catalog Q&A."""
from __future__ import annotations
from fastapi import HTTPException, Request
from pydantic import Field, field_validator
from sqlalchemy import func, select, update
from .auth import principal
from .db import session_scope
from .llm import StrictModel
from .models import utcnow
from .models_qa import QaConversation, QaTurn
from .qa_runtime import (MAX_ATTEMPTS, MAX_MODEL_CALLS, capabilities, conversation_to_dict, turn_to_dict)


class ConversationIn(StrictModel):
    title: str = Field(default="数据目录问答", min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError("标题不能为空")
        return value.strip()


class QuestionIn(StrictModel):
    question: str = Field(min_length=1, max_length=4000)
    request_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, value):
        if not value.strip():
            raise ValueError("问题不能为空")
        return value.strip()


def _conversation(session, ident, actor, *, lock=False):
    if lock:
        # A harmless owner-scoped write also serializes SQLite submissions, where FOR UPDATE is unavailable.
        session.execute(update(QaConversation).where(QaConversation.id == ident, QaConversation.actor == actor)
            .values(updated_at=utcnow()))
    row = session.scalar(select(QaConversation).where(QaConversation.id == ident, QaConversation.actor == actor))
    if row is None:
        raise HTTPException(404, "问答会话不存在")
    return row


def _turn(session, ident, actor):
    row = session.scalar(select(QaTurn).join(QaConversation).where(QaTurn.id == ident, QaConversation.actor == actor))
    if row is None:
        raise HTTPException(404, "问答不存在")
    return row


def _configured():
    status = capabilities()
    if not status["configured"]:
        raise HTTPException(503, status["reason"])


def register_qa(app, engine):
    @app.get("/api/qa-capabilities")
    def qa_capabilities(request: Request):
        principal(request)
        return capabilities()

    @app.get("/api/qa-conversations")
    def conversations(request: Request):
        actor = principal(request)
        with session_scope(engine) as session:
            rows = session.scalars(select(QaConversation).where(QaConversation.actor == actor.username)
                .order_by(QaConversation.updated_at.desc(), QaConversation.id.desc()).limit(100))
            return {"items": [conversation_to_dict(row) for row in rows]}

    @app.post("/api/qa-conversations", status_code=201)
    def create_conversation(body: ConversationIn, request: Request):
        actor = principal(request)
        with session_scope(engine) as session:
            row = QaConversation(actor=actor.username, title=body.title)
            session.add(row)
            session.flush()
            return conversation_to_dict(row)

    @app.get("/api/qa-conversations/{conversation_id}")
    def get_conversation(conversation_id: int, request: Request):
        actor = principal(request)
        with session_scope(engine) as session:
            row = _conversation(session, conversation_id, actor.username)
            turns = session.scalars(select(QaTurn).where(QaTurn.conversation_id == row.id).order_by(QaTurn.id))
            return {**conversation_to_dict(row), "turns": [turn_to_dict(turn) for turn in turns]}

    @app.post("/api/qa-conversations/{conversation_id}/turns", status_code=202)
    def ask(conversation_id: int, body: QuestionIn, request: Request):
        actor = principal(request)
        with session_scope(engine) as session:
            conversation = _conversation(session, conversation_id, actor.username, lock=True)
            prior = session.scalar(select(QaTurn).where(QaTurn.conversation_id == conversation_id,
                                                       QaTurn.request_key == body.request_key))
            if prior:
                if prior.question != body.question:
                    raise HTTPException(409, "同一请求标识不能用于不同问题")
                return turn_to_dict(prior)
            _configured()
            if session.scalar(select(QaTurn.id).where(QaTurn.conversation_id == conversation_id,
                    QaTurn.status.in_(["queued", "running"]))):
                raise HTTPException(409, "当前会话仍有问题处理中，请等待完成或取消")
            if session.scalar(select(func.count(QaTurn.id)).where(QaTurn.conversation_id == conversation_id)) >= 100:
                raise HTTPException(409, "此会话已达到 100 个问题，请新建会话")
            row = QaTurn(conversation_id=conversation_id, question=body.question, request_key=body.request_key)
            session.add(row)
            if conversation.title == "数据目录问答":
                conversation.title = body.question[:80]
            session.flush()
            return turn_to_dict(row)

    @app.post("/api/qa-turns/{turn_id}/cancel")
    def cancel(turn_id: int, request: Request):
        actor = principal(request)
        with session_scope(engine) as session:
            row = _turn(session, turn_id, actor.username)
            if row.status in {"queued", "running"}:
                # Conditional update prevents a late cancel from changing a completed turn.
                values = {"cancel_requested": True, "stage": "cancelling", "updated_at": utcnow()}
                if row.status == "queued":
                    values.update(status="cancelled", stage="cancelled", finished_at=utcnow())
                session.execute(update(QaTurn).where(QaTurn.id == row.id, QaTurn.status == row.status).values(**values))
                session.refresh(row)
            return turn_to_dict(row)

    @app.post("/api/qa-turns/{turn_id}/retry")
    def retry(turn_id: int, request: Request):
        actor = principal(request)
        with session_scope(engine) as session:
            row = _turn(session, turn_id, actor.username)
            _conversation(session, row.conversation_id, actor.username, lock=True)
            session.refresh(row)
            if row.status not in {"failed", "cancelled"}:
                raise HTTPException(409, "仅失败或已取消的问题可以重试")
            if row.attempts >= MAX_ATTEMPTS or row.model_calls >= MAX_MODEL_CALLS:
                raise HTTPException(409, "该问题的重试或模型预算已耗尽，请重新提问")
            if session.scalar(select(QaTurn.id).where(QaTurn.conversation_id == row.conversation_id,
                    QaTurn.status.in_(["queued", "running"]))):
                raise HTTPException(409, "当前会话仍有问题处理中")
            _configured()
            row.status, row.stage, row.error = "queued", "queued", ""
            row.cancel_requested, row.finished_at = False, None
            session.flush()
            return turn_to_dict(row)
