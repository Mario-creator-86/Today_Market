"""Proxy quotes: instruments shown in place of an asset that has no direct source (e.g. PAXG for gold, SPY for the
S&P 500). They are stored apart from market_ticks so a proxy can never become a price of the underlying."""
import sqlalchemy as sa
from alembic import op

from backend.database import UTCDateTime

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table('proxy_quotes'):
        op.create_table('proxy_quotes', sa.Column('id', sa.String(128), primary_key=True),
                        sa.Column('created_at', UTCDateTime(), nullable=True),
                        sa.Column('payload', sa.JSON(), nullable=True))
        op.create_index('ix_proxy_quotes_created_at', 'proxy_quotes', ['created_at'])


def downgrade():
    op.drop_index('ix_proxy_quotes_created_at', table_name='proxy_quotes')
    op.drop_table('proxy_quotes')
