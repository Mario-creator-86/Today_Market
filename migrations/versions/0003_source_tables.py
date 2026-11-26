"""Record tables for the additional official/positioning/discovery sources: credit spreads, positioning reports
(CFTC COT, futures funding/OI), official statistical series and discovery items (GDELT, OFAC, SEC full-text)."""
import sqlalchemy as sa
from alembic import op

from backend.database import UTCDateTime

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None
RECORD_TABLES_0003 = ('credit_spreads', 'positioning_reports', 'official_series', 'discovery_items')


def upgrade():
    for name in RECORD_TABLES_0003:
        if not sa.inspect(op.get_bind()).has_table(name):
            op.create_table(name, sa.Column('id', sa.String(128), primary_key=True),
                            sa.Column('created_at', UTCDateTime(), nullable=True),
                            sa.Column('payload', sa.JSON(), nullable=True))
            op.create_index(f'ix_{name}_created_at', name, ['created_at'])


def downgrade():
    for name in reversed(RECORD_TABLES_0003):
        op.drop_index(f'ix_{name}_created_at', table_name=name)
        op.drop_table(name)
