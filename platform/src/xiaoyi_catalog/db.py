"""数据库引擎与事务工具。默认 SQLite 文件，路径由调用方给出。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import Base


def create_engine_for(db_path: str | Path) -> Engine:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path.as_posix()}")

    @event.listens_for(engine, "connect")
    def _foreign_keys_on(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
