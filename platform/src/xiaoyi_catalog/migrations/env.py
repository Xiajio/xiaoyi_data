from alembic import context
from xiaoyi_catalog.db import register_models
from xiaoyi_catalog.models import Base
register_models()
connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use xiaoyi_catalog.db.migrate with a configured database engine")
context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
with context.begin_transaction():
    context.run_migrations()
