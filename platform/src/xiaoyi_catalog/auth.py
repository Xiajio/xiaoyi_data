"""Local team accounts and scoped machine sessions; no request body grants authority."""
from __future__ import annotations
import argparse
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, asdict
from datetime import timedelta, timezone
from getpass import getpass

from fastapi import HTTPException, Request
from sqlalchemy import select
from .db import create_engine_for, init_db, session_scope
from .models import Audit, utcnow
from .models_assets import AuthUser, AuthSession

ROLES = {"reader", "researcher", "reviewer", "admin"}
COOKIE = "xiaoyi_session"

def password_hash(password: str) -> str:
    if len(password) < 12:
        raise ValueError("密码至少需要 12 个字符")
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt:{salt}:{digest}"

def check_password(password: str, value: str) -> bool:
    try:
        kind, salt, expected = value.split(":")
        return kind == "scrypt" and hmac.compare_digest(
            hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex(), expected)
    except (ValueError, TypeError):
        return False

@dataclass(frozen=True)
class Principal:
    id: int
    username: str
    role: str
    kind: str = "human"

    def public(self):
        return asdict(self)

def mint_session(session, user: AuthUser, hours: int = 12) -> str:
    token = secrets.token_urlsafe(32)
    session.add(AuthSession(user_id=user.id, token_hash=hashlib.sha256(token.encode()).hexdigest(),
                            expires_at=utcnow() + timedelta(hours=hours)))
    return token

def resolve(session, token: str) -> Principal | None:
    entry = session.scalar(select(AuthSession).where(AuthSession.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    if not entry:
        return None
    expires = entry.expires_at.replace(tzinfo=timezone.utc) if entry.expires_at.tzinfo is None else entry.expires_at
    user = session.get(AuthUser, entry.user_id)
    if expires <= utcnow() or not user or not user.enabled:
        return None
    return Principal(user.id, user.username, user.role, user.kind)

def request_token(request: Request) -> str:
    token = request.headers.get("authorization", "")
    return token[7:].strip() if token.lower().startswith("bearer ") else request.cookies.get(COOKIE, "")


def principal(request: Request) -> Principal:
    if request.app.state.dev_auth:
        # Explicit fixture-only single identity. Body.role and client headers remain ignored.
        return Principal(0, "local-demo", "admin")
    token = request_token(request)
    with session_scope(request.app.state.engine) as session:
        actor = resolve(session, token) if token else None
    if not actor:
        raise HTTPException(401, "请先登录或提供有效的智能体凭据")
    return actor

def require(request: Request, roles=ROLES, *, human=False) -> Principal:
    actor = principal(request)
    if actor.role not in roles or (human and actor.kind != "human"):
        raise HTTPException(403, "当前账号没有执行此操作的权限")
    return actor

def create_user(session, username: str, password: str, role="reader", kind="human") -> AuthUser:
    if role not in ROLES or kind not in {"human", "agent"}:
        raise ValueError("无效角色或账号类型")
    if not username.strip() or len(username) > 128:
        raise ValueError("账号名不能为空或超过 128 字符")
    if session.scalar(select(AuthUser.id).where(AuthUser.username == username)):
        raise ValueError("账号已存在")
    user = AuthUser(username=username, password_hash=password_hash(password) if kind == "human" else "",
                    role=role, kind=kind)
    session.add(user)
    session.flush()
    return user

def main():
    parser = argparse.ArgumentParser(description="创建本地团队账号（不支持公开注册）")
    parser.add_argument("--db", default=os.environ.get("XIAOYI_CATALOG_DB", "./data/catalog.db"))
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", choices=sorted(ROLES), default="researcher")
    parser.add_argument("--agent", action="store_true")
    args = parser.parse_args()
    engine = create_engine_for(args.db)
    init_db(engine)
    with session_scope(engine) as session:
        user = create_user(session, args.username, "" if args.agent else getpass("Password (12+): "),
                           "researcher" if args.agent else args.role, "agent" if args.agent else "human")
        session.add(Audit(actor="administrator-cli", action="account_created", detail=args.username))
        if args.agent:
            print(mint_session(session, user, hours=24 * 30))
        else:
            print(f"账号 {args.username} 已创建")
    engine.dispose()

if __name__ == "__main__":
    main()
