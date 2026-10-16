"""SQLite/PostgreSQL engines and versioned Alembic migrations."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from importlib import import_module
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from .models import Base


def register_models() -> None:
    for name in ("models_runtime", "models_assets", "models_collection", "models_qa"):
        try:
            import_module(f"xiaoyi_catalog.{name}")
        except ModuleNotFoundError as exc:
            if exc.name != f"xiaoyi_catalog.{name}":
                raise


def create_engine_for(db_path: str | Path) -> Engine:
    value = str(db_path)
    if "://" in value:
        url = value
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path.as_posix()}"
    options = {"pool_pre_ping": True}
    if url.startswith("sqlite:"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **options)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _foreign_keys_on(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def init_db(engine: Engine) -> None:
    register_models()
    migrate(engine)


def migrate(engine: Engine) -> None:
    from alembic import command
    from alembic.config import Config
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(473718235)")
        elif connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
