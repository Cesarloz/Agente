"""Persistent agent branding and explicit web publication; existing agents stay private."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agents", sa.Column("logo_filename", sa.String(100), nullable=True))
    op.add_column("agents", sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("agents", "published")
    op.drop_column("agents", "logo_filename")
