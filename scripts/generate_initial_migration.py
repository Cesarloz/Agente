"""Development helper: freeze ORM metadata into the first Alembic revision."""
from pathlib import Path

from alembic.autogenerate import produce_migrations, render_python_code
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.models import Base

destination = Path("migrations/versions/0001_initial.py")
if destination.exists():
    raise SystemExit("Initial migration already exists; create a new revision for changes.")
with create_engine("sqlite://").connect() as connection:
    migration = produce_migrations(MigrationContext.configure(connection), Base.metadata)
    upgrade = render_python_code(migration.upgrade_ops)
    downgrade = render_python_code(migration.downgrade_ops)
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text('"""Initial transactional schema."""\nfrom alembic import op\nimport sqlalchemy as sa\n\nrevision = "0001"\ndown_revision = None\nbranch_labels = None\ndepends_on = None\n\ndef upgrade():\n' + upgrade + '\n\ndef downgrade():\n' + downgrade + '\n', encoding="utf-8")
print(destination)
