import importlib.util
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.models import Base


def test_migrations_preserve_existing_agents_match_models_and_revert(tmp_path):
    revisions = []
    for path in sorted(Path("migrations/versions").glob("*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        revision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(revision)
        revisions.append(revision)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            revisions[0].upgrade()
            connection.execute(text("INSERT INTO agents (id, name, config, created_at, updated_at) VALUES ('existing', 'Original', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
            for revision in revisions[1:]:
                revision.upgrade()
            row = connection.execute(text("SELECT name, published, logo_filename FROM agents WHERE id='existing'")).one()
            assert tuple(row) == ("Original", 0, None)
            assert compare_metadata(context, Base.metadata) == []
            assert set(inspect(connection).get_table_names()) == set(Base.metadata.tables)
            for revision in reversed(revisions):
                revision.downgrade()
            assert inspect(connection).get_table_names() == []
