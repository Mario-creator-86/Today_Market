"""Market interpretations (docs/EVENT_AWARENESS.md §5): one row per market_interpreter run or deterministic fallback,
keyed by its generation timestamp, holding the source, model, basis_hash and the validated output."""
import sqlalchemy as sa
from alembic import op

from backend.database import UTCDateTime

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table('interpretations'):
        op.create_table('interpretations', sa.Column('id', sa.String(128), primary_key=True),
                        sa.Column('created_at', UTCDateTime(), nullable=True),
                        sa.Column('payload', sa.JSON(), nullable=True))
        op.create_index('ix_interpretations_created_at', 'interpretations', ['created_at'])


def downgrade():
    op.drop_index('ix_interpretations_created_at', table_name='interpretations')
    op.drop_table('interpretations')
