"""Notification deliveries, push subscriptions, shadow decisions, backtests, provider metrics, recalculation
triggers and aggregated OHLC bars. Explicit DDL so later model edits never change what this revision creates."""
import sqlalchemy as sa
from alembic import op

from backend.database import UTCDateTime

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None
RECORD_TABLES_0002 = ('notification_deliveries', 'push_subscriptions', 'shadow_decisions', 'backtest_runs',
                      'provider_metrics', 'recalc_triggers')


def _missing(name: str) -> bool:
    return not sa.inspect(op.get_bind()).has_table(name)


def upgrade():
    # 0001 ran Base.metadata.create_all, so a database created after these models existed already has them.
    for name in RECORD_TABLES_0002:
        if _missing(name):
            op.create_table(name, sa.Column('id', sa.String(128), primary_key=True),
                            sa.Column('created_at', UTCDateTime(), nullable=True),
                            sa.Column('payload', sa.JSON(), nullable=True))
            op.create_index(f'ix_{name}_created_at', name, ['created_at'])
    if _missing('ohlc_bars'):
        op.create_table('ohlc_bars', sa.Column('symbol', sa.String(32), primary_key=True),
                        sa.Column('interval_seconds', sa.Integer(), primary_key=True),
                        sa.Column('start', UTCDateTime(), primary_key=True),
                        sa.Column('open', sa.Float(), nullable=False), sa.Column('high', sa.Float(), nullable=False),
                        sa.Column('low', sa.Float(), nullable=False), sa.Column('close', sa.Float(), nullable=False),
                        sa.Column('volume', sa.Float(), nullable=False), sa.Column('ticks', sa.Integer(), nullable=False),
                        sa.Column('provider', sa.String(64), nullable=False))
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("SELECT create_hypertable('ohlc_bars', 'start', if_not_exists => TRUE, migrate_data => TRUE)")


def downgrade():
    op.drop_table('ohlc_bars')
    for name in RECORD_TABLES_0002:
        op.drop_index(f'ix_{name}_created_at', table_name=name)
        op.drop_table(name)
