"""Convert property descriptions to utf8mb4."""

from alembic import op
import sqlalchemy as sa

revision = '20260924_property_description_utf8mb4'
down_revision = '20260908_email_verification'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name not in {'mysql', 'mariadb'}:
        raise RuntimeError('This migration requires a MySQL-compatible database.')

    bind.execute(sa.text('''
        ALTER TABLE property
        MODIFY COLUMN prop_desc TEXT
        CHARACTER SET utf8mb4
        COLLATE utf8mb4_0900_ai_ci
        NULL
    '''))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name not in {'mysql', 'mariadb'}:
        raise RuntimeError('This migration requires a MySQL-compatible database.')

    bind.execute(sa.text('''
        ALTER TABLE property
        MODIFY COLUMN prop_desc TEXT
        CHARACTER SET utf8mb3
        COLLATE utf8mb3_general_ci
        NULL
    '''))
