"""Initial relational and time-series schema."""
from alembic import op

from backend.database import Base

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    connection = op.get_bind()
    if connection.dialect.name == 'postgresql':
        op.execute('CREATE EXTENSION IF NOT EXISTS timescaledb')
        op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    Base.metadata.create_all(connection)
    if connection.dialect.name == 'postgresql':
        op.execute("SELECT create_hypertable('market_ticks', 'timestamp', if_not_exists => TRUE)")
        op.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS embedding vector(768)')

def downgrade():
    Base.metadata.drop_all(op.get_bind())
